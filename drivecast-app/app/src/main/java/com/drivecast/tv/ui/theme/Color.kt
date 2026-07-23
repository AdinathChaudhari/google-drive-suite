package com.drivecast.tv.ui.theme

import androidx.compose.ui.graphics.Color

val Background = Color(0xFF0E1116)
val Surface = Color(0xFF171B22)
val SurfaceVariant = Color(0xFF232935)
val Accent = Color(0xFF3B82F6)
val OnAccent = Color(0xFFFFFFFF)
val TextPrimary = Color(0xFFE5E7EB)
val TextSecondary = Color(0xFF9CA3AF)
val ErrorRed = Color(0xFFEF4444)

// Tonal ladder + shared overlay tokens (theme pass). Later items migrate their hard-coded
// literals onto these.
val SurfaceBright = Color(0xFF2A3140) // focused-container lift
val SurfaceDim = Color(0xFF1A1F29)
val Outline = Color(0x14FFFFFF) // hairlines / borders
val Scrim = Color(0xCC0E1116) // Background-tinted scrim, replaces hard-coded 0xCC000000 literals
