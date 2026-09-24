package com.example.lanremote.data

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings as AndroidSettings
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileInputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Downloads a LazeR release APK inside the app and hands it to Android's installer,
 * so updating doesn't mean a trip through the browser and its downloads list.
 *
 * WHAT IT DOES, and no more:
 *   [download] fetches the APK into cache/update/, hashing it as it is written, and
 *              keeps it only if the hash is the one the release's LazeR.apk.sha256
 *              names;
 *   [installIntent] hashes that file AGAIN and, only if it still matches, returns
 *              the intent that opens Android's own installer;
 *   [cancel]   stops a running download;
 *   [clear]    deletes whatever is in cache/update/.
 *
 * WHAT STILL PROTECTS THE USER. Android's package manager checks the APK is signed
 * by the same key as the installed LazeR and refuses it otherwise; nothing here can
 * weaken that. The SHA-256 check is a second, weaker line: the checksum and the APK
 * both come from this project's GitHub release, so it catches a corrupt or
 * truncated download, not a compromised account.
 *
 * WHAT IT COSTS. REQUEST_INSTALL_PACKAGES. The first time, Android asks the user to
 * allow "Install unknown apps" for LazeR; after that toggle they tap Install again.
 *
 * Design after the updater in the Twitwa project (UpdaterModule.kt), which found
 * that a browser download of a sideloaded APK can sit at 100% and never become a
 * file anyone can open.
 */
class ApkUpdater(private val context: Context) {

    sealed interface Download {
        data object Ok : Download
        data class Failed(val reason: String) : Download
    }

    sealed interface Install {
        data class Launch(val intent: Intent) : Install
        /** The user has to allow installs from LazeR first; [intent] opens that screen. */
        data class NeedsPermission(val intent: Intent) : Install
        data class Failed(val reason: String) : Install
    }

    /** One request's cancel state. Per call, not per updater, so a quick Cancel then
     *  Download can't reset a flag the first request is still reading. */
    private class Ticket {
        @Volatile var cancelled = false
        @Volatile var conn: HttpURLConnection? = null
        fun cancel() { cancelled = true; conn?.disconnect() }
    }

    // Written on the main thread (cancel) and IO threads (begin/end).
    @Volatile private var ticket: Ticket? = null

    /** Start a request, superseding any still running. */
    @Synchronized private fun begin(): Ticket {
        ticket?.cancel()
        return Ticket().also { ticket = it }
    }

    @Synchronized private fun end(t: Ticket) { if (ticket === t) ticket = null }

    /** [conn] is now the request's; a cancel that landed before this still stops it. */
    private fun attach(t: Ticket, conn: HttpURLConnection) {
        t.conn = conn
        if (t.cancelled) conn.disconnect()
    }

    private fun dir() = File(context.cacheDir, DIR)
    private fun apkFile() = File(dir(), APK)

    /** The checksum published next to the APK: the first 64 hex chars of the file. */
    suspend fun fetchSha256(shaUrl: String): String? = withContext(Dispatchers.IO) {
        if (!isReleaseAssetUrl(shaUrl)) return@withContext null
        var conn: HttpURLConnection? = null
        val t = begin()
        try {
            conn = open(shaUrl)
            attach(t, conn)
            if (conn.responseCode != 200 || conn.url.protocol != "https") return@withContext null
            val text = conn.inputStream.use { input ->
                val buf = ByteArray(1024)
                var n = 0
                while (n < buf.size) {
                    val r = input.read(buf, n, buf.size - n)
                    if (r <= 0) break
                    n += r
                }
                String(buf, 0, n, Charsets.US_ASCII)
            }
            parseSha256File(text)
        } catch (e: Exception) {
            null
        } finally {
            end(t)
            conn?.disconnect()
        }
    }

