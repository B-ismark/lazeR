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
 * packets per session. L2 was accepted through v2.x for un-updated phones and is
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

    // Inbound replay guard (mirrors the server): pin the server's sid on the first
    // authenticated reply, then require the same sid and a strictly-greater counter.
    // Without this a captured genuine OK/PONG/VOL could be replayed to spoof liveness
    // or a handshake. Guarded by [recvLock] — open() runs on several threads.
    private val recvLock = Any()
    private var srvSid: ByteArray? = null
    private var recvCtr = -1L

    /** Build a datagram for [body] (e.g. "MOVE 3 -4"). */
    fun seal(body: String): ByteArray {
        val ctr = sendCtr.incrementAndGet()
        // Wrapping the counter would repeat a nonce under a key that outlives the
        // session — the exact failure L3 exists to prevent. Refuse instead. The
        // caller treats sends as lossy, so packets simply stop and the watchdog
        // reconnects, which builds a fresh channel with a new sid.
        check(ctr <= 0xFFFFFFFFL) { "session counter exhausted; reconnect for a fresh sid" }
        val header = ByteArray(14)
        header[0] = 'L'.code.toByte()
        header[1] = '3'.code.toByte()
        System.arraycopy(sid, 0, header, 2, 8)
        putBE(header, 10, ctr, 4)
        val nonce = header.copyOfRange(2, 14)             // sid | counter
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
        cipher.updateAAD(header)
        val ct = cipher.doFinal(body.toByteArray(Charsets.UTF_8))  // ct + 16B tag
        return header + ct
    }

    /**
     * Decrypt a server reply (OK / CHAL / PONG / VOL n); null if it isn't a valid
     * packet. Strictly L3: the server answers in the dialect we opened with, and
     * there is only one now.
     */
    fun open(data: ByteArray, len: Int): String? {
        if (len < 14 + 16 || data[0] != 'L'.code.toByte() ||
            data[1] != '3'.code.toByte()) {
            return null
        }
        return try {
            val header = data.copyOfRange(0, 14)
            val nonce = data.copyOfRange(2, 14)
            val ct = data.copyOfRange(14, len)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
            cipher.updateAAD(header)
            val text = String(cipher.doFinal(ct), Charsets.UTF_8)  // valid tag ⇒ authentic
            // Freshness: reject a replayed/reordered reply (same session, counter not
            // advancing) or one from a different server session.
            val rsid = header.copyOfRange(2, 10)
            var ctr = 0L
            for (i in 10 until 14) ctr = (ctr shl 8) or (header[i].toLong() and 0xFF)
            synchronized(recvLock) {
                val pinned = srvSid
                if (pinned == null) {
                    srvSid = rsid; recvCtr = ctr
                } else if (!rsid.contentEquals(pinned) || ctr <= recvCtr) {
                    return null
                } else {
                    recvCtr = ctr
                }
            }
            text
        } catch (e: Exception) {
            null
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
