package com.drivecast.tv.data

import com.drivecast.tv.api.AwakeStatus
import com.drivecast.tv.api.ContinueItem
import com.drivecast.tv.api.DrivecastApi
import com.drivecast.tv.api.LibraryResponse
import com.drivecast.tv.api.PlaylistItem
import com.drivecast.tv.api.ProgressBody
import com.drivecast.tv.api.RemoteInfo
import com.drivecast.tv.api.SectionInfo
import com.drivecast.tv.api.SettingsPatch
import com.drivecast.tv.api.SettingsSaveResponse
import com.drivecast.tv.api.StreamRecent
import com.drivecast.tv.api.TabPatch
import com.drivecast.tv.api.Title
import com.drivecast.tv.api.WatchedMap
import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import java.io.IOException
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import retrofit2.Retrofit

/** A probed subtitle track: the streamable URL plus the MIME the player needs. */
data class SubtitleTrack(val url: String, val mimeType: String)

/**
 * The single source of truth for library data. Holds the active server URL, an
 * in-memory copy of the last library fetch, and all HTTP calls. The shared
 * OkHttpClient (with the TokenInterceptor) authorizes every request, so poster
 * and stream URLs are handed out plain — the token is added on the wire.
 */
class LibraryRepository(
    private val okHttp: OkHttpClient,
    private val json: Json,
    private val tokenHolder: TokenHolder,
    private val discovery: Discovery,
    private val configStore: ServerConfigStore,
) {
    private var baseUrl: String? = null
    private var api: DrivecastApi? = null

    /** Serializes rediscovery so concurrent failing calls don't each sweep the /24. */
    private val rediscoverMutex = Mutex()

    @Volatile
    var lastLibrary: LibraryResponse? = null
        private set

    @Volatile
    var lastSections: List<SectionInfo> = emptyList()
        private set

    fun configure(baseUrl: String, token: String) {
        val normalized = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        this.baseUrl = normalized
        tokenHolder.token = token
        api = Retrofit.Builder()
            .baseUrl(normalized)
            .client(okHttp)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(DrivecastApi::class.java)
    }

    private fun requireApi(): DrivecastApi =
        api ?: error("LibraryRepository not configured with a server yet")

    private fun requireBase(): String =
        baseUrl?.removeSuffix("/") ?: error("LibraryRepository not configured with a server yet")

    // ---- self-healing server address ----

    /**
     * Run a network read, and if it fails with a connect-level IO error (the saved
     * server address went dead — e.g. DHCP moved the Mac to a new LAN IP), sweep the
     * LAN once via [Discovery]. If a drivecast server turns up at a *different*
     * address, reconfigure to it with the same token, persist it, and retry the call
     * once. HTTP errors (HttpException from Retrofit) are NOT IOExceptions, so 401/403
     * and friends pass straight through untouched.
     */
    private suspend fun <T> withAutoRediscover(block: suspend () -> T): T =
        try {
            block()
        } catch (io: IOException) {
            if (rediscoverToNewAddress(baseUrl)) block() else throw io
        }

    /**
     * Returns true when the active server address is (now) a live, different one:
     * either a concurrent caller already relocated us, or this call found a new
     * server on the LAN and reconfigured to it. False means nothing better was found
     * and the caller should surface the original failure.
     */
    private suspend fun rediscoverToNewAddress(staleBase: String?): Boolean = rediscoverMutex.withLock {
        // A concurrent failure may have already relocated the base; if so, just retry.
        if (normalize(baseUrl) != normalize(staleBase)) return true
        val token = tokenHolder.token?.ifBlank { null } ?: return false
        val candidate = discovery.scan()
            .firstOrNull { normalize(it.baseUrl) != normalize(staleBase) }
            ?: return false
        configure(candidate.baseUrl, token)
        configStore.save(candidate.baseUrl, token)
        true
    }

    private fun normalize(url: String?): String? = url?.removeSuffix("/")

    // ---- URLs handed to Coil / ExoPlayer (token added by the interceptor) ----

    fun posterUrl(key: String?): String? {
        val k = key?.ifBlank { null } ?: return null
        return "${requireBase()}/api/poster/$k"
    }

    fun streamUrl(fileId: String): String = "${requireBase()}/stream/$fileId"

    fun subtitleUrl(fileId: String): String = "${requireBase()}/api/subtitles/$fileId"

    // ---- URLs handed to an external player (VLC), which never passes through
    // the OkHttp TokenInterceptor, so the token must be baked into the URL. ----

    fun authorizedStreamUrl(fileId: String): String = withToken(streamUrl(fileId))

    fun authorizedSubtitleUrl(fileId: String): String = withToken(subtitleUrl(fileId))

    /** The m3u playlist URL VLC expands into a real in-player playlist. Built manually (not via Retrofit). */
    fun playlistUrl(titleId: String, startFileId: String?, shuffle: Boolean = false, seed: Long = 0L): String {
        val b = requireBase().toHttpUrl().newBuilder()
            .addPathSegment("api").addPathSegment("playlist").addPathSegment("$titleId.m3u")
        if (startFileId != null) b.addQueryParameter("start", startFileId)
        if (shuffle) {
            b.addQueryParameter("shuffle", "1")
            b.addQueryParameter("seed", seed.toString())
        }
        return b.build().toString()
    }

    fun authorizedPlaylistUrl(titleId: String, startFileId: String?, shuffle: Boolean = false, seed: Long = 0L) =
        withToken(playlistUrl(titleId, startFileId, shuffle, seed))

    private fun withToken(url: String): String {
        val token = tokenHolder.token
        if (token.isNullOrBlank()) return url
        return url.toHttpUrl().newBuilder().addQueryParameter("token", token).build().toString()
    }

    // ---- reads ----

    /** Validate a pairing by hitting /api/remote. 200 = ok, 403 = remote off, 401 = bad token. */
    suspend fun validateRemote(): retrofit2.Response<RemoteInfo> = withContext(Dispatchers.IO) {
        withAutoRediscover { requireApi().remote() }
    }

    suspend fun refresh(): LibraryResponse = withContext(Dispatchers.IO) {
        withAutoRediscover {
            val lib = requireApi().library()
            lastLibrary = lib
            lib
        }
    }

    /** Section metadata (labels, icons, per-section vocabulary) for the tabbed home. */
    suspend fun sections(): List<SectionInfo> = withContext(Dispatchers.IO) {
        withAutoRediscover {
            val list = requireApi().sections().sections
            lastSections = list
            list
        }
    }

    suspend fun continueWatching(): List<ContinueItem> = withContext(Dispatchers.IO) {
        withAutoRediscover { requireApi().continueWatching().items }
    }

    suspend fun watchedMap(): WatchedMap = withContext(Dispatchers.IO) {
        withAutoRediscover { requireApi().watchedMap() }
    }

    suspend fun title(id: String): Title? = withContext(Dispatchers.IO) {
        val resp = withAutoRediscover { requireApi().title(id) }
        if (resp.isSuccessful) resp.body() else null
    }

    /** The JSON twin of [playlistUrl]/[authorizedPlaylistUrl] — used to learn the server's canonical episode order. */
    suspend fun playlistItems(
        titleId: String,
        startFileId: String?,
        shuffle: Boolean = false,
        seed: Long? = null,
    ): List<PlaylistItem> = withContext(Dispatchers.IO) {
        withAutoRediscover { requireApi().playlist(titleId, startFileId, if (shuffle) 1 else null, seed).items }
    }

    suspend fun streamRecent(): StreamRecent = withContext(Dispatchers.IO) {
        withAutoRediscover { requireApi().streamRecent() }
    }

    // ---- keep-awake ("Are you still watching?") ----

    suspend fun awakeStatus(): AwakeStatus = withContext(Dispatchers.IO) { requireApi().awakeStatus() }

    suspend fun awakeExtend(): AwakeStatus = withContext(Dispatchers.IO) { requireApi().awakeExtend() }

    suspend fun awakeRelease(): AwakeStatus = withContext(Dispatchers.IO) { requireApi().awakeRelease() }

    // ---- writes ----

    suspend fun removeContinue(fileId: String): Boolean = withContext(Dispatchers.IO) {
        runCatching { requireApi().removeContinue(fileId).removed }.getOrDefault(false)
    }

    /**
     * Full round-trip of the "tabs" list (rename/reorder) — mirrors the web UI's
     * POST /api/settings {tabs} contract. On success, refreshes [lastSections]
     * from the server's echoed (re-resolved) tab list so callers can re-render
     * without a separate [sections] fetch.
     */
    suspend fun saveTabs(tabs: List<TabPatch>): SettingsSaveResponse = withContext(Dispatchers.IO) {
        val resp = requireApi().updateSettings(SettingsPatch(tabs = tabs))
        val body = resp.body()
        if (resp.isSuccessful && body != null) {
            lastSections = body.tabs
            body
        } else {
            SettingsSaveResponse(ok = false, tabs = lastSections)
        }
    }

    suspend fun postProgress(
        fileId: String,
        name: String?,
        position: Double,
        duration: Double?,
        ended: Boolean,
    ) = withContext(Dispatchers.IO) {
        runCatching {
            requireApi().progress(
                ProgressBody(
                    fileId = fileId,
                    name = name,
                    position = position,
                    duration = duration,
                    ended = ended,
                )
            )
        }
        Unit
    }

    /**
     * HEAD-probe a file's subtitle track. Returns the track (URL + MIME derived
     * from Content-Type) when the server has one, or null on 404/any failure.
     * Never throws — playback must never block on subtitles.
     */
    suspend fun probeSubtitle(fileId: String): SubtitleTrack? = withContext(Dispatchers.IO) {
        val url = subtitleUrl(fileId)
        val request = Request.Builder().url(url).head().build()
        try {
            okHttp.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return@withContext null
                val contentType = resp.header("Content-Type")?.substringBefore(';')?.trim()
                SubtitleTrack(url = url, mimeType = mimeForSubtitle(contentType))
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun mimeForSubtitle(contentType: String?): String = when (contentType) {
        "text/vtt" -> "text/vtt"
        "text/x-ssa", "application/x-ass", "text/x-ass" -> "text/x-ssa"
        else -> "application/x-subrip"
    }
}

/**
 * The Kotlin twin of the web UI's currentTabsPayload() (app.js): rebuilds the
 * full ordered tabs payload for POST /api/settings from the current section
 * list. Each tab keeps its own key (renames must never re-slugify) and echoes
 * accent/accent2 only when the server has already assigned them, so
 * validate_tabs doesn't re-palette on an unrelated rename/reorder save.
 */
fun toTabPatch(sections: List<SectionInfo>): List<TabPatch> = sections.map { section ->
    TabPatch(
        key = section.key,
        label = section.label,
        icon = section.icon,
        behavior = section.behavior,
        accent = section.accent,
        accent2 = section.accent2,
    )
}

/** Holds the live access token so the shared interceptor can read it after re-pairing. */
class TokenHolder {
    @Volatile
    var token: String? = null
}
