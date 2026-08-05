package com.example.lanremote.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Version comparison for the update check.
 *
 * Mirrors `UpdateCheck` in `server/tests/test_wire.py`: the two implementations must
 * agree on what counts as a newer release, or the phone and the laptop would
 * disagree about whether an update exists. Nothing here touches the network —
 * `latestTag()` needs a real HTTP stack, so it is covered by the Python side's
 * stubbed-transport tests plus manual verification.
 */
class UpdateCheckerTest {

    @Test
    fun `parses the shapes a release tag can take`() {
        assertEquals(listOf(2, 0, 0), UpdateChecker.parseVersion("v2.0.0"))
        assertEquals(listOf(2, 0, 0), UpdateChecker.parseVersion("2.0.0"))
        assertEquals(listOf(2, 1, 0), UpdateChecker.parseVersion("V2.1"))
        assertEquals(listOf(2, 0, 0), UpdateChecker.parseVersion("2"))
        assertEquals(listOf(1, 2, 3, 4), UpdateChecker.parseVersion("1.2.3.4"))
    }

    @Test
    fun `drops pre-release and build suffixes`() {
        assertEquals(listOf(2, 1, 0), UpdateChecker.parseVersion("2.1.0-rc1"))
        assertEquals(listOf(2, 1, 0), UpdateChecker.parseVersion("2.1.0+win"))
        assertEquals(listOf(2, 1, 0), UpdateChecker.parseVersion("v2.1.0 "))
    }

    @Test
    fun `rejects rather than guesses unparseable versions`() {
        for (bad in listOf(null, "", "latest", "v", "2.x", "2..0", "abc", "1.2.3.4.5", "-1")) {
            assertNull("should not parse: $bad", UpdateChecker.parseVersion(bad))
        }
    }

    @Test
    fun `compares numerically not lexically`() {
        // The bug this pins: as strings "2.0.10" < "2.0.9", so a lexical compare
        // would stop offering updates after the ninth patch of any minor.
        assertTrue(UpdateChecker.isNewer("2.0.10", "2.0.9"))
        assertFalse(UpdateChecker.isNewer("2.0.9", "2.0.10"))
        assertTrue(UpdateChecker.isNewer("v2.1.0", "2.0.99"))
        assertTrue(UpdateChecker.isNewer("3.0.0", "2.9.9"))
    }

    @Test
    fun `same version is not an update`() {
        assertFalse(UpdateChecker.isNewer("2.0.0", "2.0.0"))
        assertFalse(UpdateChecker.isNewer("v2.0.0", "2.0.0"))
        // Padded equal: 2.0 and 2.0.0 are the same release.
        assertFalse(UpdateChecker.isNewer("2.0", "2.0.0"))
        assertFalse(UpdateChecker.isNewer("2.0.0", "2.0"))
    }

    @Test
    fun `an older release is never reported as an update`() {
        // Guards against a rollback or mis-tag on the repo nagging every user forever.
        assertFalse(UpdateChecker.isNewer("1.5.1", "2.0.0"))
        assertFalse(UpdateChecker.isNewer("v1.0.0", "v2.0.0"))
    }

    @Test
    fun `a garbled tag is never reported as an update`() {
        for (tag in listOf(null, "", "latest", "release-two", "vNext")) {
            assertFalse("should not nag on: $tag", UpdateChecker.isNewer(tag, "2.0.0"))
        }
        // ...and an unreadable LOCAL version is equally inconclusive.
        assertFalse(UpdateChecker.isNewer("2.1.0", null))
        assertFalse(UpdateChecker.isNewer("2.1.0", "unknown"))
    }

    @Test
    fun `extra trailing components are ordered correctly`() {
        // 2.0.0.1 is a later build than 2.0.0; 2.0.0 is not newer than itself+build.
        assertTrue(UpdateChecker.isNewer("2.0.0.1", "2.0.0"))
        assertFalse(UpdateChecker.isNewer("2.0.0", "2.0.0.1"))
    }

    @Test
    fun `the throttle interval is a full day`() {
        // A shorter window would mean a request every time the app is opened, which
        // for a release cadence measured in months is pure waste.
        assertEquals(24L * 60 * 60 * 1000, UpdateChecker.MIN_INTERVAL_MS)
    }

    @Test
    fun `the releases page is the official repo over https`() {
        assertTrue(UpdateChecker.RELEASES_PAGE.startsWith("https://"))
        assertTrue(UpdateChecker.RELEASES_PAGE.contains("B-ismark/lazeR"))
    }
}
