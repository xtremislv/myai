package com.techwiser.twai.ui.theme

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import com.techwiser.twai.R
import com.techwiser.twai.data.ThemeMode

// The web app's "Executive Slate" design (app/web/style.css), light and dark: same colours, the same
// bundled fonts (Newsreader for headings, Geist for text, JetBrains Mono for labels and code), outlined
// cards with a 12 dp radius and 8 dp controls. Keep the two in step when either changes.

// ---- fonts (res/font: converted from app/web/fonts; variable fonts, one file per family) ----
@OptIn(ExperimentalTextApi::class)
private fun variable(res: Int, weight: Int, style: FontStyle = FontStyle.Normal) =
    Font(res, FontWeight(weight), style, variationSettings = FontVariation.Settings(FontVariation.weight(weight)))

val Serif = FontFamily(
    variable(R.font.newsreader, 400), variable(R.font.newsreader, 500), variable(R.font.newsreader, 600),
    variable(R.font.newsreader_italic, 400, FontStyle.Italic), variable(R.font.newsreader_italic, 500, FontStyle.Italic),
)
val Sans = FontFamily(
    variable(R.font.geist, 400), variable(R.font.geist, 500), variable(R.font.geist, 600), variable(R.font.geist, 700),
)
val MonoFamily = FontFamily(variable(R.font.jetbrains_mono, 400), variable(R.font.jetbrains_mono, 500), variable(R.font.jetbrains_mono, 600))

// ---- colours ----
private val Light = lightColorScheme(
    primary = Color(0xFF0F172A),            // --primary
    onPrimary = Color.White,
    primaryContainer = Color(0xFFF1F5F9),   // --sunk
    onPrimaryContainer = Color(0xFF0F172A),
    secondary = Color(0xFF2563EB),          // --pine
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFEFF6FF), // --pine-tint
    onSecondaryContainer = Color(0xFF1E3A8A),
    tertiary = Color(0xFFB4783C),           // --bronze
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFFDF6EE),
    onTertiaryContainer = Color(0xFF5B3A17),
    background = Color(0xFFF8FAFC),         // --page
    onBackground = Color(0xFF1E293B),       // --ink
    surface = Color(0xFFFFFFFF),            // --raised
    onSurface = Color(0xFF1E293B),
    surfaceVariant = Color(0xFFF1F5F9),
    onSurfaceVariant = Color(0xFF64748B),   // --ink-2
    surfaceTint = Color(0xFFFFFFFF),        // no Material tint on raised surfaces: the web app has none
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFFFFFFF),
    surfaceContainer = Color(0xFFFFFFFF),   // bottom bar, menus: white like the web header
    surfaceContainerHigh = Color(0xFFFFFFFF), // dialogs
    surfaceContainerHighest = Color(0xFFF1F5F9), // text fields, chips
    surfaceBright = Color(0xFFFFFFFF),
    surfaceDim = Color(0xFFF1F5F9),
    outline = Color(0xFFCBD5E1),            // --line-strong
    outlineVariant = Color(0xFFE2E8F0),     // --line
    error = Color(0xFFBE123C),
    onError = Color.White,
    errorContainer = Color(0xFFFFF1F2),
    onErrorContainer = Color(0xFF881337),
    inverseSurface = Color(0xFF0F172A),
    inverseOnSurface = Color(0xFFF8FAFC),
)

