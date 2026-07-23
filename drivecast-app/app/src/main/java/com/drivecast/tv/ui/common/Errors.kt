package com.drivecast.tv.ui.common

import androidx.media3.common.PlaybackException
import androidx.media3.datasource.HttpDataSource

/** A user-facing playback error plus whether a single retry is worth attempting. */
data class PlaybackError(val message: String, val retriable: Boolean)

/**
 * Map an ExoPlayer failure to a friendly message. drivecast returns 502 with a
 * JSON body of {"error":"download_quota"} or {"error":"rate_limited"} when the
 * upstream Drive quota is hit; those are worth one delayed retry. Connection and
 * decoder failures get their own messages.
 */
fun mapPlaybackException(error: PlaybackException): PlaybackError {
    var cause: Throwable? = error
    while (cause != null) {
        when (cause) {
            is HttpDataSource.InvalidResponseCodeException -> {
                val body = runCatching { String(cause.responseBody) }.getOrDefault("")
                return when {
                    body.contains("download_quota") -> PlaybackError(
                        "Google Drive download quota reached for this file. Retrying…",
                        retriable = true,
                    )
                    body.contains("rate_limited") -> PlaybackError(
                        "Google Drive is rate-limiting downloads. Retrying…",
                        retriable = true,
                    )
                    cause.responseCode == 401 -> PlaybackError(
                        "Access token was rejected. Re-pair with the server.",
                        retriable = false,
                    )
                    cause.responseCode == 403 -> PlaybackError(
                        "Remote access is off on the server.",
                        retriable = false,
                    )
                    else -> PlaybackError(
                        "Server returned an error (${cause.responseCode}).",
                        retriable = false,
                    )
                }
            }
            is HttpDataSource.HttpDataSourceException -> return PlaybackError(
                "Couldn't reach the server. Check that it's running and on the same network.",
                retriable = true,
            )
        }
        cause = cause.cause
    }
    return when (error.errorCode) {
        PlaybackException.ERROR_CODE_DECODING_FAILED,
        PlaybackException.ERROR_CODE_DECODER_INIT_FAILED,
        PlaybackException.ERROR_CODE_DECODING_FORMAT_UNSUPPORTED,
        ->
            PlaybackError(
                "This Fire TV can't decode this file's format.",
                retriable = false,
            )

        PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED,
        PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT,
        ->
            PlaybackError(
                "Network error reaching the server. Retrying…",
                retriable = true,
            )

        else -> PlaybackError(
            error.localizedMessage ?: "Playback failed.",
            retriable = false,
        )
    }
}
