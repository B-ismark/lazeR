package com.example.lanremote.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.security.SecureRandom
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException

/** How a connect attempt ended. */
enum class ConnectResult {
    Connected,
    /** Nothing usable came back: laptop off or asleep, wrong address, firewall. */
    NoAnswer,
    /** The laptop requires encryption and this is a typed-code (keyless) pairing. */
    NeedsQr,
    /** The laptop can't read our packets: it was re-paired since this phone was. */
    WrongKey,
    /** The laptop answered in the older, unbound handshake, which this device has
     *  seen it outgrow — an old LazeR on the laptop, or a replayed reply. */
    Outdated,
    /** A newer connect (or [RemoteClient.cancelConnect]) superseded this one. */
    Cancelled,
}

/** [bound] = the laptop echoed our handshake nonce, so it is new enough to bind
 *  replies and to strip key padding. */
data class ConnectOutcome(val result: ConnectResult, val bound: Boolean = false)

/**
 * UDP client speaking the protocol in PROTOCOL.md. On the secure wire every packet
 * is sealed with the pairing key (no token on the wire); on the legacy plaintext
 * wire every packet is prefixed with the token: "<TOKEN> <VERB> [rest]".
 *
 * Each successful connect owns a [Session]: its socket, its secure channel and its
 * sender thread. Nothing is shared between attempts, so an older attempt still
 * finishing on an IO thread can only ever close its own socket — never the socket
 * of the connection that replaced it. Fire-and-forget sends are queued onto the
 * session's single sender thread (keeps packet order, avoids
 * NetworkOnMainThreadException) and are lossy by design — fine for high-rate
 * MOVE/SCROLL. The handshake and the probes are the only calls that wait.
 */
class RemoteClient {

    private class Session(
        val socket: DatagramSocket,
        val address: InetAddress,
        val port: Int,
        val token: String,
        val channel: SecureChannel?,   // non-null ⇒ encrypted wire
        val padKeys: Boolean,
    ) {
        val sender: ExecutorService = Executors.newSingleThreadExecutor()
        private val sendLock = Any()   // serialize counter-assign + send

        /** Blocking send on the current thread. Encrypted if on the secure wire.
         *  Locked so the secure counter is assigned and the packet sent atomically —
         *  otherwise concurrent senders (queued actions vs. PING/VGET) could deliver
         *  counters out of order and the server's replay guard would drop them. */
        fun sendNow(body: String) {
            synchronized(sendLock) {
                val ch = channel
                val payload = if (ch != null) ch.seal(body)
                else "$token $body".toByteArray(Charsets.UTF_8)
                socket.send(DatagramPacket(payload, payload.size, address, port))
            }
        }

        fun decode(p: DatagramPacket): String? {
            val ch = channel
            return if (ch != null) ch.open(p.data, p.length)
            else String(p.data, 0, p.length).trim()
        }

        /** Hard teardown: abandon anything queued. */
        fun close() {
            sender.shutdownNow()
            try { socket.close() } catch (_: Exception) {}
        }
    }

    @Volatile private var session: Session? = null

    // The socket of the attempt in flight, and a counter that makes "am I still the
    // newest attempt?" a single comparison. Both guarded by [attemptLock].
    private val attemptLock = Any()
    private var pending: DatagramSocket? = null
    private var attempt = 0L

    private val rng = SecureRandom()

    /**
     * Open a socket and perform the handshake.
     * @param key the raw 256-bit pairing key; null ⇒ plaintext wire (typed code).
     * @param requireBound refuse the older, unbound handshake. Set once this device
     *   has completed a bound one, so a replayed old-format reply can't be taken
     *   for the laptop.
     */
    suspend fun connect(
        host: String,
        port: Int,
        token: String,
        key: ByteArray? = null,
        timeoutMs: Long = 2000,
        requireBound: Boolean = false,
    ): ConnectOutcome = withContext(Dispatchers.IO) {
        val me: Long
        val sock: DatagramSocket
        synchronized(attemptLock) {
            // A superseded attempt is still blocked in receive() on its own socket.
            // Closing it wakes it now instead of letting it finish a handshake that
            // would re-pin the laptop to a socket nobody is reading.
            try { pending?.close() } catch (_: Exception) {}
            me = ++attempt
            sock = try { DatagramSocket() } catch (e: Exception) {
                pending = null
                return@withContext ConnectOutcome(ConnectResult.NoAnswer)
            }
            pending = sock
        }
        session?.let { old -> session = null; old.close() }

        fun superseded() = synchronized(attemptLock) { me != attempt }

        try {
            val addr = InetAddress.getByName(host)
            val ch = key?.let { SecureChannel(it) }
            val hs = handshake(sock, addr, port, token, ch, timeoutMs, requireBound)
            if (hs.result != ConnectResult.Connected) {
                sock.close()
                synchronized(attemptLock) { if (pending === sock) pending = null }
                return@withContext if (superseded()) ConnectOutcome(ConnectResult.Cancelled) else hs
            }
            drainStale(sock)
            // AFTER the drain, not before: drainStale sets a 60ms read timeout and
            // doesn't restore it.
            sock.soTimeout = 0
            val s = Session(sock, addr, port, token, ch, padKeys = hs.bound)
            synchronized(attemptLock) {
                if (me != attempt) {
                    s.close()
                    return@withContext ConnectOutcome(ConnectResult.Cancelled)
                }
                pending = null
                session = s
            }
            hs
        } catch (e: Exception) {
            try { sock.close() } catch (_: Exception) {}
            synchronized(attemptLock) { if (pending === sock) pending = null }
            ConnectOutcome(if (superseded()) ConnectResult.Cancelled else ConnectResult.NoAnswer)
        }
    }

