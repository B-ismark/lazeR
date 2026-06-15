package com.example.lanremote.util

import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/** Thin vibration helper with distinct feels for left-click, right-click, scroll. */
class Haptics(context: Context) {

    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)
            ?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
    }

    private val canVibrate = vibrator?.hasVibrator() == true

    /** Crisp single tick. */
    fun leftClick() = oneShot(14, strong = true)

    /** Double tap so right-click feels different from left. */
    fun rightClick() = waveform(longArrayOf(0, 12, 50, 22))

    /** Very light tick per scroll notch. */
    fun scrollTick() = oneShot(6, strong = false)

    private fun oneShot(ms: Long, strong: Boolean) {
        val v = vibrator ?: return
        if (!canVibrate) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val amp = if (strong) VibrationEffect.DEFAULT_AMPLITUDE else 60
            v.vibrate(VibrationEffect.createOneShot(ms, amp))
        } else {
            @Suppress("DEPRECATION")
            v.vibrate(ms)
        }
    }

    private fun waveform(pattern: LongArray) {
        val v = vibrator ?: return
        if (!canVibrate) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            v.vibrate(VibrationEffect.createWaveform(pattern, -1))
        } else {
            @Suppress("DEPRECATION")
            v.vibrate(pattern, -1)
        }
    }
}
