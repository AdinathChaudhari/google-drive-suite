package com.drivecast.tv.ui.home

import android.view.KeyEvent
import androidx.activity.compose.ReportDrawnWhen
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.FastOutLinearInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.LocalViewModelStoreOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavBackStackEntry
import coil.compose.AsyncImage
import coil.request.ImageRequest
import com.drivecast.tv.LocalAppContainer
import com.drivecast.tv.api.ContinueItem
import com.drivecast.tv.api.SectionInfo
import com.drivecast.tv.api.Title
import com.drivecast.tv.ui.common.BlurTransformation
import com.drivecast.tv.ui.common.PosterCard
import com.drivecast.tv.ui.common.SkeletonBox
import com.drivecast.tv.ui.theme.Accent
import com.drivecast.tv.ui.theme.Background
import com.drivecast.tv.ui.theme.ErrorRed
import com.drivecast.tv.ui.theme.MotionTokens
import com.drivecast.tv.ui.theme.OnAccent
import com.drivecast.tv.ui.theme.Outline
import com.drivecast.tv.ui.theme.Scrim
import com.drivecast.tv.ui.theme.Surface as SurfaceColor
import com.drivecast.tv.ui.theme.SurfaceVariant
import com.drivecast.tv.ui.theme.TextPrimary
import com.drivecast.tv.ui.theme.TextSecondary
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import com.drivecast.tv.ui.common.PositionFocusedItemInLazyLayout
import com.drivecast.tv.ui.common.tvFocusEnterFallback
import com.drivecast.tv.ui.common.tvFocusRestorer
import androidx.tv.material3.Border
import androidx.tv.material3.Button
import androidx.tv.material3.ButtonDefaults
import androidx.tv.material3.IconButton
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.SurfaceDefaults
import androidx.tv.material3.Text
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest

private const val ENTERTAINMENT = "entertainment"

@Composable
fun HomeScreen(
    onOpenTitle: (titleId: String) -> Unit,
    onPlay: (titleId: String, fileId: String) -> Unit,
    onOpenSettings: () -> Unit,
) {
    val container = LocalAppContainer.current
    val vm: HomeViewModel = viewModel(factory = HomeViewModel.factory(container))
    val state by vm.state.collectAsStateWithLifecycle()
    val pendingDismiss = remember { mutableStateOf<ContinueItem?>(null) }

    // Refresh-on-return from Settings: Navigation-Compose scopes each destination's
    // ViewModelStore (and SavedStateHandle) to its own NavBackStackEntry, and that entry *is*
    // exactly what LocalViewModelStoreOwner resolves to for a composable() destination — so this
    // reaches Home's own NavBackStackEntry.savedStateHandle without HomeScreen needing a
    // NavController reference or MainActivity threading one down as an extra parameter. Settings
    // is pushed on top of Home (not popUpTo-replacing it), so HomeViewModel is NOT recreated
    // across that round trip — its init{}-time load() never re-runs — which is exactly why a
    // rename/reorder saved in Settings would otherwise never reach Home's already-live state.
    // DrivecastNav's "settings" route stashes savedStateHandle["tabsChanged"] = true on *this*
    // entry right before popping back; getStateFlow (not a one-shot read) means this LaunchedEffect
    // still sees the flip even if it lands before this composable has had a chance to recompose.
    val homeBackStackEntry = LocalViewModelStoreOwner.current as? NavBackStackEntry
    val tabsChanged by homeBackStackEntry
        ?.savedStateHandle
        ?.getStateFlow("tabsChanged", false)
        ?.collectAsStateWithLifecycle()
        ?: remember { mutableStateOf(false) }
    LaunchedEffect(tabsChanged) {
        if (tabsChanged) {
            vm.refresh()
            // Clear immediately (not after refresh completes) so a rapid second Settings visit
            // re-arms the flag rather than getting swallowed by an in-flight refresh's eventual
            // "already consumed" state.
            homeBackStackEntry?.savedStateHandle?.set("tabsChanged", false)
        }
    }

    // TTFD: drawn once real content is on screen, not on the skeleton/cache-seeded first frame.
    ReportDrawnWhen { state.titles?.isNotEmpty() == true }

    Surface(
        modifier = Modifier.fillMaxSize(),
        colors = SurfaceDefaults.colors(containerColor = Background),
    ) {
        Box(Modifier.fillMaxSize()) {
            when {
                state.titles == null && state.error != null ->
                    CenterMessage(state.error!!) { vm.refresh() }
                else -> Crossfade(
                    targetState = state.titles == null,
                    animationSpec = tween(300),
                    label = "homeLoading",
                ) { loading ->
                    if (loading) {
                        HomeSkeleton()
                    } else {
                        HomeContent(
                            continueItems = state.continueItems,
                            titles = state.titles.orEmpty(),
                            sectionInfos = state.sections,
                            refreshing = state.refreshing,
                            onOpenTitle = onOpenTitle,
                            onPlayContinue = { item ->
                                val titleId = item.titleId
                                if (titleId != null) onPlay(titleId, item.fileId)
                            },
                            onDismissRequest = { pendingDismiss.value = it },
                            onRefresh = { vm.refresh() },
                            onOpenSettings = onOpenSettings,
                            posterUrl = { key -> container.repository.posterUrl(key) },
                        )
                    }
                }
            }

            // A MutableState reference is stable across recompositions; only DismissDialogHost
            // itself reads `.value`, so opening/closing the dialog never recomposes HomeScreen
            // (and therefore never recomposes HomeContent/the grid).
            DismissDialogHost(
                pendingDismiss = pendingDismiss,
                onConfirm = { item ->
                    pendingDismiss.value = null
                    vm.dismissContinueItem(item.fileId)
                },
                onCancel = { pendingDismiss.value = null },
            )
        }
    }
}

