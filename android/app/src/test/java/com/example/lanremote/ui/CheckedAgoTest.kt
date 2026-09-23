package com.example.lanremote.ui

import org.junit.Assert.assertEquals
import org.junit.Test

/** The "checked …" half of Settings → Updates' "You're up to date" line. */
class CheckedAgoTest {
    private val now = 1_000_000_000_000L
    private fun ago(ms: Long) = checkedAgo(now - ms, now)

    private val min = 60_000L
    private val hour = 60 * min
    private val day = 24 * hour

    @Test fun underAMinuteIsJustNow() = assertEquals("just now", ago(59_000))

    @Test fun minutes() = assertEquals("5 min ago", ago(5 * min + 30_000))

    @Test fun oneHourReadsAsWords() = assertEquals("an hour ago", ago(hour + 10 * min))

    @Test fun hours() = assertEquals("23 hours ago", ago(23 * hour + 59 * min))

    @Test fun oneDayIsYesterday() = assertEquals("yesterday", ago(day + 3 * hour))

    // DateUtils would switch to an absolute date here ("sep 12, 2026").
    @Test fun overAWeekStaysRelative() = assertEquals("12 days ago", ago(12 * day))

    // Clock corrected backwards after a check: never "in 3 hours".
    @Test fun aTimestampInTheFutureIsJustNow() = assertEquals("just now", ago(-3 * hour))
}
