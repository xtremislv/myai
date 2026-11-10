package com.techwiser.twai.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.techwiser.twai.data.ThemeMode

// The web app's "Executive Slate" palette (app/web/style.css), light and dark.
private val Light = lightColorScheme(
    primary = Color(0xFF0F172A),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE2E8F0),
    onPrimaryContainer = Color(0xFF0F172A),
    secondary = Color(0xFF2563EB),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFEFF6FF),
    onSecondaryContainer = Color(0xFF1E3A8A),
    tertiary = Color(0xFFB4783C),
    tertiaryContainer = Color(0xFFFDF6EE),
    onTertiaryContainer = Color(0xFF5B3A17),
    background = Color(0xFFF8FAFC),
    onBackground = Color(0xFF1E293B),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF1E293B),
    surfaceVariant = Color(0xFFF1F5F9),
    onSurfaceVariant = Color(0xFF64748B),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFF8FAFC),
    surfaceContainer = Color(0xFFF1F5F9),
    surfaceContainerHigh = Color(0xFFE9EEF5),
    surfaceContainerHighest = Color(0xFFE2E8F0),
    outline = Color(0xFFCBD5E1),
    outlineVariant = Color(0xFFE2E8F0),
    error = Color(0xFFBE123C),
    errorContainer = Color(0xFFFFF1F2),
    onErrorContainer = Color(0xFF881337),
)

private val Dark = darkColorScheme(
    primary = Color(0xFFE2E8F0),
    onPrimary = Color(0xFF0F172A),
    primaryContainer = Color(0xFF1E293B),
    onPrimaryContainer = Color(0xFFE2E8F0),
    secondary = Color(0xFF60A5FA),
    onSecondary = Color(0xFF0A0F1D),
    secondaryContainer = Color(0xFF172554),
    onSecondaryContainer = Color(0xFFDBEAFE),
    tertiary = Color(0xFFE0A86E),
    tertiaryContainer = Color(0xFF3B2A18),
    onTertiaryContainer = Color(0xFFFDE7CF),
    background = Color(0xFF0A0F1D),
    onBackground = Color(0xFFE2E8F0),
    surface = Color(0xFF111A2C),
    onSurface = Color(0xFFE2E8F0),
    surfaceVariant = Color(0xFF162036),
    onSurfaceVariant = Color(0xFF94A3B8),
    surfaceContainerLowest = Color(0xFF0A0F1D),
    surfaceContainerLow = Color(0xFF0E1424),
    surfaceContainer = Color(0xFF111A2C),
    surfaceContainerHigh = Color(0xFF162036),
    surfaceContainerHighest = Color(0xFF1C2842),
    outline = Color(0xFF334155),
    outlineVariant = Color(0xFF1E293B),
    error = Color(0xFFFB7185),
    errorContainer = Color(0xFF4C0519),
    onErrorContainer = Color(0xFFFFE4E6),
)

/** Colours the Material scheme has no slot for. */
@Immutable
data class Extra(val good: Color, val warning: Color, val mine: Color, val series: List<Color>)

private val LightExtra = Extra(
    good = Color(0xFF15803D), warning = Color(0xFFB45309), mine = Color(0xFFE2E8F0),
    series = listOf(Color(0xFF1E2A44), Color(0xFF2563EB), Color(0xFFB4783C), Color(0xFF0D9488),
        Color(0xFF7C3AED), Color(0xFFDB2777), Color(0xFF65A30D), Color(0xFF64748B)),
)
private val DarkExtra = Extra(
    good = Color(0xFF4ADE80), warning = Color(0xFFFBBF24), mine = Color(0xFF1C2842),
    series = listOf(Color(0xFF93C5FD), Color(0xFF60A5FA), Color(0xFFE0A86E), Color(0xFF2DD4BF),
        Color(0xFFA78BFA), Color(0xFFF472B6), Color(0xFFA3E635), Color(0xFF94A3B8)),
)

val LocalExtra = staticCompositionLocalOf { LightExtra }

private val Serif = FontFamily.Serif

private val Type = Typography().let { t ->
    t.copy(
        headlineLarge = t.headlineLarge.copy(fontFamily = Serif, fontWeight = FontWeight.Medium),
        headlineMedium = t.headlineMedium.copy(fontFamily = Serif, fontWeight = FontWeight.Medium),
        headlineSmall = t.headlineSmall.copy(fontFamily = Serif, fontWeight = FontWeight.Medium),
        titleLarge = t.titleLarge.copy(fontFamily = Serif, fontWeight = FontWeight.Medium),
    )
}

val Mono = TextStyle(fontFamily = FontFamily.Monospace, fontSize = 13.sp)

@Composable
fun TwTheme(mode: ThemeMode, content: @Composable () -> Unit) {
    val dark = when (mode) {
        ThemeMode.SYSTEM -> isSystemInDarkTheme()
        ThemeMode.LIGHT -> false
        ThemeMode.DARK -> true
    }
    androidx.compose.runtime.CompositionLocalProvider(LocalExtra provides if (dark) DarkExtra else LightExtra) {
        MaterialTheme(colorScheme = if (dark) Dark else Light, typography = Type, content = content)
    }
}
