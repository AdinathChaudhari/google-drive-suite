package com.drivecast.tv.ui.common

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.compose.AsyncImagePainter
import coil.request.ImageRequest
import com.drivecast.tv.LocalAppContainer
import com.drivecast.tv.ui.theme.Accent
import com.drivecast.tv.ui.theme.SurfaceBright
import com.drivecast.tv.ui.theme.SurfaceDim
import com.drivecast.tv.ui.theme.SurfaceVariant
import androidx.tv.material3.Border
import androidx.tv.material3.Card
import androidx.tv.material3.CardDefaults
import androidx.tv.material3.Glow
import androidx.tv.material3.LocalContentColor
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text

/** A gradient tile with the title centred — the poster fallback. */
@Composable
fun PosterFallback(title: String, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier.background(
            Brush.linearGradient(
                listOf(SurfaceBright, SurfaceVariant, SurfaceDim)
            )
        ),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = title,
            style = MaterialTheme.typography.titleMedium,
            color = LocalContentColor.current,
            textAlign = TextAlign.Center,
            maxLines = 3,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(12.dp),
        )
    }
}

/**
 * A focusable poster card. Loads the poster via the shared Coil loader (token
 * added on the wire); falls back to a gradient tile with the title when there's
 * no poster or the image fails to load.
 */
@Composable
fun PosterCard(
    title: String,
    posterUrl: String?,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    widthDp: Dp = 150.dp,
    aspect: Float = 2f / 3f,
    onFocused: (() -> Unit)? = null,
    overlay: @Composable BoxScope.() -> Unit = {},
) {
    val imageLoader = LocalAppContainer.current.imageLoader
    var failed by remember(posterUrl) { mutableStateOf(false) }

    Card(
        onClick = onClick,
        // The app's signature focus trio: size-graded scale + white border + tinted glow.
        // Glow silently no-ops below API 28 (minSdk is 25), so the white border is the
        // guaranteed indicator across every device — never ship scale alone.
        scale = CardDefaults.scale(focusedScale = 1.06f, pressedScale = 0.95f),
        border = CardDefaults.border(
            focusedBorder = Border(
                border = BorderStroke(2.5.dp, Color.White.copy(alpha = 0.85f)),
                inset = 3.dp,
                shape = RoundedCornerShape(10.dp),
            ),
        ),
        glow = CardDefaults.glow(
            focusedGlow = Glow(elevationColor = Accent.copy(alpha = 0.35f), elevation = 10.dp),
        ),
        modifier = modifier
            .width(widthDp)
            .onFocusChanged { if (it.isFocused) onFocused?.invoke() },
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .aspectRatio(aspect),
        ) {
            if (posterUrl != null && !failed) {
                val context = LocalContext.current
                val density = LocalDensity.current
                // Fire TV always renders the 960x540dp logical canvas at 2x density,
                // so request the poster at its actual on-screen pixel size instead of
                // the server-original resolution — the #1 named cause of GC stutter
                // and OOM on 1GB Sticks.
                val (wPx, hPx) = with(density) {
                    widthDp.roundToPx() to (widthDp / aspect).roundToPx()
                }
                val request = remember(posterUrl, wPx, hPx) {
                    ImageRequest.Builder(context)
                        .data(posterUrl)
                        .size(wPx, hPx)
                        .crossfade(200)
                        .build()
                }
                AsyncImage(
                    model = request,
                    imageLoader = imageLoader,
                    contentDescription = title,
                    contentScale = ContentScale.Crop,
                    onState = { state ->
                        if (state is AsyncImagePainter.State.Error) failed = true
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                PosterFallback(title = title, modifier = Modifier.fillMaxSize())
            }
            overlay()
        }
    }
}
