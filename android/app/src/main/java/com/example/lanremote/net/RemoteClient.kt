package com.example.lanremote.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
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

    private var socket: DatagramSocket? = null
    private var address: InetAddress? = null
    private var port: Int = 0
    private var token: String = ""
    private var channel: SecureChannel? = null   // non-null ⇒ encrypted v2 wire
    private var sender: ExecutorService? = null
    private val sendLock = Any()                 // serialize counter-assign + send

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
            sender = Executors.newSingleThreadExecutor()

            sendNow("HELLO")

            sock.soTimeout = timeoutMs.toInt()
            val buf = ByteArray(64)
            val reply = DatagramPacket(buf, buf.size)
            val ok = try {
                sock.receive(reply)
                decodeReply(reply) == "OK"
            } catch (e: Exception) {
                false
            }
            sock.soTimeout = 0

            if (!ok) close()
            ok
        } catch (e: Exception) {
            close()
            false
        }
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
     * Ask the laptop for its current volume (VGET -> "VOL n").
     * @return 0..100, or null on timeout. Runs on the IO dispatcher.
     */
    suspend fun queryVolume(timeoutMs: Int = 400): Int? = withContext(Dispatchers.IO) {
        val sock = socket ?: return@withContext null
        try {
            sendNow("VGET")
            sock.soTimeout = timeoutMs
            val buf = ByteArray(64)
            val reply = DatagramPacket(buf, buf.size)
            sock.receive(reply)
            val parts = (decodeReply(reply) ?: "").split(" ")
            if (parts.getOrNull(0) == "VOL") parts.getOrNull(1)?.toIntOrNull() else null
        } catch (e: Exception) {
            null
        } finally {
            try {
                socket?.soTimeout = 0
            } catch (_: Exception) {
            }
        }
    }

    /**
     * Ask the laptop for its current display brightness (BGET -> "BRI n").
     * @return 0..100, or null on timeout / no brightness backend. Runs on IO.
     */
    suspend fun queryBrightness(timeoutMs: Int = 400): Int? = withContext(Dispatchers.IO) {
        val sock = socket ?: return@withContext null
        try {
            sendNow("BGET")
            sock.soTimeout = timeoutMs
            val buf = ByteArray(64)
            val reply = DatagramPacket(buf, buf.size)
            sock.receive(reply)
            val parts = (decodeReply(reply) ?: "").split(" ")
            if (parts.getOrNull(0) == "BRI") parts.getOrNull(1)?.toIntOrNull() else null
        } catch (e: Exception) {
            null
        } finally {
            try {
                socket?.soTimeout = 0
            } catch (_: Exception) {
            }
        }
    }

    /** Liveness probe: send PING, expect "PONG". Used by the reconnect watchdog. */
    suspend fun ping(timeoutMs: Int = 500): Boolean = withContext(Dispatchers.IO) {
        val sock = socket ?: return@withContext false
        try {
            sendNow("PING")
            sock.soTimeout = timeoutMs
            val buf = ByteArray(64)
            val reply = DatagramPacket(buf, buf.size)
            sock.receive(reply)
            decodeReply(reply) == "PONG"
        } catch (e: Exception) {
            false
        } finally {
            try { socket?.soTimeout = 0 } catch (_: Exception) {}
        }
    }

    fun disconnect() {
        send("BYE")
        close()
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

    private fun close() {
        sender?.shutdownNow()
        sender = null
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        channel = null
    }
}
