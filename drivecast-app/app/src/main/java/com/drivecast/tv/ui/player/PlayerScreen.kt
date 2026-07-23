package com.drivecast.tv.ui.player

import android.app.Application
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.view.ViewGroup
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.foundation.focusGroup
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.PlayerView
import com.drivecast.tv.LocalAppContainer
import com.drivecast.tv.ui.common.PosterCard
import com.drivecast.tv.ui.theme.Accent
import com.drivecast.tv.ui.theme.MotionTokens
import com.drivecast.tv.ui.theme.OnAccent
import com.drivecast.tv.ui.theme.Scrim
import com.drivecast.tv.ui.theme.Surface as SurfaceColor
import com.drivecast.tv.ui.theme.TextPrimary
import com.drivecast.tv.ui.theme.TextSecondary
import kotlinx.coroutines.flow.StateFlow
import androidx.tv.material3.Button
import androidx.tv.material3.ButtonDefaults
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.SurfaceDefaults
import androidx.tv.material3.Text

private const val VLC_PACKAGE = "org.videolan.vlc"

private fun isVlcInstalled(context: android.content.Context): Boolean =
    try {
        context.packageManager.getPackageInfo(VLC_PACKAGE, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }

/**
 * Playback entry point. Hands off to VLC when it's installed (better codec
 * coverage than this device's hardware decoder); otherwise, or if the hand-off
 * turns out to be impossible, falls back to the internal ExoPlayer.
 */
@UnstableApi
@Composable
fun PlayerScreen(
    titleId: String,
    fileId: String,
    startOver: Boolean,
    shuffle: Boolean,
    seed: Long,
    onExit: () -> Unit,
) {
    val context = LocalContext.current
    var useVlc by remember { mutableStateOf(isVlcInstalled(context)) }

    if (useVlc) {
        VlcPlayerHost(
            titleId = titleId,
            fileId = fileId,
            startOver = startOver,
            shuffle = shuffle,
            seed = seed,
            onExit = onExit,
            onVlcUnavailable = { useVlc = false },
        )
    } else {
        InternalPlayerScreen(titleId, fileId, startOver, shuffle, seed, onExit)
    }
}

// ---- External (VLC) hand-off ----

@Composable
private fun VlcPlayerHost(
    titleId: String,
    fileId: String,
    startOver: Boolean,
    shuffle: Boolean,
    seed: Long,
    onExit: () -> Unit,
    onVlcUnavailable: () -> Unit,
) {
    val container = LocalAppContainer.current
    val vm: ExternalPlayerViewModel = viewModel(
        factory = ExternalPlayerViewModel.factory(container, titleId, fileId, startOver, shuffle, seed)
    )
    val state by vm.ui.collectAsStateWithLifecycle()

    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val data = result.data
        val position = data?.getLongExtra("extra_position", -1L) ?: -1L
        val duration = data?.getLongExtra("extra_duration", -1L) ?: -1L
        vm.onVlcResult(position, duration)
    }

    LaunchedEffect(Unit) {
        vm.launches.collect { req ->
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setPackage(VLC_PACKAGE)
                setDataAndType(Uri.parse(req.uri), "video/*")
                putExtra("title", req.title)
                putExtra("from_start", req.fromStart)
                if (!req.fromStart && req.positionMs > 0) putExtra("position", req.positionMs)
                req.subtitleUrl?.let { putExtra("subtitles_location", it) }
            }
            try {
                launcher.launch(intent)
                vm.onVlcLaunched()
            } catch (_: ActivityNotFoundException) {
                // VLC vanished between the install check and the launch. Fall
                // back to the internal player if nothing has played yet.
                if (!state.playing) onVlcUnavailable() else vm.onLaunchFailed()
            }
        }
    }

    LaunchedEffect(state.finished) {
        if (state.finished) onExit()
    }

    Surface(
        modifier = Modifier.fillMaxSize(),
        colors = SurfaceDefaults.colors(containerColor = Color.Black),
    ) {
        Box(Modifier.fillMaxSize()) {
            when {
                state.error != null -> ExternalMessage(
                    message = state.error!!,
                    onExit = onExit,
                    modifier = Modifier.align(Alignment.Center),
                )
                else -> PlayingInVlc(
                    title = state.nowPlayingTitle,
                    modifier = Modifier.align(Alignment.Center),
                )
            }

            AnimatedVisibility(
                visible = state.upNextVisible && state.error == null,
                modifier = Modifier.align(Alignment.BottomEnd),
                enter = slideInVertically(
                    initialOffsetY = { it / 3 },
                    animationSpec = tween(300, easing = MotionTokens.EmphasizedDecelerate),
                ) + fadeIn(tween(300)),
                exit = fadeOut(tween(150)),
            ) {
                UpNextCard(
                    nextTitle = state.nextTitle ?: "Next",
                    posterUrl = state.nextPosterUrl,
                    remaining = vm.upNextRemaining,
                    onPlayNow = { vm.playNextNow() },
                    onCancel = { vm.cancelUpNext() },
                )
            }
        }
    }
}

