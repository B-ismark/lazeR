plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.example.lanremote"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.example.lanremote"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    signingConfigs {
        // Sideload signing: reuse the local debug keystore so the hardened RELEASE
        // APK still installs without a Play account. Not debuggable; just signed.
        create("sideload") {
            val ks = file("${System.getProperty("user.home")}/.android/debug.keystore")
            if (ks.exists()) {
                storeFile = ks
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
        release {
            // Ship THIS build (assembleRelease), never the debug APK: debug is
            // debuggable, so any local app/adb could attach and read saved tokens.
            isDebuggable = false
            // R8: shrink + obfuscate unused code and strip unused resources. Cuts
            // the APK hard (ML Kit/Compose pull a lot) without dropping any feature.
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = signingConfigs.getByName("sideload")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            // Make the debug build's package distinct so a debuggable build can't
            // silently sit where users expect the hardened release.
            applicationIdSuffix = ".debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.02")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.5")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.5")
    implementation("androidx.activity:activity-compose:1.9.2")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    // QR scanning via Google's on-device code scanner (no CAMERA permission needed)
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
