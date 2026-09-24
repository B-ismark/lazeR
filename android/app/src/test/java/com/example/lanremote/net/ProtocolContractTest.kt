package com.example.lanremote.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The phone's half of `SharedContract` in server/tests/test_wire.py. Both suites pin
 * the same literals, so changing one side alone turns one of them red.
 */
class ProtocolContractTest {

    private companion object {
        /** Must equal SharedContract.GOLDEN_URI (build_uri of the test key). */
        const val GOLDEN_URI = "lazer://192.168.1.20:50505/?token=A1B2C3&name=Dearth" +
            "&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    }

    @Test
    fun `shared constants match the server`() {
        assertEquals(50505, Protocol.DEFAULT_PORT)
        assertEquals("_lazer._udp.", Protocol.SERVICE_TYPE)
        assertEquals(12_000L, Protocol.SERVER_IDLE_MS)
        assertEquals("secure-required", Protocol.REFUSE_SECURE_REQUIRED)
        assertEquals("bad-key", Protocol.REFUSE_BAD_KEY)
    }

    @Test
    fun `the idle poll fits twice inside the server's idle drop`() {
        assertTrue(Protocol.IDLE_POLL_MS * 2 < Protocol.SERVER_IDLE_MS)
    }

    @Test
    fun `parses the server's golden QR`() {
        val p = parsePairingUri(GOLDEN_URI).getOrThrow()
        assertEquals(PairingUri("192.168.1.20", 50505, "A1B2C3", "Dearth",
            "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"), p)
    }

    @Test
    fun `a QR without a port uses the default and a lowercase token is uppercased`() {
        val p = parsePairingUri("lazer://10.0.0.5/?token=abc123").getOrThrow()
        assertEquals(Protocol.DEFAULT_PORT, p.port)
        assertEquals("ABC123", p.token)
        assertEquals("10.0.0.5", p.name)   // no name ⇒ the host
        assertEquals("", p.key)
    }

    @Test
    fun `a second token appended to a genuine QR cannot override the first`() {
        val p = parsePairingUri("$GOLDEN_URI&token=EVIL00").getOrThrow()
        assertEquals("A1B2C3", p.token)
    }

    @Test
    fun `refuses what isn't a LazeR pairing`() {
        fun err(raw: String) =
            (parsePairingUri(raw).exceptionOrNull() as PairingException).error
        assertEquals(PairingError.NotLazer, err("https://192.168.1.20/?token=A1B2C3"))
        assertEquals(PairingError.NotLazer, err("not a uri at all"))
        assertEquals(PairingError.NoToken, err("lazer://192.168.1.20:50505/?name=x"))
        assertEquals(PairingError.BadHost, err("lazer://127.0.0.1:50505/?token=A1B2C3"))
        assertEquals(PairingError.BadHost, err("lazer://evil.example.com/?token=A1B2C3"))
    }

    @Test
    fun `pairable hosts are unicast literals or mDNS names`() {
        for (h in listOf("10.1.2.3", "172.16.0.1", "192.168.0.10", "169.254.3.4",
                         "100.64.1.2", "8.8.8.8", "1.0.0.1", "223.255.255.254",
                         "laptop.local", "LAPTOP.LOCAL.", "fd12::1", "fe80::1", "2001:db8::1",
                         "10.0.0.0", "1:2:3:4:5:6:7:8", "1::", "::ffff:192.168.1.5",
                         "::ffff:c0a8:105", "64:ff9b::1.2.3.4", "1:2:3:4:5:6:1.2.3.4")) {
            assertTrue("should accept $h", isPairableHost(h))
        }
        for (h in listOf("127.0.0.1", "127.9.9.9", "0.0.0.0", "0.1.2.3", "224.0.0.251",
                         "239.1.1.1", "255.255.255.255", "::", "::1", "0:0:0:0:0:0:0:1",
                         "::0001", "0::1", "ff02::fb", "fe80::g", "12345::1",
                         // IPv4-mapped / -compatible: judged by the IPv4 inside.
                         "::ffff:127.0.0.1", "::ffff:7f00:1", "::0.0.0.1", "::ffff:0.0.0.0",
                         "::0.0.0.0", "::ffff:224.0.0.251", "::2", "::1.2.3.4",
                         // Malformed IPv6.
                         "1:2", "1::2::3", "1:2:3:4:5:6:7:8:9", "1:2:3:4:5:6:7::8", ":1::",
                         "1.2:3::4.5.6.7", "::ffff:1.2.3", "fe80::1%wlan0",
                         // Leading zeros: Android reads them as octal.
                         "012.0.0.1", "1.2.3.09", "00.1.1.1", "::ffff:08.1.1.1",
                         "example.com", "laptop", "", "256.1.1.1", "10.256.0.1",
                         "192.168.1", "192.168.1.1.1", "1e1.0.0.1", "١٠.0.0.1")) {
            assertFalse("should refuse $h", isPairableHost(h))
        }
    }

    @Test
    fun `key bodies pad to whole buckets of UTF-8 bytes`() {
        val b = Protocol.KEY_PAD_BUCKET
        for (body in listOf("KEY a", "KEYSP enter", "KEYSP backspace", "KEY naïve 🙂",
                            "KEY " + "x".repeat(40))) {
            val padded = padKeyBody(body)
            val n = padded.toByteArray(Charsets.UTF_8).size
            assertEquals("$body → $n bytes", 0, n % b)
            assertTrue(n - body.toByteArray(Charsets.UTF_8).size < b)
            assertEquals(body, padded.trimEnd('\u0000'))
        }
        // The point of it: a letter and a special key look the same on the wire.
        assertEquals(padKeyBody("KEY a").length,
            padKeyBody("KEYSP backspace").toByteArray(Charsets.UTF_8).size)
        // Already on a boundary: left alone.
        val exact = "KEY " + "y".repeat(b - 4)
        assertEquals(exact, padKeyBody(exact))
    }

    @Test
    fun `volume replies carry mute from laptops that report it`() {
        fun v(t: String) = parseVolume(t.split(' '))
        assertEquals(VolumeReading(42, true), v("VOL 42 1"))
        assertEquals(VolumeReading(42, false), v("VOL 42 0"))
        assertEquals("an older laptop", VolumeReading(42, null), v("VOL 42"))
        assertEquals(null, v("VOL"))
    }
}
