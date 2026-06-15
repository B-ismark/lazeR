package com.example.lanremote.net

import android.util.Base64
import java.security.SecureRandom
import java.util.concurrent.atomic.AtomicLong
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Secure wire (v2), byte-for-byte identical to the Python server's `Wire`:
 *
 *   packet = "L2"(2) | sid(4) | counter(8, big-endian) | AES-256-GCM(ct + tag)
 *   nonce  = sid | counter                       (12 bytes)
 *   AAD    = "L2" | sid | counter                (first 14 bytes of the packet)
 *   text   = "VERB args"  (UTF-8)
 *
 * A valid GCM tag authenticates the sender (proves key possession) — no token on
 * the wire — and the monotonic counter the server enforces blocks replay. The
 * 256-bit key arrives only in the scanned QR, never over mDNS or in the clear.
 */
class SecureChannel(key: ByteArray) {

    private val keySpec = SecretKeySpec(key, "AES")
    private val sid = ByteArray(4).also { SecureRandom().nextBytes(it) }
    private val sendCtr = AtomicLong(0L)

    /** Build a v2 datagram for [body] (e.g. "MOVE 3 -4"). */
    fun seal(body: String): ByteArray {
        val ctr = sendCtr.incrementAndGet()
        val header = ByteArray(14)
        header[0] = 'L'.code.toByte(); header[1] = '2'.code.toByte()
        System.arraycopy(sid, 0, header, 2, 4)
        putLongBE(header, 6, ctr)
        val nonce = header.copyOfRange(2, 14)             // sid | counter
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
        cipher.updateAAD(header)
        val ct = cipher.doFinal(body.toByteArray(Charsets.UTF_8))  // ct + 16B tag
        return header + ct
    }

    /** Decrypt a v2 reply (OK / PONG / VOL n); null if it isn't a valid v2 packet. */
    fun open(data: ByteArray, len: Int): String? {
        if (len < 14 + 16 || data[0] != 'L'.code.toByte() || data[1] != '2'.code.toByte()) {
            return null
        }
        return try {
            val header = data.copyOfRange(0, 14)
            val nonce = data.copyOfRange(2, 14)
            val ct = data.copyOfRange(14, len)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
            cipher.updateAAD(header)
            String(cipher.doFinal(ct), Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
    }

    private fun putLongBE(buf: ByteArray, off: Int, v: Long) {
        for (i in 0 until 8) buf[off + i] = (v ushr (8 * (7 - i)) and 0xFF).toByte()
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
