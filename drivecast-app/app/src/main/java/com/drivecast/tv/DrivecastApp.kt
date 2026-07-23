package com.drivecast.tv

import android.app.Application
import android.content.ComponentCallbacks2
import android.content.pm.ApplicationInfo
import android.util.Log
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.core.content.ContextCompat
import androidx.profileinstaller.ProfileVerifier
import com.drivecast.tv.di.AppContainer

/** Application subclass: owns the manual [AppContainer] for the process lifetime. */
class DrivecastApp : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)

        val isDebuggable = (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0
        if (isDebuggable) {
            logBaselineProfileStatus()
        }
    }

    /** Debug-only: confirms the embedded baseline profile actually installed/compiled. */
    private fun logBaselineProfileStatus() {
        val future = ProfileVerifier.getCompilationStatusAsync()
        future.addListener(
            {
                val status = future.get()
                Log.d(
                    "ProfileVerifier",
                    "resultCode=${status.profileInstallResultCode} " +
                        "isCompiledWithProfile=${status.isCompiledWithProfile}",
                )
            },
            ContextCompat.getMainExecutor(this),
        )
    }

    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        // Fire OS kills memory-heavy backgrounded apps first; clearing the poster
        // memory cache when we're backgrounded preserves a fast warm start instead
        // of getting killed outright.
        if (level >= ComponentCallbacks2.TRIM_MEMORY_UI_HIDDEN) {
            container.imageLoader.memoryCache?.clear()
        }
    }
}

/** Provides the [AppContainer] to the Compose tree. */
val LocalAppContainer = staticCompositionLocalOf<AppContainer> {
    error("AppContainer not provided")
}
