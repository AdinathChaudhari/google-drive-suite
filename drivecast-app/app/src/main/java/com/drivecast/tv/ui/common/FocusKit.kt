package com.drivecast.tv.ui.common

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.gestures.BringIntoViewSpec
import androidx.compose.foundation.gestures.LocalBringIntoViewSpec
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.foundation.focusGroup
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.focus.focusRestorer

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
        // target (probe throws) degrades to a harmless Cancel no-op rather than a crash.
        val target = onRestoreFailed?.invoke() ?: FocusRequester.Default
        if (target == FocusRequester.Default || target == FocusRequester.Cancel) {
            target
        } else {
            val attached = runCatching { target.requestFocus() }.isSuccess
            if (attached) target else FocusRequester.Cancel
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
