package com.drivecast.tv.ui.common

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.dp
import com.drivecast.tv.ui.theme.SurfaceVariant

/**
 * A slow left-to-right shimmer sweep for skeleton loading placeholders. Dark-theme alphas
 * only (0.06/0.14) — do NOT reuse light-theme values elsewhere.
 */
fun Modifier.shimmer(): Modifier = composed {
    val transition = rememberInfiniteTransition(label = "shimmer")
    val x by transition.animateFloat(
        initialValue = 0f,
        targetValue = 2f,
        animationSpec = infiniteRepeatable(
            animation = tween(1200, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "shimmerX",
    )
    drawWithContent {
        drawContent()
        drawRect(
            brush = Brush.linearGradient(
                colors = listOf(
                    Color.White.copy(alpha = 0.06f),
                    Color.White.copy(alpha = 0.14f),
                    Color.White.copy(alpha = 0.06f),
                ),
                start = Offset(size.width * (x - 1f), 0f),
                end = Offset(size.width * x, size.height),
            ),
        )
    }
}

/**
 * A placeholder box at the final layout dimensions of the real content it stands in for.
 * Cap LIVE (animated) shimmer at ~2 rows on screen and pass animated=false below the fold —
 * an infinite animation competes with first-frame work on Stick GPUs.
 */
@Composable
fun SkeletonBox(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(10.dp),
    animated: Boolean = true,
) {
    Box(
        modifier = modifier
            .clip(shape)
            .background(SurfaceVariant)
            .let { if (animated) it.shimmer() else it },
    )
}
