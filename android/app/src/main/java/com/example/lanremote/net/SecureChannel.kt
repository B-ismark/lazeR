package com.example.lanremote.net

import android.util.Base64
import java.security.SecureRandom
import java.util.concurrent.atomic.AtomicLong
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Secure wire, byte-for-byte identical to the Python server's `Wire`:
 *
 *   packet = magic(2) | nonce(12) | AES-256-GCM(ct + tag)
 *   AAD    = the packet's first 14 bytes
 *   text   = "VERB args"  (UTF-8)
 *
 * Dialect L3 is the only one: nonce = sid(8) | counter(4).
 *
 * The key is PERSISTENT across launches while the sid is random per session, so a
 * sid collision means GCM nonce reuse under one key — which leaks the
 * authentication key, not merely a plaintext. The original L2 dialect split
 * sid(4)|counter(8), putting reuse at the 2^32 birthday bound (~1.2% by 10k
 * sessions, ~39% by 65k), and every reconnect mints a session. L3 moves four bytes
 * from the counter to the sid for 2^64, and a 4-byte counter still allows 4.29e9
 * packets per session. L2 was accepted until 2.2.0 for un-updated phones and is
 * now removed: a server that old simply never answers, exactly as a pre-L3 phone
 * sees silence from us.
 *
 * A valid GCM tag authenticates the sender (proves key possession) — no token on
 * the wire — and the monotonic counter the server enforces blocks replay. The
 * 256-bit key arrives only in the scanned QR, never over mDNS or in the clear.
 */
class SecureChannel(key: ByteArray) {

    private val keySpec = SecretKeySpec(key, "AES")
    private val sid = ByteArray(8).also { SecureRandom().nextBytes(it) }
    private val sendCtr = AtomicLong(0L)

    // One encrypting Cipher per channel, re-initialised with each packet's nonce.
    // getInstance per packet costs ~6 allocations and a provider lookup at the
    // trackpad's 120–240 Hz. Re-init with a fresh IV is the supported way to reuse
    // a GCM cipher; only reusing an IV is refused. Guarded by [sealLock]: the sender
    // thread and the probes (PING/VGET) both seal.
    private val sealLock = Any()
    private val encCipher: Cipher = Cipher.getInstance("AES/GCM/NoPadding")

    // Inbound replay guard (mirrors the server): pin the server's sid once a reply
    // is known to answer THIS handshake, then require the same sid and a strictly-
    // greater counter. Without this a captured genuine OK/PONG/VOL could be replayed
    // to spoof liveness or a handshake. Guarded by [recvLock] — open() runs on
    // several threads.
    private val recvLock = Any()
    private var srvSid: ByteArray? = null
    private var recvCtr = -1L

    /** An authentic reply, not yet checked for freshness. */
    class Opened(val text: String, val sid: ByteArray, val ctr: Long)

    /** Build a datagram for [body] (e.g. "MOVE 3 -4"). */
    fun seal(body: String): ByteArray {
        val ctr = sendCtr.incrementAndGet()
        // Wrapping the counter would repeat a nonce under a key that outlives the
        // session — the exact failure L3 exists to prevent. Refuse instead. The
        // caller treats sends as lossy, so packets simply stop and the watchdog
        // reconnects, which builds a fresh channel with a new sid.
        check(ctr <= 0xFFFFFFFFL) { "session counter exhausted; reconnect for a fresh sid" }
        val plain = body.toByteArray(Charsets.UTF_8)
        val out = ByteArray(14 + plain.size + 16)
        out[0] = 'L'.code.toByte()
        out[1] = '3'.code.toByte()
        System.arraycopy(sid, 0, out, 2, 8)
        putBE(out, 10, ctr, 4)
        synchronized(sealLock) {
            // sid | counter is the nonce; the whole 14-byte header is the AAD.
            encCipher.init(Cipher.ENCRYPT_MODE, keySpec, GCMParameterSpec(128, out, 2, 12))
            encCipher.updateAAD(out, 0, 14)
            encCipher.doFinal(plain, 0, plain.size, out, 14)   // ct + 16B tag
        }
        return out
    }

    /**
     * Decrypt a server reply (OK / CHAL / PONG / VOL n); null if it isn't a valid
     * packet. Strictly L3: the server answers in the dialect we opened with, and
     * there is only one now.
     *
     * Pins the server's session on the first reply if [pin] was never called —
     * kept for callers outside a handshake. The handshake itself uses [peek] and
     * pins only a reply that proved it answers this HELLO.
     */
    fun open(data: ByteArray, len: Int): String? {
        val o = peek(data, len) ?: return null
        synchronized(recvLock) {
            val pinned = srvSid
            if (pinned == null) {
                srvSid = o.sid; recvCtr = o.ctr
            } else if (!o.sid.contentEquals(pinned) || o.ctr <= recvCtr) {
                // Freshness: a replayed/reordered reply (same session, counter not
                // advancing) or one from a different server session.
                return null
            } else {
                recvCtr = o.ctr
            }
        }
        return o.text
    }

    /** Decrypt and authenticate [data] without touching the replay guard. A valid
     *  tag proves the laptop's key sealed it — not that it was sealed just now. */
    fun peek(data: ByteArray, len: Int): Opened? {
        if (len < 14 + 16 || len > data.size || data[0] != 'L'.code.toByte() ||
            data[1] != '3'.code.toByte()) {
            return null
        }
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, keySpec, GCMParameterSpec(128, data, 2, 12))
            cipher.updateAAD(data, 0, 14)
            val text = String(cipher.doFinal(data, 14, len - 14), Charsets.UTF_8)
            var ctr = 0L
            for (i in 10 until 14) ctr = (ctr shl 8) or (data[i].toLong() and 0xFF)
            Opened(text, data.copyOfRange(2, 10), ctr)
        } catch (e: Exception) {
            null
        }
    }

    /** Pin the server session a verified handshake reply came from. Every later
     *  [open] must come from that session with a higher counter. */
    fun pin(o: Opened) {
        synchronized(recvLock) {
            srvSid = o.sid
            recvCtr = o.ctr
        }
    }

    private fun putBE(buf: ByteArray, off: Int, v: Long, width: Int) {
        for (i in 0 until width) {
            buf[off + i] = (v ushr (8 * (width - 1 - i)) and 0xFF).toByte()
        }
    }

    companion object {
        /** Decode the QR's base64url key into 32 raw bytes, or null if unusable. */
        fun keyFromBase64(b64: String): ByteArray? {
            if (b64.isBlank()) return null
            return try {
                // Pad to a multiple of 4 so strict decoders accept the stripped form.
                val padded = b64 + "=".repeat((4 - b64.length % 4) % 4)
                val raw = Base64.decode(padded, Base64.URL_SAFE or Base64.NO_WRAP)
                if (raw.size == 32) raw else null
            } catch (e: Exception) {
                null
            }
        }
    }
}
