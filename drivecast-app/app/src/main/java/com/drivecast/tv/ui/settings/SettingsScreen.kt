package com.drivecast.tv.ui.settings

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.tv.material3.Border
import androidx.tv.material3.Button
import androidx.tv.material3.Icon
import androidx.tv.material3.IconButton
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.SurfaceDefaults
import androidx.tv.material3.Text
import com.drivecast.tv.LocalAppContainer
import com.drivecast.tv.api.SectionInfo
import com.drivecast.tv.ui.common.StatusView
import com.drivecast.tv.ui.common.tvFocusRestorer
import com.drivecast.tv.ui.theme.Accent
import com.drivecast.tv.ui.theme.Background
import com.drivecast.tv.ui.theme.ErrorRed
import com.drivecast.tv.ui.theme.MotionTokens
import com.drivecast.tv.ui.theme.Outline
import com.drivecast.tv.ui.theme.Scrim
import com.drivecast.tv.ui.theme.Surface as SurfaceColor
import com.drivecast.tv.ui.theme.SurfaceBright
import com.drivecast.tv.ui.theme.SurfaceVariant
import com.drivecast.tv.ui.theme.TextPrimary
import com.drivecast.tv.ui.theme.TextSecondary

/**
 * TV Settings screen: list/reorder/rename the server's tabs (see the feature spec — LIST is
 * GET /api/sections' server order, RENAME/REORDER both round-trip through POST /api/settings via
 * [SettingsViewModel]). No drag model on a D-pad remote — reorder is two discrete Move-Up/
 * Move-Down buttons per row.
 *
 * [onTabsChanged] fires once per *successful* save (mirrors [SettingsViewModel.state]'s
 * [SettingsUiState.saveVersion] ticking up) so a caller wired into [androidx.navigation.NavController]
 * (out of this screen's scope) can stash a "tabsChanged" flag on Home's
 * `NavBackStackEntry.savedStateHandle` and have [com.drivecast.tv.ui.home.HomeViewModel.refresh]
 * pick up the new tab list/order on return, per the feature spec's nav-result mechanism.
 */
@Composable
fun SettingsScreen(
    onExit: () -> Unit,
    onTabsChanged: () -> Unit = {},
) {
    val container = LocalAppContainer.current
    val vm: SettingsViewModel = viewModel(factory = SettingsViewModel.factory(container))
    val state by vm.state.collectAsStateWithLifecycle()

    // Fires onTabsChanged exactly once per successful persist, not on every recomposition —
    // saveVersion only ticks up on a genuine save (see SettingsViewModel.persist), so comparing
    // against the last-seen value (not just "> 0") survives this composable being recomposed for
    // unrelated reasons (e.g. the rename Dialog opening/closing) without re-firing spuriously.
    var lastSeenSaveVersion by remember { mutableStateOf(state.saveVersion) }
    LaunchedEffect(state.saveVersion) {
        if (state.saveVersion != lastSeenSaveVersion) {
            lastSeenSaveVersion = state.saveVersion
            onTabsChanged()
        }
    }

    Surface(
        modifier = Modifier.fillMaxSize(),
        colors = SurfaceDefaults.colors(containerColor = Background),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 48.dp, vertical = 27.dp),
        ) {
            Text("Settings", style = MaterialTheme.typography.displaySmall, color = Accent)
            Spacer(Modifier.height(4.dp))
            Text("Tabs", style = MaterialTheme.typography.titleLarge, color = TextPrimary)
            Spacer(Modifier.height(24.dp))

            when {
                state.loading -> StatusView(
                    title = "Loading tabs…",
                    body = "Fetching your tabs from the server.",
                )

                state.tabs.isEmpty() -> StatusView(
                    title = "No tabs yet",
                    body = state.error ?: "Your server hasn't reported any tabs.",
                    primaryLabel = "Retry",
                    onPrimary = vm::reload,
                    secondaryLabel = "Back",
                    onSecondary = onExit,
                )

                else -> TabList(
                    tabs = state.tabs,
                    error = state.error,
                    onMoveUp = vm::moveUp,
                    onMoveDown = vm::moveDown,
                    onRename = vm::openRename,
                )
            }
        }
    }

    val renameTarget = state.renameTarget
    if (renameTarget != null) {
        RenameDialog(
            target = renameTarget,
            onConfirm = vm::confirmRename,
            onCancel = vm::dismissRename,
        )
    }
}

@Composable
private fun TabList(
    tabs: List<SectionInfo>,
    error: String?,
    onMoveUp: (Int) -> Unit,
    onMoveDown: (Int) -> Unit,
    onRename: (SectionInfo) -> Unit,
) {
    // The fallback target for the whole lane's tvFocusRestorer: the first row's Rename button —
    // unlike that row's Move-Up (always disabled, hence unfocusable, at index 0), Rename is
    // always present and enabled regardless of list size, so it is always a safe, on-screen
    // restore target.
    val firstRowFocus = remember { FocusRequester() }
    var didInitialFocus by remember { mutableStateOf(false) }
    LaunchedEffect(tabs.isEmpty()) {
        if (!didInitialFocus && tabs.isNotEmpty()) {
            didInitialFocus = true
            runCatching { firstRowFocus.requestFocus() }
        }
    }

    Column(Modifier.fillMaxSize()) {
        if (error != null) {
            Text(error, color = ErrorRed, style = MaterialTheme.typography.labelMedium)
            Spacer(Modifier.height(12.dp))
        }
        LazyColumn(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .tvFocusRestorer { firstRowFocus },
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            itemsIndexed(tabs, key = { _, tab -> tab.key }) { index, tab ->
                TabRow(
                    tab = tab,
                    index = index,
                    lastIndex = tabs.lastIndex,
                    firstRowFocus = if (index == 0) firstRowFocus else null,
                    onMoveUp = { onMoveUp(index) },
                    onMoveDown = { onMoveDown(index) },
                    onRename = { onRename(tab) },
                )
            }
        }
    }
}