private val Dark = darkColorScheme(
    primary = Color(0xFF2563EB),            // --primary (blue in the dark theme)
    onPrimary = Color.White,
    primaryContainer = Color(0xFF1A2438),   // --hover
    onPrimaryContainer = Color(0xFFF8FAFC),
    secondary = Color(0xFF60A5FA),          // --pine
    onSecondary = Color(0xFF0A0F1D),
    secondaryContainer = Color(0xFF16233F), // --pine-tint over --raised
    onSecondaryContainer = Color(0xFFDBEAFE),
    tertiary = Color(0xFFD4975A),           // --bronze
    onTertiary = Color(0xFF0A0F1D),
    tertiaryContainer = Color(0xFF2A2420),
    onTertiaryContainer = Color(0xFFFDE7CF),
    background = Color(0xFF0A0F1D),         // --page
    onBackground = Color(0xFFE2E8F0),
    surface = Color(0xFF111A2C),            // --raised
    onSurface = Color(0xFFE2E8F0),
    surfaceVariant = Color(0xFF162034),
    onSurfaceVariant = Color(0xFF94A3B8),
    surfaceTint = Color(0xFF111A2C),
    surfaceContainerLowest = Color(0xFF0A0F1D),
    surfaceContainerLow = Color(0xFF0E1424), // --side
    surfaceContainer = Color(0xFF0E1424),
    surfaceContainerHigh = Color(0xFF111A2C),
    surfaceContainerHighest = Color(0xFF162034), // --raised-2
    surfaceBright = Color(0xFF162034),
    surfaceDim = Color(0xFF0A0F1D),
    outline = Color(0xFF334155),
    outlineVariant = Color(0xFF1E293B),
    error = Color(0xFFFB7185),
    onError = Color(0xFF0A0F1D),
    errorContainer = Color(0xFF2A1520),
    onErrorContainer = Color(0xFFFFE4E6),
    inverseSurface = Color(0xFFE2E8F0),
    inverseOnSurface = Color(0xFF0F172A),
)

/** Web tokens the Material scheme has no slot for. */
@Immutable
data class Extra(
    val good: Color,
    val warning: Color,
    val mine: Color,          // --mine: your own messages
    val bronze: Color,        // the rule beside the assistant's answers
    val line: Color,          // --line
    val lineStrong: Color,    // --line-strong
    val ink2: Color,
    val ink3: Color,
    val inkStrong: Color,
    val tintGood: Color,
    val tintWarning: Color,
    val series: List<Color>,
)

private val LightExtra = Extra(
    good = Color(0xFF15803D), warning = Color(0xFFB45309), mine = Color(0xFFF1F5F9), bronze = Color(0xFFB4783C),
    line = Color(0xFFE2E8F0), lineStrong = Color(0xFFCBD5E1), ink2 = Color(0xFF64748B), ink3 = Color(0xFF94A3B8),
    inkStrong = Color(0xFF0F172A), tintGood = Color(0xFFF0FDF4), tintWarning = Color(0xFFFFFBEB),
    series = listOf(Color(0xFF1E2A44), Color(0xFF2563EB), Color(0xFFB4783C), Color(0xFF0D9488),
        Color(0xFF7C3AED), Color(0xFFDB2777), Color(0xFF65A30D), Color(0xFF64748B)),
)
private val DarkExtra = Extra(
    good = Color(0xFF4ADE80), warning = Color(0xFFFBBF24), mine = Color(0xFF162034), bronze = Color(0xFFD4975A),
    line = Color(0xFF1E293B), lineStrong = Color(0xFF334155), ink2 = Color(0xFF94A3B8), ink3 = Color(0xFF64748B),
    inkStrong = Color(0xFFF8FAFC), tintGood = Color(0xFF13261F), tintWarning = Color(0xFF2A2516),
    series = listOf(Color(0xFF3B82F6), Color(0xFF60A5FA), Color(0xFFD4975A), Color(0xFF2DD4BF),
        Color(0xFFA78BFA), Color(0xFFF472B6), Color(0xFFA3E635), Color(0xFF94A3B8)),
)

val LocalExtra = staticCompositionLocalOf { LightExtra }

