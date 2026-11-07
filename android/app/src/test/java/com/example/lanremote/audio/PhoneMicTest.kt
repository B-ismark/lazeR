package com.example.lanremote.audio

import org.junit.Assert.assertArrayEquals
import org.junit.Test

class PhoneMicTest {

    @Test
    fun `48 kHz comes down to 16 kHz by averaging threes`() {
        val src = shortArrayOf(3, 6, 9, -300, -300, -300, 32767, 32767, 32767)
        assertArrayEquals(shortArrayOf(6, -300, 32767), downsample3(src))
    }

    @Test
    fun `a full 48 kHz frame becomes one 16 kHz frame`() {
        val out = downsample3(ShortArray(PhoneMic.FRAME * 3))
        assertArrayEquals(ShortArray(PhoneMic.FRAME), out)
    }

    @Test
    fun `samples go out little-endian, as the laptop reads int16`() {
        val bytes = toBytes(shortArrayOf(0x0102, -2))
        assertArrayEquals(byteArrayOf(0x02, 0x01, 0xFE.toByte(), 0xFF.toByte()), bytes)
    }
}
