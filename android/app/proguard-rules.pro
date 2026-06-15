# LazeR release (R8) keep rules.
# R8 shrinks/obfuscates unused code + strips unused resources — no feature, UI, or
# security behaviour changes. These keeps protect the few things touched reflectively
# or by Google Play Services at runtime, so nothing breaks after shrinking.

# --- Google ML Kit barcode / code-scanner (QR pairing) ---
# Loaded via Play Services with internal reflection; keep its public surface.
-keep class com.google.mlkit.** { *; }
-keep class com.google.android.gms.internal.mlkit_** { *; }
-dontwarn com.google.mlkit.**
-dontwarn com.google.android.gms.**

# --- javax.crypto AES-GCM (secure wire) is platform code, but be explicit ---
-dontwarn javax.crypto.**

# --- Our saved-device model is (de)serialised by name via org.json; keep names ---
-keepclassmembers class com.example.lanremote.data.Device { *; }

# Kotlin metadata + Compose are handled by AGP/R8 defaults; nothing extra needed.
