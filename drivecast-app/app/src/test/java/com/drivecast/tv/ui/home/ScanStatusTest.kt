package com.drivecast.tv.ui.home

import com.drivecast.tv.api.RefreshStatus
import org.junit.Assert.assertEquals
import org.junit.Test

class ScanStatusTest {

    @Test
    fun totalZero_rendersUpdatingLibrary_notAFraction() {
        val st = RefreshStatus(running = true, scanned = 0, total = 0)
        assertEquals("Updating library…", scanStatusLabel(st))
    }

    @Test
    fun totalZero_withAdded_appendsNewCount() {
        val st = RefreshStatus(running = true, scanned = 0, total = 0, added = 3)
        assertEquals("Updating library… · +3 new", scanStatusLabel(st))
    }

    @Test
    fun fewScopeNames_rendersRefreshingList() {
        val st = RefreshStatus(running = true, scanned = 1, total = 2, scopeNames = listOf("A", "B"))
        assertEquals("Refreshing A, B… 1/2", scanStatusLabel(st))
    }

    @Test
    fun manyScopeNames_rendersScanningDrives() {
        val st = RefreshStatus(running = true, scanned = 2, total = 4, scopeNames = listOf("A", "B", "C", "D"))
        assertEquals("Scanning drives… 2/4", scanStatusLabel(st))
    }

    @Test
    fun addedSuffix_onlyWhenPositive() {
        val noAdded = RefreshStatus(running = true, scanned = 1, total = 4)
        assertEquals("Scanning drives… 1/4", scanStatusLabel(noAdded))
        val withAdded = RefreshStatus(running = true, scanned = 1, total = 4, added = 1)
        assertEquals("Scanning drives… 1/4 · +1 new", scanStatusLabel(withAdded))
    }

    // ---- scanCompleteNotice: a finished scan must say SOMETHING, even when nothing changed ----

    @Test
    fun completeNotice_nothingChanged_saysUpToDate() {
        assertEquals("Library up to date", scanCompleteNotice(added = 0, removed = 0))
    }

    @Test
    fun completeNotice_addedOnly() {
        assertEquals("+3 new", scanCompleteNotice(added = 3, removed = 0))
    }

    @Test
    fun completeNotice_removedOnly() {
        assertEquals("3 removed", scanCompleteNotice(added = 0, removed = 3))
    }

    @Test
    fun completeNotice_addedAndRemoved() {
        assertEquals("+2 new · 1 removed", scanCompleteNotice(added = 2, removed = 1))
    }
}