@Composable
private fun PlayingInVlc(title: String?, modifier: Modifier = Modifier) {
    Column(modifier = modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Text("drivecast", color = Accent, style = MaterialTheme.typography.displaySmall)
        Spacer(Modifier.height(20.dp))
        Text("Playing in VLC…", color = TextPrimary, style = MaterialTheme.typography.bodyLarge)
        title?.let {
            Spacer(Modifier.height(6.dp))
            Text(it, color = TextSecondary, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun ExternalMessage(message: String, onExit: () -> Unit, modifier: Modifier = Modifier) {
    Column(modifier = modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Text(message, color = TextPrimary, style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(20.dp))
        Button(onClick = onExit) { Text("Back") }
    }
}

// ---- Internal (ExoPlayer) player ----

@UnstableApi
@Composable
private fun InternalPlayerScreen(
    titleId: String,
    fileId: String,
    startOver: Boolean,
    shuffle: Boolean,
    seed: Long,
    onExit: () -> Unit,
) {
    val container = LocalAppContainer.current
    val app = LocalContext.current.applicationContext as Application
    val vm: PlayerViewModel = viewModel(
        factory = PlayerViewModel.factory(app, container, titleId, fileId, startOver, shuffle, seed)
    )
    val state by vm.ui.collectAsStateWithLifecycle()

    DisposableEffect(Unit) {
        onDispose { vm.reportStop() }
    }

    // The Compose overlay (up-next card / error retry) and the Media3 PlayerView's
    // own controller must never fight for the D-pad at the same time — otherwise
    // the up-next countdown can expire while presses land on the controller
    // underneath. Unfocus the PlayerView whenever a Compose overlay is showing.
    val overlayVisible = state.error != null || state.upNextVisible

    Surface(
        modifier = Modifier.fillMaxSize(),
        colors = SurfaceDefaults.colors(containerColor = Color.Black),
    ) {
        Box(Modifier.fillMaxSize()) {
            AndroidView(
                factory = { ctx ->
                    PlayerView(ctx).apply {
                        player = vm.player
                        useController = true
                        keepScreenOn = true
                        setShowSubtitleButton(true)
                        layoutParams = ViewGroup.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.MATCH_PARENT,
                        )
                    }
                },
                update = { view -> view.isFocusable = !overlayVisible },
                modifier = Modifier.fillMaxSize(),
            )

            state.error?.let { message ->
                ErrorOverlay(
                    message = message,
                    retriable = state.errorRetriable,
                    onRetry = { vm.retry() },
                    onExit = onExit,
                )
            }

            AnimatedVisibility(
                visible = state.upNextVisible && state.error == null,
                modifier = Modifier.align(Alignment.BottomEnd),
                enter = slideInVertically(
                    initialOffsetY = { it / 3 },
                    animationSpec = tween(300, easing = MotionTokens.EmphasizedDecelerate),
                ) + fadeIn(tween(300)),
                exit = fadeOut(tween(150)),
            ) {
                UpNextCard(
                    nextTitle = state.nextTitle ?: "Next",
                    posterUrl = state.nextPosterUrl,
                    remaining = vm.upNextRemaining,
                    onPlayNow = { vm.playNextNow() },
                    onCancel = { vm.cancelUpNext() },
                )
            }
        }
    }
}

@Composable
private fun ErrorOverlay(
    message: String,
    retriable: Boolean,
    onRetry: () -> Unit,
    onExit: () -> Unit,
) {
    val visibleState = remember { MutableTransitionState(false) }
    LaunchedEffect(Unit) { visibleState.targetState = true }

    // Guarantee a focused action even when there's no Retry: an error is never a
    // dead end for the D-pad.
    val primaryFocus = remember { FocusRequester() }

    AnimatedVisibility(
        visibleState = visibleState,
        enter = slideInVertically(
            initialOffsetY = { it / 3 },
            animationSpec = tween(300, easing = MotionTokens.EmphasizedDecelerate),
        ) + fadeIn(tween(300)),
        exit = fadeOut(tween(150)),
    ) {
        // Inside the AnimatedVisibility content lambda: this LaunchedEffect only runs once the
        // Retry/Back button below has actually composed, so requestFocus() succeeds immediately
        // instead of throwing against a not-yet-attached target.
        LaunchedEffect(Unit) { runCatching { primaryFocus.requestFocus() } }

        Box(
            modifier = Modifier.fillMaxSize().background(Scrim),
            contentAlignment = Alignment.Center,
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    message,
                    color = TextPrimary,
                    style = MaterialTheme.typography.titleMedium,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(horizontal = 48.dp).widthIn(max = 640.dp),
                )
                Spacer(Modifier.height(20.dp))
                Row {
                    if (retriable) {
                        Button(onClick = onRetry, modifier = Modifier.focusRequester(primaryFocus)) {
                            Text("Retry")
                        }
                        Spacer(Modifier.width(12.dp))
                        Button(onClick = onExit) { Text("Back") }
                    } else {
                        Button(onClick = onExit, modifier = Modifier.focusRequester(primaryFocus)) {
                            Text("Back")
                        }
                    }
                }
            }
        }
    }
}

/**
 * The one up-next design shared by the internal (ExoPlayer) and external (VLC)
 * hosts. [remaining] is collected only inside [CountdownRing] — a leaf far below
 * this composable — so the 1Hz tick never recomposes the card, the poster
 * thumbnail, or the buttons around it.
 */
@Composable
private fun UpNextCard(
    nextTitle: String,
    posterUrl: String?,
    remaining: StateFlow<Int?>,
    onPlayNow: () -> Unit,
    onCancel: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val playNowFocus = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { playNowFocus.requestFocus() } }

    Surface(
        shape = RoundedCornerShape(12.dp),
        colors = SurfaceDefaults.colors(containerColor = SurfaceColor.copy(alpha = 0.95f)),
        modifier = modifier
            .padding(48.dp)
            .width(420.dp)
            .focusGroup(),
    ) {
        Row(
            modifier = Modifier.padding(20.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            PosterCard(
                title = nextTitle,
                posterUrl = posterUrl,
                onClick = {},
                widthDp = 80.dp,
                modifier = Modifier.focusProperties { canFocus = false },
            )
            Spacer(Modifier.width(16.dp))
            Column(Modifier.weight(1f)) {
                Text("Up next", color = TextSecondary, style = MaterialTheme.typography.labelMedium)
                Spacer(Modifier.height(4.dp))
                Text(
                    nextTitle,
                    color = TextPrimary,
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 2,
                )
                Spacer(Modifier.height(12.dp))
                Row {
                    Button(
                        onClick = onPlayNow,
                        colors = ButtonDefaults.colors(containerColor = Accent, contentColor = OnAccent),
                        modifier = Modifier.focusRequester(playNowFocus),
                    ) { Text("Play now") }
                    Spacer(Modifier.width(12.dp))
                    Button(onClick = onCancel) { Text("Cancel") }
                }
            }
            Spacer(Modifier.width(16.dp))
            CountdownRing(remaining)
        }
    }
}

/** The only node that collects the 1Hz countdown — a smooth draining ring, not a jumping digit. */
@Composable
private fun CountdownRing(remaining: StateFlow<Int?>, modifier: Modifier = Modifier) {
    val remainingValue by remaining.collectAsStateWithLifecycle()
    val progress by animateFloatAsState(
        targetValue = (remainingValue ?: 0) / 5f,
        animationSpec = tween(1_000, easing = LinearEasing),
        label = "upNextCountdown",
    )
    Box(modifier = modifier.size(40.dp), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            drawArc(
                color = Accent,
                startAngle = -90f,
                sweepAngle = 360f * progress,
                useCenter = false,
                style = Stroke(width = 3.dp.toPx(), cap = StrokeCap.Round),
            )
        }
        Text("${remainingValue ?: 0}", color = TextPrimary, style = MaterialTheme.typography.labelMedium)
    }
}
