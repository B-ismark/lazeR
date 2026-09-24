package com.example.lanremote.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

/**
 * Asks GitHub whether a newer LazeR release exists.
 *
 * One of the app's only two kinds of internet request (the other is [ApkUpdater]'s
 * download, which the user starts). Everything else is LAN-only by design, so this
 * is deliberately narrow:
 *
 *  * **Opt-out.** [Settings.updateCheck]; off means this class is never called.
 *  * **Checks only.** This reads the release's tag and asset links; it never
 *    fetches the APK. Downloading happens in [ApkUpdater], and only when the user
 *    taps Download & install.
 *  * **Anonymous.** No token, no cookie, no device identifier. A plain GET whose
 *    only header is the User-Agent that GitHub requires.
 *  * **Quiet on failure.** Offline, rate-limited, GitHub down, garbled JSON — all
 *    mean "we don't know". An update check is not something the user asked for, so
 *    it must never interrupt them: the only trace of a failure is the status line
 *    in Settings → Updates, which the user has to go and look at.
 *  * **Throttled.** At most one request per [MIN_INTERVAL_MS]; see [SettingsStore].
 */
object UpdateChecker {

    private const val API = "https://api.github.com/repos/B-ismark/lazeR/releases/latest"
    const val RELEASES_PAGE = "https://github.com/B-ismark/lazeR/releases/latest"

    /** Don't ask more than once a day: releases are rare and the answer is stable. */
    const val MIN_INTERVAL_MS = 24L * 60 * 60 * 1000

    private const val TIMEOUT_MS = 6_000
    // The fields we need sit near the top of GitHub's answer; the release notes
    // come last and can be long. Read at most this much, and parse what we got
    // even when the cut leaves the JSON unfinished.
    private const val MAX_BODY = 256_000

    /** The published asset names (release.yml). */
    const val APK_ASSET = "LazeR.apk"
    const val SHA_ASSET = "LazeR.apk.sha256"

    /** A release: its tag and, when it has them, the APK and its checksum. */
    data class Release(val tag: String, val apkUrl: String?, val shaUrl: String?)

    private val TAG_RE = Regex(""""tag_name"\s*:\s*"([^"\\]{1,64})"""")
    private val URL_RE = Regex(""""browser_download_url"\s*:\s*"([^"\\]{1,512})"""")

    /**
     * The tag and asset links from a releases-API body.
     *
     * By pattern, not by JSON parser, on purpose: a body cut off at [MAX_BODY]
     * leaves invalid JSON (see there).
     * (It is also plain JVM, so it is unit-tested; org.json is an Android stub
     * there.) The first tag_name is the release's own — nothing above it has one.
     */
    fun parseRelease(body: String): Release? {
        val tag = TAG_RE.find(body)?.groupValues?.get(1)?.trim()
        if (tag.isNullOrBlank()) return null
        val urls = URL_RE.findAll(body).map { it.groupValues[1] }.toList()
        return Release(
            tag = tag,
            apkUrl = urls.firstOrNull { it.endsWith("/$APK_ASSET") },
            shaUrl = urls.firstOrNull { it.endsWith("/$SHA_ASSET") },
        )
    }

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
            // ASCII digits only: isDigit() and toIntOrNull() also accept other
            // scripts' digits ("٢"), which no tag of ours contains.
            if (p.isEmpty() || !p.all { it in '0'..'9' }) return null
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
     * The newest release, or null if we couldn't find out. Runs on IO.
     *
     * Every failure collapses to null on purpose — see the class doc. The caller
     * can't tell "unreachable" from "GitHub said no" (rate limit, no release), so
     * Settings words a null as "couldn't check", without blaming either side.
     */
    suspend fun latestRelease(): Release? = withContext(Dispatchers.IO) {
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
            parseRelease(body)
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
