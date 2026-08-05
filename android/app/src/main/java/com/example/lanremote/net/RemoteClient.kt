package com.example.lanremote.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * UDP client speaking the protocol in PROTOCOL.md. Every packet is prefixed
 * with the shared token: "<TOKEN> <VERB> [rest]".
 *
 * One long-lived DatagramSocket. Fire-and-forget sends are queued onto a single
 * sender thread (keeps packet order, avoids NetworkOnMainThreadException) and are
 * lossy by design — fine for high-rate MOVE/SCROLL. The handshake and the volume
 * query are the only calls that wait for a reply.
 */
class RemoteClient {

    // Written on the IO dispatcher (connect) and on the sender thread (teardown),
    // read on both plus the main thread — so publication has to be guaranteed
    // rather than assumed.
    @Volatile private var socket: DatagramSocket? = null
    @Volatile private var address: InetAddress? = null
    @Volatile private var port: Int = 0
    @Volatile private var token: String = ""
    @Volatile private var channel: SecureChannel? = null  // non-null ⇒ encrypted v2 wire
    @Volatile private var sender: ExecutorService? = null
    private val sendLock = Any()                 // serialize counter-assign + send

    /** How the live session is routed. LAN = direct local; DIRECT = off-LAN via a
     *  hole-punched path (near-local latency); RELAY = off-LAN through the
     *  rendezvous (higher RTT). The watchdog reads this to widen its timeouts on
     *  the slower remote paths so transient relay latency isn't seen as a drop. */
    enum class Mode { LAN, DIRECT, RELAY }
    @Volatile var mode: Mode = Mode.LAN
        private set
    val isRemote: Boolean get() = mode != Mode.LAN

    /**
     * Open the socket and perform the HELLO handshake.
     * @param key the 256-bit secret from the QR (base64url); blank ⇒ plaintext wire.
     * @return true if the server's (decrypted) reply was "OK" within [timeoutMs].
     */
    suspend fun connect(
        host: String,
        port: Int,
        token: String,
        key: String = "",
        timeoutMs: Long = 2000,
    ): Boolean = withContext(Dispatchers.IO) {
        close()
        try {
            val addr = InetAddress.getByName(host)
            val sock = DatagramSocket()
            socket = sock
            address = addr
            this@RemoteClient.port = port
            this@RemoteClient.token = token
            channel = SecureChannel.keyFromBase64(key)?.let { SecureChannel(it) }
            mode = Mode.LAN

            // HELLO → (encrypted CHAL → AUTH) → OK on the secure wire; plain HELLO →
            // OK on v1. Same handshake as the remote path, just a tighter budget.
            val ok = doHandshake(sock, addr, port, timeoutMs)
            sock.soTimeout = 0
            if (ok) sender = Executors.newSingleThreadExecutor() else close()
            ok
        } catch (e: Exception) {
            close()
            false
        }
    }

