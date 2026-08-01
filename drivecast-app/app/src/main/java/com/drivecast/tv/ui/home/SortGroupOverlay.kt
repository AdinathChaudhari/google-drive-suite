package com.drivecast.tv.ui.home

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.scaleIn
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.State
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.tv.material3.Border
import androidx.tv.material3.Button
import androidx.tv.material3.ButtonDefaults
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.SurfaceDefaults
import androidx.tv.material3.Text
import com.drivecast.tv.ui.theme.Accent
import com.drivecast.tv.ui.theme.MotionTokens
import com.drivecast.tv.ui.theme.Outline
import com.drivecast.tv.ui.theme.Scrim
import com.drivecast.tv.ui.theme.Surface as SurfaceColor
import com.drivecast.tv.ui.theme.SurfaceVariant
import com.drivecast.tv.ui.theme.TextPrimary

enum class HomeOverlay { SORT, GROUP }

/**
 * Same MutableState-reference pattern as DismissDialogHost: only this host reads
 * [request].value (and [groupContext].value), so opening/closing the overlay never recomposes
 * the grid.
 */
@Composable
fun SortGroupOverlayHost(
    request: MutableState<HomeOverlay?>,
    sort: SortSpec,
    // The EFFECTIVE group (post category-chip suppression), not the raw pref — must match
    // whichever selection the pill that opened this overlay is showing, or the pill and the
    // overlay disagree about what's currently "on". See onOpenGroupMenu's call site in
    // HomeScreen for where this is captured.
    groupContext: State<GroupKey>,
    onPickSort: (SortKey) -> Unit,
    onPickGroup: (GroupKey) -> Unit,
) {
    when (request.value) {
        null -> return
        HomeOverlay.SORT -> OptionOverlay(
            title = "Sort by",
            options = SortKey.entries.map { OverlayOption(sortOptionLabel(it, sort), selected = it == sort.key) },
            onSelect = { idx -> onPickSort(SortKey.entries[idx]); request.value = null },
            onDismiss = { request.value = null },
        )
        HomeOverlay.GROUP -> OptionOverlay(
            title = "Group by",
            options = GroupKey.entries.map {
                OverlayOption(groupOptionLabel(it), selected = it == groupContext.value)
            },
            onSelect = { idx -> onPickGroup(GroupKey.entries[idx]); request.value = null },
            onDismiss = { request.value = null },
        )
    }
}

private data class OverlayOption(val label: String, val selected: Boolean)

@Composable
private fun OptionOverlay(
    title: String,
    options: List<OverlayOption>,
    onSelect: (Int) -> Unit,
    onDismiss: () -> Unit,
) {
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(dismissOnBackPress = true, usePlatformDefaultWidth = false),
    ) {
        val visibleState = remember { MutableTransitionState(false) }
        LaunchedEffect(Unit) { visibleState.targetState = true }
        val selectedFocus = remember { FocusRequester() }

        Box(Modifier.fillMaxSize()) {
            AnimatedVisibility(visibleState = visibleState, enter = fadeIn(tween(150))) {
                Box(Modifier.fillMaxSize().background(Scrim))
            }
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                AnimatedVisibility(
                    visibleState = visibleState,
                    enter = fadeIn(tween(220, easing = MotionTokens.EmphasizedDecelerate)) +
                        scaleIn(initialScale = 0.92f, animationSpec = tween(220, easing = MotionTokens.EmphasizedDecelerate)),
                ) {
                    // Focus the CURRENTLY-SELECTED option (exactly one is always selected):
                    // SELECT-on-open is then "toggle direction" for sort — the primary action.
                    // Requested here, inside the content lambda, for the same not-yet-composed
                    // reason documented on DismissDialog.
                    LaunchedEffect(Unit) { runCatching { selectedFocus.requestFocus() } }

                    Surface(
                        shape = RoundedCornerShape(16.dp),
                        colors = SurfaceDefaults.colors(containerColor = SurfaceColor),
                        border = Border(border = BorderStroke(1.dp, Outline), shape = RoundedCornerShape(16.dp)),
                        modifier = Modifier.width(400.dp),
                    ) {
                        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(title, style = MaterialTheme.typography.titleLarge, color = TextPrimary)
                            Spacer(Modifier.height(4.dp))
                            options.forEachIndexed { idx, opt ->
                                Button(
                                    onClick = { onSelect(idx) },
                                    colors = ButtonDefaults.colors(
                                        containerColor = if (opt.selected) Accent.copy(alpha = 0.20f) else SurfaceVariant,
                                        contentColor = if (opt.selected) Accent else TextPrimary,
                                    ),
                                    modifier = (if (opt.selected) Modifier.focusRequester(selectedFocus) else Modifier)
                                        .fillMaxWidth(),
                                ) { Text(opt.label) }
                            }
                        }
                    }
                }
            }
        }
    }
}