@OptIn(ExperimentalComposeUiApi::class)
@Composable
private fun HomeContent(
    continueItems: List<ContinueItem>,
    titles: List<Title>,
    sectionInfos: List<SectionInfo>,
    refreshing: Boolean,
    onOpenTitle: (String) -> Unit,
    onPlayContinue: (ContinueItem) -> Unit,
    onDismissRequest: (ContinueItem) -> Unit,
    onRefresh: () -> Unit,
    onOpenSettings: () -> Unit,
    posterUrl: (String?) -> String?,
) {
    // ONE stable (String) -> Unit reference handed to every grid item lambda below, instead of
    // each item closing over `onOpenTitle` + its own `title` fresh every recomposition.
    val currentOnOpenTitle by rememberUpdatedState(onOpenTitle)
    val onTitleClick = remember { { id: String -> currentOnOpenTitle(id) } }

    val bySection = remember(titles) { titles.groupBy { sectionKeyOf(it) } }
    val tabs = remember(titles, sectionInfos) { buildTabs(bySection, sectionInfos) }

    // AnimatedContent has no SaveableStateHolder of its own, so a rememberSaveable inside its
    // content lambda is simply discarded when that content leaves composition on a tab switch.
    // This hoisted holder performs save-on-dispose / restore-on-re-entry per tab index, giving
    // each tab its own persisted grid scroll+focus position instead of resetting to the top.
    val tabStateHolder = rememberSaveableStateHolder()

    // rememberSaveable: back-navigation from detail must return to whichever tab the user was
    // browsing, not always tab 0.
    var selectedTab by rememberSaveable { mutableIntStateOf(0) }
    val tabIndex = selectedTab.coerceIn(0, (tabs.size - 1).coerceAtLeast(0))

    // Category filter (entertainment tab only). null == "All". Resets whenever the tab changes.
    var selectedCat by remember(tabIndex) { mutableStateOf<String?>(null) }

    // Initial focus: the first Continue Watching card if the opening tab has one, else the
    // first grid tile. HomeContent only enters composition once real data has landed (the
    // Crossfade above gates it), so a Unit-keyed effect already fires after the real tree
    // exists and never on the skeleton. Gated by a rememberSaveable flag: NavHost disposes and
    // recomposes the home destination on every back-nav from detail (it renders instantly from
    // cache), and without the gate this effect would re-fire on every re-entry, snapping focus
    // back to the first card and defeating tvFocusRestorer + the saved grid state's job of
    // returning the user to the exact card they left. rememberSaveable survives that dispose via
    // NavHost's SaveableStateProvider, so the snap happens exactly once per back-stack entry.
    // Pills row is static chrome — always exactly one instance on screen, even mid tab-crossfade —
    // so its requester is safe to hoist here. The per-tab lane requesters (continueLane,
    // continueFirst, chipsFirst, firstTile) are NOT hoisted: AnimatedContent composes both the
    // outgoing and incoming tab for the ~300ms crossfade, so a requester shared across tabs would
    // get attached to TWO nodes at once, and a restore/fallback could land focus on the outgoing
    // tab's node just before it's disposed — focus would silently die or jump. They're declared
    // per-tab instead, inside the AnimatedContent content lambda below.
    val pillsFirst = remember { FocusRequester() }
    var focusedOnce by rememberSaveable { mutableStateOf(false) }

    // Ambient backdrop identity. Written only by PosterCard's onFocused hook below — never read
    // by LibraryTile/ContinueCard or threaded through their parameters — so cards themselves never
    // recompose when focus moves; only HomeBackdrop (which reads backdropItem) does. A 400ms dwell
    // sits between "focused" and "shown": rapid D-pad scrubbing across a row re-fires the effect
    // before it fires, so the crossfade never thrashes on every keypress. The focus indicator
    // itself (scale/border) is never debounced — only this side effect is.
    var focusedItem by remember { mutableStateOf<BackdropItem?>(null) }
    val onCardFocused = remember { { item: BackdropItem -> focusedItem = item } }
    var backdropItem by remember { mutableStateOf<BackdropItem?>(null) }
    // snapshotFlow + collectLatest instead of a LaunchedEffect(focusedItem) key: a keyed effect
    // reads focusedItem at HomeContent's OWN composition scope, so every onCardFocused write
    // (every D-pad focus move) re-ran this whole composable's body. This reproduces the same
    // 400ms dwell debounce (a new focus cancels the pending delay) with zero composition-scope
    // reads here — HomeBackdrop reads backdropItem itself, in its own scope, below.
    LaunchedEffect(Unit) {
        snapshotFlow { focusedItem }.collectLatest {
            delay(400)
            backdropItem = it
        }
    }

    Box(Modifier.fillMaxSize()) {
        HomeBackdrop(item = { backdropItem }, posterUrl = posterUrl)

        // Secondary/defense-in-depth for the onRestoreFailed fallback-recycling bug (see
        // tvFocusRestorer's fallback chain above and FocusKit.kt's onRestoreFailed handling for
        // the actual fixes): a 0.10f pivot pins the focused row almost flush with the top of the
        // viewport, so descending just one or two grid rows already scrolls the Continue shelf +
        // chips header fully out of the composed/cached window, recycling continueLane/
        // continueFirst/chipsFirst — exactly the targets a restore fallback may need. A gentler
        // 0.30f pivot leaves more headroom above the focused row, so the same downward scroll
        // covers less distance per row and the header rows stay composed for longer before they
        // scroll off, without going so far (e.g. a centered ~0.5f) that it fights the natural
        // reading position of a focused tile.
        PositionFocusedItemInLazyLayout(0.30f) {
            Column(Modifier.fillMaxSize()) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(start = 48.dp, end = 48.dp, top = 28.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text("drivecast", style = MaterialTheme.typography.headlineMedium, color = Accent)
                    if (refreshing) RefreshIndicator()
                }
                Row(
                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    // Demoted from a prime top-right "Refresh" button to a small icon so the
                    // wordmark + tab titles own the header's visual hierarchy.
                    IconButton(onClick = onRefresh) {
                        Text("⟳", style = MaterialTheme.typography.titleLarge, color = TextSecondary)
                    }
                    IconButton(onClick = onOpenSettings) {
                        Text("⚙", style = MaterialTheme.typography.titleLarge, color = TextSecondary)
                    }
                }
            }

            if (tabs.size > 1) {
                Spacer(Modifier.height(16.dp))
                LazyRow(
                    modifier = Modifier.fillMaxWidth()
                        .tvFocusEnterFallback { pillsFirst },
                    contentPadding = PaddingValues(horizontal = 48.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    itemsIndexed(
                        tabs,
                        key = { _, tab -> tab.key },
                        contentType = { _, _ -> "tabPill" },
                    ) { index, tab ->
                        PillButton(
                            selected = index == tabIndex,
                            label = tabLabel(tab),
                            onClick = { selectedTab = index },
                            modifier = if (index == tabIndex) Modifier.focusRequester(pillsFirst) else Modifier,
                        )
                    }
                }
            }

            Spacer(Modifier.height(16.dp))

            if (tabs.isEmpty()) {
                NoTabsMessage(Modifier.weight(1f))
            } else {
                // The canonical Material fade-through between tabs: the grid state is already
                // keyed per tab (below), so a tab switch glides instead of hard-cutting.
                AnimatedContent(
                    targetState = tabIndex,
                    transitionSpec = {
                        (fadeIn(tween(210, easing = LinearOutSlowInEasing)) +
                            scaleIn(initialScale = 0.98f, animationSpec = tween(210))) togetherWith
                            fadeOut(tween(90, easing = FastOutLinearInEasing))
                    },
                    modifier = Modifier.fillMaxSize().weight(1f),
                    label = "tabContent",
                ) { idx ->
                    // AnimatedContent has no SaveableStateHolder of its own: when a tab's content
                    // leaves composition on a tab switch, a plain rememberSaveable inside it is
                    // simply discarded rather than cached-and-restored, so every switch back would
                    // reset that tab's scroll to the top. Wrapping each child in this hoisted
                    // holder's SaveableStateProvider(idx) performs save-on-dispose / restore-on-
                    // re-entry, giving each tab its own persisted scroll+focus position.
                    tabStateHolder.SaveableStateProvider(idx) {
                    val section = tabs.getOrNull(idx)
                    val sectionKey = section?.key ?: ENTERTAINMENT
                    // Branch on the server-declared behavior, not the key; fall back to the key
                    // for legacy servers that predate the behaviors refactor.
                    val isEntertainment =
                        section?.behavior?.let { it == ENTERTAINMENT } ?: (sectionKey == ENTERTAINMENT)

                    val sectionTitles = remember(bySection, sectionKey) {
                        (bySection[sectionKey] ?: emptyList()).sortedWith(
                            compareByDescending<Title> { it.addedAt ?: Double.NEGATIVE_INFINITY }
                                .thenBy { it.displayTitle.lowercase() }
                        )
                    }
                    val chips = remember(sectionTitles, isEntertainment) {
                        if (isEntertainment) visibleChips(sectionTitles) else emptyList()
                    }
                    val gridTitles = remember(sectionTitles, selectedCat, isEntertainment) {
                        if (isEntertainment) sectionTitles.filter { matchesCategory(it, selectedCat) } else sectionTitles
                    }
                    val sectionContinue = remember(continueItems, sectionKey) {
                        continueItems.filter { sectionKeyOf(it) == sectionKey }
                    }

                    // Focus-lane requesters, declared here (per-tab) rather than hoisted at
                    // HomeContent scope: AnimatedContent keeps the outgoing tab composed alongside
                    // the incoming one for the ~300ms crossfade, so a requester shared across tabs
                    // would be attached to TWO nodes simultaneously, and a restore/fallback could
                    // land focus on the outgoing tab's node moments before it's disposed — focus
                    // would silently die or jump. One instance per tab (scoped by the enclosing
                    // SaveableStateProvider(idx)) means each tab's lanes only ever reference that
                    // tab's own, currently-composed nodes. Each lane's tvFocusRestorer still gets a
                    // fallback so a failed restore can never swallow the key press: worst case,
                    // focus lands on the lane's first item.
                    val continueLane = remember { FocusRequester() }
                    val continueFirst = remember { FocusRequester() }
                    val chipsFirst = remember { FocusRequester() }
                    val firstTile = remember { FocusRequester() }

                    // One scroll+focus position per tab (fixes scroll carryover across tabs);
                    // rememberSaveable survives process death and, combined with tvFocusRestorer
                    // below, restores both scroll offset and the focused card on back-navigation.
                    // Scoped by the enclosing SaveableStateProvider(idx) above, so no explicit idx
                    // key is needed here.
                    val gridState = rememberSaveable(saver = LazyGridState.Saver) { LazyGridState() }

                    // The continue shelf and chips row each occupy one LazyGridScope item() slot
                    // ahead of itemsIndexed(gridTitles), so a tile's *global* grid index is offset
                    // by however many of those header slots are actually present.
                    val headerSlots = (if (sectionContinue.isNotEmpty()) 1 else 0) +
                        (if (chips.size > 1) 1 else 0)

                    // `firstTile` used to be pinned to the grid's literal first tile (local index
                    // 0) — but that's exactly as scrollable-away as continueLane/continueFirst:
                    // once the pivot (PositionFocusedItemInLazyLayout above; gentled to 0.30f as a
                    // secondary defense, see its comment) has scrolled the header out of view,
                    // index 0 can be just as recycled/off-screen as the continue shelf is.
                    // Deriving the target tile from the LazyGridState's own visible-items
                    // report instead means `firstTile` always names whichever tile the layout itself
                    // currently reports on-screen — something the restorer's requestFocus() probe
                    // can actually land on — rather than a fixed index that may have scrolled off.
                    // derivedStateOf collapses per-frame scroll-offset churn down to a value that
                    // only changes when the reported item index actually changes, so this doesn't
                    // recompose the visible tiles on every scroll pixel.
                    // Keyed on gridTitles too (not just headerSlots): a category-chip switch
                    // rebuilds gridTitles without necessarily changing headerSlots, and remember()
                    // would otherwise keep the old derivedStateOf around, closing over the stale
                    // list's size for the coerceIn bound below.
                    val firstVisibleTileIndex by remember(headerSlots, gridTitles) {
                        derivedStateOf {
                            val globalIdx = gridState.layoutInfo.visibleItemsInfo.firstOrNull()?.index ?: 0
                            (globalIdx - headerSlots).coerceIn(0, (gridTitles.size - 1).coerceAtLeast(0))
                        }
                    }

                    LaunchedEffect(Unit) {
                        if (idx == 0 && !focusedOnce) {
                            runCatching { (if (sectionContinue.isNotEmpty()) continueFirst else firstTile).requestFocus() }
                            focusedOnce = true
                        }
                    }

                    LazyVerticalGrid(
                        state = gridState,
                        columns = GridCells.Adaptive(160.dp),
                        contentPadding = PaddingValues(start = 48.dp, end = 48.dp, top = 8.dp, bottom = 48.dp),
                        verticalArrangement = Arrangement.spacedBy(16.dp),
                        horizontalArrangement = Arrangement.spacedBy(16.dp),
                        // The grid is the outermost focus group: entering it from the pills (or
                        // from another screen) always resolves to a live, currently-visible tile —
                        // NOT Modifier.focusRestorer()'s built-in "restore the previously-focused
                        // child" step. That step matches by the composite-key-hash of the child's
                        // *position in the composition tree*, which for a recycling LazyVerticalGrid
                        // is effectively per recycled slot, not per logical item: after scrolling the
                        // grid away and back (e.g. DOWN through several rows, UP and out to the tab
                        // pills, then DOWN again), the saved hash can match whatever item now happens
                        // to occupy that slot. requestFocus() on it can report success even though
                        // that slot is mid-recycle for the current scroll position, and focus quietly
                        // ends up nowhere a frame later with no fallback — because
                        // Modifier.focusRestorer() already told the search machinery the move was
                        // handled (see tvFocusEnterFallback's doc comment). That is the "DOWN from the
                        // tab pills into the grid deterministically drops to zero focused nodes" bug.
                        // tvFocusEnterFallback skips that step entirely and always re-resolves the
                        // target itself. Same fallback order as before: firstTile is tied to
                        // whichever tile the grid itself reports visible (see firstVisibleTileIndex
                        // above), so it stays a legitimate, currently-composed target regardless of
                        // scroll depth; chipsFirst/continueFirst are only reached when there are no
                        // tiles at all, i.e. the grid hasn't scrolled past the header yet, so they're
                        // still on-screen whenever they're actually picked. The `idx != tabIndex`
                        // branch is what actually blocks the outgoing tab's subtree from taking focus
                        // during the AnimatedContent crossfade (both tabs are briefly composed
                        // together), so a D-pad press mid-fade can never wander into a subtree that's
                        // about to be disposed.
                        modifier = Modifier.fillMaxSize().tvFocusEnterFallback {
                            if (idx != tabIndex) {
                                FocusRequester.Cancel
                            } else {
                                // Entering the grid from the tab pills (DOWN) should mirror the
                                // reverse (UP) path's row order. When the grid is at the very top
                                // the header lanes (Continue shelf, then category chips) are
                                // on-screen and composed, so land on the topmost one instead of
                                // skipping straight to the first tile — otherwise DOWN from the
                                // tabs jumps past the sub-tabs the user can plainly see (while UP
                                // walks through them, an asymmetry). Once scrolled past them the
                                // headers recycle (the 2251e7b restore-by-hash bug), so fall back
                                // to the currently-visible tile, whose target the grid reports live.
                                val atTop = gridState.firstVisibleItemIndex == 0 &&
                                    gridState.firstVisibleItemScrollOffset == 0
                                when {
                                    atTop && sectionContinue.isNotEmpty() -> continueFirst
                                    atTop && chips.size > 1 -> chipsFirst
                                    gridTitles.isNotEmpty() -> firstTile
                                    chips.size > 1 -> chipsFirst
                                    sectionContinue.isNotEmpty() -> continueFirst
                                    else -> FocusRequester.Default
                                }
                            }
                        },
                    ) {
                        if (sectionContinue.isNotEmpty()) {
                            item(span = { GridItemSpan(maxLineSpan) }) {
                                Column {
                                    ShelfHeader(section?.continueLabel?.ifBlank { null } ?: "Continue Watching")
                                    Spacer(Modifier.height(4.dp))
                                    LazyRow(
                                        horizontalArrangement = Arrangement.spacedBy(16.dp),
                                        contentPadding = PaddingValues(vertical = 24.dp),
                                        modifier = Modifier
                                            .focusRequester(continueLane)
                                            .tvFocusRestorer { continueFirst },
                                    ) {
                                        itemsIndexed(
                                            sectionContinue,
                                            key = { _, item -> item.fileId },
                                            contentType = { _, _ -> "continue" },
                                        ) { i, item ->
                                            ContinueCard(
                                                item = item,
                                                posterUrl = posterUrl(item.poster),
                                                onClick = { onPlayContinue(item) },
                                                onDismiss = { onDismissRequest(item) },
                                                onFocused = { onCardFocused(BackdropItem(item.fileId, item.poster)) },
                                                focusRequester = if (i == 0) continueFirst else null,
                                            )
                                        }
                                    }
                                }
                            }
                        }

                        if (chips.size > 1) {
                            item(span = { GridItemSpan(maxLineSpan) }) {
                                Row(
                                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                                    modifier = Modifier.tvFocusRestorer { chipsFirst },
                                ) {
                                    chips.forEachIndexed { chipIdx, chip ->
                                        PillButton(
                                            selected = selectedCat == chip.category,
                                            label = chip.label,
                                            onClick = { selectedCat = chip.category },
                                            modifier = if (chipIdx == 0) Modifier.focusRequester(chipsFirst) else Modifier,
                                        )
                                    }
                                }
                            }
                        }

                        itemsIndexed(
                            gridTitles,
                            key = { _, title -> title.id },
                            contentType = { _, _ -> "poster" },
                        ) { i, title ->
                            LibraryTile(
                                title = title,
                                section = section,
                                isEntertainment = isEntertainment,
                                posterUrl = posterUrl(title.poster),
                                onOpenTitle = onTitleClick,
                                onFocused = { onCardFocused(BackdropItem(title.id, title.poster)) },
                                focusRequester = if (i == firstVisibleTileIndex) firstTile else null,
                                // Chip filtering removes/adds items; the remaining ones glide to
                                // their new position instead of popping.
                                modifier = Modifier.animateItem(
                                    placementSpec = tween(300, easing = MotionTokens.Emphasized)
                                ),
                            )
                        }
                    }
                    }
                }
            }
        }
    }
    }
}

