plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.example.lanremote"
    // compileSdk 35 is the floor for Compose Material3 1.4.0 (M3 Expressive).
    // targetSdk stays 34 so we don't opt into Android 15 runtime behavior changes here.
    compileSdk = 35

    defaultConfig {
        applicationId = "com.example.lanremote"
        minSdk = 24
        targetSdk = 34
        // Bumped for v2.0: off-LAN access removed and the wire gained the L3
        // dialect. These were left at 1/"1.0" through six tagged releases, so every
        // shipped APK claimed the same version and users couldn't tell builds apart.
        // publish_release.ps1 now asserts -Tag matches versionName.
        versionCode = 20000
        versionName = "2.0.0"
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

    lint {
        // Compose Material3 1.5.0-alpha trips a bug in the lint bundled with AGP 8.7.3:
        // NonNullableMutableLiveDataDetector throws IncompatibleClassChangeError and
        // crashes lintVitalRelease. It's a lint-tooling incompatibility with the alpha
        // libraries, not a code defect — skip lint on release builds (debug lint still
        // runs). Revisit when bumping off the material3 alpha.
        checkReleaseBuilds = false
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
    // BOM 2025.10.00 pins the stable Compose 1.9.x train (ui/foundation/animation) and
    // is compileSdk-35 friendly.
    val composeBom = platform("androidx.compose:compose-bom:2025.10.00")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.5")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.5")
    implementation("androidx.activity:activity-compose:1.9.2")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    // M3 Expressive lives ONLY in the material3 1.5.0-alpha line — the expressive APIs
    // (MaterialExpressiveTheme, ButtonGroup, LoadingIndicator, FloatingToolbar,
    // MaterialShapes, MotionScheme, expressive button/icon shape-morph) were pulled from
    // 1.4.0 stable. alpha12 is the newest alpha still on the Compose 1.8/1.9 train, so it
    // pairs with the BOM above and keeps compileSdk at 35 (alpha16+ jumps to Compose
    // 1.11/1.12 → compileSdk 36/37 + AGP 9). Explicit version overrides the BOM for this
    // one artifact only. Pinned exactly: alpha APIs churn between releases.
    implementation("androidx.compose.material3:material3:1.5.0-alpha12")
    implementation("androidx.compose.material:material-icons-extended")

    // QR scanning via Google's on-device code scanner (no CAMERA permission needed)
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")

    // Plain JVM unit tests (no Robolectric). SecureChannelTest asserts the v2 wire
    // against the same golden packet as server/tests/test_wire.py, so a layout
    // change on either side fails CI instead of breaking pairing silently.
    // NOTE: deliberately no `testOptions { unitTests.isReturnDefaultValues = true }`
    // — that would make an accidental android.util.Base64 call quietly return null
    // instead of throwing, hiding the bug it's meant to surface.
    testImplementation("junit:junit:4.13.2")
}
