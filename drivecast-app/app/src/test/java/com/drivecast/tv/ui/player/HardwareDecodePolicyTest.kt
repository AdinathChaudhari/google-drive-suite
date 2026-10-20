package com.drivecast.tv.ui.player

import com.drivecast.tv.api.MediaInfo
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The app only forwards the server's boolean; unknown/failed lookups keep hardware. */
class HardwareDecodePolicyTest {
    @Test
    fun nullInfoKeepsHardware() = assertFalse(shouldDisableHardware(null))

    @Test
    fun serverSaysHardwareKeepsHardware() =
        assertFalse(shouldDisableHardware(MediaInfo(fileId = "f", known = true, codec = "hevc")))

    @Test
    fun serverSaysSoftwareDisablesHardware() =
        assertTrue(shouldDisableHardware(MediaInfo(fileId = "f", known = true, codec = "h264", vlcDisableHardware = true)))
}
