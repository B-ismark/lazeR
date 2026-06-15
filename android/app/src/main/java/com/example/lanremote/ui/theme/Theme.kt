package com.example.lanremote.ui.theme

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

// Fallback brand scheme used on devices without dynamic color (Android < 12).
private val FallbackDark = darkColorScheme(
    primary = Color(0xFFB9C3FF),
    secondary = Color(0xFFC2C5DD),
    tertiary = Color(0xFFE5BAD8),
)
private val FallbackLight = lightColorScheme(
    primary = Color(0xFF4A56C0),
    secondary = Color(0xFF5A5D72),
    tertiary = Color(0xFF77536D),
)

@Composable
fun LanRemoteTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val context = LocalContext.current
    val colorScheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (darkTheme) dynamicDarkColorScheme(context)
            else dynamicLightColorScheme(context)
        darkTheme -> FallbackDark
        else -> FallbackLight
    }

    MaterialTheme(
        colorScheme = colorScheme,
        typography = Typography(),
        content = content,
    )
}
