package com.drivecast.tv.ui.common

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.gestures.BringIntoViewSpec
import androidx.compose.foundation.gestures.LocalBringIntoViewSpec
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.foundation.focusGroup
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.focus.focusRestorer
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import kotlinx.coroutines.launch

/**
 * One wrapper around [Modifier.focusRestorer] so the Compose 1.8 signature
 * change (when we eventually bump past Compose 1.7) is a one-line fix here
 * instead of a sweep across every call site. [Modifier.focusGroup] shrinks
 * the D-pad focus search space to this subtree.
 *
 * [onRestoreFailed] is the reason this wrapper exists at all: without it, a
 * restorer whose remembered child left composition (tab switch rebuilt the
 * lane, or a lazy layout recycled the card) fails the restore and SWALLOWS
 * the key event — the "dead D-pad press". Every lane must pass a fallback
 * (usually a FocusRequester on its first item) so entering the lane always
 * lands somewhere.
 *
 * **Never put this on a `Lazy*` layout — use [tvFocusEnterFallback] there.**
 * [Modifier.focusRestorer] PINS the lane's focused child through the lazy
 * layout's `PinnableContainer` when focus leaves the lane, and releases that
 * pin again from its own `onDetach`. Tear a pinned lane down while the pin is
 * still live — a Crossfade/AnimatedContent swapping the list's contents, a
 * tab rebuild — and the pin is released twice, which foundation answers with
 * `IllegalStateException("Release should only be called once")` thrown out of
 * the measure/layout pass. That is a hard process death, not a dead press:
 * it killed the app on every "open a show, step LEFT onto the season list,
 * pick another season" in DetailScreen (retraced stack:
 * `LazyLayoutPinnableItem.release <- FocusRestorerNode.onDetach`). The lane
 * being short or rarely recycled is no defence — only the pin's lifetime
 * matters. The one surviving call site is SettingsScreen's tab list, where
 * the lane is the screen's only focusable content, so focus never exits it
 * and no pin is ever taken.
 */
@OptIn(ExperimentalComposeUiApi::class)
fun Modifier.tvFocusRestorer(onRestoreFailed: (() -> FocusRequester)? = null): Modifier =
    this.focusRestorer {
        // A lane fallback points at the first item of a Lazy* layout (or a tab subtree
        // disposing during the AnimatedContent crossfade). If that node is recycled/detached
        // at restore time, requestFocus() throws "FocusRequester is not initialized" straight
        // out of dispatchKeyEvent and kills the process — so probe it under a guard first.
        //
        // The probe result decides what we hand back, but unconditionally returning Cancel
        // here (the old behavior) is itself the bug: Cancel tells the focus-search machinery
        // this move was aborted, which can roll back whatever focus the probe just placed,
        // leaving zero focused nodes even though a perfectly good, on-screen target existed.
        // Returning the target FocusRequester instead is the tv-material/foundation idiom —
        // Modifier.focusRestorer's own "enter" property re-requests focus on it as the
        // officially-timed, authoritative attempt and reports the true Redirected /
        // RedirectCancelled outcome, instead of a result we've already forced to "cancelled."
        // The crash guard is preserved for the one case it exists for: a genuinely-detached
        // target (probe throws) degrades to Default (NOT Cancel — see below) so search can
        // still find something instead of crashing.
        val target = onRestoreFailed?.invoke() ?: FocusRequester.Default
        if (target == FocusRequester.Default || target == FocusRequester.Cancel) {
            target
        } else {
            // On Android, AndroidComposeView's key-input path (dispatchKeyEvent ->
            // focusSearch(direction) { it.requestFocus(direction) ?: true }) coerces a
            // Cancelled requestFocus() result to `true`, which means "this move was handled
            // (or cancelled) — stop searching, consume the press." A dead target here would
            // therefore not degrade to a normal search landing on some other candidate; it
            // would consume the D-pad press and leave focus wherever it last was (a dead
            // press). Returning Default instead makes performCustomEnter report "no custom
            // enter happened," so the search proceeds to find a real focusable child normally.
            val attached = runCatching { target.requestFocus() }.isSuccess
            if (attached) target else FocusRequester.Default
        }
    }.focusGroup()

/**
 * A [tvFocusRestorer] alternative for a focus group whose children come from a *recycling* lazy
 * layout (e.g. the home grid's `LazyVerticalGrid`) rather than a fixed set of nodes (a plain
 * `Row`/`Column`).
 *
 * [Modifier.focusRestorer]'s built-in "restore the previously-focused child" step
 * (`restoreFocusedChild()`) matches by the *composite key hash of the child's position in the
 * composition tree* — for a recycling lazy layout, that hash is effectively per *recycled slot*,
 * not per logical item. After the group is scrolled away from and back to (e.g. DOWN through
 * several rows, then back UP and out to a sibling like a tab-pills row, then DOWN again), the
 * saved hash can match whatever item now happens to occupy that slot. `requestFocus()` on it can
 * report success even though that slot is mid-recycle for the *current* scroll position, and the
 * focus silently ends up nowhere a frame later — with no fallback, because
 * [Modifier.focusRestorer] already told the search machinery the move was handled (it returns
 * [FocusRequester.Cancel] on a "successful" restore). That is the "DOWN from the tab pills into
 * the grid deterministically drops to zero focused nodes" bug: [onRestoreFailed] never even runs,
 * since the built-in restore step believes it already succeeded.
 *
 * This variant skips that built-in step entirely and always resolves [onEnter] fresh, requesting
 * focus on whatever it returns ourselves. [onEnter] is expected to name a target derived from the
 * group's own *current* state (e.g. "whichever tile the grid currently reports visible") rather
 * than a memory of what was focused before — which sacrifices restoring the exact previously
 * focused item after a scroll round-trip, but that memory was never trustworthy here to begin
 * with, and a slightly-off restore target beats a dead D-pad.
 */