// ---- Ambient backdrop ----

/**
 * Minimal identity for the ambient backdrop: distinct from [Title]/[ContinueItem] because both the
 * Continue Watching shelf and the grid feed it, and the backdrop only ever needs a stable key (for
 * Crossfade identity) plus the poster path to resolve through the existing posterUrl() lookup.
 */
private data class BackdropItem(val key: String, val poster: String?)

/**
 * The hand-rolled immersive-list backdrop (androidx.tv.material3's ImmersiveList was removed).
 * Sits BEHIND [HomeContent]'s Column, one 480x270px bitmap at a time, stack-blurred at decode
 * (Modifier.blur is a no-op below API 31, so the blur is baked into the cached bitmap; the small
 * decode is free detail-loss the blur would erase anyway). [item] is fed by a 400ms dwell
 * debounce upstream, so this Crossfade only ever fires on rest, not on every D-pad press.
 */
@Composable
private fun HomeBackdrop(item: () -> BackdropItem?, posterUrl: (String?) -> String?) {
    val imageLoader = LocalAppContainer.current.imageLoader
    val context = LocalContext.current

    Crossfade(
        targetState = item(),
        animationSpec = tween(320),
        label = "homeBackdrop",
    ) { target ->
        val url = target?.let { posterUrl(it.poster) }
        if (url != null) {
            Box(Modifier.fillMaxSize()) {
                val request = remember(url) {
                    ImageRequest.Builder(context)
                        .data(url)
                        .size(384, 216)
                        .transformations(BlurTransformation(radius = 18))
                        .build()
                }
                AsyncImage(
                    model = request,
                    imageLoader = imageLoader,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .fillMaxSize()
                        .graphicsLayer { alpha = 0.35f },
                )
                // Gradient into the Background token (not pure black) so the image dissolves into
                // the UI instead of hard-cutting to a letterbox.
                Box(
                    Modifier
                        .fillMaxSize()
                        .drawWithCache {
                            val brush = Brush.verticalGradient(
                                colors = listOf(Color.Transparent, Background),
                                startY = size.height * 0.3f,
                                endY = size.height,
                            )
                            onDrawBehind { drawRect(brush) }
                        },
                )
                // Full wash so grid text stays WCAG-readable over the ambient image.
                Box(Modifier.fillMaxSize().background(Background.copy(alpha = 0.55f)))
            }
        }
    }
}

