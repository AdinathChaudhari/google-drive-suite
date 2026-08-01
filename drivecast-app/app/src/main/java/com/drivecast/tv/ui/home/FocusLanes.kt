package com.drivecast.tv.ui.home

// Pure decision logic behind the home grid's deterministic D-pad hops (continue shelf <->
// controls row <-> first tile row). See ui/common/FocusKit.kt's tvDpadHop for what wires these
// decisions to real FocusRequesters, and HomeScreen.kt's usage for how the live state (rows,
// columns, the focused tile's index) is derived from the composition. Kept out of the composable
// so FocusLanesTest can cover the decision table without a Compose harness — same rationale as
// SortAndGroup.kt.

/**
 * The flat-list [GridRow] indices that make up the grid's very FIRST visual tile row — i.e. what
 * index 0 would mean for a plain non-adaptive, headerless grid, made header-aware: skips any
 * leading [GridRow.Header] (grouping's header always precedes its tiles) and stops at whichever
 * comes first of [columns] tiles or the next [GridRow.Header] (a group smaller than [columns]
 * still forms one complete visual row on its own; its next sibling is a header, not a
 * continuation of the same row). Independent of scroll position — unlike the grid's own
 * visible-items report (see [firstTileRowIndexOf]), this always names the row a fresh mount or a
 * scroll-to-top would show. Empty when [rows] has no [GridRow.Tile] at all.
 */
fun firstTileRowRange(rows: List<GridRow>, columns: Int): IntRange {
    val start = rows.indexOfFirst { it is GridRow.Tile }
    if (start < 0) return IntRange.EMPTY
    var end = start
    while (end < rows.size && end < start + columns && rows[end] is GridRow.Tile) end++
    return start until end
}

/**
 * Lifted from the `derivedStateOf` scan HomeScreen.kt uses to keep the reusable `firstTile`
 * FocusRequester pinned to a currently-*visible* tile (so a restore probe always lands on a live,
 * composed target regardless of scroll depth) — NOT the same question as [firstTileRowRange]
 * above, which is scroll-independent. [visibleRowIndices] is the grid's own `visibleItemsInfo`,
 * already offset-adjusted from grid-global index to an index into [rows] (i.e.
 * `info.index - headerSlots`, the continue-shelf/controls-row slots stripped out) and in
 * on-screen order. Falls back to the first [GridRow.Tile] overall when the visible window is
 * headers-only (true for at most a single frame — a header is always immediately followed by its
 * tiles). Result is always a valid index into [rows] (or 0 for an empty/all-header list).
 */
fun firstTileRowIndexOf(visibleRowIndices: List<Int>, rows: List<GridRow>): Int {
    val visibleTileRow = visibleRowIndices.firstOrNull { rows.getOrNull(it) is GridRow.Tile }
    val rowIdx = visibleTileRow ?: rows.indexOfFirst { it is GridRow.Tile }
    return rowIdx.coerceIn(0, (rows.size - 1).coerceAtLeast(0))
}

/** What a deterministic UP hop resolves to — see [Modifier.tvDpadHop][com.drivecast.tv.ui.common.tvDpadHop]. */
sealed interface UpHop {
    /** UP from the grid's first tile row should land on the controls row (chips/Sort/Group). */
    data object ToControls : UpHop

    /** UP from the controls row should land on the continue shelf's first card. */
    data object ToShelf : UpHop
}

/**
 * Hop 3's decision (first tile row -> controls row): [focusedRow] is the currently-focused tile's
 * index into `rows` (kept live by its `onFocused` hook), or -1 while focus sits on a lane rather
 * than a tile (that case is owned by the lane's own hop instead — see [controlsUpHopTarget]).
 * Resolves [UpHop.ToControls] only when [focusedRow] actually sits in [firstTileRow]; an ordinary
 * UP within a lower row falls through to Compose's normal focus search (returns null). The
 * controls row exists on every tab (the Sort pill is unconditional) whether or not a continue
 * shelf sits above it, so [hasShelf] never changes this hop's outcome — kept for signature
 * symmetry with [controlsUpHopTarget], not because it's read here.
 */
fun upHopTarget(focusedRow: Int, firstTileRow: IntRange, hasShelf: Boolean): UpHop? {
    if (focusedRow == -1 || focusedRow !in firstTileRow) return null
    return UpHop.ToControls
}

/**
 * Hop 2's decision (controls row -> continue shelf): UP from the controls row lands on the
 * shelf's first card when the tab actually has one; with no shelf there is nothing above the
 * controls row but the tab bar, so this returns null and the press falls through to the normal
 * search — the tab-0 (no-shelf) parity already verified correct on-device.
 */
fun controlsUpHopTarget(hasShelf: Boolean): UpHop? = if (hasShelf) UpHop.ToShelf else null
