package com.example.lanremote.audio

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioRecordingConfiguration
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.Process
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Records the phone's microphone as 20 ms frames of 16 kHz, 16-bit mono PCM and
 * hands each one to [onFrame] on its own thread.
 *
 * It records with the VOICE_COMMUNICATION source, the one phone calls use, so the
 * phone's own noise suppression applies; the effects below are switched on
 * explicitly too, for phones that only enable them on request. (Its echo
 * cancelling only removes sound the PHONE plays — not the laptop's speakers.)
 *
 * 16 kHz is what calls use and keeps one frame (640 bytes) inside a single Wi-Fi
 * packet. A phone that won't record at 16 kHz records at 48 kHz instead and is
 * brought down to 16 kHz here ([Decimator3]).
 *
 * It looks after itself: a recorder that stops working (Android's audio server
 * restarting, a route change) is reopened; one that can't be reopened ends the
 * recording, and [isRunning] turns false so the app can say so. [onSilenced]
 * reports when another app (a phone call, a recorder) has taken the mic.
 *
 * The caller must hold RECORD_AUDIO; [start] returns false if it can't record.
 */
class PhoneMic(
    context: Context,
    private val onFrame: (ByteArray) -> Unit,
    private val onSilenced: (Boolean) -> Unit = {},
) {
    private val audio = context.getSystemService(AudioManager::class.java)
    private val main = Handler(Looper.getMainLooper())

    @Volatile var muted = false
    // Each recording gets its own flag, so a stop() followed at once by start()
    // can't leave the old loop reading on (it would see the new session's flag).
    private var live: AtomicBoolean? = null
    private var thread: Thread? = null
    @Volatile private var session = 0
    private var silenced = false
    private var callback: Any? = null   // AudioManager.AudioRecordingCallback, API 29+

    @SuppressLint("MissingPermission")   // checked by the caller before start()
    @Synchronized
    fun start(): Boolean {
        if (live?.get() == true) return true
        // Let a just-stopped recording release the mic first; some phones refuse a
        // second recorder while the first is still open. The old loop notices its
        // flag within one 20 ms read, so this is short.
        thread?.let { if (it.isAlive) it.join(300) }
        val first = openStarted() ?: return false
        val running = AtomicBoolean(true)
        live = running
        watchSilencing(true)
        thread = Thread({ record(first, running) }, "LazeR-phone-mic")
            .apply { isDaemon = true; start() }
        return true
    }

    @Synchronized
    fun stop() {
        live?.set(false)   // the thread tears down the recorder itself when it sees this
        live = null
        watchSilencing(false)
    }

    val isRunning get() = live?.get() == true

    // ── the recording thread ──

    private class Rec(val record: AudioRecord, val effects: List<android.media.audiofx.AudioEffect>) {
        val factor = record.sampleRate / 16000
        fun release() {
            try { record.stop() } catch (_: Exception) {}
            effects.forEach { try { it.release() } catch (_: Exception) {} }
            try { record.release() } catch (_: Exception) {}
        }
    }

    private fun record(first: Rec, running: AtomicBoolean) {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        var rec: Rec? = first
        var reopenFails = 0
        try {
            while (running.get()) {
                val r = rec ?: openStarted().also { rec = it }
                if (r == null) {
                    // Couldn't get the mic back: try a few times, then give up and
                    // let isRunning say so, rather than show "on" over nothing.
                    if (++reopenFails > 5) break
                    Thread.sleep(400)
                    continue
                }
                reopenFails = 0
                if (!readLoop(r, running)) {
                    r.release()          // broken: reopen it
                    rec = null
                    Thread.sleep(100)
                }
            }
        } catch (_: Exception) {
            // Anything unexpected ends this recording rather than the app.
        } finally {
            running.set(false)   // isRunning tells the truth if the loop died
            rec?.release()
        }
    }

    /** Reads frames until stopped (true) or the recorder breaks (false). */
    private fun readLoop(r: Rec, running: AtomicBoolean): Boolean {
        val raw = ShortArray(FRAME * r.factor)
        val decimate = if (r.factor == 3) Decimator3() else null
        var errors = 0
        while (running.get()) {
            var got = 0
            while (got < raw.size && running.get()) {
                val n = r.record.read(raw, got, raw.size - got)
                if (n == AudioRecord.ERROR_DEAD_OBJECT) return false
                if (n <= 0) {
                    // ~0.5 s of errors in a row: this recorder isn't coming back.
                    if (++errors > 25) return false
                    Thread.sleep(20)
                    break
                }
                errors = 0
                got += n
            }
            if (got < raw.size) continue
            val pcm = decimate?.process(raw) ?: raw
            // Muted: keep reading (so the mic stays warm and unmuting is instant)
            // but send nothing — the laptop plays silence.
            if (!muted) onFrame(toBytes(pcm))
        }
        return true
    }

    // ── opening ──

    private fun openStarted(): Rec? {
        val record = open(16000) ?: open(48000) ?: return null
        val effects = attachEffects(record.audioSessionId)
        val rec = Rec(record, effects)
        return try {
            record.startRecording()
            session = record.audioSessionId
            rec
        } catch (_: Exception) {
            rec.release()
            null
        }
    }

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

    // ── another app taking the mic (Android 10+) ──
    // A phone call or a foreground recorder silences ours: it keeps "recording"
    // zeros. Android says so through the recording callback, so the app can show
    // it instead of leaving the user talking into nothing.

    private fun watchSilencing(on: Boolean) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || audio == null) return
        if (on && callback == null) {
            val cb = object : AudioManager.AudioRecordingCallback() {
                override fun onRecordingConfigChanged(configs: MutableList<AudioRecordingConfiguration>) {
                    val mine = configs.firstOrNull { it.clientAudioSessionId == session }
                    report(mine?.isClientSilenced == true)
                }
            }
            try { audio.registerAudioRecordingCallback(cb, main); callback = cb } catch (_: Exception) {}
        } else if (!on) {
            (callback as? AudioManager.AudioRecordingCallback)?.let {
                try { audio.unregisterAudioRecordingCallback(it) } catch (_: Exception) {}
            }
            callback = null
            report(false)
        }
    }

    private fun report(s: Boolean) {
        if (s != silenced) { silenced = s; onSilenced(s) }
    }

    companion object {
        /** Samples in one 20 ms frame at 16 kHz. */
        const val FRAME = 320
    }
}

