package com.drivecast.tv.ui.home

import com.drivecast.tv.api.Episode
import com.drivecast.tv.api.Season
import com.drivecast.tv.api.Title
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

private fun t(
    id: String,
    title: String? = id,
    year: Int? = null,
    addedAt: Double? = null,
    category: String? = null,
    type: String? = null,
    fileId: String? = null,
) = Title(id = id, title = title, year = year, addedAt = addedAt, category = category, type = type, fileId = fileId)

/** Builds a show Title with the given seasons' episode file IDs (a "extras" pseudo-season is
 *  marked via [Season.extras] on the season carrying the given name). */
private fun show(id: String, vararg seasons: Pair<String, List<String?>>, extrasSeasonName: String? = null) =
    Title(
        id = id,
        title = id,
        type = "show",
        seasons = seasons.map { (name, fileIds) ->
            Season(
                name = name,
                extras = name == extrasSeasonName,
                episodes = fileIds.map { Episode(fileId = it) },
            )
        },
    )

class SortAndGroupTest {

    // ---- sortTitles / comparatorFor ----

    @Test
    fun recentDefault_newestFirst_nullsLast() {
        val input = listOf(
            t("a", addedAt = 100.0),
            t("b", addedAt = 300.0),
            t("c", addedAt = null),
            t("d", addedAt = 200.0),
        )
        val result = sortTitles(input, SortSpec(SortKey.RECENT, ascending = false))
        assertEquals(listOf("b", "d", "a", "c"), result.map { it.id })
    }

    @Test
    fun recentAscending_oldestFirst_nullsStillLast() {
        val input = listOf(
            t("a", addedAt = 100.0),
            t("b", addedAt = 300.0),
            t("c", addedAt = null),
            t("d", addedAt = 200.0),
        )
        val result = sortTitles(input, SortSpec(SortKey.RECENT, ascending = true))
        assertEquals(listOf("a", "d", "b", "c"), result.map { it.id })
    }

    @Test
    fun recentTies_brokenByTitleAscending() {
        val input = listOf(
            t("a", title = "Banana", addedAt = 100.0),
            t("b", title = "apple", addedAt = 100.0),
            t("c", title = "Cherry", addedAt = 100.0),
        )
        val result = sortTitles(input, SortSpec(SortKey.RECENT, ascending = false))
        assertEquals(listOf("apple", "Banana", "Cherry"), result.map { it.displayTitle })
    }

    @Test
    fun titleAscending_caseInsensitive() {
        val input = listOf(
            t("a", title = "banana"),
            t("b", title = "Apple"),
            t("c", title = "cherry"),
            t("d", title = null),
        )
        val result = sortTitles(input, SortSpec(SortKey.TITLE, ascending = true))
        // null title falls back to "Untitled", which sorts after "cherry".
        assertEquals(listOf("Apple", "banana", "cherry", "Untitled"), result.map { it.displayTitle })
    }

    @Test
    fun titleDescending_reverses() {
        val input = listOf(t("a", title = "banana"), t("b", title = "Apple"), t("c", title = "cherry"))
        val asc = sortTitles(input, SortSpec(SortKey.TITLE, ascending = true)).map { it.id }
        val desc = sortTitles(input, SortSpec(SortKey.TITLE, ascending = false)).map { it.id }
        assertEquals(asc.reversed(), desc)
    }

    @Test
    fun yearDescending_nullsLast() {
        val input = listOf(
            t("a", year = 1999),
            t("b", year = 2024),
            t("c", year = null),
            t("d", year = 2020),
        )
        val result = sortTitles(input, SortSpec(SortKey.YEAR, ascending = false))
        assertEquals(listOf("b", "d", "a", "c"), result.map { it.id })
    }

    @Test
    fun yearAscending_nullsLast() {
        val input = listOf(
            t("a", year = 1999),
            t("b", year = 2024),
            t("c", year = null),
            t("d", year = 2020),
        )
        val result = sortTitles(input, SortSpec(SortKey.YEAR, ascending = true))
        assertEquals(listOf("a", "d", "b", "c"), result.map { it.id })
    }

    @Test
    fun yearTies_brokenByTitleAscending() {
        val input = listOf(
            t("a", title = "Banana", year = 2020),
            t("b", title = "apple", year = 2020),
            t("c", title = "Cherry", year = 2020),
        )
        val asc = sortTitles(input, SortSpec(SortKey.YEAR, ascending = true)).map { it.displayTitle }
        val desc = sortTitles(input, SortSpec(SortKey.YEAR, ascending = false)).map { it.displayTitle }
        assertEquals(listOf("apple", "Banana", "Cherry"), asc)
        assertEquals(listOf("apple", "Banana", "Cherry"), desc)
    }

