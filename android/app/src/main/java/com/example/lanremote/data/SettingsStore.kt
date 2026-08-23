package com.example.lanremote.data

import android.content.Context

/** Small persisted preferences: pointer feel, scroll direction, haptics, last device. */
data class Settings(
    val sensitivity: Float = 1.6f,    // cursor speed multiplier
    // Default OFF = the traditional wheel feel: fingers/wheel up moves the view up —
    // the scroll runs INVERSE to finger movement. Natural (touchscreen) scrolling,
    // where content follows the fingers, is the opt-in.
    val naturalScroll: Boolean = false,
    val haptics: Boolean = true,
    val acceleration: Boolean = true,   // fast flicks travel farther (pointer accel curve)
    // The only setting that governs internet access: everything else in the app is
    // LAN-only. On by default because a sideloaded APK has no store to notify the
    // user for it, so without this a stale install stays stale silently — but it is
    // a plain toggle, and off means UpdateChecker is never called at all.
    val updateCheck: Boolean = true,
)

class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("lazer_settings", Context.MODE_PRIVATE)

    fun load() = Settings(
        sensitivity = prefs.getFloat("sensitivity", 1.6f),
        naturalScroll = prefs.getBoolean("naturalScroll", false),
        haptics = prefs.getBoolean("haptics", true),
        acceleration = prefs.getBoolean("acceleration", true),
        updateCheck = prefs.getBoolean("updateCheck", true),
    )

    fun save(s: Settings) {
        prefs.edit()
            .putFloat("sensitivity", s.sensitivity)
            .putBoolean("naturalScroll", s.naturalScroll)
            .putBoolean("haptics", s.haptics)
            .putBoolean("acceleration", s.acceleration)
            .putBoolean("updateCheck", s.updateCheck)
            .apply()
    }

    var lastDeviceId: String?
        get() = prefs.getString("lastDeviceId", null)
        set(v) { prefs.edit().putString("lastDeviceId", v).apply() }

    // --- update check bookkeeping ---
    // Persisted so the throttle survives a restart: the app is opened many times a
    // day, and re-checking on every launch would be a request per launch for an
    // answer that changes a few times a year.

    /** When we last successfully reached GitHub, epoch millis (0 = never). */
    var lastUpdateCheckMs: Long
        get() = prefs.getLong("lastUpdateCheckMs", 0L)
        set(v) { prefs.edit().putLong("lastUpdateCheckMs", v).apply() }

    /** The newest tag seen on the last successful check, so the banner can show
     *  immediately on launch instead of only after a fresh network round trip. */
    var lastKnownTag: String?
        get() = prefs.getString("lastKnownTag", null)
        set(v) { prefs.edit().putString("lastKnownTag", v).apply() }

    /** True when the throttle window has elapsed and it's worth asking again. */
    fun updateCheckDue(nowMs: Long): Boolean {
        val last = lastUpdateCheckMs
        // A clock that moved backwards (timezone/NTP correction, or a user setting
        // the date) would otherwise park `last` in the future and never check again.
        if (last > nowMs) return true
        return nowMs - last >= UpdateChecker.MIN_INTERVAL_MS
    }
}