@OptIn(ExperimentalComposeUiApi::class)
fun Modifier.tvFocusEnterFallback(onEnter: () -> FocusRequester): Modifier =
    this.focusProperties {
        canFocus = false // same as focusGroup()'s own default — D-pad search still recurses into the children instead of stopping on the group itself
        enter = {
            val target = onEnter()
            if (target == FocusRequester.Default || target == FocusRequester.Cancel) {
                target
            } else {
                // We're doing the requestFocus() ourselves (instead of just returning target and
                // letting the search machinery redirect to it), so tell it the move is already
                // handled either way: Cancel on success (mirrors focusRestorer's own successful-
                // restore contract), Default on failure so a normal descendant search gets a
                // chance instead of silently dropping the key press.
                val attached = runCatching { target.requestFocus() }.isSuccess
                if (attached) FocusRequester.Cancel else FocusRequester.Default
            }
        }
    }.focusGroup()

/**
 * Pins the pivot fraction used when bringing a focused child of a lazy
 * layout into view, so the focused card holds a stable keyline near the
 * overscan-safe zone instead of drifting to wherever foundation's default
 * pivot lands.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun PositionFocusedItemInLazyLayout(
    parentFraction: Float = 0.10f,
    content: @Composable () -> Unit,
) {
    val spec = remember(parentFraction) {
        object : BringIntoViewSpec {
            override fun calculateScrollDistance(offset: Float, size: Float, containerSize: Float): Float {
                // Already fully within the viewport -> don't re-pin; matches foundation's default
                // minimal bring-into-view and keeps the focused card from animating on every step.
                if (offset >= 0f && offset + size <= containerSize) return 0f
                val initial = parentFraction * containerSize
                val target = if (size <= containerSize && (containerSize - initial) < size) containerSize - size else initial
                return offset - target
            }
        }
    }
    CompositionLocalProvider(LocalBringIntoViewSpec provides spec, content = content)
}

/**
 * A [tvDpadHop] resolution: the requester to focus, and an optional suspend action (typically a
 * lazy layout's `scrollToItem`) to run first when the target isn't attached yet — e.g. it's
 * currently scrolled/recycled out of composition and needs to be brought back before it can take
 * focus at all.
 */
class HopTarget(val requester: FocusRequester, val scrollFirst: (suspend () -> Unit)? = null)

/**
 * Bypasses Compose's focus search entirely for a DPAD UP/DOWN press on this node, in favor of a
 * deterministic target — for the specific hops (continue shelf <-> controls row <-> first tile
 * row) where the search's own two failure exits strand focus on a shelf tab (see tvFocusRestorer's
 * and tvFocusEnterFallback's doc comments above, and the investigation this fixes: Cancel-from-
 * enter consumes the press instead of redirecting, and a deactivated/recycling lane can be dropped
 * from the candidate set mid-search). [onUp]/[onDown] resolve lazily on every matching press
 * (rather than once) since the decision can depend on live state (e.g. "does this tab currently
 * have a continue shelf"); returning null declines the hop for this press, leaving the key event
 * unconsumed so it falls through to Compose's normal focus search exactly as if this modifier
 * weren't here (e.g. a shelf-less tab's controls row still lets UP reach the tab bar).
 *
 * On a resolved [HopTarget], `requestFocus()` is tried first; only if that fails AND a
 * [HopTarget.scrollFirst] was supplied does this launch it (then retry focus) — a lane that's
 * still composed skips the scroll entirely, and a lane with no scroll option that fails to focus
 * simply falls through rather than eating the press.
 *
 * Caution: this consumes the matching direction's KeyDown on its container wholesale. Any future
 * lane inserted between the shelf, the controls row, and the tile grid will be shadowed by these
 * hops until the resolvers wiring them (in HomeScreen.kt) are updated to route through it.
 */
@OptIn(ExperimentalComposeUiApi::class)
fun Modifier.tvDpadHop(
    onUp: (() -> HopTarget?)? = null,
    onDown: (() -> HopTarget?)? = null,
): Modifier = composed {
    val scope = rememberCoroutineScope()
    onPreviewKeyEvent { event ->
        if (event.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
        val resolver = when (event.key) {
            Key.DirectionUp -> onUp
            Key.DirectionDown -> onDown
            else -> null
        } ?: return@onPreviewKeyEvent false
        val hop = resolver() ?: return@onPreviewKeyEvent false
        when {
            runCatching { hop.requester.requestFocus() }.isSuccess -> true
            hop.scrollFirst != null -> {
                val scrollFirst = hop.scrollFirst
                val requester = hop.requester
                scope.launch {
                    scrollFirst()
                    runCatching { requester.requestFocus() }
                }
                true
            }
            else -> false
        }
    }
}
