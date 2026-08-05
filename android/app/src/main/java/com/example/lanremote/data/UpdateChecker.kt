package com.example.lanremote.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Asks GitHub whether a newer LazeR release exists.
 *
 * This is the ONLY outbound internet request the app makes. Everything else is
 * LAN-only by design — v2.0 removed off-LAN access entirely — so it is deliberately
 * narrow:
 *
 *  * **Opt-out.** [Settings.updateCheck]; off means this class is never called.
 *  * **Notify-only.** Never downloads or installs anything. The UI links to the
 *    release page and the user takes it from there. Self-updating an APK would mean
 *    asking for install permissions to fetch a binary over a channel the app can't
 *    verify — a much bigger trust ask than the feature is worth.
 *  * **Anonymous.** No token, no cookie, no device identifier. A plain GET whose
 *    only header is the User-Agent that GitHub requires.
 *  * **Silent on failure.** Offline, rate-limited, GitHub down, garbled JSON — all
 *    mean "we don't know", which shows nothing rather than an error. An update
 *    check is not something the user asked for, so it must never interrupt them.
 *  * **Throttled.** At most one request per [MIN_INTERVAL_MS]; see [SettingsStore].
 */
object UpdateChecker {

    private const val API = "https://api.github.com/repos/B-ismark/lazeR/releases/latest"
    const val RELEASES_PAGE = "https://github.com/B-ismark/lazeR/releases/latest"

    /** Don't ask more than once a day: releases are rare and the answer is stable. */
    const val MIN_INTERVAL_MS = 24L * 60 * 60 * 1000

    private const val TIMEOUT_MS = 6_000
    private const val MAX_BODY = 64_000   // we need one short field; cap the read

    /**
     * Parse a release tag into comparable numbers. `"v2.1"` → `[2, 1, 0]`.
     *
     * Compared as INTS, never as strings: lexically `"2.0.10" < "2.0.9"`, which
     * would silently stop offering updates after the ninth patch of any minor.
     * Short forms are padded so `2.1` and `2.1.0` compare equal. Returns null for
     * anything non-numeric, so a garbled tag is never mistaken for a release.
     */
    fun parseVersion(text: String?): List<Int>? {
        var s = (text ?: return null).trim()
        if (s.startsWith("v") || s.startsWith("V")) s = s.substring(1)
        // Drop any pre-release / build suffix ("2.1.0-rc1", "2.1.0+win").
        for (sep in charArrayOf('-', '+', ' ')) s = s.substringBefore(sep)
        if (s.isEmpty()) return null
        val parts = s.split(".")
        if (parts.size > 4) return null
        val out = ArrayList<Int>(4)
        for (p in parts) {
            if (p.isEmpty() || !p.all { it.isDigit() }) return null
            out.add(p.toIntOrNull() ?: return null)
        }
        while (out.size < 3) out.add(0)
        return out
    }

    /** True iff [latest] is a strictly newer release than [current]. Unparseable
     *  input answers false — never nag on a tag we don't understand. */
    fun isNewer(latest: String?, current: String?): Boolean {
        val a = parseVersion(latest) ?: return false
        val b = parseVersion(current) ?: return false
        val n = maxOf(a.size, b.size)
        for (i in 0 until n) {
            val x = a.getOrElse(i) { 0 }
            val y = b.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    /**
     * The newest release's tag, or null if we couldn't find out. Runs on IO.
     *
     * Every failure collapses to null on purpose — see the class doc. The caller
     * cannot distinguish "up to date" from "couldn't check", and doesn't need to:
     * both mean show nothing.
     */
    suspend fun latestTag(): String? = withContext(Dispatchers.IO) {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(API).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                // GitHub rejects requests with no User-Agent outright (403).
                setRequestProperty("User-Agent", "LazeR-Android")
                setRequestProperty("Accept", "application/vnd.github+json")
            }
            if (conn.responseCode != HttpURLConnection.HTTP_OK) return@withContext null
            val body = conn.inputStream.use { input ->
                val buf = ByteArray(MAX_BODY)
                var n = 0
                while (n < buf.size) {
                    val r = input.read(buf, n, buf.size - n)
                    if (r <= 0) break
                    n += r
                }
                String(buf, 0, n, Charsets.UTF_8)
            }
            val tag = JSONObject(body).optString("tag_name", "")
            if (tag.isBlank()) null else tag
        } catch (e: Exception) {
            null
        } finally {
            try {
                conn?.disconnect()
            } catch (_: Exception) {
            }
        }
    }
}
