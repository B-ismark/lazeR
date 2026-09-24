package com.example.lanremote.net

import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketException
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.concurrent.thread

/**
 * RemoteClient against a fake laptop on a real loopback socket: the handshake's
 * binding, the refusal hints, key padding, and one attempt never touching another.
 */
class RemoteClientTest {

    private companion object {
        val KEY = ByteArray(32) { it.toByte() }
        const val TOKEN = "A1B2C3"
    }

    /** How the fake laptop answers a HELLO. */
    enum class Mode {
        Bound, Legacy, WrongEcho, BadKey, SecureRequired, Silent, PlainOk,
        /** A spoofed plaintext ERR arrives first, then the real laptop answers. */
        BadKeyThenBound,
        /** The first OK is lost, and a stale unbound CHAL arrives in between. */
        LostOkStaleChal,
        /** A bound CHAL, then — for that AUTH — only a captured unbound OK. */
        BoundChalPlainOk,
        /** The first challenge is never answered (its OK always lost); a second
         *  HELLO draws a fresh one that is. */
        FirstChalSpent,
    }

    private class FakeLaptop(val mode: Mode, val staleFirst: Boolean = false) {
        val sock = DatagramSocket(0, InetAddress.getLoopbackAddress())
        val port get() = sock.localPort
        private val ch = SecureChannel(KEY)            // the laptop's own send session
        private val stale = SecureChannel(KEY)         // "an earlier session" of it
        val received = CopyOnWriteArrayList<String>()
        private val t = thread(isDaemon = true) { loop() }

        private fun send(to: InetSocketAddress, b: ByteArray) =
            sock.send(DatagramPacket(b, b.size, to))

        private fun loop() {
            val buf = ByteArray(2048)
            var cnonce = ""
            var auths = 0
            var hellos = 0
            while (true) {
                val p = DatagramPacket(buf, buf.size)
                try { sock.receive(p) } catch (e: SocketException) { return }
                val from = p.socketAddress as InetSocketAddress
                val plain = String(p.data, 0, p.length)
                if (plain.startsWith("$TOKEN ")) {                 // v1 wire
                    received.add(plain)
                    when (mode) {
                        Mode.SecureRequired -> send(from, "ERR secure-required".toByteArray())
                        Mode.Silent -> {}
                        else -> if (plain == "$TOKEN HELLO") send(from, "OK".toByteArray())
                    }
                    continue
                }
                val text = SecureChannel(KEY).peek(p.data, p.length)?.text ?: continue
                received.add(text)
                val words = text.split(' ')
                when (words[0]) {
                    "HELLO" -> {
                        cnonce = words.getOrElse(1) { "" }
                        when (mode) {
                            Mode.Bound -> {
                                if (staleFirst) send(from, stale.seal("CHAL old ${"f".repeat(32)}"))
                                send(from, ch.seal("CHAL n1 $cnonce"))
                            }
                            Mode.Legacy -> send(from, ch.seal("CHAL n1"))
                            Mode.WrongEcho -> send(from, ch.seal("CHAL n1 ${"0".repeat(32)}"))
                            Mode.BadKey -> send(from, "ERR bad-key".toByteArray())
                            Mode.BadKeyThenBound -> {
                                send(from, "ERR bad-key".toByteArray())
                                send(from, ch.seal("CHAL n1 $cnonce"))
                            }
                            Mode.BoundChalPlainOk -> send(from, ch.seal("CHAL n1 $cnonce"))
                            Mode.FirstChalSpent ->
                                send(from, ch.seal("CHAL ${if (hellos++ == 0) "n0" else "n1"} $cnonce"))
                            Mode.LostOkStaleChal -> {
                                send(from, ch.seal("CHAL n1 $cnonce"))
                                send(from, stale.seal("CHAL old"))
                            }
                            // A replayed old-format OK, sent straight at the HELLO.
                            Mode.PlainOk -> send(from, ch.seal("OK"))
                            else -> {}
                        }
                    }
                    "AUTH" -> if (words.getOrNull(1) == "n1" &&
                        !(mode == Mode.LostOkStaleChal && auths++ == 0)) {
                        // A replayer holds only replies captured earlier, which echo
                        // an earlier HELLO's nonce, never this one's.
                        if (staleFirst) send(from, stale.seal("OK ${"f".repeat(32)}"))
                        if (mode == Mode.BoundChalPlainOk) {
                            send(from, stale.seal("OK"))
                            continue
                        }
                        send(from, ch.seal(if (mode == Mode.Legacy) "OK" else "OK ${
                            if (mode == Mode.WrongEcho) "0".repeat(32) else cnonce}"))
                    }
                    "PING" -> send(from, ch.seal("PONG"))
                    "VGET" -> {
                        if (staleFirst) send(from, stale.seal("VOL 99"))
                        send(from, ch.seal("VOL 42"))
                    }
                }
            }
        }

        fun close() { sock.close(); t.join(1000) }
    }

