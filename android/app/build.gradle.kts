plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.valleylotto.scanner"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.valleylotto.scanner"
        minSdk = 24            // Android 7+, covers any realistic counter device
        targetSdk = 34
        // Every CI build must outrank the one on the phone, or Android calls the
        // install a downgrade and refuses it. CI passes the run number.
        versionCode = (System.getenv("VL_VERSION_CODE") ?: "1").toInt()
        versionName = "1.0"
    }

    // A STABLE signing key, supplied by CI from repository secrets. This matters
    // more than it looks: Android only installs an update when the new APK is
    // signed with the same key as the installed one, and a CI runner's automatic
    // debug key is regenerated from scratch on every run — so consecutive builds
    // were signed by different keys and the phone rejected the second one with
    // "package conflicts with an existing package". With this, every build
    // installs cleanly over the last.
    val keystore = System.getenv("VL_KEYSTORE")
    signingConfigs {
        if (!keystore.isNullOrBlank()) {
            create("valley") {
                storeFile = file(keystore)
                storePassword = System.getenv("VL_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("VL_KEY_ALIAS") ?: "valleylotto"
                keyPassword = System.getenv("VL_KEY_PASSWORD")
                    ?: System.getenv("VL_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Falls back to the debug key for a local build with no keystore
            // configured — fine for trying it out, not for a store device.
            signingConfig = signingConfigs.findByName("valley")
                ?: signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
