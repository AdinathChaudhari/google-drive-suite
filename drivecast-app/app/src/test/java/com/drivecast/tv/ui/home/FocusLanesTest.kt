package com.drivecast.tv.ui.home

import com.drivecast.tv.api.Title
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

private fun tile(id: String) = GridRow.Tile(Title(id = id, title = id, year = null, addedAt = null, category = null, type = null))
private fun header(label: String) = GridRow.Header(label)

class FocusLanesTest {

    // ---- firstTileRowRange ----

    @Test
    fun firstTileRowRange_noHeaders_isZeroUntilColumns() {
        val rows = listOf(tile("a"), tile("b"), tile("c"), tile("d"), tile("e"))
        assertEquals(0 until 3, firstTileRowRange(rows, columns = 3))
    }

    @Test
    fun firstTileRowRange_groupingHeaderFirst_startsAfterHeader() {
        val rows = listOf(header("Movies"), tile("a"), tile("b"), tile("c"), tile("d"))
        assertEquals(1 until 4, firstTileRowRange(rows, columns = 3))
    }

    @Test
    fun firstTileRowRange_firstGroupSmallerThanColumns_cappedAtNextHeader() {
        val rows = listOf(
            header("Movies"), tile("m1"), tile("m2"),
            header("TV Shows"), tile("s1"), tile("s2"), tile("s3"),
        )
        // Only 2 tiles precede the next header, even though columns allows up to 4.
        assertEquals(1 until 3, firstTileRowRange(rows, columns = 4))
    }

    @Test
    fun firstTileRowRange_emptyRows_isEmpty() {
        assertEquals(IntRange.EMPTY, firstTileRowRange(emptyList(), columns = 4))
    }

    @Test
    fun firstTileRowRange_allHeaders_isEmpty() {
        assertEquals(IntRange.EMPTY, firstTileRowRange(listOf(header("Movies")), columns = 4))
    }

    // ---- firstTileRowIndexOf ----

    @Test
    fun firstTileRowIndexOf_headersOnlyVisible_fallsBackToFirstTileOverall() {
        val rows = listOf(header("Movies"), tile("a"), tile("b"))
        // Only the header's own index (0) is reported visible.
        assertEquals(1, firstTileRowIndexOf(visibleRowIndices = listOf(0), rows = rows))
    }

    @Test
    fun firstTileRowIndexOf_scrolledWindow_returnsFirstVisibleTileRow() {
        val rows = (0 until 10).map { tile("t$it") }
        // Scrolled well past row 0 — the visible window starts at row 4.
        assertEquals(4, firstTileRowIndexOf(visibleRowIndices = listOf(4, 5, 6), rows = rows))
    }

    @Test
    fun firstTileRowIndexOf_resultAlwaysWithinBounds() {
        // No tiles at all: indexOfFirst returns -1; must coerce up to 0, not stay negative.
        assertEquals(0, firstTileRowIndexOf(visibleRowIndices = emptyList(), rows = emptyList()))
        // A visible index that doesn't resolve to a Tile falls back, and the fallback is still
        // coerced into range.
        val rows = listOf(header("Movies"))
        assertEquals(0, firstTileRowIndexOf(visibleRowIndices = listOf(0), rows = rows))
    }

    // ---- upHopTarget (hop 3: first tile row -> controls) ----

    @Test
    fun upHopTarget_focusedRowInFirstTileRow_withShelf_resolvesToControls() {
        assertEquals(UpHop.ToControls, upHopTarget(focusedRow = 1, firstTileRow = 0..2, hasShelf = true))
    }

    @Test
    fun upHopTarget_focusedRowInFirstTileRow_noShelf_resolvesToControls() {
        assertEquals(UpHop.ToControls, upHopTarget(focusedRow = 1, firstTileRow = 0..2, hasShelf = false))
    }

    @Test
    fun upHopTarget_focusedRowBeyondFirstRow_isNull() {
        assertEquals(null, upHopTarget(focusedRow = 5, firstTileRow = 0..2, hasShelf = true))
    }

    @Test
    fun upHopTarget_focusedRowIsMinusOne_isNull() {
        // Focus sits on a lane, not a tile — this hop declines regardless of range/shelf.
        assertEquals(null, upHopTarget(focusedRow = -1, firstTileRow = 0..2, hasShelf = true))
        assertEquals(null, upHopTarget(focusedRow = -1, firstTileRow = 0..2, hasShelf = false))
    }

    // ---- controlsUpHopTarget (hop 2: controls row -> continue shelf) ----

    @Test
    fun controlsUpHopTarget_shelfPresent_resolvesToShelf() {
        assertEquals(UpHop.ToShelf, controlsUpHopTarget(hasShelf = true))
    }

    @Test
    fun controlsUpHopTarget_noShelf_isNull() {
        assertEquals(null, controlsUpHopTarget(hasShelf = false))
    }
}
