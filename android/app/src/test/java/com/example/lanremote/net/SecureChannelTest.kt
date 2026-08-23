package com.example.lanremote.net

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The Kotlin half of the cross-implementation contract.
 *
 * The secure wire is implemented twice — here and in the Python server's `Wire` —
 * and the two must agree byte for byte or pairing silently breaks. [GOLDEN_L3_HEX]
 * is the SAME frozen packet asserted by `server/tests/test_wire.py` (class
 * `GoldenVectorL3`). Change the header layout, the nonce derivation, or the AAD on
 * either side and exactly one of the two suites goes red, so the skew is caught in
 * CI instead of on a user's phone.
 *
 * The legacy L2 dialect was accepted through v2.x and is now removed; its golden
 * vector was deleted from BOTH suites together (the documented procedure). One L2
 * packet is kept inline below purely to prove open() refuses it.
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

        /** L3 (current): sid(8)|counter(4) = 0102030405060708 / 1, "MOVE 3 -4".
         *  Must match GoldenVectorL3.HEX in test_wire.py. */
        const val GOLDEN_L3_HEX =
            "4c3301020304050607080000000128" +
                "c4b7f683f190dd940a5b824850a6437fe2cda8b9655271b8"

        const val GOLDEN_PLAINTEXT = "MOVE 3 -4"

        fun hex(s: String) = ByteArray(s.length / 2) {
            s.substring(it * 2, it * 2 + 2).toInt(16).toByte()
        }
    }

    // ── the anchor ───────────────────────────────────────────────────────────

    @Test
    fun `opens the L3 golden packet produced by the python server`() {
        val pkt = hex(GOLDEN_L3_HEX)
        assertEquals(GOLDEN_PLAINTEXT, SecureChannel(KEY).open(pkt, pkt.size))
    }

    @Test
    fun `L3 golden packet layout is frozen`() {
        val pkt = hex(GOLDEN_L3_HEX)
        // Field offsets are part of the contract, not an implementation detail:
        // magic(2) | sid(8) | counter(4 BE) | ciphertext+tag.
        assertEquals(14 + GOLDEN_PLAINTEXT.length + 16, pkt.size)
        assertEquals('L'.code.toByte(), pkt[0])
        assertEquals('3'.code.toByte(), pkt[1])
        assertArrayEquals(byteArrayOf(1, 2, 3, 4, 5, 6, 7, 8), pkt.copyOfRange(2, 10))
        var ctr = 0L
        for (i in 10 until 14) ctr = (ctr shl 8) or (pkt[i].toLong() and 0xFF)
        assertEquals(1L, ctr)
    }

    @Test
    fun `an L2 packet is no longer readable`() {
        // The legacy dialect is removed: open() must refuse it outright rather than
        // mis-slice its 4-byte sid as the first bytes of an 8-byte one.
        val l2 = hex(
            "4c32010203040000000000000001" +
                "9cbe917b3b744aee3cc2838803178e6aa93726c156c2f73bb4")
        assertNull(SecureChannel(KEY).open(l2, l2.size))
    }

    // ── inbound replay guard (mirrors the server's watermark) ────────────────

    @Test
    fun `replayed reply is rejected`() {
        val pkt = hex(GOLDEN_L3_HEX)
        val ch = SecureChannel(KEY)
        assertEquals(GOLDEN_PLAINTEXT, ch.open(pkt, pkt.size))
        // Same sid, same counter ⇒ not strictly greater ⇒ dropped. Without this a
        // captured OK/PONG could be replayed at the phone to fake liveness.
        assertNull(ch.open(pkt, pkt.size))
    }

    @Test
    fun `reply from a different server session is rejected`() {
        val ch = SecureChannel(KEY)
        val golden = hex(GOLDEN_L3_HEX)
        assertEquals(GOLDEN_PLAINTEXT, ch.open(golden, golden.size))

        // A *cryptographically valid* packet under the same key but a different
        // session id — exactly what a second server (or a restarted one) emits.
        // It must be refused by the pinned-sid check, not by a tag failure, so the
        // packet is sealed properly rather than byte-flipped.
        val impostor = SecureChannel(KEY).seal("OK")
        assertNotEquals(golden.copyOfRange(2, 10).toList(),
                        impostor.copyOfRange(2, 10).toList())
        assertNull(ch.open(impostor, impostor.size))
    }

    @Test
    fun `tampered tag is rejected`() {
        val pkt = hex(GOLDEN_L3_HEX)
        pkt[pkt.size - 1] = (pkt[pkt.size - 1].toInt() xor 0x01).toByte()
        assertNull(SecureChannel(KEY).open(pkt, pkt.size))
    }

    @Test
    fun `too-short and non-v2 packets are rejected`() {
        val ch = SecureChannel(KEY)
        val pkt = hex(GOLDEN_L3_HEX)
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
        val pkt = hex(GOLDEN_L3_HEX)
        val padded = pkt.copyOf(pkt.size + 20)
        assertEquals(GOLDEN_PLAINTEXT, SecureChannel(KEY).open(padded, pkt.size))
    }

    // ── outbound layout ──────────────────────────────────────────────────────

    @Test
    fun `seal emits the L3 header with a monotonic counter from one`() {
        val ch = SecureChannel(KEY)
        val first = ch.seal("PING")
        val second = ch.seal("PING")

        // Deliberately no magic-byte assertion split across two tests: this one owns
        // both the dialect marker and the counter/nonce-uniqueness contract.
        assertEquals('L'.code.toByte(), first[0])
        assertEquals('3'.code.toByte(), first[1])
        assertEquals(14 + "PING".length + 16, first.size)
        assertEquals(1L, counterOf(first))
        assertEquals(2L, counterOf(second))
        // Same session id across sends...
        assertArrayEquals(first.copyOfRange(2, 10), second.copyOfRange(2, 10))
        // ...but distinct nonces, so identical plaintext never repeats on the wire.
        assertNotEquals(first.toList(), second.toList())
    }

    @Test
    fun `round trip preserves payload including spaces and unicode`() {
        // A fresh channel with the same key stands in for the server's Wire: if
        // seal's nonce/AAD construction ever drifts from what open expects, or the
        // payload isn't handled as UTF-8, these break.
        for (body in listOf("OK", "VOL 42", "KEY hello  world", "KEY naïve 🙂",
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
        val a = SecureChannel(KEY).seal("PING").copyOfRange(2, 10)
        val b = SecureChannel(KEY).seal("PING").copyOfRange(2, 10)
        assertNotEquals(a.toList(), b.toList())
    }

    /** L3 counter: 4 bytes at offset 10. */
    private fun counterOf(pkt: ByteArray): Long = beAt(pkt, 10, 4)

    private fun beAt(buf: ByteArray, off: Int, width: Int): Long {
        var v = 0L
        for (i in off until off + width) v = (v shl 8) or (buf[i].toLong() and 0xFF)
        return v
    }
}