    private val laptops = mutableListOf<FakeLaptop>()
    private val client = RemoteClient()

    private fun laptop(mode: Mode, staleFirst: Boolean = false) =
        FakeLaptop(mode, staleFirst).also { laptops += it }

    @After
    fun tearDown() {
        client.disconnect()
        laptops.forEach { it.close() }
    }

    private fun connect(l: FakeLaptop, key: ByteArray? = KEY, requireBound: Boolean = false,
                        timeoutMs: Long = 1500) = runBlocking {
        client.connect("127.0.0.1", l.port, TOKEN, key, timeoutMs, requireBound)
    }

    @Test
    fun `a bound laptop connects bound and gets our nonce in HELLO`() {
        val l = laptop(Mode.Bound)
        assertEquals(ConnectOutcome(ConnectResult.Connected, bound = true), connect(l))
        val hello = l.received.first { it.startsWith("HELLO") }
        assertTrue(hello, Regex("HELLO [0-9a-f]{32}").matches(hello))
    }

    @Test
    fun `an older laptop still connects, unbound`() {
        assertEquals(ConnectOutcome(ConnectResult.Connected, bound = false),
            connect(laptop(Mode.Legacy)))
    }

    @Test
    fun `a device that has been bound refuses an unbound answer`() {
        assertEquals(ConnectResult.Outdated,
            connect(laptop(Mode.Legacy), requireBound = true).result)
    }

    @Test
    fun `a bare unbound OK is refused once the device has been bound`() {
        // No CHAL at all — just an authentic, captured "OK" from an older session.
        assertEquals(ConnectResult.Outdated,
            connect(laptop(Mode.PlainOk), requireBound = true).result)
    }

    @Test
    fun `an unbound OK before any challenge is not a connection`() {
        // Even for a device never bound: nothing ties that OK to this attempt, and
        // pinning it would report Connected to a laptop that never pinned us.
        assertEquals(ConnectResult.NoAnswer, connect(laptop(Mode.PlainOk)).result)
    }

    @Test
    fun `a stale unbound CHAL can't replace this handshake's bound one`() {
        val l = laptop(Mode.LostOkStaleChal)
        assertEquals(ConnectOutcome(ConnectResult.Connected, bound = true), connect(l))
        assertFalse(l.received.toString(), "AUTH old" in l.received)
    }

    @Test
    fun `after a bound CHAL, an unbound OK is refused even on a first connect`() {
        assertEquals(ConnectResult.NoAnswer, connect(laptop(Mode.BoundChalPlainOk)).result)
    }

    @Test
    fun `a challenge whose OK never comes is given up for a fresh one`() {
        // AUTH retries are capped, then a new HELLO draws a new challenge.
        val l = laptop(Mode.FirstChalSpent)
        assertEquals(ConnectResult.Connected, connect(l, timeoutMs = 4000).result)
        assertEquals("the first AUTH and three retries", 4, l.received.count { it == "AUTH n0" })
    }

    @Test
    fun `a spoofed refusal doesn't end an attempt the laptop then answers`() {
        assertEquals(ConnectResult.Connected, connect(laptop(Mode.BadKeyThenBound)).result)
    }