    /**
     * Off-LAN connect via the rendezvous coordinator (see PROTOCOL.md). Requires
     * the secure [key] — remote access is v2-only. Tries a direct UDP hole-punch
     * to the laptop's public endpoint first; on failure (symmetric / carrier NAT)
     * falls back to relaying the still-encrypted packets through the rendezvous.
     *
     * On success the socket target is left pointed at the working path (the peer
     * for a direct punch, the rendezvous for relay) so every later send() just
     * works. @return true once the server's OK comes back over that path.
     */
    suspend fun connectRemote(
        rdvHost: String,
        rdvPort: Int,
        token: String,
        key: String,
    ): Boolean = withContext(Dispatchers.IO) {
        close()
        val rawKey = SecureChannel.keyFromBase64(key) ?: return@withContext false
        try {
            val sock = DatagramSocket()
            socket = sock
            this@RemoteClient.token = token
            channel = SecureChannel(rawKey)
            val room = Rendezvous.roomId(rawKey)
            val rdvAddr = InetAddress.getByName(rdvHost)
            val reg = "REG P $room".toByteArray(Charsets.UTF_8)

            // Phase A — register, learn the laptop's public endpoint (PEER reply).
            // Bound on WALL-CLOCK time, not accumulated timeouts: otherwise a hostile
            // rendezvous streaming non-PEER datagrams (each returns before the socket
            // timeout) would spin this loop forever (C-F1).
            var peer: InetSocketAddress? = null
            val buf = ByteArray(256)
            sock.soTimeout = 300
            val deadline = System.currentTimeMillis() + 2500
            var lastReg = 0L
            while (System.currentTimeMillis() < deadline && peer == null) {
                val nowMs = System.currentTimeMillis()
                if (nowMs - lastReg >= 900) {           // (re)announce ~every 0.9s
                    sock.send(DatagramPacket(reg, reg.size, rdvAddr, rdvPort))
                    lastReg = nowMs
                }
                try {
                    val p = DatagramPacket(buf, buf.size)
                    sock.receive(p)
                    val line = String(p.data, 0, p.length).trim()
                    if (line.startsWith("PEER ")) {
                        val parts = line.split(" ")
                        val ip = parts.getOrNull(1)
                        val pt = parts.getOrNull(2)?.toIntOrNull()
                        if (ip != null && pt != null) peer = InetSocketAddress(ip, pt)
                    }
                } catch (e: SocketTimeoutException) {
                    // no datagram this window — loop re-checks the deadline
                }
            }

            // Phase B — direct hole-punch to the laptop's public endpoint. Give it a
            // few seconds to overlap the laptop's ~5s punch-back before relaying.
            peer?.let { pe ->
                if (doHandshake(sock, pe.address, pe.port, 4000, stepMs = 120)) {
                    address = pe.address; port = pe.port
                    mode = Mode.DIRECT
                    drainStale(sock)
                    sender = Executors.newSingleThreadExecutor()
                    sock.soTimeout = 0
                    return@withContext true
                }
            }

            // Phase C — relay fallback: route encrypted packets via the rendezvous.
            val relay = "RELAY P $room".toByteArray(Charsets.UTF_8)
            sock.send(DatagramPacket(relay, relay.size, rdvAddr, rdvPort))
            if (doHandshake(sock, rdvAddr, rdvPort, 3000, stepMs = 120)) {
                address = rdvAddr; port = rdvPort
                mode = Mode.RELAY
                drainStale(sock)
                sender = Executors.newSingleThreadExecutor()
                sock.soTimeout = 0
                return@withContext true
            }

            close()
            false
        } catch (e: Exception) {
            close()
            false
        }
    }

    /** Discard datagrams already queued from the handshake — the server answers OK
     *  to every retried HELLO/AUTH, so several stale OKs can be waiting. Without this
     *  the first VGET/PING would read one and the watchdog would misfire a reconnect.
     *  Bounded by BOTH an idle timeout and a wall-clock deadline + packet cap, so a
     *  hostile rendezvous flooding the socket can't trap us here (C-F2). */
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

    /** Handshake to (addr,port) until the server pins us: send HELLO, answer the
     *  server's encrypted CHAL with an AUTH echoing its nonce, then get OK. The
     *  challenge proves freshness — a replayed HELLO/AUTH can't complete it — which
     *  is what stops a captured session being replayed by a keyless attacker.
     *
     *  Also the hole-punch opener: each (re)send holds our NAT mapping open, so we
     *  resend on a short [stepMs] cadence for up to [budgetMs]. If OK is lost after
     *  AUTH (the challenge is single-use, so blind AUTH resends would stall), we drop
     *  the nonce and re-HELLO to draw a fresh challenge. v1 (no channel) just sends a
     *  plaintext HELLO and waits for OK. Stray rendezvous text is ignored. */
    private fun doHandshake(
        sock: DatagramSocket, addr: InetAddress, port: Int, budgetMs: Long, stepMs: Int = 250,
    ): Boolean {
        val ch = channel
        val buf = ByteArray(64)
        sock.soTimeout = stepMs
        val deadline = System.currentTimeMillis() + budgetMs
        var nonce: String? = null
        var authTries = 0

        fun sendHello(): Boolean = try {
            val pkt = if (ch != null) ch.seal("HELLO")
            else "$token HELLO".toByteArray(Charsets.UTF_8)
            sock.send(DatagramPacket(pkt, pkt.size, addr, port)); true
        } catch (e: Exception) { false }

        fun sendAuth(n: String): Boolean = try {
            val pkt = ch!!.seal("AUTH $n")
            sock.send(DatagramPacket(pkt, pkt.size, addr, port)); true
        } catch (e: Exception) { false }

        if (!sendHello()) return false
        while (System.currentTimeMillis() < deadline) {
            try {
                val p = DatagramPacket(buf, buf.size)
                sock.receive(p)
                if (ch != null) {
                    val d = ch.open(p.data, p.length)
                    when {
                        d == "OK" -> return true
                        d != null && d.startsWith("CHAL ") -> {
                            nonce = d.substring(5).trim(); authTries = 0
                            if (!sendAuth(nonce)) return false
                        }
                    }
                } else if (String(p.data, 0, p.length).trim() == "OK") {
                    return true
                }
            } catch (e: SocketTimeoutException) {
                val n = nonce
                if (n != null && authTries < 3) {
                    authTries++
                    if (!sendAuth(n)) return false
                } else {                        // no challenge yet, or OK lost → re-HELLO
                    nonce = null
                    if (!sendHello()) return false
                }
            }
        }
        return false
    }

