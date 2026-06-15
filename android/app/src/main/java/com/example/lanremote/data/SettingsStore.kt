package com.example.lanremote.data

import android.content.Context

/** Small persisted preferences: pointer feel, scroll direction, haptics, last device. */
data class Settings(
    val sensitivity: Float = 1.6f,    // cursor speed multiplier
    val naturalScroll: Boolean = true,  // touchscreen feel: content follows fingers
    val haptics: Boolean = true,
)

class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("lazer_settings", Context.MODE_PRIVATE)

    fun load() = Settings(
        sensitivity = prefs.getFloat("sensitivity", 1.6f),
        naturalScroll = prefs.getBoolean("naturalScroll", true),
        haptics = prefs.getBoolean("haptics", true),
    )

    fun save(s: Settings) {
        prefs.edit()
            .putFloat("sensitivity", s.sensitivity)
            .putBoolean("naturalScroll", s.naturalScroll)
            .putBoolean("haptics", s.haptics)
            .apply()
    }

    var lastDeviceId: String?
        get() = prefs.getString("lastDeviceId", null)
        set(v) { prefs.edit().putString("lastDeviceId", v).apply() }
}