    @Test
    fun sortTitles_doesNotMutateInput() {
        val input = listOf(t("c", addedAt = 1.0), t("a", addedAt = 3.0), t("b", addedAt = 2.0))
        val original = input.toList()
        sortTitles(input, SortSpec(SortKey.RECENT, ascending = false))
        assertEquals(original, input)
    }

    // ---- buildGridRows ----

    @Test
    fun buildGridRows_none_hasNoHeaders() {
        val input = listOf(t("a"), t("b"), t("c"))
        val rows = buildGridRows(input, GroupKey.NONE)
        assertTrue(rows.all { it is GridRow.Tile })
        assertEquals(input.size, rows.size)
        assertEquals(input.map { it.id }, rows.map { (it as GridRow.Tile).title.id })
    }

    @Test
    fun buildGridRows_category_orderIsMoviesShowsOther() {
        val input = listOf(
            t("m1", category = "movie"),
            t("s1", category = "show"),
            t("d1", category = "documentary"),
        )
        val rows = buildGridRows(input, GroupKey.CATEGORY)
        assertEquals(
            listOf("Movies", "m1", "TV Shows", "s1", "Other", "d1"),
            rows.map {
                when (it) {
                    is GridRow.Header -> it.label
                    is GridRow.Tile -> it.title.id
                }
            },
        )
    }

    @Test
    fun buildGridRows_category_omitsEmptyGroups() {
        val input = listOf(t("m1", category = "movie"), t("m2", category = "movie"))
        val rows = buildGridRows(input, GroupKey.CATEGORY)
        val headers = rows.filterIsInstance<GridRow.Header>().map { it.label }
        assertEquals(listOf("Movies"), headers)
    }

    @Test
    fun buildGridRows_category_preservesSortWithinGroups() {
        val input = listOf(
            t("m2", category = "movie", addedAt = 200.0),
            t("m1", category = "movie", addedAt = 300.0),
            t("s2", category = "show", addedAt = 50.0),
            t("s1", category = "show", addedAt = 60.0),
        )
        // Pre-sorted by the caller (mirrors sortTitles(...) output) — groupBy must preserve it.
        val sorted = sortTitles(input, SortSpec(SortKey.RECENT, ascending = false))
        val rows = buildGridRows(sorted, GroupKey.CATEGORY)
        val tileIds = rows.filterIsInstance<GridRow.Tile>().map { it.title.id }
        assertEquals(listOf("m1", "m2", "s1", "s2"), tileIds)
    }

    @Test
    fun buildGridRows_nullOrBlankCategory_fallsBackViaIsShow() {
        val input = listOf(
            t("show1", category = null, type = "show"),
            t("movie1", category = null, type = "movie"),
            t("unknown1", category = "documentary"),
        )
        val rows = buildGridRows(input, GroupKey.CATEGORY)
        val headers = rows.filterIsInstance<GridRow.Header>().map { it.label }
        assertEquals(listOf("Movies", "TV Shows", "Other"), headers)
        val byId = rows.filterIsInstance<GridRow.Tile>().associate { it.title.id to categoryOf(it.title) }
        assertEquals("movie", byId["movie1"])
        assertEquals("show", byId["show1"])
        assertEquals("documentary", byId["unknown1"])
    }

    // ---- nextSortSpec / fromId ----

    @Test
    fun nextSortSpec_samePick_togglesDirection() {
        val current = SortSpec(SortKey.RECENT, ascending = false)
        val next = nextSortSpec(current, SortKey.RECENT)
        assertEquals(SortKey.RECENT, next.key)
        assertEquals(true, next.ascending)
        val again = nextSortSpec(next, SortKey.RECENT)
        assertEquals(false, again.ascending)
    }

    @Test
    fun nextSortSpec_differentPick_usesDefaultDirection() {
        val current = SortSpec(SortKey.RECENT, ascending = false)
        assertEquals(SortSpec(SortKey.TITLE, ascending = true), nextSortSpec(current, SortKey.TITLE))
        assertEquals(SortSpec(SortKey.YEAR, ascending = false), nextSortSpec(current, SortKey.YEAR))
        assertEquals(SortSpec(SortKey.RECENT, ascending = false), nextSortSpec(SortSpec(SortKey.TITLE, true), SortKey.RECENT))
    }

    @Test
    fun sortKeyFromId_unknownFallsBackToRecent_andGroupKeyFromId_unknownFallsBackToNone() {
        assertEquals(SortKey.RECENT, SortKey.fromId(null))
        assertEquals(SortKey.RECENT, SortKey.fromId("bogus"))
        assertEquals(SortKey.TITLE, SortKey.fromId("title"))
        assertEquals(GroupKey.NONE, GroupKey.fromId(null))
        assertEquals(GroupKey.NONE, GroupKey.fromId("bogus"))
        assertEquals(GroupKey.CATEGORY, GroupKey.fromId("category"))
    }