    /** Decode a server reply: decrypt if on the secure wire, else plaintext. */
    private fun decodeReply(p: DatagramPacket): String? {
        val ch = channel
        return if (ch != null) ch.open(p.data, p.length)
        else String(p.data, 0, p.length).trim()
    }

    fun move(dx: Int, dy: Int) = send("MOVE $dx $dy")
    fun scroll(dx: Int, dy: Int) = send("SCROLL $dx $dy")
    fun zoom(steps: Int) = send("ZOOM $steps")             // ctrl+wheel pinch zoom (+ in, − out)
    fun click() = send("CLICK")
    fun rightClick() = send("RCLICK")
    fun middleClick() = send("MCLICK")
    fun mouseDown() = send("MDOWN")                        // drag-lock press
    fun mouseUp() = send("MUP")                            // drag-lock release
    fun setVolume(percent: Int) = send("VOL ${percent.coerceIn(0, 100)}")
    fun setBrightness(percent: Int) = send("BRIGHT ${percent.coerceIn(0, 100)}")
    fun clipboardPaste(text: String) = send("CLIP $text")  // set laptop clipboard + paste
    fun media(action: String) = send("MEDIA $action")     // play_pause | next | prev
    fun key(text: String) = send("KEY $text")              // literal text, spaces ok
    fun keySpecial(name: String) = send("KEYSP $name")     // enter | backspace | ...
    fun combo(spec: String) = send("COMBO $spec")          // "ctrl c", "alt tab", ...
    fun appSwitch(dir: String) = send("ASW $dir")          // next | prev | end (Alt-Tab session)
    fun system(action: String) = send("SYS $action")       // lock | sleep | mute
    fun presentation(action: String) = send("PRES $action") // start|end|next|prev|blank

    /**
     * Send a request, then wait up to [timeoutMs] for a reply whose first token is
     * [prefix], **consuming and discarding** any other replies that arrive first.
     *
     * This is the fix for reply desync on a high-latency link: over the relay the
     * round trip is ~360ms, so a reply to the *previous* probe (e.g. a late PONG)
     * can be sitting in the buffer when the next probe reads — a single read would
     * grab the wrong type and report a false miss. Looping until the expected
     * prefix (or the deadline) makes each probe robust to that cross-talk, and
     * means one probe's reply also clears the other's staleness.
     * @return the whitespace-split reply tokens, or null on timeout.
     */
    private fun awaitReply(prefix: String, timeoutMs: Int): List<String>? {
        val sock = socket ?: return null
        val deadline = System.currentTimeMillis() + timeoutMs
        val buf = ByteArray(64)
        while (true) {
            val remaining = (deadline - System.currentTimeMillis()).toInt()
            if (remaining <= 0) return null
            try {
                sock.soTimeout = remaining
                val reply = DatagramPacket(buf, buf.size)
                sock.receive(reply)
                val parts = (decodeReply(reply) ?: "").split(" ")
                if (parts.getOrNull(0) == prefix) return parts
                // other reply type (stale cross-talk) — discard and keep waiting
            } catch (e: Exception) {
                return null
            }
        }
    }

