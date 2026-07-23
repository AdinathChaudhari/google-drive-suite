package com.drivecast.tv.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * DTOs for the drivecast HTTP API. Every field is nullable with a default so a
 * partial or evolving server response never crashes deserialization
 * (the Json parser is also configured with ignoreUnknownKeys + coerceInputValues).
 */

@Serializable
data class Ping(
    val app: String? = null,
    val remote: Boolean? = null,
    val version: String? = null,
)

@Serializable
data class RemoteUrl(
    val label: String? = null,
    val url: String? = null,
)

@Serializable
data class RemoteInfo(
    val enabled: Boolean = false,
    val token: String? = null,
    val port: Int? = null,
    val urls: List<RemoteUrl> = emptyList(),
)

@Serializable
data class SectionsResponse(
    val sections: List<SectionInfo> = emptyList(),
)

@Serializable
data class SectionInfo(
    val key: String = "",
    val label: String? = null,
    val icon: String? = null,
    // "continue" is a Kotlin keyword; map the JSON field explicitly.
    @SerialName("continue") val continueLabel: String? = null,
    val lib: String? = null,
    val empty: String? = null,
    // Optional per-section vocabulary (courses -> Module/Lesson, etc.).
    val season: String? = null,
    val episode: String? = null,
    // Which content-type this tab renders like ("entertainment" | "courses" |
    // "podcasts" | a plugin key). Null on legacy servers that predate behaviors.
    val behavior: String? = null,
    // Tab accent palette (hex colors), assigned server-side by validate_tabs.
    // Null on legacy servers or tabs not yet paletted (e.g. "entertainment").
    val accent: String? = null,
    val accent2: String? = null,
)

/**
 * A single tab entry as sent to POST /api/settings ("tabs" list). Mirrors the
 * web UI's currentTabsPayload() contract exactly: every field present, key
 * kept stable across renames, accent/accent2 echoed back so validate_tabs
 * doesn't re-palette.
 */
@Serializable
data class TabPatch(
    val key: String = "",
    val label: String? = null,
    val icon: String? = null,
    val behavior: String? = null,
    val accent: String? = null,
    val accent2: String? = null,
)

@Serializable
data class SettingsPatch(
    val tabs: List<TabPatch> = emptyList(),
)

@Serializable
data class SettingsSaveResponse(
    val ok: Boolean = false,
    val tabs: List<SectionInfo> = emptyList(),
)

@Serializable
data class LibraryResponse(
    val titles: List<Title> = emptyList(),
    @SerialName("generated_at") val generatedAt: Double? = null,
    val scanning: Boolean = false,
    @SerialName("selected_drives") val selectedDrives: List<String> = emptyList(),
)

@Serializable
data class Title(
    val id: String = "",
    val type: String? = null,               // "movie" | "show"
    val title: String? = null,
    val year: Int? = null,
    val poster: String? = null,
    val overview: String? = null,
    val quality: String? = null,
    val section: String? = null,
    val category: String? = null,
    @SerialName("added_at") val addedAt: Double? = null,
    // Movie-only fields
    @SerialName("file_id") val fileId: String? = null,
    val size: Long? = null,
    @SerialName("duration_ms") val durationMs: Long? = null,
    // Show-only field
    val seasons: List<Season> = emptyList(),
    // Movie-only: labelled groups of bonus clips (featurettes/extras/...).
    // Reuses the Season shape; does NOT feed isShow (movies keep seasons empty).
    val extras: List<Season> = emptyList(),
) {
    val isShow: Boolean get() = type == "show" || seasons.isNotEmpty()
    val displayTitle: String get() = title?.ifBlank { null } ?: "Untitled"
}

@Serializable
data class Season(
    val season: Int? = null,
    // Pseudo-season label (e.g. "Featurettes", "Featurettes · Season 2").
    val name: String? = null,
    // Marks an extras pseudo-season (bonus material, not a real season).
    val extras: Boolean = false,
    val episodes: List<Episode> = emptyList(),
)

@Serializable
data class Episode(
    val title: String? = null,
    val episode: Int? = null,
    @SerialName("file_id") val fileId: String? = null,
    val name: String? = null,
    @SerialName("duration_ms") val durationMs: Long? = null,
    val size: Long? = null,
    @SerialName("parent_id") val parentId: String? = null,
)

@Serializable
data class ContinueResponse(
    val items: List<ContinueItem> = emptyList(),
)

@Serializable
data class ContinueItem(
    @SerialName("file_id") val fileId: String = "",
    val name: String? = null,
    val position: Double = 0.0,
    val duration: Double? = null,
    val percent: Double = 0.0,
    val watched: Boolean = false,
    @SerialName("last_played") val lastPlayed: Double? = null,
    // Enriched from the owning library title
    val title: String? = null,
    @SerialName("title_id") val titleId: String? = null,
    val type: String? = null,
    val poster: String? = null,
    val section: String? = null,
) {
    val displayName: String get() = title?.ifBlank { null } ?: name?.ifBlank { null } ?: "Untitled"
}

@Serializable
data class RemoveResponse(
    val ok: Boolean = false,
    val removed: Boolean = false,
)

@Serializable
data class WatchedMap(
    val map: Map<String, Double> = emptyMap(),
    val progress: Map<String, WatchedProgress> = emptyMap(),
)

@Serializable
data class WatchedProgress(
    val percent: Double = 0.0,
    val watched: Boolean = false,
)

@Serializable
data class ProgressBody(
    @SerialName("file_id") val fileId: String,
    val name: String? = null,
    val position: Double,
    val duration: Double? = null,
    val ended: Boolean = false,
)

@Serializable
data class OkResponse(
    val ok: Boolean = false,
)

/**
 * Keep-awake status from the server's macOS power-assertion manager.
 * phase is one of "active" | "grace" | "prompt" | "idle"; secondsLeft is the
 * countdown within the current grace/prompt window (null when idle/active).
 */
@Serializable
data class AwakeStatus(
    val active: Int = 0,
    val holding: Boolean = false,
    val phase: String? = null,
    @SerialName("seconds_left") val secondsLeft: Double? = null,
)

@Serializable
data class PlaylistResponse(
    @SerialName("title_id") val titleId: String? = null,
    val items: List<PlaylistItem> = emptyList(),
)

@Serializable
data class PlaylistItem(
    @SerialName("file_id") val fileId: String = "",
    val name: String? = null,
    @SerialName("duration_ms") val durationMs: Long? = null,
)

@Serializable
data class StreamRecent(
    val now: Double? = null,
    val items: List<StreamActivity> = emptyList(),
)

@Serializable
data class StreamActivity(
    @SerialName("file_id") val fileId: String = "",
    val ts: Double? = null,
    val age: Double? = null,
)
