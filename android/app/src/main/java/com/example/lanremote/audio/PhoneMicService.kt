package com.example.lanremote.audio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.example.lanremote.MainActivity
import com.example.lanremote.R

/**
 * Keeps the phone microphone working while LazeR is in the background or the
 * screen is off — the normal state during a call.
 *
 * Android only lets an app use the mic from the background while it runs a
 * foreground service of the "microphone" type, with a notification saying so.
 * This service is only that permission slip: the recording itself lives in
 * [PhoneMic], owned by the view model. Started when the user picks Phone mic,
 * stopped when they switch back, disconnect, or leave the app for good.
 */
class PhoneMicService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, notification(), type)
        } catch (_: Exception) {
            // Refused (e.g. not started from the foreground): the mic still works
            // while the app is open, so carry on without the background guarantee.
            stopSelf()
        }
        // Not restarted by the system: after a crash the view model that owns the
        // recording is gone too, and a mic notification with nothing behind it lies.
        return START_NOT_STICKY
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Phone microphone",
                NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while your phone is the laptop's microphone"
            })
        }
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_monochrome)
            .setContentTitle("Phone is the laptop's microphone")
            .setContentText("Tap to open LazeR and switch back")
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    companion object {
        private const val CHANNEL = "phone_mic"
        private const val NOTIFICATION_ID = 0x4d1c

        fun start(ctx: Context) {
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, PhoneMicService::class.java))
            } catch (_: Exception) {
                // Not allowed right now (app in the background): foreground use still works.
            }
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, PhoneMicService::class.java))
        }
    }
}
