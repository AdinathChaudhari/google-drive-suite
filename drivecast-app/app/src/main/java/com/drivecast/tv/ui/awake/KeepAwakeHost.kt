package com.drivecast.tv.ui.awake

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.scaleIn
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.repeatOnLifecycle
import com.drivecast.tv.LocalAppContainer
import com.drivecast.tv.ui.theme.MotionTokens
import com.drivecast.tv.ui.theme.Outline
import com.drivecast.tv.ui.theme.Scrim
import com.drivecast.tv.ui.theme.Surface as SurfaceColor
import com.drivecast.tv.ui.theme.TextPrimary
import com.drivecast.tv.ui.theme.TextSecondary
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.ceil
import androidx.tv.material3.Border
import androidx.tv.material3.Button
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.SurfaceDefaults
import androidx.tv.material3.Text

/**
 * Global "Are you still watching?" host. Polls the server's keep-awake status
 * while the app is foregrounded AND this device has started playback this
 * session, and shows a D-pad-focusable prompt over any screen when the server
 * enters its "prompt" window. Rendered once, above the nav host.
 *
 * @param onRequestExitPlayer invoked when the user answers "No" — the caller
 *   exits the player back to detail if the player screen is currently open.
 */
@Composable
fun KeepAwakeHost(onRequestExitPlayer: () -> Unit) {
    val container = LocalAppContainer.current
    val started by container.keepAwake.playbackStarted.collectAsStateWithLifecycle()
    val status by container.keepAwake.status.collectAsStateWithLifecycle()
    val lifecycleOwner = LocalLifecycleOwner.current
    val scope = rememberCoroutineScope()

    // Lifecycle-aware polling: repeatOnLifecycle cancels the loop when the app is
    // backgrounded and restarts it when foregrounded. Only runs once playback
    // has started this session.
    LaunchedEffect(started) {
        if (!started) return@LaunchedEffect
        lifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (isActive) {
                val s = container.keepAwake.poll()
                val secondsLeft = s?.secondsLeft ?: Double.MAX_VALUE
                val tighten = s?.phase == "prompt" ||
                    (s?.phase == "grace" && secondsLeft < 40.0)
                delay(if (tighten) 5_000L else 15_000L)
            }
        }
    }

    if (status?.phase == "prompt") {
        StillWatchingDialog(
            initialSeconds = status?.secondsLeft ?: 0.0,
            onYes = { scope.launch { container.keepAwake.extend() } },
            onNo = {
                scope.launch { container.keepAwake.release() }
                onRequestExitPlayer()
            },
        )
    }
}

/**
 * A real windowed dialog (not a plain scrim Box drawn inline): the platform window traps D-pad
 * focus so it can never walk into whatever screen is open behind it, and dismissOnBackPress gives
 * Back-cancel for free instead of Back falling through to the screen underneath.
 */
@Composable
private fun StillWatchingDialog(
    initialSeconds: Double,
    onYes: () -> Unit,
    onNo: () -> Unit,
) {
    // Live per-second countdown, re-synced whenever a fresh poll arrives.
    var remaining by remember { mutableStateOf(initialSeconds) }
    LaunchedEffect(initialSeconds) {
        remaining = initialSeconds
        while (remaining > 0.0) {
            delay(1_000)
            remaining -= 1.0
        }
    }

    val secondsText = ceil(remaining.coerceAtLeast(0.0)).toInt()

    Dialog(
        onDismissRequest = onNo,
        properties = DialogProperties(dismissOnBackPress = true, usePlatformDefaultWidth = false),
    ) {
        val visibleState = remember { MutableTransitionState(false) }
        LaunchedEffect(Unit) { visibleState.targetState = true }
        val yesFocus = remember { FocusRequester() }

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
                    // Inside the AnimatedVisibility content lambda: this LaunchedEffect only runs
                    // once "Yes, keep watching" below has actually composed, so requestFocus()
                    // succeeds immediately instead of throwing against a not-yet-attached target.
                    LaunchedEffect(Unit) { runCatching { yesFocus.requestFocus() } }

                    Surface(
                        shape = RoundedCornerShape(16.dp),
                        colors = SurfaceDefaults.colors(containerColor = SurfaceColor),
                        border = Border(
                            border = BorderStroke(1.dp, Outline),
                            shape = RoundedCornerShape(16.dp),
                        ),
                        modifier = Modifier.width(520.dp),
                    ) {
                        Column(Modifier.padding(28.dp)) {
                            Text(
                                "Are you still watching?",
                                style = MaterialTheme.typography.titleLarge,
                                color = TextPrimary,
                            )
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "The server will sleep in ${secondsText}s unless you keep watching.",
                                color = TextSecondary,
                            )
                            Spacer(Modifier.height(24.dp))
                            Row {
                                Button(onClick = onYes, modifier = Modifier.focusRequester(yesFocus)) {
                                    Text("Yes, keep watching")
                                }
                                Spacer(Modifier.width(12.dp))
                                Button(onClick = onNo) { Text("No") }
                            }
                        }
                    }
                }
            }
        }
    }
}