    /** Abandon the attempt in flight, if any. It returns [ConnectResult.Cancelled]. */
    fun cancelConnect() {
        synchronized(attemptLock) {
            attempt++
            try { pending?.close() } catch (_: Exception) {}
            pending = null
        }
    }

    /** Discard datagrams already queued from the handshake — the server answers
     *  every retried AUTH, so several stale OKs can be waiting. Without this the
     *  first VGET/PING would read one and the watchdog would misfire a reconnect.
     *  Bounded by BOTH an idle timeout and a wall-clock deadline + packet cap, so a
     *  flood of datagrams can't trap us here. */
    private fun drainStale(sock: DatagramSocket) {
        try {
            sock.soTimeout = 60
            val buf = ByteArray(256)
            val deadline = System.currentTimeMillis() + 200
            var n = 0
            while (n++ < 64 && System.currentTimeMillis() < deadline) {
                try {
                    sock.receive(DatagramPacket(buf, buf.size))
                } catch (e: SocketTimeoutException) {
                    break
                }
            }
        } catch (e: Exception) {
            // best effort
        }
    }

    /** A fresh client nonce: 16 random bytes as 32 hex chars, inside the server's
     *  accepted 16–64 [A-Za-z0-9_-] range. */
    private fun newClientNonce(): String {
        val b = ByteArray(16).also { rng.nextBytes(it) }
        return b.joinToString("") { "%02x".format(it) }
    }

