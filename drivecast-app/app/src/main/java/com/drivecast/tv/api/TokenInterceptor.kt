package com.drivecast.tv.api

import okhttp3.Interceptor
import okhttp3.Response

/**
 * Appends `?token=<token>` to every request that doesn't already carry one.
 * The drivecast remote-access middleware accepts the token as a query param on
 * any request (API JSON, /stream, /api/poster, /api/subtitles), so this single
 * interceptor authorizes the whole shared OkHttpClient — including the
 * ExoPlayer stream data source and the Coil poster loader.
 *
 * The token is read live from a supplier so re-pairing without rebuilding the
 * client works.
 */
class TokenInterceptor(private val tokenProvider: () -> String?) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val token = tokenProvider()
        if (token.isNullOrBlank() || request.url.queryParameter("token") != null) {
            return chain.proceed(request)
        }
        val url = request.url.newBuilder()
            .addQueryParameter("token", token)
            .build()
        return chain.proceed(request.newBuilder().url(url).build())
    }
}
