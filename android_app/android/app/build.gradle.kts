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
        // flutter_inappwebview requires API 21+.
        minSdk = 21
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
