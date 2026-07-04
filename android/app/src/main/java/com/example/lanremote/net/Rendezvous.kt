package com.example.lanremote.net

import android.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Client-side helpers for off-LAN (remote) access via the public rendezvous
 * coordinator (see rendezvous/rendezvous_server.py and PROTOCOL.md).
 *
 * The rendezvous only ever sees an opaque `room` id — never the AES key — and
 * cannot control or decrypt the laptop. The actual hole-punch / relay handshake
 * lives in [RemoteClient.connectRemote]; this object holds the shared derivations
 * and parsing so both sides agree byte-for-byte with the Python server.
 */
object Rendezvous {

    const val DEFAULT_PORT = 50510

    // Must match the Python server: room = base64url(HMAC-SHA256(key, INFO)[:16]).
    private const val ROOM_INFO = "lazer-rdv-v1"

    /** The 128-bit room id both paired devices derive from the shared key. */
    fun roomId(key: ByteArray): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        val full = mac.doFinal(ROOM_INFO.toByteArray(Charsets.US_ASCII))
        val id = full.copyOfRange(0, 16)
        return Base64.encodeToString(id, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
    }

    /** Parse a stored "host" or "host:port" into (host, port); null if blank.
     *  An out-of-range or non-numeric port falls back to the default rather than
     *  producing a bad value (C-F6). Bracketed IPv6 literals ("[::1]:50510") are
     *  handled so the colons in the address aren't mistaken for the port separator. */
    fun parse(spec: String): Pair<String, Int>? {
        val s = spec.trim()
        if (s.isEmpty()) return null
        // [ipv6]:port  or  [ipv6]
        if (s.startsWith("[")) {
            val close = s.indexOf(']')
            if (close < 0) return null
            val host = s.substring(1, close)
            val rest = s.substring(close + 1)
            val port = if (rest.startsWith(":")) validPort(rest.substring(1)) else DEFAULT_PORT
            return host to port
        }
        val i = s.lastIndexOf(':')
        // no colon, or a bare IPv6 with multiple colons and no brackets → whole thing is the host
        if (i <= 0 || s.indexOf(':') != i) return s to DEFAULT_PORT
        val host = s.substring(0, i)
        return host to validPort(s.substring(i + 1))
    }

    private fun validPort(s: String): Int {
        val p = s.toIntOrNull() ?: return DEFAULT_PORT
        return if (p in 1..65535) p else DEFAULT_PORT
    }
}
