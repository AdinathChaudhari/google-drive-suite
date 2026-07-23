package com.drivecast.tv.api

import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

/**
 * Retrofit interface for the drivecast JSON endpoints. Discovery (/api/ping) and
 * the subtitle HEAD probe are done with raw OkHttp elsewhere because they need
 * short timeouts / raw response headers.
 */
interface DrivecastApi {

    /** Validation target during setup. 403 -> remote disabled, 401 -> bad token. */
    @GET("api/remote")
    suspend fun remote(): Response<RemoteInfo>

    @GET("api/library")
    suspend fun library(): LibraryResponse

    @GET("api/sections")
    suspend fun sections(): SectionsResponse

    // ---- tabs edit/reorder (TV Settings screen) ----

    /** Full round-trip of the "tabs" list (rename/reorder). Auth via TokenInterceptor. */
    @POST("api/settings")
    suspend fun updateSettings(@Body body: SettingsPatch): Response<SettingsSaveResponse>

    @GET("api/title/{id}")
    suspend fun title(@Path("id") id: String): Response<Title>

    @GET("api/continue")
    suspend fun continueWatching(): ContinueResponse

    @DELETE("api/continue/{fileId}")
    suspend fun removeContinue(@Path("fileId") fileId: String): RemoveResponse

    @GET("api/watched-map")
    suspend fun watchedMap(): WatchedMap

    @POST("api/progress")
    suspend fun progress(@Body body: ProgressBody): OkResponse

    // ---- keep-awake ("Are you still watching?") ----

    @GET("api/awake/status")
    suspend fun awakeStatus(): AwakeStatus

    /** User said yes: restart a fresh 120s grace window. */
    @POST("api/awake/extend")
    suspend fun awakeExtend(): AwakeStatus

    /** User said no: release the power assertion immediately. */
    @POST("api/awake/release")
    suspend fun awakeRelease(): AwakeStatus

    // ---- playlist hand-off (VLC playlist Next/Prev) ----

    @GET("api/playlist/{id}")
    suspend fun playlist(
        @Path("id") id: String,
        @Query("start") start: String? = null,
        @Query("shuffle") shuffle: Int? = null,
        @Query("seed") seed: Long? = null,
    ): PlaylistResponse

    @GET("api/stream/recent")
    suspend fun streamRecent(): StreamRecent
}
