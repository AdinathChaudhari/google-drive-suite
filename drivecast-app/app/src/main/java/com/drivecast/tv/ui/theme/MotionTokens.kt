package com.drivecast.tv.ui.theme

import androidx.compose.animation.core.CubicBezierEasing

/**
 * The app's single motion vocabulary. Every animation elsewhere imports its duration/easing
 * from here instead of inlining magic numbers, so the whole app reads as one system.
 */
object MotionTokens {
    val Emphasized = CubicBezierEasing(0.2f, 0f, 0f, 1f)
    val EmphasizedDecelerate = CubicBezierEasing(0.05f, 0.7f, 0.1f, 1f) // entrances
    val EmphasizedAccelerate = CubicBezierEasing(0.3f, 0f, 0.8f, 0.15f) // exits
    val StandardDecelerate = CubicBezierEasing(0f, 0f, 0.2f, 1f)

    const val DurationShort = 200
    const val DurationMedium = 300
    const val DurationLong = 500
}