    // ---- effectiveGroupFor ----

    @Test
    fun effectiveGroupFor_suppressesWhileACategoryChipIsActive() {
        assertEquals(GroupKey.CATEGORY, effectiveGroupFor(isEntertainment = true, selectedCat = null, group = GroupKey.CATEGORY))
        assertEquals(GroupKey.NONE, effectiveGroupFor(isEntertainment = true, selectedCat = "movie", group = GroupKey.CATEGORY))
        assertEquals(GroupKey.NONE, effectiveGroupFor(isEntertainment = true, selectedCat = "other", group = GroupKey.CATEGORY))
    }

    @Test
    fun effectiveGroupFor_nonEntertainmentTabsNeverGroup() {
        assertEquals(GroupKey.NONE, effectiveGroupFor(isEntertainment = false, selectedCat = null, group = GroupKey.CATEGORY))
    }

    // ---- labels ----

    @Test
    fun sortPillLabel_and_sortOptionLabel_matrix() {
        assertEquals("Sort: Recent", sortPillLabel(SortSpec(SortKey.RECENT, ascending = false)))
        assertEquals("Sort: Oldest", sortPillLabel(SortSpec(SortKey.RECENT, ascending = true)))
        assertEquals("Sort: A–Z", sortPillLabel(SortSpec(SortKey.TITLE, ascending = true)))
        assertEquals("Sort: Z–A", sortPillLabel(SortSpec(SortKey.TITLE, ascending = false)))
        assertEquals("Sort: Year ↓", sortPillLabel(SortSpec(SortKey.YEAR, ascending = false)))
        assertEquals("Sort: Year ↑", sortPillLabel(SortSpec(SortKey.YEAR, ascending = true)))

        val recentDesc = SortSpec(SortKey.RECENT, ascending = false)
        assertEquals("Recently added · Newest first", sortOptionLabel(SortKey.RECENT, recentDesc))
        assertEquals("Title", sortOptionLabel(SortKey.TITLE, recentDesc))
        assertEquals("Year", sortOptionLabel(SortKey.YEAR, recentDesc))

        val recentAsc = SortSpec(SortKey.RECENT, ascending = true)
        assertEquals("Recently added · Oldest first", sortOptionLabel(SortKey.RECENT, recentAsc))

        val titleAsc = SortSpec(SortKey.TITLE, ascending = true)
        assertEquals("Title · A–Z", sortOptionLabel(SortKey.TITLE, titleAsc))
        val titleDesc = SortSpec(SortKey.TITLE, ascending = false)
        assertEquals("Title · Z–A", sortOptionLabel(SortKey.TITLE, titleDesc))

        val yearDesc = SortSpec(SortKey.YEAR, ascending = false)
        assertEquals("Year · Newest first", sortOptionLabel(SortKey.YEAR, yearDesc))
        val yearAsc = SortSpec(SortKey.YEAR, ascending = true)
        assertEquals("Year · Oldest first", sortOptionLabel(SortKey.YEAR, yearAsc))

        assertEquals("Sort: Watched ↓", sortPillLabel(SortSpec(SortKey.WATCHED, ascending = false)))
        assertEquals("Sort: Watched ↑", sortPillLabel(SortSpec(SortKey.WATCHED, ascending = true)))
        val watchedDesc = SortSpec(SortKey.WATCHED, ascending = false)
        assertEquals("Recently watched · Newest first", sortOptionLabel(SortKey.WATCHED, watchedDesc))
        val watchedAsc = SortSpec(SortKey.WATCHED, ascending = true)
        assertEquals("Recently watched · Oldest first", sortOptionLabel(SortKey.WATCHED, watchedAsc))
        assertEquals("Recently watched", sortOptionLabel(SortKey.WATCHED, recentDesc))

        assertEquals("Group: None", groupPillLabel(GroupKey.NONE))
        assertEquals("Group: Type", groupPillLabel(GroupKey.TYPE))
        assertEquals("Group: Category", groupPillLabel(GroupKey.CATEGORY))
        assertEquals("None", groupOptionLabel(GroupKey.NONE))
        assertEquals("Type", groupOptionLabel(GroupKey.TYPE))
        assertEquals("Category", groupOptionLabel(GroupKey.CATEGORY))
    }

    // ---- watched-key comparator (map passed in, never a singleton) ----

    @Test
    fun watchedDescending_newestFirst_neverWatchedLast() {
        val watchedMap = mapOf("f-a" to 100.0, "f-b" to 300.0, "f-d" to 200.0)
        val input = listOf(
            t("a", fileId = "f-a"),
            t("b", fileId = "f-b"),
            t("c", fileId = "f-c"),   // no entry in the map -> never watched
            t("d", fileId = "f-d"),
        )
        val result = sortTitles(input, SortSpec(SortKey.WATCHED, ascending = false), watchedMap)
        assertEquals(listOf("b", "d", "a", "c"), result.map { it.id })
    }

