package com.example.lanremote.net

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The Kotlin half of the cross-implementation contract.
 *
 * The v2 wire is implemented twice — here and in the Python server's `Wire` —
 * and the two must agree byte for byte or pairing silently breaks. [GOLDEN_HEX]
 * below is the SAME frozen packet asserted by `server/tests/test_wire.py`
 * (class `GoldenVector`). Change the header layout, the nonce derivation, or the
 * AAD on either side and exactly one of the two suites goes red, so the skew is
 * caught in CI instead of on a user's phone.
 *
 * Plain JVM unit test — no Robolectric. That means anything touching
 * `android.util.Base64` (i.e. [SecureChannel.keyFromBase64]) is deliberately NOT
 * covered here: the android.jar stubs on the unit-test classpath throw at
 * runtime. We also intentionally do NOT set `unitTests.isReturnDefaultValues`,
 * because that would make such a call quietly return null instead of failing
 * loudly. `seal`/`open` use only `javax.crypto` + `java.security`, so they run
 * natively.
 */
class SecureChannelTest {

    private companion object {
        /** key = bytes(0..31) — deterministic test key, never a real one. */
        val KEY = ByteArray(32) { it.toByte() }

        /** sid = 01020304, counter = 1, plaintext = "MOVE 3 -4". AES-GCM is
         *  deterministic for a fixed (key, nonce, aad, plaintext), so these bytes
         *  are stable forever. Must match GoldenVector.HEX in test_wire.py. */
        const val GOLDEN_HEX =
            "4c32010203040000000000000001" +
                "9cbe917b3b744aee3cc2838803178e6aa93726c156c2f73bb4"
        const val GOLDEN_PLAINTEXT = "MOVE 3 -4"

        fun hex(s: String) = ByteArray(s.length / 2) {
            s.substring(it * 2, it * 2 + 2).toInt(16).toByte()
        }
    }

    // ── the anchor ───────────────────────────────────────────────────────────

    @Test
    fun `opens the golden packet produced by the python server`() {
        val pkt = hex(GOLDEN_HEX)
        val ch = SecureChannel(KEY)
        assertEquals(GOLDEN_PLAINTEXT, ch.open(pkt, pkt.size))
    }

    @Test
    fun `golden packet layout is frozen`() {
        val pkt = hex(GOLDEN_HEX)
        // magic(2) | sid(4) | counter(8 BE) | ciphertext+tag(len+16)
        assertEquals(14 + GOLDEN_PLAINTEXT.length + 16, pkt.size)
        assertEquals('L'.code.toByte(), pkt[0])
        assertEquals('2'.code.toByte(), pkt[1])
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), pkt.copyOfRange(2, 6))
        var ctr = 0L
        for (i in 6 until 14) ctr = (ctr shl 8) or (pkt[i].toLong() and 0xFF)
        assertEquals(1L, ctr)
    }

    // ── inbound replay guard (mirrors the server's watermark) ────────────────

    @Test
    fun `replayed reply is rejected`() {
        val pkt = hex(GOLDEN_HEX)
        val ch = SecureChannel(KEY)
        assertEquals(GOLDEN_PLAINTEXT, ch.open(pkt, pkt.size))
        // Same sid, same counter ⇒ not strictly greater ⇒ dropped. Without this a
        // captured OK/PONG could be replayed at the phone to fake liveness.
        assertNull(ch.open(pkt, pkt.size))
    }

    @Test
    fun `reply from a different server session is rejected`() {
        val ch = SecureChannel(KEY)
        val golden = hex(GOLDEN_HEX)
        assertEquals(GOLDEN_PLAINTEXT, ch.open(golden, golden.size))

        // A *cryptographically valid* packet under the same key but a different
        // session id — exactly what a second server (or a restarted one) emits.
        // It must be refused by the pinned-sid check, not by a tag failure, so the
        // packet is sealed properly rather than byte-flipped.
        val impostor = SecureChannel(KEY).seal("OK")
        assertNotEquals(golden.copyOfRange(2, 6).toList(),
                        impostor.copyOfRange(2, 6).toList())
        assertNull(ch.open(impostor, impostor.size))
    }

    @Test
    fun `tampered tag is rejected`() {
        val pkt = hex(GOLDEN_HEX)
        pkt[pkt.size - 1] = (pkt[pkt.size - 1].toInt() xor 0x01).toByte()
        assertNull(SecureChannel(KEY).open(pkt, pkt.size))
    }

    @Test
    fun `too-short and non-v2 packets are rejected`() {
        val ch = SecureChannel(KEY)
        val pkt = hex(GOLDEN_HEX)
        for (n in intArrayOf(0, 1, 2, 13, 14, 29)) {
            assertNull("accepted a $n-byte packet", ch.open(pkt.copyOf(n), n))
        }
        val plain = "OK".toByteArray()
        assertNull(ch.open(plain, plain.size))
    }

    @Test
    fun `truncated length argument is honoured over array size`() {
        // DatagramPacket reuses an oversized buffer, so `len` — not `data.size` —
        // is the real packet boundary.
        val pkt = hex(GOLDEN_HEX)
        val padded = pkt.copyOf(pkt.size + 20)
        assertEquals(GOLDEN_PLAINTEXT, SecureChannel(KEY).open(padded, pkt.size))
    }

    // ── outbound layout ──────────────────────────────────────────────────────

    @Test
    fun `seal emits the v2 header with a monotonic counter from one`() {
        val ch = SecureChannel(KEY)
        val first = ch.seal("PING")
        val second = ch.seal("PING")

        assertEquals('L'.code.toByte(), first[0])
        assertEquals('2'.code.toByte(), first[1])
        assertEquals(14 + "PING".length + 16, first.size)
        assertEquals(1L, counterOf(first))
        assertEquals(2L, counterOf(second))
        // Same session id across sends...
        assertArrayEquals(first.copyOfRange(2, 6), second.copyOfRange(2, 6))
        // ...but distinct nonces, so identical plaintext never repeats on the wire.
        assertNotEquals(first.toList(), second.toList())
    }

    @Test
    fun `round trip preserves payload including spaces and unicode`() {
        // A fresh channel with the same key stands in for the server's Wire: if
        // seal's nonce/AAD construction ever drifts from what open expects, or the
        // payload isn't handled as UTF-8, these break.
        for (body in listOf("OK", "VOL 42", "KEY hello  world", "CLIP naïve 🙂",
                            "COMBO ctrl shift t")) {
            val sender = SecureChannel(KEY)
            val receiver = SecureChannel(KEY)
            val pkt = sender.seal(body)
            assertEquals(body, receiver.open(pkt, pkt.size))
        }
    }

    @Test
    fun `session ids differ between channels`() {
        // sid is random per session; a collision under a persistent key would mean
        // GCM nonce reuse, so this at least catches a constant/zero sid.
        val a = SecureChannel(KEY).seal("PING").copyOfRange(2, 6)
        val b = SecureChannel(KEY).seal("PING").copyOfRange(2, 6)
        assertNotEquals(a.toList(), b.toList())
    }

    private fun counterOf(pkt: ByteArray): Long {
        var ctr = 0L
        for (i in 6 until 14) ctr = (ctr shl 8) or (pkt[i].toLong() and 0xFF)
        return ctr
    }
}