// ---- Section / category helpers ----

private fun sectionKeyOf(title: Title): String =
    (title.section ?: ENTERTAINMENT).ifBlank { ENTERTAINMENT }

private fun sectionKeyOf(item: ContinueItem): String =
    (item.section ?: ENTERTAINMENT).ifBlank { ENTERTAINMENT }

/** The tab set: one per section that has titles, in server order. May be empty. */
private fun buildTabs(
    bySection: Map<String, List<Title>>,
    sectionInfos: List<SectionInfo>,
): List<SectionInfo> {
    val infoByKey = sectionInfos.associateBy { it.key }
    fun info(key: String) = infoByKey[key] ?: SectionInfo(key = key, label = prettyKey(key))

    val result = mutableListOf<SectionInfo>()
    val seen = mutableSetOf<String>()

    fun consider(key: String) {
        if (key in seen) return
        if (bySection[key]?.isNotEmpty() == true) {
            result += info(key)
            seen += key
        }
    }

    sectionInfos.forEach { consider(it.key) }
    bySection.keys.forEach { consider(it) }
    return result
}

private fun prettyKey(key: String): String =
    key.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }

private fun tabLabel(tab: SectionInfo): String {
    val label = tab.label?.ifBlank { null } ?: prettyKey(tab.key)
    val icon = tab.icon?.ifBlank { null }
    return if (icon != null) "$icon $label" else label
}