    /**
     * Handshake to (addr,port) until the server pins us: send HELLO, answer the
     * server's encrypted CHAL with an AUTH echoing its nonce, then get OK. The
     * challenge proves freshness to the SERVER — a replayed HELLO/AUTH can't
     * complete it.
     *
     * The client nonce in our HELLO does the same for US. The key outlives every
     * session, so any sealed CHAL or OK captured earlier still decrypts; a laptop
     * that echoes our nonce in its CHAL and OK ("bound") proves the reply answers
     * this HELLO. Only that reply is allowed to pin the server's session. An older
     * laptop ignores the nonce and answers unbound, which is accepted unless
     * [requireBound] — once a device has answered bound, it always will, so an
     * unbound reply from it is either a downgrade or a replay.
     *
     * The laptop's plaintext `ERR <reason>` hints are unauthenticated, so they only
     * shape the failure message: the attempt keeps listening for a real answer
     * until the deadline, and anyone on the LAN able to send one gains nothing.
     *
     * Resends on a short [stepMs] cadence for up to [budgetMs], since UDP can drop
     * any leg. If OK is lost after AUTH (the challenge is single-use, so blind AUTH
     * resends would stall), we drop the nonce and re-HELLO to draw a fresh one.
     * v1 (no channel) just sends a plaintext HELLO and waits for OK.
     */
    private fun handshake(
        sock: DatagramSocket, addr: InetAddress, port: Int, token: String,
        ch: SecureChannel?, budgetMs: Long, requireBound: Boolean, stepMs: Int = 250,
    ): ConnectOutcome {
        // Generously sized: a sealed CHAL is already ~90 bytes with the echo, and
        // DatagramPacket TRUNCATES silently rather than erroring, so a reply that
        // outgrew the buffer would fail to decrypt with no diagnostic at all.
        val buf = ByteArray(512)
        sock.soTimeout = stepMs
        val deadline = System.currentTimeMillis() + budgetMs
        val cnonce = newClientNonce()
        var snonce: String? = null
        var bound = false
        var authTries = 0
        // An unbound OK carries nothing tying it to this attempt, so it only counts
        // once we have sent an AUTH it could be answering. Before that it is a
        // replay: pinning it would report Connected to a laptop that never pinned us.
        var authed = false
        var refusal: ConnectResult? = null
        var sawUnbound = false

        fun send(pkt: ByteArray): Boolean = try {
            sock.send(DatagramPacket(pkt, pkt.size, addr, port)); true
        } catch (e: Exception) { false }

        fun sendHello(): Boolean = send(
            if (ch != null) ch.seal("HELLO $cnonce")
            else "$token HELLO".toByteArray(Charsets.UTF_8)
        )

        fun sendAuth(n: String): Boolean { authed = true; return send(ch!!.seal("AUTH $n")) }

        fun failure() = ConnectOutcome(
            refusal ?: if (sawUnbound) ConnectResult.Outdated else ConnectResult.NoAnswer)

        if (!sendHello()) return failure()
        while (System.currentTimeMillis() < deadline) {
            try {
                val p = DatagramPacket(buf, buf.size)
                sock.receive(p)
                val plain = plaintextRefusal(p)
                if (plain != null) {
                    refusal = plain
                    continue
                }
                if (ch == null) {
                    if (String(p.data, 0, p.length).trim() == "OK") {
                        return ConnectOutcome(ConnectResult.Connected)
                    }
                    continue
                }
                val o = ch.peek(p.data, p.length) ?: continue
                val words = o.text.split(' ')
                when (words[0]) {
                    "CHAL" -> {
                        val n = words.getOrNull(1) ?: continue
                        val echo = words.getOrNull(2)
                        if (echo != null) {
                            if (echo != cnonce) continue          // an old handshake's CHAL
                            bound = true
                        } else if (requireBound || bound) {
                            // A downgrade — or, after a bound CHAL in this same
                            // handshake, a stale one that would replace the live
                            // challenge with one the laptop has already spent.
                            if (requireBound) sawUnbound = true
                            continue
                        }
                        snonce = n; authTries = 0
                        if (!sendAuth(n)) return failure()
                    }
                    "OK" -> {
                        val echo = words.getOrNull(1)
                        val ok = if (echo != null) echo == cnonce
                        else !requireBound && !bound && authed
                        if (echo == null && requireBound) sawUnbound = true
                        if (ok) {
                            ch.pin(o)
                            return ConnectOutcome(ConnectResult.Connected, bound = echo != null)
                        }
                    }
                }
            } catch (e: SocketTimeoutException) {
                val n = snonce
                if (n != null && authTries < 3) {
                    authTries++
                    if (!sendAuth(n)) return failure()
                } else {                        // no challenge yet, or OK lost → re-HELLO
                    snonce = null
                    if (!sendHello()) return failure()
                }
            }
        }
        return failure()
    }

    /** The laptop's plaintext `ERR <reason>` refusal, as a result; null otherwise. */
    private fun plaintextRefusal(p: DatagramPacket): ConnectResult? {
        if (p.length < 4 || p.length > 64) return null
        val text = String(p.data, 0, p.length, Charsets.US_ASCII).trim()
        return when (text) {
            "ERR ${Protocol.REFUSE_SECURE_REQUIRED}" -> ConnectResult.NeedsQr
            "ERR ${Protocol.REFUSE_BAD_KEY}" -> ConnectResult.WrongKey
            else -> null
        }
    }

    fun move(dx: Int, dy: Int) = send("MOVE $dx $dy")
    fun scroll(dx: Int, dy: Int) = send("SCROLL $dx $dy")
    fun zoom(steps: Int) = send("ZOOM $steps")             // ctrl+wheel pinch zoom (+ in, − out)
    fun click() = send("CLICK")
    fun rightClick() = send("RCLICK")
    fun middleClick() = send("MCLICK")
    fun mouseDown() = send("MDOWN")                        // drag-lock press
    fun mouseUp() {
        // Twice: a lost MUP leaves the laptop's button held until the next click
        // (a drag that never ends). The server's release is idempotent.
        send("MUP")
        send("MUP")
    }
    fun setVolume(percent: Int) = send("VOL ${percent.coerceIn(0, 100)}")
    fun setBrightness(percent: Int) = send("BRIGHT ${percent.coerceIn(0, 100)}")
    fun media(action: String) = send("MEDIA $action")     // play_pause | next | prev
    fun key(text: String) = sendKey("KEY $text")           // literal text, spaces ok
    fun keySpecial(name: String) = sendKey("KEYSP $name")  // enter | backspace | ...
    fun combo(spec: String) = send("COMBO $spec")          // "ctrl c", "alt tab", ...
    fun appSwitch(dir: String) = send("ASW $dir")          // next | prev | end (Alt-Tab session)
    fun system(action: String) = send("SYS $action")       // lock | sleep | mute

