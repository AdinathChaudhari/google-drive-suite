package com.drivecast.tv.ui.setup

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutLinearInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Home
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.drivecast.tv.LocalAppContainer
import com.drivecast.tv.data.DiscoveredServer
import com.drivecast.tv.ui.common.StatusView
import com.drivecast.tv.ui.theme.Accent
import com.drivecast.tv.ui.theme.Background
import com.drivecast.tv.ui.theme.ErrorRed
import com.drivecast.tv.ui.theme.MotionTokens
import com.drivecast.tv.ui.theme.SurfaceBright
import com.drivecast.tv.ui.theme.SurfaceVariant
import com.drivecast.tv.ui.theme.TextPrimary
import com.drivecast.tv.ui.theme.TextSecondary
import kotlinx.coroutines.launch
import androidx.tv.material3.Button
import androidx.tv.material3.Icon
import androidx.tv.material3.ListItem
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Surface
import androidx.tv.material3.Text

private enum class SetupStep { DISCOVER, TOKEN }

@Composable
fun SetupScreen(onPaired: () -> Unit) {
    val container = LocalAppContainer.current
    val scope = rememberCoroutineScope()

    var step by remember { mutableStateOf(SetupStep.DISCOVER) }
    var scanning by remember { mutableStateOf(true) }
    var discovered by remember { mutableStateOf<List<DiscoveredServer>>(emptyList()) }
    var manualIp by remember { mutableStateOf("") }
    var manualIpError by remember { mutableStateOf<String?>(null) }
    var baseUrl by remember { mutableStateOf<String?>(null) }
    var token by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    // Field-level errors that stay inline (fixed-height slot, no navigation away from the step).
    var tokenError by remember { mutableStateOf<String?>(null) }
    // A pairing failure that makes the whole step unusable until the server is reachable again
    // gets the full StatusView treatment instead of an inline message.
    var pairNetworkError by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        scanning = true
        discovered = runCatching { container.discovery.scan() }.getOrDefault(emptyList())
        scanning = false
    }

    fun connectManual() {
        val ip = manualIp.trim()
        if (ip.isEmpty()) return
        busy = true
        manualIpError = null
        scope.launch {
            val server = container.discovery.probe(ip)
            busy = false
            if (server != null) {
                baseUrl = server.baseUrl
                tokenError = null
                pairNetworkError = null
                step = SetupStep.TOKEN
            } else {
                manualIpError = "No drivecast server answered at $ip:8737."
            }
        }
    }

    fun pair() {
        val base = baseUrl ?: return
        val t = token.trim()
        if (t.isEmpty()) {
            tokenError = "Enter the access token from the drivecast Remote Access screen."
            return
        }
        busy = true
        tokenError = null
        pairNetworkError = null
        scope.launch {
            container.repository.configure(base, t)
            val result = runCatching { container.repository.validateRemote() }
            busy = false
            result.onSuccess { resp ->
                when {
                    resp.isSuccessful -> {
                        container.configStore.save(base, t)
                        onPaired()
                    }
                    resp.code() == 403 ->
                        tokenError = "Remote access is off. Turn it on in drivecast's Remote Access settings."
                    resp.code() == 401 -> tokenError = "Wrong token. Check the code in drivecast and re-enter it."
                    else -> tokenError = "Server rejected the pairing (HTTP ${resp.code()})."
                }
            }.onFailure {
                pairNetworkError = "Couldn't reach $base. Is the server running on this network?"
            }
        }
    }

    Surface(
        modifier = Modifier.fillMaxSize(),
        colors = androidx.tv.material3.SurfaceDefaults.colors(containerColor = Background),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 48.dp, vertical = 27.dp),
        ) {
            Text("drivecast", style = MaterialTheme.typography.displaySmall, color = Accent)
            Spacer(Modifier.height(4.dp))
            Text(
                when (step) {
                    SetupStep.DISCOVER -> "Find your server"
                    SetupStep.TOKEN -> "Enter the access token"
                },
                style = MaterialTheme.typography.titleLarge,
                color = TextPrimary,
            )
            Spacer(Modifier.height(24.dp))

            AnimatedContent(
                targetState = step,
                transitionSpec = {
                    (
                        fadeIn(tween(210, delayMillis = 90, easing = LinearOutSlowInEasing)) +
                            scaleIn(
                                initialScale = 0.96f,
                                animationSpec = tween(210, delayMillis = 90, easing = LinearOutSlowInEasing),
                            )
                        ) togetherWith fadeOut(tween(90, easing = FastOutLinearInEasing))
                },
                label = "setupStep",
            ) { currentStep ->
                when (currentStep) {
                    SetupStep.DISCOVER -> DiscoverStep(
                        scanning = scanning,
                        discovered = discovered,
                        manualIp = manualIp,
                        manualIpError = manualIpError,
                        busy = busy,
                        onManualIpChange = { manualIp = it },
                        onPick = { server ->
                            baseUrl = server.baseUrl
                            tokenError = null
                            pairNetworkError = null
                            manualIpError = null
                            step = SetupStep.TOKEN
                        },
                        onConnectManual = ::connectManual,
                        onRescan = {
                            scope.launch {
                                scanning = true
                                discovered = runCatching { container.discovery.scan() }.getOrDefault(emptyList())
                                scanning = false
                            }
                        },
                    )

                    SetupStep.TOKEN -> TokenStep(
                        baseUrl = baseUrl.orEmpty(),
                        token = token,
                        busy = busy,
                        tokenError = tokenError,
                        networkError = pairNetworkError,
                        onTokenChange = { token = it },
                        onPair = ::pair,
                        onBack = {
                            step = SetupStep.DISCOVER
                            tokenError = null
                            pairNetworkError = null
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun DiscoverStep(
    scanning: Boolean,
    discovered: List<DiscoveredServer>,
    manualIp: String,
    manualIpError: String?,
    busy: Boolean,
    onManualIpChange: (String) -> Unit,
    onPick: (DiscoveredServer) -> Unit,
    onConnectManual: () -> Unit,
    onRescan: () -> Unit,
) {
    val firstServerFocus = remember { FocusRequester() }
    val manualIpFocus = remember { FocusRequester() }

    // Initial focus: the first discovered server once a scan lands results, else the manual
    // IP field — the D-pad is never left with nothing focused after scanning finishes.
    LaunchedEffect(scanning, discovered) {
        if (!scanning) {
            runCatching {
                if (discovered.isNotEmpty()) firstServerFocus.requestFocus() else manualIpFocus.requestFocus()
            }
        }
    }

    if (scanning) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            InlineSpinner(modifier = Modifier.size(28.dp), strokeWidth = 2.5.dp)
            Spacer(Modifier.width(12.dp))
            Text("Scanning your network…", color = TextSecondary)
        }
        return
    }

    if (discovered.isNotEmpty()) {
        Text("Servers found", color = TextSecondary, style = MaterialTheme.typography.labelMedium)
        Spacer(Modifier.height(8.dp))
        LazyColumn(
            verticalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.fillMaxWidth().heightIn(max = 260.dp),
        ) {
            itemsIndexed(discovered, key = { _, server -> server.ip }) { index, server ->
                ListItem(
                    selected = false,
                    onClick = { onPick(server) },
                    headlineContent = { Text(server.ip) },
                    supportingContent = { Text("port ${server.port}") },
                    leadingContent = {
                        Icon(imageVector = Icons.Default.Home, contentDescription = null, tint = Accent)
                    },
                    modifier = Modifier
                        .fillMaxWidth()
                        .let { if (index == 0) it.focusRequester(firstServerFocus) else it },
                )
            }
        }
        Spacer(Modifier.height(16.dp))
    } else {
        Text("No servers found automatically.", color = TextSecondary)
        Spacer(Modifier.height(16.dp))
    }

    Text("Or enter the server IP", color = TextSecondary, style = MaterialTheme.typography.labelMedium)
    Spacer(Modifier.height(8.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        TvTextField(
            value = manualIp,
            onValueChange = onManualIpChange,
            placeholder = "192.168.1.50",
            keyboardType = KeyboardType.Uri,
            modifier = Modifier.width(280.dp),
            focusRequester = manualIpFocus,
        )
        Spacer(Modifier.width(12.dp))
        Button(onClick = onConnectManual, enabled = !busy) { Text("Connect") }
        Spacer(Modifier.width(12.dp))
        Button(onClick = onRescan, enabled = !busy) { Text("Rescan") }
    }
    FieldError(manualIpError)
}

@Composable
private fun TokenStep(
    baseUrl: String,
    token: String,
    busy: Boolean,
    tokenError: String?,
    networkError: String?,
    onTokenChange: (String) -> Unit,
    onPair: () -> Unit,
    onBack: () -> Unit,
) {
    if (networkError != null) {
        // A pairing failure this total isn't an inline, edit-and-retry moment — the server
        // itself is unreachable, so this is a full StatusView with a focused Retry.
        StatusView(
            title = "Can't reach your server",
            body = networkError,
            primaryLabel = "Retry",
            onPrimary = onPair,
            secondaryLabel = "Change server",
            onSecondary = onBack,
        )
        return
    }

    Text(baseUrl, color = TextSecondary, style = MaterialTheme.typography.bodyMedium)
    Spacer(Modifier.height(16.dp))
    TvTextField(
        value = token,
        onValueChange = onTokenChange,
        placeholder = "Access token",
        keyboardType = KeyboardType.Password,
        visualTransformation = PasswordVisualTransformation(),
        modifier = Modifier.width(360.dp),
    )
    FieldError(tokenError)
    Spacer(Modifier.height(4.dp))
    Row {
        Button(onClick = onPair, enabled = !busy) {
            if (busy) {
                InlineSpinner(modifier = Modifier.size(16.dp))
                Spacer(Modifier.width(8.dp))
            }
            Text("Pair")
        }
        Spacer(Modifier.width(12.dp))
        Button(onClick = onBack, enabled = !busy) { Text("Back") }
    }
}

/** Reserves a fixed-height slot under a field so an appearing/disappearing error never shifts layout. */
@Composable
private fun FieldError(message: String?) {
    Box(modifier = Modifier.height(24.dp)) {
        if (message != null) {
            Text(message, color = ErrorRed, style = MaterialTheme.typography.labelMedium)
        }
    }
}

/** A small rotating arc — the app's non-mobile-material busy indicator (Canvas + drawArc only). */
@Composable
private fun InlineSpinner(
    modifier: Modifier = Modifier,
    strokeWidth: Dp = 2.dp,
    color: Color = Accent,
) {
    val transition = rememberInfiniteTransition(label = "inlineSpinner")
    val angle by transition.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(900, easing = LinearEasing)),
        label = "inlineSpinnerAngle",
    )
    Canvas(modifier = modifier.graphicsLayer { rotationZ = angle }) {
        drawArc(
            color = color,
            startAngle = -90f,
            sweepAngle = 270f,
            useCenter = false,
            style = Stroke(width = strokeWidth.toPx(), cap = StrokeCap.Round),
        )
    }
}

/**
 * A focusable text field (foundation only) suited to D-pad + on-screen keyboard. The focused
 * state is unmistakable: border color/width and container both animate on focus instead of
 * a permanent, focus-blind Accent outline.
 */
@Composable
private fun TvTextField(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    keyboardType: KeyboardType,
    modifier: Modifier = Modifier,
    visualTransformation: VisualTransformation = VisualTransformation.None,
    focusRequester: FocusRequester? = null,
) {
    var focused by remember { mutableStateOf(false) }
    val borderColor by animateColorAsState(
        targetValue = if (focused) Accent else SurfaceVariant,
        animationSpec = tween(MotionTokens.DurationShort, easing = MotionTokens.Emphasized),
        label = "fieldBorderColor",
    )
    // SurfaceBright is the theme's own "focused-container lift" token (WI-05); using it here
    // instead of the spec's literal "Surface" (which is actually darker than SurfaceVariant in
    // this app's tonal ladder — see deviations) keeps the container genuinely lifting on focus.
    val containerColor by animateColorAsState(
        targetValue = if (focused) SurfaceBright else SurfaceVariant,
        animationSpec = tween(MotionTokens.DurationShort, easing = MotionTokens.Emphasized),
        label = "fieldContainerColor",
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
            keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
            visualTransformation = visualTransformation,
            modifier = Modifier
                .fillMaxWidth()
                .onFocusChanged { focused = it.isFocused }
                .let { if (focusRequester != null) it.focusRequester(focusRequester) else it },
        )
        if (value.isEmpty()) {
            Text(placeholder, color = TextSecondary, style = MaterialTheme.typography.bodyLarge)
        }
    }
}
