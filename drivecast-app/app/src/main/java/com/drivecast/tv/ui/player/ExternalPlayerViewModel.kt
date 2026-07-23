package com.drivecast.tv.ui.player

import android.os.SystemClock
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.drivecast.tv.api.WatchedProgress
import com.drivecast.tv.di.AppContainer
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** A single VLC hand-off the host composable should fire as an intent. */
data class VlcLaunchRequest(
    val uri: String,
    val title: String,
    val fromStart: Boolean,
    val positionMs: Long,
    val subtitleUrl: String?,
)

data class ExternalPlayerUiState(
    val loading: Boolean = true,
    val nowPlayingTitle: String? = null,
    val playing: Boolean = false,
    val error: String? = null,
    // The up-next banner's identity/visibility only; the 1Hz countdown value lives
    // in [ExternalPlayerViewModel.upNextRemaining] (see PlayerViewModel for why).
    val upNextVisible: Boolean = false,
    val nextTitle: String? = null,
    val nextPosterUrl: String? = null,
    val finished: Boolean = false,
)

/**
 * Drives playback handed off to VLC. Builds the same queue/resume as the
 * internal player via [PlaybackQueue], emits one-shot [VlcLaunchRequest]s the
 * host composable turns into intents, folds VLC's returned position back into
 * the server's progress reporting, autoplays the next episode, and keeps the
 * Mac awake while VLC is foreground (the in-app prompt can't be seen then).
 *
 * For a show played sequentially (or shuffled), playback is handed to VLC as a
 * real `#EXTM3U` playlist so VLC's native Next/Prev works. VLC loses the
 * `position` extra on playlist expansion (no mid-episode resume into a
 * playlist) and, on exit, reports the position/duration of whatever was
 * playing when the user backed out — but the returned URI is still the
 * original playlist URL, so [onVlcResult] must infer which episode that was
 * from the server's `/api/stream/recent` activity log.
 */
