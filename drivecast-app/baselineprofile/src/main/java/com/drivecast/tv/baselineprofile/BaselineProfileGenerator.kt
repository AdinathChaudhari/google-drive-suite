package com.drivecast.tv.baselineprofile

import androidx.benchmark.macro.junit4.BaselineProfileRule
import androidx.test.filters.LargeTest
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import org.junit.Rule
import org.junit.Test

/**
 * Generates a baseline profile covering the app's critical cold-launch journey: splash ->
 * home grid -> browse the continue-watching shelf and a couple of grid rows -> open a
 * detail screen.
 *
 * NOT RUN in this environment: baseline profile generation needs a rooted device or an
 * API 28+ emulator, and the only hardware available here is an unrooted Fire TV Stick
 * (API < 28), which cannot collect profiles. This class only scaffolds the generator so it
 * can be run later:
 *
 *   ./gradlew :app:generateReleaseBaselineProfile
 *
 * against a real, rooted Fire TV Stick or an API-30 TV emulator. Per the plan's
 * dependency_notes, re-run this LAST, after every other UI work item has landed, so the
 * embedded profile reflects the final code paths.
 */
@LargeTest
class BaselineProfileGenerator {

    @get:Rule
    val baselineProfileRule = BaselineProfileRule()

    @Test
    fun generate() = baselineProfileRule.collect(
        packageName = "com.drivecast.tv",
    ) {
        pressHome()
        startActivityAndWait()

        // Wait past the splash screen for the first focusable content (a poster card) to
        // render, i.e. the home grid's real first frame.
        device.wait(Until.hasObject(By.focusable(true)), 5_000)

        // D-pad right across the continue-watching shelf.
        repeat(10) {
            device.pressDPadRight()
        }

        // D-pad down through a few grid rows.
        repeat(3) {
            device.pressDPadDown()
        }

        // Open one detail screen and let it settle.
        device.pressDPadCenter()
        device.waitForIdle()
    }
}