    @Test
    fun `replies echoing someone else's nonce are not accepted`() {
        assertEquals(ConnectResult.NoAnswer, connect(laptop(Mode.WrongEcho)).result)
    }

    @Test
    fun `a replayed reply from an earlier session is not what gets pinned`() {
        // The stale session's CHAL and OK are authentic under the key; only the
        // current session's OK echoes this HELLO. If the stale one were pinned, the
        // live session's PONG would then be refused.
        val l = laptop(Mode.Bound, staleFirst = true)
        assertEquals(ConnectResult.Connected, connect(l).result)
        // Its CHAL echoes another HELLO's nonce, so it is never answered.
        assertFalse(l.received.toString(), "AUTH old" in l.received)
        // The session pinned is the one that answered this HELLO — not merely the
        // first sealed packet to arrive after it (here the stale "VOL 99"), which a
        // replayer could send.
        assertEquals(42, runBlocking { client.queryVolume(1000) }?.level)
        assertTrue(runBlocking { client.ping(1000) })
    }

    @Test
    fun `the laptop's refusal hints become results`() {
        assertEquals(ConnectResult.WrongKey, connect(laptop(Mode.BadKey)).result)
        assertEquals(ConnectResult.NeedsQr, connect(laptop(Mode.SecureRequired), key = null).result)
    }

    @Test
    fun `typing is padded only for a bound laptop`() {
        for ((mode, padded) in listOf(Mode.Bound to true, Mode.Legacy to false)) {
            val l = laptop(mode)
            assertEquals(ConnectResult.Connected, connect(l).result)
            client.key("a")
            client.keySpecial("backspace")
            val got = waitFor(l) { it.startsWith("KEY") }
            assertEquals(2, got.size)
            for (body in got) {
                val n = body.toByteArray(Charsets.UTF_8).size
                if (padded) assertEquals(body, 0, n % Protocol.KEY_PAD_BUCKET)
                else assertFalse(body, '\u0000' in body)
            }
            assertEquals(listOf("KEY a", "KEYSP backspace"), got.map { it.trimEnd('\u0000') })
            client.disconnect()
        }
    }

    @Test
    fun `mouse up is sent twice`() {
        val l = laptop(Mode.Bound)
        assertEquals(ConnectResult.Connected, connect(l).result)
        client.mouseUp()
        assertEquals(listOf("MUP", "MUP"), waitFor(l, 2) { it == "MUP" })
    }

    @Test
    fun `a superseded attempt is cancelled and leaves the new session alone`() = runBlocking {
        val silent = laptop(Mode.Silent)
        val live = laptop(Mode.Bound)
        val stale = async {
            client.connect("127.0.0.1", silent.port, TOKEN, KEY, timeoutMs = 5000)
        }
        delay(300)   // the stale attempt is now blocked waiting for its CHAL
        val now = client.connect("127.0.0.1", live.port, TOKEN, KEY, timeoutMs = 1500)
        assertEquals(ConnectResult.Connected, now.result)
        assertEquals(ConnectResult.Cancelled, stale.await().result)
        assertTrue("the stale attempt tore down the live session", client.ping(1000))
    }

    @Test
    fun `cancelConnect ends an attempt in flight`() = runBlocking {
        val silent = laptop(Mode.Silent)
        val attempt = async {
            client.connect("127.0.0.1", silent.port, TOKEN, KEY, timeoutMs = 5000)
        }
        delay(300)
        val t0 = System.currentTimeMillis()
        client.cancelConnect()
        assertEquals(ConnectResult.Cancelled, attempt.await().result)
        assertTrue(System.currentTimeMillis() - t0 < 2000)
    }

    private fun waitFor(l: FakeLaptop, n: Int = 2, match: (String) -> Boolean): List<String> {
        val deadline = System.currentTimeMillis() + 2000
        while (System.currentTimeMillis() < deadline) {
            val got = l.received.filter(match)
            if (got.size >= n) return got
            Thread.sleep(20)
        }
        return l.received.filter(match)
    }
}