class ExternalPlayerViewModel(
    private val container: AppContainer,
    private val titleId: String,
    private val startFileId: String,
    private val startOver: Boolean,
    private val shuffle: Boolean,
    private val seed: Long,
) : ViewModel() {

    private val repo = container.repository

    private val _ui = MutableStateFlow(ExternalPlayerUiState())
    val ui: StateFlow<ExternalPlayerUiState> = _ui.asStateFlow()

    // Sliced out of [ui] so the 1Hz tick recomposes only the leaf collecting this
    // flow (the countdown ring), never the whole overlay/host tree.
    private val _upNextRemaining = MutableStateFlow<Int?>(null)
    val upNextRemaining: StateFlow<Int?> = _upNextRemaining.asStateFlow()

    private val _launches = Channel<VlcLaunchRequest>(Channel.BUFFERED)
    val launches = _launches.receiveAsFlow()

    private var queue: List<QueueItem> = emptyList()
    private var currentIndex = 0
    private var progressMap: Map<String, WatchedProgress> = emptyMap()
    private var currentPosterUrl: String? = null

    private var playlistSession = false
    private var isShowPath = false
    private var launchElapsedMs = 0L

    private var upNextJob: Job? = null
    private var awakeJob: Job? = null

    init {
        viewModelScope.launch { bootstrap() }
    }

    private suspend fun bootstrap() {
        val title = runCatching { repo.title(titleId) }.getOrNull()
        currentPosterUrl = title?.poster
        isShowPath = (title?.isShow == true) && (shuffle || PlaybackQueue.isMainShowEpisode(title, startFileId))
        // Build the queue so it EQUALS the server's m3u order.
        if (shuffle) {
            queue = PlaybackQueue.build(title, startFileId, shuffle = true, seed = seed)
            currentIndex = 0
        } else if (isShowPath) {
            queue = runCatching { repo.playlistItems(titleId, startFileId) }.getOrNull()
                ?.takeIf { it.isNotEmpty() }
                ?.map { QueueItem(it.fileId, it.name, it.durationMs) }
                ?: PlaybackQueue.buildShow(title!!).let { it.drop(PlaybackQueue.startIndex(it, startFileId)) }
            currentIndex = 0
        } else {
            queue = PlaybackQueue.build(title, startFileId)
            currentIndex = PlaybackQueue.startIndex(queue, startFileId)
        }
        progressMap = runCatching { repo.watchedMap().progress }.getOrDefault(emptyMap())
        launchCurrent(fromStart = startOver || shuffle)
    }

    private suspend fun launchCurrent(fromStart: Boolean) {
        val item = queue.getOrNull(currentIndex)
        if (item == null) {
            _ui.value = _ui.value.copy(loading = false, playing = false, finished = true)
            return
        }
        val resumeMs = if (fromStart) 0L else PlaybackQueue.resumeMsFor(item, progressMap)

        _upNextRemaining.value = null
        _ui.value = _ui.value.copy(
            loading = false,
            nowPlayingTitle = item.name,
            playing = true,
            upNextVisible = false,
            error = null,
        )

        if (isShowPath && resumeMs == 0L) {
            // PLAYLIST session: hand VLC the m3u so its native Next/Prev works.
            playlistSession = true
            // Always anchor the playlist at the current item: the server slices the
            // (shuffled or sequential) order to this file, so an up-next relaunch
            // resumes at the next episode instead of replaying from the top.
            val uri = repo.authorizedPlaylistUrl(
                titleId,
                startFileId = item.fileId,
                shuffle = shuffle,
                seed = seed,
            )
            // Best-effort subtitle for the first episode only — it may not
            // survive VLC's playlist expansion, and that's acceptable.
            val subtitle = runCatching { repo.probeSubtitle(item.fileId) }.getOrNull()
            val subtitleUrl = if (subtitle != null) repo.authorizedSubtitleUrl(item.fileId) else null
            _launches.send(
                VlcLaunchRequest(
                    uri = uri,
                    title = item.name ?: "Video",
                    fromStart = true,
                    positionMs = 0,
                    subtitleUrl = subtitleUrl,
                )
            )
        } else {
            // Movie / extra clip / resume bridge: single-item request, exactly as before.
            playlistSession = false
            val subtitle = runCatching { repo.probeSubtitle(item.fileId) }.getOrNull()
            val subtitleUrl = if (subtitle != null) repo.authorizedSubtitleUrl(item.fileId) else null
            _launches.send(
                VlcLaunchRequest(
                    uri = repo.authorizedStreamUrl(item.fileId),
                    title = item.name ?: "Video",
                    fromStart = fromStart,
                    positionMs = resumeMs,
                    subtitleUrl = subtitleUrl,
                )
            )
        }
    }

    /** Host tells us VLC actually launched; opt into the keep-awake handshake. */
    fun onVlcLaunched() {
        launchElapsedMs = SystemClock.elapsedRealtime()
        container.keepAwake.markPlaybackStarted()
        startAwakePolling()
    }

    /**
     * VLC returned. [positionMs] / [durationMs] are VLC's extras in ms, or < 0
     * when it reported nothing (in which case we must not clobber stored
     * progress). Reports progress in SECONDS, then autoplays or pops back.
     */
    fun onVlcResult(positionMs: Long, durationMs: Long) {
        stopAwakePolling()

        if (!playlistSession) {
            // Today's exact single-item logic. The countdown relaunch below
            // routes through launchCurrent(fromStart = true), which — for a
            // show — starts a PLAYLIST at the next episode (the resume→
            // playlist bridge).
            val item = queue.getOrNull(currentIndex)
            if (item == null) {
                _ui.value = _ui.value.copy(playing = false, finished = true)
                return
            }
            if (positionMs < 0) {
                // VLC gave us no position; leave the server's stored progress alone.
                _ui.value = _ui.value.copy(playing = false, finished = true)
                return
            }

            val durMs = if (durationMs > 0) durationMs else (item.durationMs ?: 0L)
            val fraction = if (durMs > 0) positionMs.toDouble() / durMs else 0.0
            val finishedEpisode = fraction >= 0.95

            val positionSec = positionMs / 1000.0
            val durationSec = if (durMs > 0) durMs / 1000.0 else null
            viewModelScope.launch {
                repo.postProgress(
                    fileId = item.fileId,
                    name = item.name,
                    position = positionSec,
                    duration = durationSec,
                    ended = finishedEpisode,
                )
            }

            val nextIndex = currentIndex + 1
            if (finishedEpisode && nextIndex in queue.indices) {
                startUpNextCountdown(nextIndex)
            } else {
                _ui.value = _ui.value.copy(playing = false, finished = true)
            }
            return
        }

        // Playlist session: the returned URI is still the original m3u, so we
        // must infer which episode VLC was actually playing at exit.
        if (positionMs < 0) {
            _ui.value = _ui.value.copy(playing = false, finished = true)
            return
        }

        viewModelScope.launch {
            val recent = runCatching { repo.streamRecent() }.getOrNull()
            val windowSec = (SystemClock.elapsedRealtime() - launchElapsedMs) / 1000.0 + 120.0
            val active = recent?.items.orEmpty().filter { (it.age ?: Double.MAX_VALUE) <= windowSec }

            val attributed = active.asSequence()
                .mapNotNull { a ->
                    queue.indexOfFirst { it.fileId == a.fileId }.takeIf { i -> i >= 0 }
                        ?.let { it to (a.age ?: 0.0) }
                }
                .minByOrNull { it.second }?.first
                ?: (if (durationMs > 0) {
                    queue.indices.filter { kotlin.math.abs((queue[it].durationMs ?: Long.MIN_VALUE) - durationMs) < 2000 }
                        .singleOrNull()
                } else null)
                ?: currentIndex

            // Mark intermediate episodes (skipped past via VLC's own Next) that
            // appear in the activity log as watched.
            val activeFileIds = active.map { it.fileId }.toSet()
            for (i in currentIndex until attributed) {
                val mid = queue[i]
                if (mid.fileId in activeFileIds && mid.durationMs != null) {
                    val durSec = mid.durationMs / 1000.0
                    repo.postProgress(
                        fileId = mid.fileId,
                        name = mid.name,
                        position = durSec,
                        duration = durSec,
                        ended = true,
                    )
                }
            }

            val attributedItem = queue[attributed]
            val durMs = if (durationMs > 0) durationMs else (attributedItem.durationMs ?: 0L)
            val fraction = if (durMs > 0) positionMs.toDouble() / durMs else 0.0
            val finishedEpisode = fraction >= 0.95
            val durationSec = if (durMs > 0) durMs / 1000.0 else null
            repo.postProgress(
                fileId = attributedItem.fileId,
                name = attributedItem.name,
                position = positionMs / 1000.0,
                duration = durationSec,
                ended = finishedEpisode,
            )

            if (finishedEpisode && attributed + 1 in queue.indices) {
                currentIndex = attributed
                startUpNextCountdown(attributed + 1)
            } else {
                _ui.value = _ui.value.copy(playing = false, finished = true)
            }
        }
    }

    /** Host couldn't launch VLC (e.g. it vanished mid-session). */
    fun onLaunchFailed() {
        stopAwakePolling()
        _ui.value = _ui.value.copy(playing = false, finished = true)
    }

    private fun startUpNextCountdown(nextIndex: Int) {
        upNextJob?.cancel()
        val next = queue[nextIndex]
        _ui.value = _ui.value.copy(
            playing = false,
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
            launchCurrent(fromStart = true)
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
            viewModelScope.launch { launchCurrent(fromStart = true) }
        } else {
            _ui.value = _ui.value.copy(finished = true)
        }
    }

    fun cancelUpNext() {
        upNextJob?.cancel()
        _upNextRemaining.value = null
        _ui.value = _ui.value.copy(upNextVisible = false, finished = true)
    }

    private fun startAwakePolling() {
        if (awakeJob?.isActive == true) return
        awakeJob = viewModelScope.launch {
            while (isActive) {
                val status = container.keepAwake.poll()
                if (status?.phase == "prompt") container.keepAwake.extend()
                delay(30_000)
            }
        }
    }

    private fun stopAwakePolling() {
        awakeJob?.cancel()
        awakeJob = null
    }

    override fun onCleared() {
        stopAwakePolling()
        upNextJob?.cancel()
        super.onCleared()
    }

    companion object {
        fun factory(
            container: AppContainer,
            titleId: String,
            fileId: String,
            startOver: Boolean,
            shuffle: Boolean,
            seed: Long,
        ): ViewModelProvider.Factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T =
                ExternalPlayerViewModel(container, titleId, fileId, startOver, shuffle, seed) as T
        }
    }
}
