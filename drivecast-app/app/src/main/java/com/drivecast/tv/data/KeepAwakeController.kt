package com.drivecast.tv.data

import com.drivecast.tv.api.AwakeStatus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Session-scoped state for the server's keep-awake ("Are you still watching?")
 * handshake. The server holds a macOS power assertion while a stream is active
 * and, after ~2 minutes with no bytes, enters a 30-second "prompt" window before
 * letting the Mac sleep. This device only participates once it has actually
 * started playback this session, so idle browsers never see the prompt.
 *
 * Owned by [com.drivecast.tv.di.AppContainer]; the polling loop and the global
 * overlay both read from here.
 */
class KeepAwakeController(private val repository: LibraryRepository) {

    /** Flips true the first time ExoPlayer plays; gates all polling. */
    val playbackStarted = MutableStateFlow(false)

    private val _status = MutableStateFlow<AwakeStatus?>(null)
    val status: StateFlow<AwakeStatus?> = _status.asStateFlow()

    fun markPlaybackStarted() {
        playbackStarted.value = true
    }

    suspend fun poll(): AwakeStatus? {
        val s = runCatching { repository.awakeStatus() }.getOrNull()
        if (s != null) _status.value = s
        return s
    }

    suspend fun extend() {
        runCatching { repository.awakeExtend() }.getOrNull()?.let { _status.value = it }
    }

    suspend fun release() {
        runCatching { repository.awakeRelease() }.getOrNull()?.let { _status.value = it }
    }
}