    /** Typing goes out padded when the laptop strips padding; see [padKeyBody]. */
    private fun sendKey(body: String) {
        val s = session ?: return
        send(s, if (s.padKeys) padKeyBody(body) else body)
    }

    /**
     * Send a request, then wait up to [timeoutMs] for a reply whose first token is
     * [prefix], **consuming and discarding** any other replies that arrive first.
     *
     * A reply to the *previous* probe (e.g. a late PONG) can be sitting in the
     * buffer when the next probe reads — a single read would grab the wrong type and
     * report a false miss. Looping until the expected prefix (or the deadline) makes
     * each probe robust to that cross-talk, and means one probe's reply also clears
     * the other's staleness.
     * @return the whitespace-split reply tokens, or null on timeout.
     */
    private fun awaitReply(s: Session, prefix: String, timeoutMs: Int): List<String>? {
        val deadline = System.currentTimeMillis() + timeoutMs
        val buf = ByteArray(256)   // see handshake: silent truncation, so leave room
        while (true) {
            val remaining = (deadline - System.currentTimeMillis()).toInt()
            if (remaining <= 0) return null
            try {
                s.socket.soTimeout = remaining
                val reply = DatagramPacket(buf, buf.size)
                s.socket.receive(reply)
                val parts = (s.decode(reply) ?: "").split(" ")
                if (parts.getOrNull(0) == prefix) return parts
                // other reply type (stale cross-talk) — discard and keep waiting
            } catch (e: Exception) {
                return null
            }
        }
    }

    private suspend fun probe(verb: String, prefix: String, timeoutMs: Int): List<String>? =
        withContext(Dispatchers.IO) {
            val s = session ?: return@withContext null
            try {
                s.sendNow(verb)
                awaitReply(s, prefix, timeoutMs)
            } catch (e: Exception) {
                null
            } finally {
                try { s.socket.soTimeout = 0 } catch (_: Exception) {}
            }
        }

    /** Ask the laptop for its current volume (VGET -> "VOL n [m]"), or null. */
    suspend fun queryVolume(timeoutMs: Int = 400): VolumeReading? =
        probe("VGET", "VOL", timeoutMs)?.let(::parseVolume)

    /** The laptop's display brightness (BGET -> "BRI n"), or null on timeout / no
     *  brightness backend. */
    suspend fun queryBrightness(timeoutMs: Int = 400): Int? =
        probe("BGET", "BRI", timeoutMs)?.getOrNull(1)?.toIntOrNull()

    /** Liveness probe: send PING, expect "PONG". Used by the reconnect watchdog. */
    suspend fun ping(timeoutMs: Int = 500): Boolean = probe("PING", "PONG", timeoutMs) != null

    /**
     * Leave cleanly, actually getting the BYE onto the wire.
     *
     * The BYE and the socket close are queued as one ordered task on the session's
     * sender thread, then the executor is shut down with shutdown() (which lets the
     * queue drain) rather than shutdownNow(), which would throw the BYE away and
     * leave the server waiting out its idle timeout. Sending inline isn't an option
     * either: this runs on the main thread, where a socket write is a
     * NetworkOnMainThreadException.
     */
    fun disconnect() {
        cancelConnect()
        val s = session ?: return
        session = null
        try {
            s.sender.execute {
                try {
                    s.sendNow("BYE")
                } catch (_: Exception) {
                    // best effort — the idle timeout is the backstop
                }
                try { s.socket.close() } catch (_: Exception) {}
            }
            s.sender.shutdown()
        } catch (_: RejectedExecutionException) {
            // Already shut down; nothing will run our task, so close it here.
            try { s.socket.close() } catch (_: Exception) {}
        }
    }

    /** Queue a packet on the current session's sender thread. */
    private fun send(body: String) {
        send(session ?: return, body)
    }

    private fun send(s: Session, body: String) {
        try {
            s.sender.execute {
                try {
                    s.sendNow(body)
                } catch (_: Exception) {
                    // lossy by design
                }
            }
        } catch (_: RejectedExecutionException) {
            // The session was closed between the read and the queue: drop it.
        }
    }
}

/** The laptop's volume, 0..100, and whether it is muted — null from a laptop too
 *  old to say, or one whose audio backend can't tell. */
data class VolumeReading(val level: Int, val muted: Boolean?)

/** A `VOL n [m]` reply, split into words. */
internal fun parseVolume(words: List<String>): VolumeReading? {
    val level = words.getOrNull(1)?.toIntOrNull() ?: return null
    val muted = when (words.getOrNull(2)) { "1" -> true; "0" -> false; else -> null }
    return VolumeReading(level, muted)
}
