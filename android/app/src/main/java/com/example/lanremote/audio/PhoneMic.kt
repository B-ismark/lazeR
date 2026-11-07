package com.example.lanremote.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Process

/**
 * Records the phone's microphone as 20 ms frames of 16 kHz, 16-bit mono PCM and
 * hands each one to [onFrame] on its own thread.
 *
 * It records with the VOICE_COMMUNICATION source, the one phone calls use, so the
 * phone's own echo cancelling and noise suppression apply; the effects below are
 * switched on explicitly too, for phones that only enable them on request.
 *
 * 16 kHz is what calls use and keeps one frame (640 bytes) inside a single Wi-Fi
 * packet. A phone that won't record at 16 kHz records at 48 kHz instead and is
 * brought down to 16 kHz here ([downsample3]).
 *
 * The caller must hold RECORD_AUDIO; [start] returns false if it can't record.
 */
class PhoneMic(private val onFrame: (ByteArray) -> Unit) {

    @Volatile var muted = false
    // Each recording gets its own flag, so a stop() followed at once by start()
    // can't leave the old loop reading on (it would see the new session's flag).
    private var live: java.util.concurrent.atomic.AtomicBoolean? = null
    private var thread: Thread? = null

    @SuppressLint("MissingPermission")   // checked by the caller before start()
    @Synchronized
    fun start(): Boolean {
        if (live?.get() == true) return true
        // Let a just-stopped recording release the mic first; some phones refuse a
        // second recorder while the first is still open.
        thread?.join(500)
        val rec = open(16000) ?: open(48000) ?: return false
        val factor = rec.sampleRate / 16000
        val effects = attachEffects(rec.audioSessionId)
        try {
            rec.startRecording()
        } catch (_: Exception) {
            effects.forEach { it.release() }
            rec.release()
            return false
        }
        val running = java.util.concurrent.atomic.AtomicBoolean(true)
        live = running
        thread = Thread({
            Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
            val raw = ShortArray(FRAME * factor)
            try {
                while (running.get()) {
                    var got = 0
                    while (got < raw.size && running.get()) {
                        val n = rec.read(raw, got, raw.size - got)
                        // An error (the mic taken by a phone call, say): back off
                        // instead of spinning until it comes back.
                        if (n <= 0) { Thread.sleep(20); break }
                        got += n
                    }
                    if (got < raw.size) continue
                    // Muted: keep reading (so the mic stays warm and unmuting is
                    // instant) but send nothing — the laptop plays silence.
                    if (muted) continue
                    val pcm = if (factor == 3) downsample3(raw) else raw
                    onFrame(toBytes(pcm))
                }
            } finally {
                try { rec.stop() } catch (_: Exception) {}
                effects.forEach { it.release() }
                rec.release()
            }
        }, "LazeR-phone-mic").apply { isDaemon = true; start() }
        return true
    }

    @Synchronized
    fun stop() {
        live?.set(false)   // the thread tears down the recorder itself when it sees this
        live = null
    }

    val isRunning get() = live?.get() == true

    @SuppressLint("MissingPermission")
    private fun open(rate: Int): AudioRecord? {
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0) return null
        return try {
            val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, rate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(min, rate / 25 * 2 * 4))   // at least ~4 frames of headroom
            if (rec.state == AudioRecord.STATE_INITIALIZED) rec else { rec.release(); null }
        } catch (_: Exception) {
            null
        }
    }

    private fun attachEffects(session: Int): List<android.media.audiofx.AudioEffect> {
        val out = ArrayList<android.media.audiofx.AudioEffect>(3)
        fun add(make: () -> android.media.audiofx.AudioEffect?) {
            try { make()?.let { it.enabled = true; out += it } } catch (_: Exception) {}
        }
        if (AcousticEchoCanceler.isAvailable()) add { AcousticEchoCanceler.create(session) }
        if (NoiseSuppressor.isAvailable()) add { NoiseSuppressor.create(session) }
        if (AutomaticGainControl.isAvailable()) add { AutomaticGainControl.create(session) }
        return out
    }

    companion object {
        /** Samples in one 20 ms frame at 16 kHz. */
        const val FRAME = 320
    }
}

/** 48 kHz → 16 kHz: the average of each three samples. Averaging is a gentle
 *  low-pass, enough for speech, which carries almost nothing above 8 kHz. */
internal fun downsample3(src: ShortArray): ShortArray {
    val out = ShortArray(src.size / 3)
    for (i in out.indices) {
        val j = i * 3
        out[i] = ((src[j] + src[j + 1] + src[j + 2]) / 3).toShort()
    }
    return out
}

/** Little-endian 16-bit PCM, the byte order the laptop plays. */
internal fun toBytes(pcm: ShortArray): ByteArray {
    val out = ByteArray(pcm.size * 2)
    for (i in pcm.indices) {
        val v = pcm[i].toInt()
        out[2 * i] = v.toByte()
        out[2 * i + 1] = (v shr 8).toByte()
    }
    return out
}