    /**
     * Ask the laptop for its current volume (VGET -> "VOL n").
     * @return 0..100, or null on timeout. Runs on the IO dispatcher.
     */
    suspend fun queryVolume(timeoutMs: Int = 400): Int? = withContext(Dispatchers.IO) {
        socket ?: return@withContext null
        try {
            sendNow("VGET")
            awaitReply("VOL", timeoutMs)?.getOrNull(1)?.toIntOrNull()
        } catch (e: Exception) {
            null
        } finally {
            try { socket?.soTimeout = 0 } catch (_: Exception) {}
        }
    }

    /**
     * Ask the laptop for its current display brightness (BGET -> "BRI n").
     * @return 0..100, or null on timeout / no brightness backend. Runs on IO.
     */
    suspend fun queryBrightness(timeoutMs: Int = 400): Int? = withContext(Dispatchers.IO) {
        socket ?: return@withContext null
        try {
            sendNow("BGET")
            awaitReply("BRI", timeoutMs)?.getOrNull(1)?.toIntOrNull()
        } catch (e: Exception) {
            null
        } finally {
            try { socket?.soTimeout = 0 } catch (_: Exception) {}
        }
    }

    /** Liveness probe: send PING, expect "PONG". Used by the reconnect watchdog. */
    suspend fun ping(timeoutMs: Int = 500): Boolean = withContext(Dispatchers.IO) {
        socket ?: return@withContext false
        try {
            sendNow("PING")
            awaitReply("PONG", timeoutMs) != null
        } catch (e: Exception) {
            false
        } finally {
            try { socket?.soTimeout = 0 } catch (_: Exception) {}
        }
    }

    /**
     * Leave cleanly, actually getting the BYE onto the wire.
     *
     * This used to be `send("BYE"); close()` — but close() calls shutdownNow(),
     * which cancels queued tasks, so the BYE the sender thread had just been handed
     * was usually thrown away. The server then had to wait out its ~12s idle
     * timeout before it noticed we'd gone, showing a phone as connected long after
     * it left. Sending inline instead isn't an option either: disconnect() runs on
     * the main thread and a blocking socket write there is a
     * NetworkOnMainThreadException.
     *
     * So queue the BYE *and* the socket close as one ordered task on the sender
     * thread, then shutdown() (which lets the queue drain) rather than
     * shutdownNow(). The packet goes out, the socket closes right behind it, and
     * the main thread never blocks.
     */
    fun disconnect() {
        val s = sender
        sender = null
        if (s == null) {
            closeSocket()
            return
        }
        s.execute {
            try {
                sendNow("BYE")
            } catch (_: Exception) {
                // best effort — the idle timeout is the backstop
            }
            closeSocket()
        }
        s.shutdown()
    }

    /** Queue a packet on the sender thread. */
    private fun send(body: String) {
        sender?.execute {
            try {
                sendNow(body)
            } catch (_: Exception) {
                // lossy by design
            }
        }
    }

    /** Blocking send on the current thread. Encrypted if on the secure wire.
     *  Locked so the secure counter is assigned and the packet sent atomically —
     *  otherwise concurrent senders (queued actions vs. PING/VGET) could deliver
     *  counters out of order and the server's replay guard would drop them. */
    private fun sendNow(body: String) {
        val sock = socket ?: return
        val addr = address ?: return
        synchronized(sendLock) {
            val ch = channel
            val payload = if (ch != null) ch.seal(body)
            else "$token $body".toByteArray(Charsets.UTF_8)
            sock.send(DatagramPacket(payload, payload.size, addr, port))
        }
    }

    /** Drop the socket and secure session. Safe to call from any thread — and it
     *  IS called from two (the main thread via close(), the sender thread at the
     *  end of a disconnect), which is why the fields it touches are @Volatile. */
    private fun closeSocket() {
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        channel = null
    }

    /** Hard teardown: abandon anything queued. Used when a connect attempt fails
     *  or is superseded, where there's no session worth saying goodbye to. */
    private fun close() {
        sender?.shutdownNow()
        sender = null
        closeSocket()
    }
}
