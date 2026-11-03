package com.example.lanremote.util

import android.content.Context
import android.util.Log
import com.google.mlkit.common.MlKitException
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning

private const val TAG = "LazeR.QrScanner"

// MlKitException's code-scanner codes, as literals: they were added to the constant
// list over several ML Kit releases, and naming one the pinned version lacks would
// break the build. Values are from the public MlKitException reference.
private const val CODE_SCANNER_UNAVAILABLE = 200
private const val CODE_SCANNER_TASK_IN_PROGRESS = 204
private const val CODE_SCANNER_PIPELINE_INITIALIZATION_ERROR = 205
private const val CODE_SCANNER_GOOGLE_PLAY_SERVICES_VERSION_TOO_OLD = 207

/**
 * Launches Google's on-device code scanner (no CAMERA permission required).
 * Calls [onResult] with the raw QR text, or [onError] with a short message.
 */
fun startQrScan(
    context: Context,
    onResult: (String) -> Unit,
    onError: (String) -> Unit,
) {
    val options = GmsBarcodeScannerOptions.Builder()
        .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
        .build()
    val scanner = GmsBarcodeScanning.getClient(context, options)
    scanner.startScan()
        .addOnSuccessListener { barcode ->
            val value = barcode.rawValue
            if (value.isNullOrBlank()) onError("That QR code was empty. Scan the one in the LazeR window.")
            else onResult(value)
        }
        .addOnCanceledListener { /* user backed out — no-op */ }
        .addOnFailureListener { e ->
            // The raw message is Play services' wording ("Waiting for the Barcode UI
            // module to be downloaded.") — useful in logcat, not on screen.
            Log.w(TAG, "QR scan failed", e)
            onError(scanFailureMessage(e))
        }
}

private fun scanFailureMessage(e: Exception): String =
    when ((e as? MlKitException)?.errorCode) {
        // The scanner module is downloaded on first use; until it lands, every
        // attempt fails with one of these.
        MlKitException.UNAVAILABLE, CODE_SCANNER_PIPELINE_INITIALIZATION_ERROR ->
            "The scanner is still being set up by Google Play services. Try again in a minute."
        CODE_SCANNER_UNAVAILABLE, CODE_SCANNER_GOOGLE_PLAY_SERVICES_VERSION_TOO_OLD ->
            "This phone can't scan QR codes. Pair with a typed code instead."
        CODE_SCANNER_TASK_IN_PROGRESS -> "The scanner is already open."
        else -> "The scanner couldn't start. Try again, or pair with a typed code."
    }
