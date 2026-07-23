plugins {
    alias(libs.plugins.android.test)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.androidx.baselineprofile)
}

android {
    namespace = "com.drivecast.tv.baselineprofile"
    compileSdk = 34

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    defaultConfig {
        // Macrobenchmark/baseline-profile generation requires API 28+ regardless of the
        // app's own minSdk (25) -- this module only ever runs as instrumentation, never
        // ships to a device.
        minSdk = 28
        targetSdk = 34

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    targetProjectPath = ":app"
    experimentalProperties["android.experimental.self-instrumenting"] = true
}

// No managed devices are configured here: this environment has no rooted device and no
// API 28+ emulator (the only hardware available is an unrooted Fire TV Stick, which cannot
// collect baseline profiles). Run this module's `generateReleaseBaselineProfile` task later
// with `-Pandroidx.baselineprofile.useconnecteddevices=true` against a real, rooted Fire TV
// Stick or an API-30 TV emulator, once all UI clusters have landed (per the plan's
// dependency_notes: this item's profile journey is regenerated LAST).
baselineProfile {
    useConnectedDevices = true
}

dependencies {
    implementation(libs.androidx.test.ext.junit)
    implementation(libs.androidx.test.espresso.core)
    implementation(libs.androidx.test.uiautomator)
    implementation(libs.androidx.benchmark.macro.junit4)
}