@Composable
private fun TabRow(
    tab: SectionInfo,
    index: Int,
    lastIndex: Int,
    firstRowFocus: FocusRequester?,
    onMoveUp: () -> Unit,
    onMoveDown: () -> Unit,
    onRename: () -> Unit,
) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        colors = SurfaceDefaults.colors(containerColor = SurfaceVariant),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 20.dp, vertical = 14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = tabDisplayLabel(tab),
                style = MaterialTheme.typography.bodyLarge,
                color = TextPrimary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(16.dp))
            IconButton(onClick = onMoveUp, enabled = index > 0) {
                Icon(Icons.Default.KeyboardArrowUp, contentDescription = "Move up")
            }
            Spacer(Modifier.width(8.dp))
            IconButton(onClick = onMoveDown, enabled = index < lastIndex) {
                Icon(Icons.Default.KeyboardArrowDown, contentDescription = "Move down")
            }
            Spacer(Modifier.width(16.dp))
            Button(
                onClick = onRename,
                modifier = firstRowFocus?.let { Modifier.focusRequester(it) } ?: Modifier,
            ) {
                Icon(Icons.Default.Edit, contentDescription = null)
                Spacer(Modifier.width(6.dp))
                Text("Rename")
            }
        }
    }
}

private fun tabDisplayLabel(tab: SectionInfo): String {
    val label = tab.label?.ifBlank { null } ?: tab.key
    val icon = tab.icon?.ifBlank { null }
    return if (icon != null) "$icon $label" else label
}

/**
 * A real windowed dialog (matches [com.drivecast.tv.ui.home.HomeScreen]'s DismissDialog pattern):
 * the platform window traps D-pad focus off the tab list behind it, and dismissOnBackPress gives
 * Back-cancel for free. Cancel — not Save — is focus-requested by default, the same
 * belt-and-braces choice DismissDialog makes for its own destructive action.
 */
@Composable
private fun RenameDialog(
    target: SectionInfo,
    onConfirm: (String) -> Unit,
    onCancel: () -> Unit,
) {
    var text by remember(target.key) { mutableStateOf(target.label.orEmpty()) }

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
                    // Requesting focus inside the AnimatedVisibility content lambda (not an effect
                    // scoped to the outer Dialog composable) means this only runs once Cancel has
                    // actually composed, so requestFocus() succeeds instead of throwing against a
                    // not-yet-composed target.
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
                                "Rename tab",
                                style = MaterialTheme.typography.titleLarge,
                                color = TextPrimary,
                            )
                            Spacer(Modifier.height(16.dp))
                            TvTextField(
                                value = text,
                                onValueChange = { text = it },
                                placeholder = "Tab name",
                                modifier = Modifier.fillMaxWidth(),
                            )
                            Spacer(Modifier.height(24.dp))
                            Row {
                                Button(
                                    onClick = onCancel,
                                    modifier = Modifier.focusRequester(cancelFocus),
                                ) { Text("Cancel") }
                                Spacer(Modifier.width(12.dp))
                                Button(onClick = { onConfirm(text) }) { Text("Save") }
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * A focusable text field (foundation only), the same visual pattern as
 * [com.drivecast.tv.ui.setup.SetupScreen]'s private TvTextField (border/container color both
 * animate on focus) — duplicated locally rather than shared since that one is private to its file
 * and extracting a shared component is outside this item's file scope.
 */
@Composable
private fun TvTextField(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    modifier: Modifier = Modifier,
) {
    var focused by remember { mutableStateOf(false) }
    val borderColor by animateColorAsState(
        targetValue = if (focused) Accent else SurfaceVariant,
        animationSpec = tween(MotionTokens.DurationShort, easing = MotionTokens.Emphasized),
        label = "settingsFieldBorderColor",
    )
    val containerColor by animateColorAsState(
        targetValue = if (focused) SurfaceBright else SurfaceVariant,
        animationSpec = tween(MotionTokens.DurationShort, easing = MotionTokens.Emphasized),
        label = "settingsFieldContainerColor",
    )
    val borderWidth = if (focused) 2.dp else 1.dp

    Box(
        modifier = modifier
            .clip(RoundedCornerShape(8.dp))
            .background(containerColor)
            .border(borderWidth, borderColor, RoundedCornerShape(8.dp))
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            singleLine = true,
            textStyle = MaterialTheme.typography.bodyLarge.copy(color = TextPrimary),
            cursorBrush = SolidColor(Accent),
            modifier = Modifier
                .fillMaxWidth()
                .onFocusChanged { focused = it.isFocused },
        )
        if (value.isEmpty()) {
            Text(placeholder, color = TextSecondary, style = MaterialTheme.typography.bodyLarge)
        }
    }
}
