package com.example.lanremote.audio

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin

class PhoneMicTest {

    private fun tone(hz: Double, n: Int, amp: Double = 10000.0) =
        ShortArray(n) { (amp * sin(2 * PI * hz * it / 48000.0)).toInt().toShort() }

    private fun peak(s: ShortArray, from: Int = 0) =
        (from until s.size).maxOf { abs(s[it].toInt()) }

    @Test
    fun `a full 48 kHz frame becomes one 16 kHz frame`() {
        val out = Decimator3().process(ShortArray(PhoneMic.FRAME * 3))
        assertEquals(PhoneMic.FRAME, out.size)
    }

    @Test
    fun `a steady level comes through unchanged`() {
        val d = Decimator3()
        d.process(ShortArray(960) { 1000 })              // let the filter fill
        val out = d.process(ShortArray(960) { 1000 })
        out.forEach { assertTrue("got $it", abs(it - 1000) <= 1) }
    }

    @Test
    fun `speech frequencies pass`() {
        val d = Decimator3()
        d.process(tone(1000.0, 960))
        val out = d.process(tone(1000.0, 960))
        assertTrue(peak(out) > 9000)
    }

    @Test
    fun `hiss above 8 kHz does not fold back down`() {
        // Plain averaging of threes let a 14 kHz tone through as a 2 kHz alias.
        val d = Decimator3()
        d.process(tone(14000.0, 960))
        val out = d.process(tone(14000.0, 960))
        assertTrue("peak ${peak(out)}", peak(out) < 300)
    }

    @Test
    fun `frames join without a click`() {
        val d = Decimator3()
        val src = tone(1000.0, 1920)
        val whole = Decimator3().process(src)
        val split = d.process(src.copyOfRange(0, 960)) + d.process(src.copyOfRange(960, 1920))
        assertArrayEquals(whole, split)
    }

    @Test
    fun `samples go out little-endian, as the laptop reads int16`() {
        val bytes = toBytes(shortArrayOf(0x0102, -2))
        assertArrayEquals(byteArrayOf(0x02, 0x01, 0xFE.toByte(), 0xFF.toByte()), bytes)
    }
}