    @Test
    fun watchedAscending_oldestFirst_neverWatchedStillLast() {
        val watchedMap = mapOf("f-a" to 100.0, "f-b" to 300.0, "f-d" to 200.0)
        val input = listOf(
            t("a", fileId = "f-a"),
            t("b", fileId = "f-b"),
            t("c", fileId = "f-c"),
            t("d", fileId = "f-d"),
        )
        val result = sortTitles(input, SortSpec(SortKey.WATCHED, ascending = true), watchedMap)
        assertEquals(listOf("a", "d", "b", "c"), result.map { it.id })
    }

    @Test
    fun watchedTies_brokenByTitleAscending() {
        val watchedMap = mapOf("f-a" to 100.0, "f-b" to 100.0, "f-c" to 100.0)
        val input = listOf(
            t("a", title = "Banana", fileId = "f-a"),
            t("b", title = "apple", fileId = "f-b"),
            t("c", title = "Cherry", fileId = "f-c"),
        )
        val result = sortTitles(input, SortSpec(SortKey.WATCHED, ascending = false), watchedMap)
        assertEquals(listOf("apple", "Banana", "Cherry"), result.map { it.displayTitle })
    }

    @Test
    fun watched_showAggregatesMaxAcrossAllSeasonsEpisodes() {
        val watchedMap = mapOf("s1e1" to 100.0, "s1e2" to 500.0, "extra1" to 900.0)
        val title = show(
            "sh1",
            "Season 1" to listOf("s1e1", "s1e2"),
            "Featurettes" to listOf("extra1"),
            extrasSeasonName = "Featurettes",
        )
        // The max across every season INCLUDING the extras pseudo-season is 900.0.
        assertEquals(900.0, lastPlayedOf(title, watchedMap))
    }

    @Test
    fun watched_movieUsesFileIdOnly_movieExtrasIgnored() {
        val watchedMap = mapOf("m1" to 50.0, "extra-of-m1" to 999.0)
        // A movie's `extras` list uses the Season shape too, but must NOT feed lastPlayedOf.
        val movie = Title(
            id = "m1",
            title = "m1",
            fileId = "m1",
            extras = listOf(Season(name = "Extras", extras = true, episodes = listOf(Episode(fileId = "extra-of-m1")))),
        )
        assertEquals(50.0, lastPlayedOf(movie, watchedMap))
    }

    @Test
    fun watched_emptyMap_allNull_fallsBackToTitleOrder() {
        val input = listOf(t("a", title = "Banana", fileId = "f-a"), t("b", title = "apple", fileId = "f-b"))
        val result = sortTitles(input, SortSpec(SortKey.WATCHED, ascending = false), emptyMap())
        assertEquals(listOf("apple", "Banana"), result.map { it.displayTitle })
    }

    // ---- buildGridRows(..., GroupKey.TYPE) ----

    @Test
    fun buildGridRows_type_orderIsMoviesThenShows_emptyOmitted() {
        val input = listOf(t("m1", type = "movie"), t("s1", type = "show"))
        val rows = buildGridRows(input, GroupKey.TYPE)
        assertEquals(
            listOf("Movies", "m1", "TV Shows", "s1"),
            rows.map { when (it) { is GridRow.Header -> it.label; is GridRow.Tile -> it.title.id } },
        )

        val moviesOnly = buildGridRows(listOf(t("m1", type = "movie"), t("m2", type = "movie")), GroupKey.TYPE)
        assertEquals(listOf("Movies"), moviesOnly.filterIsInstance<GridRow.Header>().map { it.label })
    }

    @Test
    fun buildGridRows_type_nullType_bucketsViaIsShow_nothingDropped() {
        val input = listOf(
            t("show1", type = null).copy(seasons = listOf(Season(season = 1, episodes = emptyList()))),
            t("movie1", type = null),
            t("documentary1", type = "documentary"),
        )
        val rows = buildGridRows(input, GroupKey.TYPE)
        val tiles = rows.filterIsInstance<GridRow.Tile>().map { it.title.id }
        assertEquals(input.size, tiles.size)   // count in == count out, nothing dropped
        val headers = rows.filterIsInstance<GridRow.Header>().map { it.label }
        assertEquals(listOf("Movies", "TV Shows"), headers)
    }

    @Test
    fun sortKeyFromId_watched_andGroupKeyFromId_type() {
        assertEquals(SortKey.WATCHED, SortKey.fromId("watched"))
        assertEquals(GroupKey.TYPE, GroupKey.fromId("type"))
    }
}
