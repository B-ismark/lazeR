package com.example.lanremote.ui

import org.junit.Assert.assertEquals
import org.junit.Test

/** The pad pulls in from a side only by how far it reaches into that side's
 *  back-gesture zone. A 1000px window with 60px zones on both edges. */
class BackZoneClearanceTest {
    private fun clear(left: Float, right: Float, zoneLeft: Int = 60, zoneRight: Int = 60) =
        backZoneClearance(left, right, 1000f, zoneLeft, zoneRight)

    @Test fun clearOfBothZonesAddsNothing() = assertEquals(0 to 0, clear(100f, 900f))

    @Test fun reachingIntoTheLeftZonePullsInOnTheLeftOnly() =
        assertEquals(20 to 0, clear(40f, 900f))

    @Test fun reachingIntoTheRightZonePullsInOnTheRightOnly() =
        assertEquals(0 to 25, clear(100f, 965f))

    @Test fun edgeToEdgePullsInOnBothSides() = assertEquals(60 to 60, clear(0f, 1000f))

    @Test fun zonesOfDifferentWidthsAreEachHonoured() =
        assertEquals(48 to 30, clear(0f, 1000f, zoneLeft = 48, zoneRight = 30))

    @Test fun aFractionalOverlapRoundsUpSoNoPixelIsLeftInTheZone() =
        assertEquals(1 to 0, clear(59.5f, 900f))

    @Test fun noZoneWithButtonNavigationAddsNothing() =
        assertEquals(0 to 0, clear(0f, 1000f, zoneLeft = 0, zoneRight = 0))
}
