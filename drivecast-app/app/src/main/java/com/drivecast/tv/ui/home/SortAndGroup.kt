package com.drivecast.tv.ui.home

import com.drivecast.tv.api.Title

enum class SortKey(val id: String, val defaultAscending: Boolean) {
    RECENT("recent", defaultAscending = false),
    TITLE("title", defaultAscending = true),
    YEAR("year", defaultAscending = false),
    WATCHED("watched", defaultAscending = false);
    companion object { fun fromId(id: String?): SortKey = entries.firstOrNull { it.id == id } ?: RECENT }
}

data class SortSpec(
    val key: SortKey = SortKey.RECENT,
    val ascending: Boolean = SortKey.RECENT.defaultAscending,
)

enum class GroupKey(val id: String) {
    NONE("none"), TYPE("type"), CATEGORY("category");
    companion object { fun fromId(id: String?): GroupKey = entries.firstOrNull { it.id == id } ?: NONE }
}

/** In-memory snapshot of the persisted prefs, cached on AppContainer (see Step 3). */
data class SortAndGroupPrefs(val sort: SortSpec = SortSpec(), val group: GroupKey = GroupKey.NONE)

/** Overlay pick semantics: re-pick active key = flip direction; new key = its default direction. */
fun nextSortSpec(current: SortSpec, picked: SortKey): SortSpec =
    if (picked == current.key) current.copy(ascending = !current.ascending)
    else SortSpec(picked, picked.defaultAscending)

fun sortTitles(titles: List<Title>, spec: SortSpec, watchedMap: Map<String, Double> = emptyMap()): List<Title> =
    titles.sortedWith(comparatorFor(spec, watchedMap))

// Nulls last REGARDLESS of direction: direction is applied inside the non-null branch only —
// never via Comparator.reversed(), which would move nulls first. The comparator stays PURE: the
// watched-map is passed in, never read from a singleton.
internal fun comparatorFor(spec: SortSpec, watchedMap: Map<String, Double> = emptyMap()): Comparator<Title> {
    val titleTiebreak = compareBy<Title>({ it.displayTitle.lowercase() }, { it.id })
    return when (spec.key) {
        SortKey.RECENT  -> nullsLastBy(spec.ascending) { it.addedAt }.then(titleTiebreak)
        SortKey.YEAR    -> nullsLastBy(spec.ascending) { it.year }.then(titleTiebreak)
        SortKey.WATCHED -> nullsLastBy(spec.ascending) { lastPlayedOf(it, watchedMap) }.then(titleTiebreak)
        SortKey.TITLE   -> {
            val primary = compareBy<Title> { it.displayTitle.lowercase() } // displayTitle is never null ("Untitled" fallback)
            (if (spec.ascending) primary else primary.reversed()).thenBy { it.id }
        }
    }
}

/** Newest last-played epoch across a title's playable files; null = never watched
 *  (sorts LAST in both directions via nullsLastBy — the canonical nulls rule).
 *  Mirrors app.js fileIdsOf/lastPlayedOf: shows aggregate over ALL seasons'
 *  episodes (including extras pseudo-seasons); movies use file_id only (movie
 *  `extras` do NOT count, matching the web). */
internal fun lastPlayedOf(title: Title, watchedMap: Map<String, Double>): Double? =
    fileIdsOf(title).mapNotNull { watchedMap[it] }.maxOrNull()

internal fun fileIdsOf(title: Title): List<String> =
    if (title.isShow) title.seasons.flatMap { s -> s.episodes.mapNotNull { it.fileId } }
    else listOfNotNull(title.fileId)

private fun <K : Comparable<K>> nullsLastBy(ascending: Boolean, selector: (Title) -> K?): Comparator<Title> =
    Comparator { a, b ->
        val ka = selector(a); val kb = selector(b)
        when {
            ka == null && kb == null -> 0
            ka == null -> 1
            kb == null -> -1
            else -> if (ascending) ka.compareTo(kb) else kb.compareTo(ka)
        }
    }

/** What the grid renders: headers interleaved with tiles, already in final order. */
sealed interface GridRow {
    data class Header(val label: String) : GridRow
    data class Tile(val title: Title) : GridRow
}

/**
 * [sortedTitles] must already be sorted (groupBy is stable, so within-group order == sort order).
 * NONE -> tiles only, no headers. TYPE -> Movies, TV Shows (structural: title.isShow, so nothing
 * is ever dropped). CATEGORY -> Movies, TV Shows, then trailing "Other" (unknown category values);
 * empty groups omitted. Mirrors the chip vocabulary exactly.
 */
