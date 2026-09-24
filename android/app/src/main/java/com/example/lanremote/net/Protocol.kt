package com.example.lanremote.net

import java.net.URI
import java.net.URLDecoder

/**
 * Values the phone and the laptop must agree on, in one place.
 *
 * Each one is pinned by ProtocolContractTest here and by `SharedContract` in
 * server/tests/test_wire.py, against the same literal, so changing either side
 * alone turns a suite red instead of quietly breaking pairing.
 */
object Protocol {
    /** The laptop's UDP port (`PORT` in remote_server.py). */
    const val DEFAULT_PORT = 50505

    /** The mDNS service type the laptop announces (`SERVICE_TYPE`, minus ".local."). */
    const val SERVICE_TYPE = "_lazer._udp."

    /** The laptop drops a phone it hasn't heard from in this long (`CLIENT_IDLE_S`). */
    const val SERVER_IDLE_MS = 12_000L

    /** The health loop's slowest poll. Must fit at least twice in [SERVER_IDLE_MS],
     *  or an idle phone gets dropped between polls. */
    const val IDLE_POLL_MS = 4_000L

    /** Plaintext refusal hints (`REFUSE_SECURE_REQUIRED` / `REFUSE_BAD_KEY`). */
    const val REFUSE_SECURE_REQUIRED = "secure-required"
    const val REFUSE_BAD_KEY = "bad-key"

    /** KEY / KEYSP bodies are padded with NULs up to a multiple of this, on a
     *  laptop that has shown it strips them (a bound handshake). */
    const val KEY_PAD_BUCKET = 32
}

/** What a scanned `lazer://` QR carries. [key] is the raw base64url text of `k`,
 *  blank when the QR has none. */
data class PairingUri(
    val host: String,
    val port: Int,
    val token: String,
    val name: String,
    val key: String,
)

/** Why a scanned code was refused. */
enum class PairingError { NotLazer, NoToken, BadHost }

/**
 * Parse the laptop's QR text (`build_uri` in remote_server.py):
 * `lazer://<ip>:<port>/?token=<T>&name=<N>&k=<base64url key>`.
 *
 * Pure JVM on purpose (java.net.URI, not android.net.Uri) so the format is
 * covered by a plain unit test against the server's golden string.
 */
fun parsePairingUri(raw: String): Result<PairingUri> {
    val uri = try { URI(raw.trim()) } catch (e: Exception) { null }
    if (uri == null || !uri.scheme.equals("lazer", ignoreCase = true)) {
        return Result.failure(PairingException(PairingError.NotLazer))
    }
    // An IPv6 literal comes back bracketed; InetAddress wants it bare.
    val host = uri.host?.removePrefix("[")?.removeSuffix("]")
    if (host.isNullOrBlank()) return Result.failure(PairingException(PairingError.NotLazer))
    val q = queryParams(uri.rawQuery)
    val token = q["token"].orEmpty().trim().uppercase()
    if (token.isBlank()) return Result.failure(PairingException(PairingError.NoToken))
    // The laptop always puts its IP in the QR; a name would send everything typed
    // on this phone wherever DNS says.
    if (!isPairableHost(host)) return Result.failure(PairingException(PairingError.BadHost))
    return Result.success(
        PairingUri(
            host = host,
            port = if (uri.port > 0) uri.port else Protocol.DEFAULT_PORT,
            token = token,
            name = q["name"]?.takeIf { it.isNotBlank() } ?: host,
            key = q["k"].orEmpty().trim(),
        )
    )
}

class PairingException(val error: PairingError) : Exception(error.name)

private fun queryParams(raw: String?): Map<String, String> {
    if (raw.isNullOrEmpty()) return emptyMap()
    val out = LinkedHashMap<String, String>()
    for (pair in raw.split('&')) {
        if (pair.isEmpty()) continue
        val i = pair.indexOf('=')
        val k = if (i < 0) pair else pair.substring(0, i)
        val v = if (i < 0) "" else pair.substring(i + 1)
        val dk = try { URLDecoder.decode(k, "UTF-8") } catch (e: Exception) { k }
        // First wins, so a second token= appended to a genuine QR can't override it.
        if (dk !in out) out[dk] = try { URLDecoder.decode(v, "UTF-8") } catch (e: Exception) { v }
    }
    return out
}

