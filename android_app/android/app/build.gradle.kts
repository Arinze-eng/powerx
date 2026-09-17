plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.powerx.powerx_android"
    compileSdk = flutter.compileSdkVersion
    // Pinned to the highest NDK required by the resolved plugin set
    // (device_info_plus, file_picker, record_android, ...). NDK releases are
    // backward compatible, so a single version satisfies every plugin and
    // silences the "plugin(s) depend on a different Android NDK" warning.
    ndkVersion = "27.0.12077973"

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_11.toString()
    }

    defaultConfig {
        applicationId = "com.powerx.powerx_android"
        // API 23 (Marshmallow) is the floor imposed by the voice-note stack:
        // record_android declares minSdk 23 in its manifest, and the Android
        // manifest merger hard-fails when the app floor is lower
        // ("uses-sdk:minSdkVersion 21 cannot be smaller than version 23
        // declared in library [:record_android]"). 23 is also the API level
        // that introduced the runtime-permission model the mic prompt relies
        // on, so this raises the floor rather than forcing the library in with
        // tools:overrideLibrary (which would only defer the crash to runtime).
        minSdk = 23
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // Debug-signed release so the APK installs directly without a
            // custom keystore. Swap in a real signing config for Play Store
            // distribution.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

flutter {
    source = "../.."
}