fun buildGridRows(sortedTitles: List<Title>, group: GroupKey): List<GridRow> = when (group) {
    GroupKey.NONE -> sortedTitles.map { GridRow.Tile(it) }
    GroupKey.TYPE -> {
        val buckets = sortedTitles.groupBy { if (it.isShow) "show" else "movie" }
        buildList {
            listOf("movie" to "Movies", "show" to "TV Shows").forEach { (k, label) ->
                buckets[k]?.let { add(GridRow.Header(label)); it.forEach { t -> add(GridRow.Tile(t)) } }
            }
        }
    }
    GroupKey.CATEGORY -> {
        val buckets = sortedTitles.groupBy { categoryOf(it).let { c -> if (c in KNOWN_CATEGORIES) c else "other" } }
        buildList {
            listOf("movie" to "Movies", "show" to "TV Shows", "other" to "Other").forEach { (cat, label) ->
                buckets[cat]?.let { titles ->
                    add(GridRow.Header(label))
                    titles.forEach { add(GridRow.Tile(it)) }
                }
            }
        }
    }
}

// ---- Pill / overlay labels (pure, unit-tested) ----
fun sortPillLabel(spec: SortSpec): String = when (spec.key) {
    SortKey.RECENT  -> if (spec.ascending) "Sort: Oldest" else "Sort: Recent"
    SortKey.TITLE   -> if (spec.ascending) "Sort: A–Z" else "Sort: Z–A"
    SortKey.YEAR    -> if (spec.ascending) "Sort: Year ↑" else "Sort: Year ↓"
    SortKey.WATCHED -> if (spec.ascending) "Sort: Watched ↑" else "Sort: Watched ↓"
}

fun groupPillLabel(group: GroupKey): String = when (group) {
    GroupKey.NONE -> "Group: None"
    GroupKey.TYPE -> "Group: Type"
    GroupKey.CATEGORY -> "Group: Category"
}

/**
 * The group actually applied to the grid: a specific category chip already narrows the tab down
 * to one category, so grouping by category on top of that would render exactly one header
 * repeating the chip's own label — chrome, not information. Non-entertainment tabs never group
 * (they have no category vocabulary at all).
 */
fun effectiveGroupFor(isEntertainment: Boolean, selectedCat: String?, group: GroupKey): GroupKey =
    if (isEntertainment && selectedCat == null) group else GroupKey.NONE

/** Active key shows its live direction (SELECT on it flips); inactive keys show just the name. */
fun sortOptionLabel(key: SortKey, current: SortSpec): String {
    val base = when (key) {
        SortKey.RECENT -> "Recently added"
        SortKey.TITLE -> "Title"
        SortKey.YEAR -> "Year"
        SortKey.WATCHED -> "Recently watched"
    }
    if (key != current.key) return base
    val dir = when (key) {
        SortKey.TITLE -> if (current.ascending) "A–Z" else "Z–A"
        else          -> if (current.ascending) "Oldest first" else "Newest first"
    }
    return "$base · $dir"
}

fun groupOptionLabel(group: GroupKey): String = when (group) {
    GroupKey.NONE -> "None"
    GroupKey.TYPE -> "Type"
    GroupKey.CATEGORY -> "Category"
}

// ---- MOVED (verbatim, made internal) from HomeScreen.kt lines 701–724 ----
internal fun categoryOf(title: Title): String =
    title.category?.ifBlank { null } ?: if (title.isShow) "show" else "movie"

internal data class CategoryChip(val label: String, val category: String?)

internal val KNOWN_CATEGORIES = setOf("movie", "show")

internal fun matchesCategory(title: Title, selected: String?): Boolean = when (selected) {
    null -> true
    "other" -> categoryOf(title) !in KNOWN_CATEGORIES
    else -> categoryOf(title) == selected
}

internal fun visibleChips(titles: List<Title>): List<CategoryChip> {
    val present = titles.map { categoryOf(it) }.toSet()
    val hasOther = titles.any { categoryOf(it) !in KNOWN_CATEGORIES }
    return buildList {
        add(CategoryChip("All", null))
        if ("movie" in present) add(CategoryChip("Movies", "movie"))
        if ("show" in present) add(CategoryChip("TV Shows", "show"))
        if (hasOther) add(CategoryChip("Other", "other"))
    }
}
