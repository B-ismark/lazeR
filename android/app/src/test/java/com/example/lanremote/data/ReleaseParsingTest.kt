package com.example.lanremote.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The release fields the in-app updater needs, and the checks on what it fetches. */
class ReleaseParsingTest {

    private val base = "https://github.com/B-ismark/lazeR/releases/download/v2.3.0"

    private fun body(notes: String = "notes", withSha: Boolean = true) =
        """{"url":"https://api.github.com/repos/B-ismark/lazeR/releases/1",""" +
            """"author":{"login":"B-ismark"},"tag_name":"v2.3.0","name":"LazeR v2.3.0",""" +
            """"assets":[{"name":"LazeR.exe","browser_download_url":"$base/LazeR.exe"},""" +
            """{"name":"LazeR.apk","browser_download_url":"$base/LazeR.apk"}""" +
            (if (withSha) """,{"name":"LazeR.apk.sha256","browser_download_url":"$base/LazeR.apk.sha256"}""" else "") +
            """],"body":"$notes"}"""

    @Test
    fun `finds the tag and both asset links`() {
        assertEquals(
            UpdateChecker.Release("v2.3.0", "$base/LazeR.apk", "$base/LazeR.apk.sha256"),
            UpdateChecker.parseRelease(body()))
    }

    @Test
    fun `a body cut off inside long notes still yields the release`() {
        val cut = body("x".repeat(500_000)).take(300_000)
        assertEquals("v2.3.0", UpdateChecker.parseRelease(cut)?.tag)
        assertEquals("$base/LazeR.apk", UpdateChecker.parseRelease(cut)?.apkUrl)
    }

    @Test
    fun `a release without a checksum has no sha link`() {
        val r = UpdateChecker.parseRelease(body(withSha = false))!!
        assertEquals("$base/LazeR.apk", r.apkUrl)
        assertNull(r.shaUrl)
    }

    @Test
    fun `no tag means no release`() {
        assertNull(UpdateChecker.parseRelease("""{"message":"API rate limit exceeded"}"""))
        assertNull(UpdateChecker.parseRelease(""))
    }

    @Test
    fun `non-ASCII digits are not a version`() {
        assertNull(UpdateChecker.parseVersion("٢.1.0"))
        assertFalse(UpdateChecker.isNewer("v٩.0.0", "2.0.0"))
    }

    @Test
    fun `only this project's release assets are downloadable`() {
        assertTrue(ApkUpdater.isReleaseAssetUrl("$base/LazeR.apk"))
        for (bad in listOf(
            "http://github.com/B-ismark/lazeR/releases/download/v2.3.0/LazeR.apk",
            "https://github.com/someone/lazeR/releases/download/v2.3.0/LazeR.apk",
            "https://github.com/B-ismark/lazeR/releases/download/",
            "https://github.com/B-ismark/lazeR/releases/download/../../evil/LazeR.apk",
            "https://github.com/B-ismark/lazeR/releases/download/v1/LazeR.apk?x=1",
            "https://github.com.evil.io/B-ismark/lazeR/releases/download/v1/LazeR.apk",
            // The HTTP stack decodes these to "..", and reads "\" as "/".
            "https://github.com/B-ismark/lazeR/releases/download/%2e%2e/%2E%2E/evil/LazeR.apk",
            "https://github.com/B-ismark/lazeR/releases/download/.%2e/x/LazeR.apk",
            "https://github.com/B-ismark/lazeR/releases/download/v1\\evil/LazeR.apk",
        )) {
            assertFalse(bad, ApkUpdater.isReleaseAssetUrl(bad))
        }
    }

    @Test
    fun `reads a sha256sum-style checksum file`() {
        val h = "4ac2b3b56c7288999370596d11f489874d87b6fda54bd0de81a80d2bd42849dc"
        assertEquals(h, ApkUpdater.parseSha256File("$h  LazeR.apk\n"))
        assertEquals(h, ApkUpdater.parseSha256File(h.uppercase()))
        assertNull(ApkUpdater.parseSha256File("not a hash  LazeR.apk"))
        assertNull(ApkUpdater.parseSha256File(h.dropLast(1)))
        assertNull(ApkUpdater.parseSha256File(""))
    }
}