private fun categoryOf(title: Title): String =
    title.category?.ifBlank { null } ?: if (title.isShow) "show" else "movie"

private data class CategoryChip(val label: String, val category: String?)

private val KNOWN_CATEGORIES = setOf("movie", "show")

private fun matchesCategory(title: Title, selected: String?): Boolean = when (selected) {
    null -> true
    "other" -> categoryOf(title) !in KNOWN_CATEGORIES
    else -> categoryOf(title) == selected
}

/** Chips for the categories actually present; "All" always, "Other" only if any uncategorised. */
private fun visibleChips(titles: List<Title>): List<CategoryChip> {
    val present = titles.map { categoryOf(it) }.toSet()
    val hasOther = titles.any { categoryOf(it) !in KNOWN_CATEGORIES }
    return buildList {
        add(CategoryChip("All", null))
        if ("movie" in present) add(CategoryChip("Movies", "movie"))
        if ("show" in present) add(CategoryChip("TV Shows", "show"))
        if (hasOther) add(CategoryChip("Other", "other"))
    }
}

private fun subtitleFor(title: Title, section: SectionInfo?, isEntertainment: Boolean): String {
    if (isEntertainment) {
        return if (title.isShow) plural(title.seasons.size, "season") else title.year?.toString().orEmpty()
    }
    val seasonWord = section?.season?.ifBlank { null } ?: "Season"
    val episodeWord = section?.episode?.ifBlank { null } ?: "Episode"
    val seasons = title.seasons.size
    val episodes = title.seasons.sumOf { it.episodes.size }
    return "${plural(seasons, seasonWord)} · ${plural(episodes, episodeWord)}"
}