/**
 * 48 kHz → 16 kHz. A low-pass first (cut at ~7 kHz, well under the new 8 kHz
 * limit), then every third sample. Simply averaging threes let the 8–24 kHz band
 * fold back down as hiss on "s" sounds. Keeps its history between frames, so
 * frame edges don't click.
 */
internal class Decimator3 {
    private val hist = FloatArray(TAPS.size - 1)

    fun process(src: ShortArray): ShortArray {
        val n = TAPS.size
        val x = FloatArray(hist.size + src.size)
        hist.copyInto(x)
        for (i in src.indices) x[hist.size + i] = src[i].toFloat()
        val out = ShortArray(src.size / 3)
        for (o in out.indices) {
            val end = hist.size + o * 3      // newest input sample for this output
            var acc = 0f
            for (k in 0 until n) acc += TAPS[k] * x[end - k]
            out[o] = acc.coerceIn(-32768f, 32767f).toInt().toShort()
        }
        x.copyInto(hist, 0, x.size - hist.size, x.size)
        return out
    }

    companion object {
        // 31-tap windowed-sinc (Hamming), cutoff 7 kHz at 48 kHz, unity DC gain.
        val TAPS: FloatArray = run {
            val n = 31
            val fc = 7000.0 / 48000.0
            val m = (n - 1) / 2.0
            val t = DoubleArray(n) { i ->
                val k = i - m
                val sinc = if (k == 0.0) 2 * fc else Math.sin(2 * Math.PI * fc * k) / (Math.PI * k)
                sinc * (0.54 - 0.46 * Math.cos(2 * Math.PI * i / (n - 1)))
            }
            val sum = t.sum()
            FloatArray(n) { (t[it] / sum).toFloat() }
        }
    }
}

/** 16-bit samples as little-endian bytes, the order the laptop reads int16 in. */
internal fun toBytes(pcm: ShortArray): ByteArray {
    val out = ByteArray(pcm.size * 2)
    for (i in pcm.indices) {
        val v = pcm[i].toInt()
        out[2 * i] = v.toByte()
        out[2 * i + 1] = (v shr 8).toByte()
    }
    return out
}
