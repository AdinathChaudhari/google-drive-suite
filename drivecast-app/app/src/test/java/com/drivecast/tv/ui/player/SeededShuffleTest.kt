package com.drivecast.tv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Test

class SeededShuffleTest {

    @Test
    fun matchesServerVector_ints() {
        val result = SeededShuffle.shuffle((0..9).toList(), 123456789L)
        assertEquals(listOf(8, 3, 9, 2, 4, 6, 1, 5, 0, 7), result)
    }

    @Test
    fun matchesServerVector_strings() {
        val result = SeededShuffle.shuffle(listOf("a", "b", "c", "d", "e", "f"), 42L)
        assertEquals(listOf("e", "d", "a", "c", "f", "b"), result)
    }

    @Test
    fun matchesServerVector_zeroSeed() {
        val result = SeededShuffle.shuffle((0..4).toList(), 0L)
        assertEquals(listOf(2, 3, 1, 4, 0), result)
    }

    @Test
    fun sameSeedTwice_producesEqualOrder() {
        val input = (0..19).toList()
        val first = SeededShuffle.shuffle(input, 999L)
        val second = SeededShuffle.shuffle(input, 999L)
        assertEquals(first, second)
    }

    @Test
    fun inputListIsNotMutated() {
        val input = (0..9).toList()
        val original = input.toList()
        SeededShuffle.shuffle(input, 123456789L)
        assertEquals(original, input)
    }
}