private fun plural(n: Int, word: String): String = "$n $word" + if (n == 1) "" else "s"

// ---- Tiles & chrome ----

@Composable
private fun LibraryTile(
    title: Title,
    section: SectionInfo?,
    isEntertainment: Boolean,
    posterUrl: String?,
    onOpenTitle: (String) -> Unit,
    modifier: Modifier = Modifier,
    onFocused: (() -> Unit)? = null,
    focusRequester: FocusRequester? = null,
) {
    Column(modifier.width(160.dp)) {
        PosterCard(
            title = title.displayTitle,
            posterUrl = posterUrl,
            onClick = { onOpenTitle(title.id) },
            widthDp = 160.dp,
            onFocused = onFocused,
            modifier = focusRequester?.let { Modifier.focusRequester(it) } ?: Modifier,
        ) {
            if (isEntertainment && title.isShow) {
                TileBadge("TV", Modifier.align(Alignment.TopStart).padding(6.dp))
            }
            title.quality?.ifBlank { null }?.let { quality ->
                TileBadge(quality, Modifier.align(Alignment.TopEnd).padding(6.dp))
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(
            title.displayTitle,
            style = MaterialTheme.typography.bodyMedium,
            color = TextPrimary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        val subtitle = subtitleFor(title, section, isEntertainment)
        if (subtitle.isNotBlank()) {
            Text(
                subtitle,
                style = MaterialTheme.typography.labelSmall,
                color = TextSecondary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

@Composable
private fun TileBadge(text: String, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .background(Scrim, RoundedCornerShape(4.dp))
            .padding(horizontal = 6.dp, vertical = 2.dp),
    ) {
        Text(text, color = TextPrimary, style = MaterialTheme.typography.labelSmall)
    }
}

@Composable
private fun PillButton(
    selected: Boolean,
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val containerColor by animateColorAsState(
        targetValue = if (selected) Accent else SurfaceVariant,
        animationSpec = tween(MotionTokens.DurationShort, easing = MotionTokens.Emphasized),
        label = "pillContainer",
    )
    val contentColor by animateColorAsState(
        targetValue = if (selected) OnAccent else TextPrimary,
        animationSpec = tween(MotionTokens.DurationShort, easing = MotionTokens.Emphasized),
        label = "pillContent",
    )
    Button(
        onClick = onClick,
        scale = ButtonDefaults.scale(focusedScale = 1.025f),
        colors = ButtonDefaults.colors(containerColor = containerColor, contentColor = contentColor),
        modifier = modifier,
    ) { Text(label) }
}

@Composable
private fun ShelfHeader(text: String) {
    Text(text, style = MaterialTheme.typography.titleLarge, color = TextPrimary)
}

@Composable
private fun ContinueCard(
    item: ContinueItem,
    posterUrl: String?,
    onClick: () -> Unit,
    onDismiss: () -> Unit,
    onFocused: (() -> Unit)? = null,
    focusRequester: FocusRequester? = null,
) {
    var focused by remember { mutableStateOf(false) }

    Column(Modifier.width(140.dp)) {
        PosterCard(
            title = item.displayName,
            posterUrl = posterUrl,
            onClick = onClick,
            widthDp = 140.dp,
            onFocused = onFocused,
            modifier = Modifier
                .onFocusChanged { focused = it.isFocused }
                .onKeyEvent { e ->
                    val native = e.nativeKeyEvent
                    when {
                        native.keyCode == KeyEvent.KEYCODE_MENU && e.type == KeyEventType.KeyUp -> {
                            onDismiss(); true
                        }
                        native.keyCode == KeyEvent.KEYCODE_DPAD_CENTER && native.isLongPress -> {
                            onDismiss(); true
                        }
                        else -> false
                    }
                }
                .let { m -> focusRequester?.let { m.focusRequester(it) } ?: m },
        ) {
            // Progress bar overlay: a 4dp bar inset 6dp from the poster edges, rounded, on a
            // faint track — replaces the old flush 6dp strip.
            val fraction = (item.percent / 100.0).coerceIn(0.0, 1.0).toFloat()
            Box(
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(6.dp)
                    .fillMaxWidth()
                    .height(4.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(Color.White.copy(alpha = 0.25f)),
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxHeight()
                        .fillMaxWidth(fraction)
                        .background(Accent),
                )
            }
        }
        // Fixed-height slot so the hint fading in/out never shifts the shelf row's layout.
        Box(Modifier.height(16.dp)) {
            RemoveHint(visible = focused)
        }
    }
}

/**
 * Discoverability for the long-press/MENU dismiss gesture on [ContinueCard], which otherwise has
 * zero affordance. Pulled into its own function (rather than nested directly inside the
 * Column/Box call site) so the plain top-level [AnimatedVisibility] overload resolves instead of
 * a Column/Row/BoxScope-receiver variant.
 */
@Composable
private fun RemoveHint(visible: Boolean) {
    AnimatedVisibility(
        visible = visible,
        enter = fadeIn(tween(210, delayMillis = 400)),
        exit = fadeOut(tween(90)),
    ) {
        Text(
            "Hold SELECT to remove",
            style = MaterialTheme.typography.labelSmall,
            color = TextSecondary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * Owns nothing itself — [pendingDismiss] is hoisted in [HomeScreen] as a [MutableState] so its
 * setter can be threaded down into [ContinueCard]'s onDismiss. Passing the MutableState
 * *reference* down (instead of its `.value`) keeps HomeScreen's own recomposition scope from ever
 * subscribing to it: only this composable reads `.value`, so opening/closing the confirm dialog
 * recomposes just this tiny subtree, never the grid above it.
 */
@Composable
private fun DismissDialogHost(
    pendingDismiss: MutableState<ContinueItem?>,
    onConfirm: (ContinueItem) -> Unit,
    onCancel: () -> Unit,
) {
    val item = pendingDismiss.value ?: return
    DismissDialog(
        title = item.displayName,
        onConfirm = { onConfirm(item) },
        onCancel = onCancel,
    )
}

/**
 * A real windowed dialog (not a plain scrim Box drawn inline): the platform window traps D-pad
 * focus so it can never walk into the grid behind it, and dismissOnBackPress gives Back-cancel
 * for free instead of Back falling through to exit the whole screen.
 */
@Composable
private fun DismissDialog(title: String, onConfirm: () -> Unit, onCancel: () -> Unit) {
    Dialog(
        onDismissRequest = onCancel,
        properties = DialogProperties(dismissOnBackPress = true, usePlatformDefaultWidth = false),
    ) {
        val visibleState = remember { MutableTransitionState(false) }
        LaunchedEffect(Unit) { visibleState.targetState = true }
        val cancelFocus = remember { FocusRequester() }

        Box(Modifier.fillMaxSize()) {
            AnimatedVisibility(
                visibleState = visibleState,
                enter = fadeIn(tween(150)),
            ) {
                Box(Modifier.fillMaxSize().background(Scrim))
            }

            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                AnimatedVisibility(
                    visibleState = visibleState,
                    enter = fadeIn(tween(220, easing = MotionTokens.EmphasizedDecelerate)) +
                        scaleIn(
                            initialScale = 0.92f,
                            animationSpec = tween(220, easing = MotionTokens.EmphasizedDecelerate),
                        ),
                ) {
                    // Requesting focus here — inside the AnimatedVisibility content lambda —
                    // rather than in an effect scoped to the outer Dialog composable means this
                    // LaunchedEffect only runs once the Cancel button below has actually composed,
                    // so requestFocus() succeeds immediately instead of throwing (FocusRequester
                    // not attached) against a not-yet-composed target.
                    LaunchedEffect(Unit) { runCatching { cancelFocus.requestFocus() } }

                    Surface(
                        shape = RoundedCornerShape(16.dp),
                        colors = SurfaceDefaults.colors(containerColor = SurfaceColor),
                        border = Border(
                            border = BorderStroke(1.dp, Outline),
                            shape = RoundedCornerShape(16.dp),
                        ),
                        modifier = Modifier.width(480.dp),
                    ) {
                        Column(Modifier.padding(28.dp)) {
                            Text(
                                "Remove from Continue Watching?",
                                style = MaterialTheme.typography.titleLarge,
                                color = TextPrimary,
                            )
                            Spacer(Modifier.height(8.dp))
                            Text(title, color = TextSecondary)
                            Spacer(Modifier.height(24.dp))
                            Row {
                                // Cancel before Remove: a belt-and-braces default so even if focus
                                // request timing ever regressed, layout-order focus still lands on
                                // the safe action, not the destructive one.
                                Button(
                                    onClick = onCancel,
                                    modifier = Modifier.focusRequester(cancelFocus),
                                ) { Text("Cancel") }
                                Spacer(Modifier.width(12.dp))
                                Button(
                                    onClick = onConfirm,
                                    colors = ButtonDefaults.colors(
                                        containerColor = ErrorRed.copy(alpha = 0.15f),
                                        contentColor = ErrorRed,
                                        focusedContainerColor = ErrorRed,
                                        focusedContentColor = Color.White,
                                    ),
                                ) { Text("Remove") }
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * True cold start only (no [AppContainer.homeCache] to seed from): mirrors [HomeContent]'s real
 * layout at its exact final dimensions so there is no layout jump when the first fetch lands.
 * Shimmer is capped at the top 2 poster rows (the Continue Watching shelf + the first grid row) —
 * an infinite animation competes with first-frame work on Stick GPUs, so everything below is
 * static per [SkeletonBox]'s own guidance.
 */
@Composable
private fun HomeSkeleton() {
    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(start = 48.dp, end = 48.dp, top = 28.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            SkeletonBox(Modifier.width(150.dp).height(32.dp), animated = false)
        }

        Spacer(Modifier.height(16.dp))
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 48.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            repeat(3) {
                SkeletonBox(
                    modifier = Modifier.width(96.dp).height(32.dp),
                    shape = RoundedCornerShape(50),
                    animated = false,
                )
            }
        }

        Spacer(Modifier.height(16.dp))
        Column(Modifier.weight(1f).padding(horizontal = 48.dp)) {
            // Continue Watching shelf — the 1st of the "top 2 rows" that shimmer. Width matches
            // the real 140dp ContinueCard so the skeleton->content Crossfade doesn't reflow.
            // 5 boxes at 140dp + 16dp spacing = 764dp, fits the 864dp canvas (960 - 96 padding).
            SkeletonBox(Modifier.width(180.dp).height(20.dp), animated = false)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                repeat(5) {
                    SkeletonBox(modifier = Modifier.width(140.dp).aspectRatio(2f / 3f), animated = true)
                }
            }

            Spacer(Modifier.height(16.dp))
            // Grid row 1 — the 2nd of the "top 2 rows" that shimmer. Width matches the real
            // 160dp GridCells.Adaptive LibraryTile. 4 boxes at 160dp + 16dp spacing = 688dp, fits.
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                repeat(4) {
                    SkeletonBox(modifier = Modifier.width(160.dp).aspectRatio(2f / 3f), animated = true)
                }
            }

            Spacer(Modifier.height(16.dp))
            // Grid row 2 — below the fold, static.
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                repeat(4) {
                    SkeletonBox(modifier = Modifier.width(160.dp).aspectRatio(2f / 3f), animated = false)
                }
            }
        }
    }
}

/** A small graphicsLayer-driven spin — no material3 dependency, no layout-affecting animation. */
@Composable
private fun RefreshIndicator(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "refreshSpin")
    val angle by transition.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(animation = tween(900, easing = LinearEasing)),
        label = "refreshAngle",
    )
    Canvas(
        modifier = modifier
            .size(20.dp)
            .graphicsLayer { rotationZ = angle },
    ) {
        drawArc(
            color = Accent,
            startAngle = 0f,
            sweepAngle = 270f,
            useCenter = false,
            style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round),
        )
    }
}

@Composable
private fun CenterMessage(message: String, onRetry: () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(message, color = ErrorRed, style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(16.dp))
            Button(onClick = onRetry) { Text("Retry") }
        }
    }
}

/** Shown when the server has zero tabs (a fresh install with no sections created yet). */
@Composable
private fun NoTabsMessage(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text(
            "No tabs yet — create one in drivecast on your Mac, then assign drives to it.",
            color = TextSecondary,
            style = MaterialTheme.typography.titleMedium,
        )
    }
}
