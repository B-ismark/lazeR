package com.example.lanremote.util

import android.content.Context
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning

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
            if (value.isNullOrBlank()) onError("Empty QR code") else onResult(value)
        }
        .addOnCanceledListener { /* user backed out — no-op */ }
        .addOnFailureListener { e ->
            onError(e.localizedMessage ?: "Scanner unavailable")
        }
}
