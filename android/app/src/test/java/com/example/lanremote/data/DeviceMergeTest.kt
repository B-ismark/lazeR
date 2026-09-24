package com.example.lanremote.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceMergeTest {

    private val qr = Device("192.168.1.5:50505", "Dearth", "192.168.1.5", 50505, "A1B2C3",
        key = "KEY-A", bound = true)

    @Test
    fun `a typed-code save never blanks a saved key`() {
        val typed = Device("192.168.1.5:50505", "Dearth", "192.168.1.5", 50505, "A1B2C3")
        val out = mergeDevice(listOf(qr), typed)
        assertEquals(1, out.size)
        assertEquals("KEY-A", out[0].key)
        assertTrue("same key, so still bound", out[0].bound)
    }

    @Test
    fun `a laptop rescanned at a new address replaces its old record`() {
        val moved = Device("192.168.1.77:50505", "Dearth", "192.168.1.77", 50505, "A1B2C3",
            key = "KEY-A")
        val out = mergeDevice(listOf(qr), moved)
        assertEquals(1, out.size)
        assertEquals("192.168.1.77", out[0].ip)
        assertEquals("the id follows the laptop", qr.id, out[0].id)
    }

    @Test
    fun `duplicates saved before the key rule are folded into one`() {
        val dup = qr.copy(id = "192.168.1.9:50505", ip = "192.168.1.9")
        val other = Device("10.0.0.2:50505", "Other", "10.0.0.2", 50505, "ZZZZZZ", key = "KEY-B")
        val out = mergeDevice(listOf(qr, other, dup), qr.copy(ip = "192.168.1.50"))
        assertEquals(listOf(qr.id, other.id), out.map { it.id })
    }

    @Test
    fun `a new key resets the bound flag`() {
        val repaired = qr.copy(key = "KEY-NEW", bound = false)
        val out = mergeDevice(listOf(qr), repaired)
        assertEquals("KEY-NEW", out[0].key)
        assertFalse(out[0].bound)
    }

    @Test
    fun `a rescan takes the bound flag from its own handshake`() {
        val rolledBack = qr.copy(bound = false)
        assertFalse(mergeDevice(listOf(qr), rolledBack, rescanned = true)[0].bound)
        assertTrue(mergeDevice(listOf(qr), rolledBack.copy(bound = true), rescanned = true)[0].bound)
        assertTrue("a reconnect keeps it", mergeDevice(listOf(qr), rolledBack)[0].bound)
    }

    @Test
    fun `a laptop that moves onto another laptop's old address keeps both records`() {
        // DHCP gave A's lease to B's old address while B is away.
        val b = Device("192.168.1.20:50505", "Bee", "192.168.1.20", 50505, "BBBBBB", key = "KEY-B")
        val moved = qr.copy(ip = "192.168.1.20")
        val out = mergeDevice(listOf(b, qr), moved)
        assertEquals(listOf(b.id, qr.id), out.map { it.id })
        assertEquals("KEY-B", out[0].key)
        assertEquals("192.168.1.20", out[1].ip)
    }

    @Test
    fun `an exact key match wins over a stale record at the same address`() {
        val stale = Device("192.168.1.20:50505", "Old", "192.168.1.20", 50505, "OOOOOO", key = "KEY-O")
        val scanned = Device("192.168.1.20:50505", "Dearth", "192.168.1.20", 50505, "A1B2C3",
            key = "KEY-A")
        assertEquals(qr, savedMatch(listOf(stale, qr), scanned))
        val out = mergeDevice(listOf(stale, qr), scanned)
        assertEquals(listOf(stale.id, qr.id), out.map { it.id })
        assertEquals("192.168.1.20", out[1].ip)
    }

    @Test
    fun `the record holding the key is the one kept`() {
        // A typed-code record sits at the address the QR laptop is now on.
        val typed = Device("192.168.1.9:50505", "Typed", "192.168.1.9", 50505, "A1B2C3")
        val here = qr.copy(id = "192.168.1.9:50505", ip = "192.168.1.9")
        val out = mergeDevice(listOf(typed, qr), here)
        assertEquals(listOf(qr.id), out.map { it.id })
    }

    @Test
    fun `a different key at a saved address is found, for the replace prompt`() {
        val scanned = Device("192.168.1.5:50505", "Dearth", "192.168.1.5", 50505, "NEWNEW",
            key = "KEY-NEW")
        assertEquals(qr, savedMatch(listOf(qr), scanned))
    }

    @Test
    fun `an unrelated laptop is appended`() {
        val other = Device("10.0.0.2:50505", "Other", "10.0.0.2", 50505, "ZZZZZZ", key = "KEY-B")
        assertEquals(listOf(qr, other), mergeDevice(listOf(qr), other))
    }
}