    /** Fetch [url] into the cache, keeping it only if its SHA-256 is [sha256].
     *  [onProgress] gets (bytes, total or -1) every [PROGRESS_STEP] bytes. */
    suspend fun download(
        url: String,
        sha256: String,
        onProgress: (Long, Long) -> Unit,
    ): Download = withContext(Dispatchers.IO) {
        // Checked here, at the last point before the network, not only by the caller.
        if (!isReleaseAssetUrl(url)) return@withContext Download.Failed("url")
        if (!SHA256_RE.matches(sha256)) return@withContext Download.Failed("sha256")
        val dir = dir()
        dir.mkdirs()
        dir.listFiles()?.forEach { it.delete() }
        val t = begin()
        // Its own name, so a superseded request winding down can't touch this one's.
        val part = File(dir, "$APK.${System.nanoTime()}.part")
        val apk = apkFile()

        var conn: HttpURLConnection? = null
        try {
            conn = open(url)
            attach(t, conn)
            val code = conn.responseCode
            if (code != 200) return@withContext Download.Failed("http-$code")
            // GitHub answers with a redirect to its asset host. HttpURLConnection
            // follows https to https and refuses to fall back to http, which is
            // the only redirect this should ever take — checked anyway.
            if (conn.url.protocol != "https") return@withContext Download.Failed("not-https")
            val total = conn.contentLengthLong
            if (total > MAX_BYTES) return@withContext Download.Failed("too-big")

            val digest = MessageDigest.getInstance("SHA-256")
            var bytes = 0L
            var reported = 0L
            // readTimeout bounds one silent read, not the whole file: a link that
            // trickles a byte every 29 s would never end. This does.
            val deadline = System.nanoTime() + DEADLINE_MS * 1_000_000
            var late = false
            conn.inputStream.use { src ->
                part.outputStream().use { dst ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = src.read(buf)
                        if (n < 0 || t.cancelled) break
                        bytes += n
                        if (bytes > MAX_BYTES) break
                        if (System.nanoTime() > deadline) { late = true; break }
                        digest.update(buf, 0, n)
                        dst.write(buf, 0, n)
                        if (bytes - reported >= PROGRESS_STEP) {
                            reported = bytes
                            onProgress(bytes, total)
                        }
                    }
                }
            }
            when {
                t.cancelled -> { part.delete(); Download.Failed("cancelled") }
                bytes > MAX_BYTES -> { part.delete(); Download.Failed("too-big") }
                late -> { part.delete(); Download.Failed("slow") }
                total >= 0 && bytes != total -> { part.delete(); Download.Failed("short") }
                hex(digest.digest()) != sha256 -> { part.delete(); Download.Failed("digest") }
                !part.renameTo(apk) -> { part.delete(); Download.Failed("rename") }
                else -> Download.Ok
            }
        } catch (e: Exception) {
            // No network, a timeout, a connection dropped mid-file — and a
            // cancel's disconnect lands here too, as a closed socket.
            part.delete()
            Download.Failed(if (t.cancelled) "cancelled" else "network")
        } finally {
            end(t)
            conn?.disconnect()
        }
    }

    /** Stop a running request. The flag stops the loop between chunks; the
     *  disconnect wakes a read blocked on a silent network. */
    @Synchronized fun cancel() {
        ticket?.cancel()
        ticket = null
    }

    fun clear() {
        dir().listFiles()?.forEach { it.delete() }
    }

    /** What to launch to install the downloaded APK, re-checked against [sha256]. */
    suspend fun installIntent(sha256: String): Install = withContext(Dispatchers.IO) {
        if (!SHA256_RE.matches(sha256)) return@withContext Install.Failed("sha256")
        val apk = apkFile()
        if (!apk.isFile) return@withContext Install.Failed("missing")
        // Again: the file has sat in the cache since download().
        val sum = try { hashFile(apk) } catch (e: Exception) {
            return@withContext Install.Failed("unreadable")
        }
        if (sum != sha256) {
            apk.delete()
            return@withContext Install.Failed("digest")
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !context.packageManager.canRequestPackageInstalls()) {
            return@withContext Install.NeedsPermission(
                Intent(AndroidSettings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${context.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }
        try {
            val uri = FileProvider.getUriForFile(context, "${context.packageName}$AUTHORITY_SUFFIX", apk)
            Install.Launch(
                Intent(Intent.ACTION_VIEW)
                    .setDataAndType(uri, APK_MIME)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        } catch (e: IllegalArgumentException) {
            Install.Failed("provider")   // the path is outside every <paths> entry
        } catch (e: ActivityNotFoundException) {
            Install.Failed("no-installer")
        }
    }

    private fun open(url: String): HttpURLConnection =
        (URL(url).openConnection() as HttpURLConnection).apply {
            instanceFollowRedirects = true
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            setRequestProperty("User-Agent", "LazeR-Android")
        }

    private fun hashFile(f: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        FileInputStream(f).use { src ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = src.read(buf)
                if (n < 0) break
                digest.update(buf, 0, n)
            }
        }
        return hex(digest.digest())
    }

    companion object {
        private const val DIR = "update"            // res/xml/update_paths.xml
        private const val APK = "lazer-update.apk"
        private const val APK_MIME = "application/vnd.android.package-archive"
        // Must match android:authorities in AndroidManifest.xml.
        private const val AUTHORITY_SUFFIX = ".updates"
        /** Only assets of this project's own releases are ever fetched. */
        const val DOWNLOAD_PREFIX = "https://github.com/B-ismark/lazeR/releases/download/"
        private val SHA256_RE = Regex("^[0-9a-f]{64}$")
        // The APK is ~10 MB. Ten times that is room to grow and small enough that
        // a wrong URL cannot fill the phone.
        private const val MAX_BYTES = 100L * 1024 * 1024
        private const val PROGRESS_STEP = 256L * 1024
        private const val CONNECT_TIMEOUT_MS = 15_000
        private const val READ_TIMEOUT_MS = 30_000
        // Ten minutes for 10 MB is 17 KB/s; slower than that, "try again later" is
        // the honest answer.
        private const val DEADLINE_MS = 10L * 60 * 1000

        /** No `%` or `\` either: the HTTP stack decodes `%2e%2e` to `..` and reads a
         *  backslash as `/`, which would walk out of the prefix. Release asset URLs
         *  never contain them. */
        fun isReleaseAssetUrl(url: String): Boolean =
            url.startsWith(DOWNLOAD_PREFIX) && url.length > DOWNLOAD_PREFIX.length &&
                ".." !in url && url.none { it in "?#%\\" }

        /** The hash in a `sha256sum`-style file ("<64 hex>  LazeR.apk"), lowercased;
         *  null if the file doesn't start with one. */
        fun parseSha256File(text: String): String? {
            val first = text.trim().split(Regex("\\s+"), limit = 2).firstOrNull()?.lowercase()
            return first?.takeIf { SHA256_RE.matches(it) }
        }

        fun hex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }
    }
}