/**
 * True for an address a laptop can be dialed at: a unicast IPv4 or IPv6 literal, or
 * an mDNS `.local` name. Public and carrier-grade NAT ranges count — some campus and
 * office LANs use them. Refused: DNS hostnames, and loopback, unspecified, multicast
 * and broadcast (224.0.0.0 and up) addresses, none of which can be another machine —
 * including inside an IPv4-mapped IPv6 literal (`::ffff:127.0.0.1`).
 */
fun isPairableHost(host: String): Boolean {
    val h = host.trim().lowercase().removeSuffix(".")
    if (h.isEmpty()) return false
    if (h.endsWith(".local")) return true
    if (':' !in h) return ipv4Octets(h)?.let(::unicastV4) ?: false
    val b = ipv6Bytes(h) ?: return false
    if (b[0] == 0xFF.toByte()) return false                          // multicast
    if ((0 until 10).any { b[it] != 0.toByte() }) return true
    // ::/80 holds ::, ::1, the deprecated IPv4-compatible ::a.b.c.d, and IPv4-mapped
    // ::ffff:a.b.c.d, which Android dials as the IPv4 address it carries.
    if (b[10] != 0xFF.toByte() || b[11] != 0xFF.toByte()) return false
    return unicastV4(IntArray(4) { b[12 + it].toInt() and 0xFF })
}

private fun unicastV4(o: IntArray) = o[0] != 0 && o[0] != 127 && o[0] < 224

/**
 * The octets of a dotted-quad IPv4 literal, or null. ASCII digits only
 * (isDigit()/toInt() also take other scripts' digits, which InetAddress would then
 * resolve as a hostname), and no leading zeros: Android's resolver reads `012` as
 * octal 10, so accepting it would check one address and dial another.
 */
internal fun ipv4Octets(s: String): IntArray? {
    val parts = s.split('.')
    if (parts.size != 4) return null
    return IntArray(4) { i ->
        val p = parts[i]
        if (p.isEmpty() || p.length > 3 || !p.all { it in '0'..'9' }) return null
        if (p.length > 1 && p[0] == '0') return null
        p.toInt().takeIf { it <= 255 } ?: return null
    }
}

/** The 16 bytes of an IPv6 literal (`::` compression and a trailing dotted quad
 *  allowed; no zone id), or null. */
internal fun ipv6Bytes(s: String): ByteArray? {
    if (!s.all { it in '0'..'9' || it in 'a'..'f' || it == ':' || it == '.' }) return null
    var head = s
    var v4: IntArray? = null
    if ('.' in s) {
        val cut = s.lastIndexOf(':')
        if (cut < 0) return null
        v4 = ipv4Octets(s.substring(cut + 1)) ?: return null
        head = s.substring(0, cut + 1)
        if (!head.endsWith("::")) head = head.dropLast(1)
    }
    val groups = if (v4 != null) 6 else 8
    fun hextets(part: String): List<Int>? = if (part.isEmpty()) emptyList()
    else part.split(':').map { w ->
        if (w.isEmpty() || w.length > 4) return null
        w.toIntOrNull(16) ?: return null
    }
    val halves = head.split("::")
    if (halves.size > 2) return null
    val left = hextets(halves[0]) ?: return null
    val right = if (halves.size == 2) hextets(halves[1]) ?: return null else emptyList()
    val n = left.size + right.size
    // "::" stands for at least one group of zeros.
    if (if (halves.size == 1) n != groups else n > groups - 1) return null
    val words = left + List(groups - n) { 0 } + right
    val out = ByteArray(16)
    words.forEachIndexed { i, w ->
        out[2 * i] = (w shr 8).toByte()
        out[2 * i + 1] = w.toByte()
    }
    v4?.forEachIndexed { i, o -> out[12 + i] = o.toByte() }
    return out
}

/**
 * [body] padded with trailing NULs to the next multiple of [Protocol.KEY_PAD_BUCKET]
 * UTF-8 bytes (a body already on a boundary is left alone). GCM adds no padding of
 * its own, so without this each typed chunk's ciphertext length is its text length,
 * and a special key's name ("enter", "backspace") can be read off the packet size.
 * Only for a laptop that strips it — see [RemoteClient].
 */
fun padKeyBody(body: String): String {
    val len = body.toByteArray(Charsets.UTF_8).size
    val bucket = Protocol.KEY_PAD_BUCKET
    val target = ((len + bucket - 1) / bucket).coerceAtLeast(1) * bucket
    return if (target == len) body else body + "\u0000".repeat(target - len)
}
