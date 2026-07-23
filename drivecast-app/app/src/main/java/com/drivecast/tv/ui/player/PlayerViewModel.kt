package com.drivecast.tv.ui.player

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import com.drivecast.tv.di.AppContainer
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class PlayerUiState(
    val error: String? = null,
    val errorRetriable: Boolean = false,
    // The up-next banner's identity/visibility only. The 1Hz countdown value lives
    // separately in [PlayerViewModel.upNextRemaining] so the ticking second doesn't
    // re-emit this whole state (and recompose the AndroidView update block) every second.
    val upNextVisible: Boolean = false,
    val nextTitle: String? = null,
    val nextPosterUrl: String? = null,
)

@UnstableApi
class PlayerViewModel(
    app: Application,
    private val container: AppContainer,
    private val titleId: String,
    private val startFileId: String,
    private val startOver: Boolean,
    private val shuffle: Boolean,
    private val seed: Long,
) : AndroidViewModel(app) {

    private val repo = container.repository

    val player: ExoPlayer = ExoPlayer.Builder(app)
        .setMediaSourceFactory(
            DefaultMediaSourceFactory(OkHttpDataSource.Factory(container.okHttp))
        )
        .setAudioAttributes(AudioAttributes.DEFAULT, /* handleAudioFocus = */ true)
        .setHandleAudioBecomingNoisy(true)
        .build()

    private val _ui = MutableStateFlow(PlayerUiState())
    val ui: StateFlow<PlayerUiState> = _ui.asStateFlow()

    // The up-next countdown, sliced out of [ui] so the 1Hz tick only recomposes the
    // leaf that collects this flow (the countdown ring), never the screen content
    // lambda that hosts the AndroidView(PlayerView).
    private val _upNextRemaining = MutableStateFlow<Int?>(null)
    val upNextRemaining: StateFlow<Int?> = _upNextRemaining.asStateFlow()

    private var queue: List<QueueItem> = emptyList()
    private var currentIndex = 0
    private var progressMap: Map<String, com.drivecast.tv.api.WatchedProgress> = emptyMap()
    private var currentPosterUrl: String? = null

    private var tickerJob: Job? = null
    private var upNextJob: Job? = null
    private var retriedCurrent = false

    init {
        player.addListener(PlayerListener())
        viewModelScope.launch { bootstrap() }
    }

    private suspend fun bootstrap() {
        val title = runCatching { repo.title(titleId) }.getOrNull()
        currentPosterUrl = title?.poster
        queue = PlaybackQueue.build(title, startFileId, shuffle, seed)
        currentIndex = PlaybackQueue.startIndex(queue, startFileId, shuffle)
        progressMap = runCatching { repo.watchedMap().progress }.getOrDefault(emptyMap())
        prepareCurrent(honorResume = !startOver && !shuffle)
    }

    private suspend fun prepareCurrent(honorResume: Boolean) {
        retriedCurrent = false
        _upNextRemaining.value = null
        _ui.value = _ui.value.copy(error = null, upNextVisible = false)
        val item = queue[currentIndex]

        val subtitle = runCatching { repo.probeSubtitle(item.fileId) }.getOrNull()
        val builder = MediaItem.Builder().setUri(repo.streamUrl(item.fileId))
        if (subtitle != null) {
            builder.setSubtitleConfigurations(
                listOf(
                    MediaItem.SubtitleConfiguration.Builder(Uri.parse(subtitle.url))
                        .setMimeType(subtitle.mimeType)
                        .setLanguage("en")
                        .setSelectionFlags(C.SELECTION_FLAG_DEFAULT)
                        .build()
                )
            )
        }

        player.setMediaItem(builder.build())
        player.prepare()

        val resumeMs = if (honorResume) PlaybackQueue.resumeMsFor(item, progressMap) else 0L
        if (resumeMs > 0) player.seekTo(resumeMs)
        player.playWhenReady = true
    }

    private fun startTicker() {
        if (tickerJob?.isActive == true) return
        tickerJob = viewModelScope.launch {
            while (true) {
                delay(10_000)
                if (player.isPlaying) postProgress(ended = false)
            }
        }
    }

    private fun stopTicker() {
        tickerJob?.cancel()
        tickerJob = null
    }

    private fun postProgress(ended: Boolean) {
        val item = queue.getOrNull(currentIndex) ?: return
        val positionSec = player.currentPosition / 1000.0
        val durationMs = player.duration
        val durationSec = if (durationMs > 0) durationMs / 1000.0 else item.durationMs?.let { it / 1000.0 }
        viewModelScope.launch {
            repo.postProgress(
                fileId = item.fileId,
                name = item.name,
                position = positionSec,
                duration = durationSec,
                ended = ended,
            )
        }
    }

    private fun onEnded() {
        postProgress(ended = true)
        val nextIndex = currentIndex + 1
        if (nextIndex in queue.indices) {
            startUpNextCountdown(nextIndex)
        }
    }

    private fun startUpNextCountdown(nextIndex: Int) {
        upNextJob?.cancel()
        val next = queue[nextIndex]
        _ui.value = _ui.value.copy(
            upNextVisible = true,
            nextTitle = next.name ?: "Next",
            nextPosterUrl = currentPosterUrl,
        )
        upNextJob = viewModelScope.launch {
            for (seconds in 5 downTo 1) {
                _upNextRemaining.value = seconds
                delay(1_000)
            }
            _upNextRemaining.value = null
            _ui.value = _ui.value.copy(upNextVisible = false)
            currentIndex = nextIndex
            prepareCurrent(honorResume = true)
        }
    }

    fun playNextNow() {
        upNextJob?.cancel()
        if (!_ui.value.upNextVisible) return
        _upNextRemaining.value = null
        _ui.value = _ui.value.copy(upNextVisible = false)
        val nextIndex = currentIndex + 1
        if (nextIndex in queue.indices) {
            currentIndex = nextIndex
            viewModelScope.launch { prepareCurrent(honorResume = true) }
        }
    }

    fun cancelUpNext() {
        upNextJob?.cancel()
        _upNextRemaining.value = null
        _ui.value = _ui.value.copy(upNextVisible = false)
    }

    fun retry() {
        viewModelScope.launch { prepareCurrent(honorResume = true) }
    }

    /** Report the final position; call from the screen when leaving. */
    fun reportStop() {
        if (queue.isNotEmpty()) postProgress(ended = false)
    }

    override fun onCleared() {
        stopTicker()
        upNextJob?.cancel()
        player.release()
        super.onCleared()
    }

    private inner class PlayerListener : Player.Listener {
        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (isPlaying) {
                // This device is now watching: opt into the server's keep-awake
                // handshake for the rest of the session.
                container.keepAwake.markPlaybackStarted()
                startTicker()
            } else {
                stopTicker()
                // Report on pause (but not on the transient stop before ENDED).
                if (player.playbackState != Player.STATE_ENDED) postProgress(ended = false)
            }
        }

        override fun onPlaybackStateChanged(state: Int) {
            if (state == Player.STATE_ENDED) {
                stopTicker()
                onEnded()
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            val mapped = com.drivecast.tv.ui.common.mapPlaybackException(error)
            if (mapped.retriable && !retriedCurrent) {
                retriedCurrent = true
                _ui.value = _ui.value.copy(error = mapped.message, errorRetriable = true)
                viewModelScope.launch {
                    delay(5_000)
                    _ui.value = _ui.value.copy(error = null)
                    player.prepare()
                    player.playWhenReady = true
                }
            } else {
                _ui.value = _ui.value.copy(error = mapped.message, errorRetriable = mapped.retriable)
            }
        }
    }

    companion object {
        fun factory(
            app: Application,
            container: AppContainer,
            titleId: String,
            fileId: String,
            startOver: Boolean,
            shuffle: Boolean,
            seed: Long,
        ): ViewModelProvider.Factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T =
                PlayerViewModel(app, container, titleId, fileId, startOver, shuffle, seed) as T
        }
    }
}