// ---- type: body 14/1.6 Geist, headings in Newsreader (h1 34, h2 20), caps labels in mono ----
private val Type = Typography().let { t ->
    val sans = { s: TextStyle -> s.copy(fontFamily = Sans) }
    t.copy(
        displayLarge = t.displayLarge.copy(fontFamily = Serif, fontWeight = FontWeight.Normal),
        displayMedium = t.displayMedium.copy(fontFamily = Serif, fontWeight = FontWeight.Normal),
        displaySmall = t.displaySmall.copy(fontFamily = Serif, fontWeight = FontWeight.Normal, letterSpacing = (-0.02).em),
        headlineLarge = TextStyle(fontFamily = Serif, fontWeight = FontWeight.Normal, fontSize = 34.sp, lineHeight = 39.sp, letterSpacing = (-0.02).em),
        headlineMedium = TextStyle(fontFamily = Serif, fontWeight = FontWeight.Normal, fontSize = 28.sp, lineHeight = 34.sp, letterSpacing = (-0.02).em),
        headlineSmall = TextStyle(fontFamily = Serif, fontWeight = FontWeight.Medium, fontSize = 24.sp, lineHeight = 30.sp, letterSpacing = (-0.01).em),
        titleLarge = TextStyle(fontFamily = Serif, fontWeight = FontWeight.Medium, fontSize = 22.sp, lineHeight = 28.sp, letterSpacing = (-0.01).em),
        titleMedium = TextStyle(fontFamily = Serif, fontWeight = FontWeight.Medium, fontSize = 18.sp, lineHeight = 24.sp, letterSpacing = (-0.01).em),
        titleSmall = TextStyle(fontFamily = Sans, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, lineHeight = 20.sp),
        bodyLarge = TextStyle(fontFamily = Sans, fontSize = 15.sp, lineHeight = 24.sp),
        bodyMedium = TextStyle(fontFamily = Sans, fontSize = 14.sp, lineHeight = 22.sp),
        bodySmall = TextStyle(fontFamily = Sans, fontSize = 12.5.sp, lineHeight = 18.sp),
        labelLarge = sans(t.labelLarge).copy(fontWeight = FontWeight.Medium, fontSize = 13.5.sp, letterSpacing = 0.sp),
        labelMedium = sans(t.labelMedium).copy(fontWeight = FontWeight.Medium, letterSpacing = 0.sp),
        labelSmall = sans(t.labelSmall).copy(fontWeight = FontWeight.Medium, fontSize = 11.5.sp, letterSpacing = 0.sp),
    )
}

/** Code and ids: JetBrains Mono, as on the web. */
val Mono = TextStyle(fontFamily = MonoFamily, fontSize = 13.sp)

/** The web app's `.eyebrow`: small spaced capitals in mono above a title. */
val EyebrowStyle = TextStyle(fontFamily = MonoFamily, fontWeight = FontWeight.Medium, fontSize = 11.sp, lineHeight = 15.sp, letterSpacing = 0.12.em)

/** The web app's `.caps` labels (section names, field labels). */
val CapsStyle = TextStyle(fontFamily = Sans, fontWeight = FontWeight.SemiBold, fontSize = 11.sp, lineHeight = 15.sp, letterSpacing = 0.08.em)

// ---- shapes: 6 dp pills, 8 dp buttons and fields, 12 dp cards ----
private val TwShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(12.dp),
    large = RoundedCornerShape(12.dp),
    extraLarge = RoundedCornerShape(16.dp),
)

/** Buttons are 8 dp rounded rectangles on the web, not Material's pills. */
val ButtonShape = RoundedCornerShape(8.dp)
val CardShape = RoundedCornerShape(12.dp)

@Composable
fun TwTheme(mode: ThemeMode, content: @Composable () -> Unit) {
    val dark = when (mode) {
        ThemeMode.SYSTEM -> isSystemInDarkTheme()
        ThemeMode.LIGHT -> false
        ThemeMode.DARK -> true
    }
    CompositionLocalProvider(LocalExtra provides if (dark) DarkExtra else LightExtra) {
        MaterialTheme(colorScheme = if (dark) Dark else Light, typography = Type, shapes = TwShapes, content = content)
    }
}

/** The web app's `.card`: raised surface, 1 dp line, 12 dp corners. */
@Composable
fun TwCard(
    modifier: Modifier = Modifier,
    padding: PaddingValues = PaddingValues(horizontal = 18.dp, vertical = 16.dp),
    content: @Composable ColumnScope.() -> Unit,
) {
    Surface(
        modifier = modifier,
        shape = CardShape,
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(1.dp, LocalExtra.current.line),
        shadowElevation = 1.dp,
    ) {
        Column(Modifier.padding(padding), content = content)
    }
}

/** The web app's `.eyebrow` line, e.g. "PERSONAL · SETTINGS". */
@Composable
fun Eyebrow(text: String, modifier: Modifier = Modifier) {
    Text(text.uppercase(), style = EyebrowStyle, color = LocalExtra.current.ink3, modifier = modifier)
}

/** A page title as on the web (`heading(title, text, {eyebrow})`): eyebrow line over a serif title. */
@Composable
fun TwTitle(eyebrow: String, title: String) {
    Column {
        Eyebrow(eyebrow)
        Text(title, style = MaterialTheme.typography.titleLarge, color = LocalExtra.current.inkStrong)
    }
}
