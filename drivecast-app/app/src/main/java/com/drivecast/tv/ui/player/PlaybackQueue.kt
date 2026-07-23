package com.drivecast.tv.ui.player

import com.drivecast.tv.api.Title
import com.drivecast.tv.api.WatchedProgress

/** One entry in the playback queue (a movie, or the ordered episodes of a show). */
data class QueueItem(val fileId: String, val name: String?, val durationMs: Long?)

/**
 * Shared playback bootstrap. Both the internal ExoPlayer path
 * ([PlayerViewModel]) and the external VLC hand-off ([ExternalPlayerViewModel])
 * build the same episode queue and honor the same resume rule through here, so
 * the two players always agree on what plays next and where it resumes.
 */
object PlaybackQueue {

    /** Build the ordered play queue for a title (all episodes of a show, or the single movie). */
    fun build(
        title: Title?,
        startFileId: String,
        shuffle: Boolean = false,
        seed: Long = 0L,
    ): List<QueueItem> = when {
        title == null -> listOf(QueueItem(startFileId, null, null))
        title.isShow -> {
            val main = title.seasons
                .filterNot { it.extras }
                .flatMap { it.episodes }
                .mapNotNull { ep -> ep.fileId?.let { QueueItem(it, ep.name ?: ep.title, ep.durationMs) } }
            when {
                // An extras/featurette clip of a show lives in an extras season, so it
                // isn't in the main flatten. Play just that clip (looked up across ALL
                // seasons) — never fall through to the main episodes. Shuffle never hits
                // this: the Shuffle button always starts on a main-season file.
                !shuffle && main.none { it.fileId == startFileId } ->
                    title.seasons.flatMap { it.episodes }
                        .firstOrNull { it.fileId == startFileId }
                        ?.let { listOf(QueueItem(startFileId, it.name ?: it.title, it.durationMs)) }
                        ?: main.ifEmpty { listOf(QueueItem(startFileId, title.title, null)) }
                shuffle -> SeededShuffle.shuffle(main, seed)
                    .ifEmpty { listOf(QueueItem(startFileId, title.title, null)) }
                else -> main.ifEmpty { listOf(QueueItem(startFileId, title.title, null)) }
            }
        }
        startFileId == title.fileId ->
            listOf(QueueItem(startFileId, title.title, title.durationMs))
        // A featurette/extra clip: use ITS own name + duration (not the film's)
        // so resume + progress reporting are correct. Single item, so playback
        // never auto-advances into another clip or the feature.
        else -> title.extras.flatMap { it.episodes }
            .firstOrNull { it.fileId == startFileId }
            ?.let { listOf(QueueItem(startFileId, it.name ?: it.title, it.durationMs)) }
            ?: listOf(QueueItem(startFileId, title.title, title.durationMs))
    }

    /** Index of the requested start file in the queue, or 0 if it isn't found (0 always for a shuffled queue). */
    fun startIndex(queue: List<QueueItem>, startFileId: String, shuffle: Boolean = false): Int =
        if (shuffle) 0 else queue.indexOfFirst { it.fileId == startFileId }.let { if (it < 0) 0 else it }

    /** True when [fileId] belongs to one of [title]'s real (non-extras) seasons. */
    fun isMainShowEpisode(title: Title?, fileId: String): Boolean =
        title?.isShow == true && title.seasons.any { !it.extras && it.episodes.any { ep -> ep.fileId == fileId } }

    /** The same flatten as [build]'s show branch (extras excluded, no shuffle) — local fallback. */
    fun buildShow(title: Title): List<QueueItem> = title.seasons
        .filterNot { it.extras }
        .flatMap { it.episodes }
        .mapNotNull { ep -> ep.fileId?.let { QueueItem(it, ep.name ?: ep.title, ep.durationMs) } }

    /** Resume position in ms from the server's stored percent, when in the 1..90% band. */
    fun resumeMsFor(item: QueueItem, progress: Map<String, WatchedProgress>): Long {
        val percent = progress[item.fileId]?.percent ?: 0.0
        val durationMs = item.durationMs ?: return 0L
        if (percent <= 1.0 || percent >= 90.0) return 0L
        return (percent / 100.0 * durationMs).toLong()
    }
}
