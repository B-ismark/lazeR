package com.example.lanremote

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.lanremote.ui.ConnectionScreen
import com.example.lanremote.ui.ControlActions
import com.example.lanremote.ui.ControlScreen
import com.example.lanremote.ui.SettingsActions
import com.example.lanremote.ui.theme.LanRemoteTheme
import com.example.lanremote.util.Haptics
import com.example.lanremote.util.startQrScan
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    // Held here rather than only inside the composable so onResume can reach it. Same
    // instance either way — both resolve against this activity's ViewModelStore.
    private val vm: RemoteViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent {
            LanRemoteTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    RemoteApp(vm)
                }
            }
        }
    }

    /**
     * Coming back to the app is the moment to retry, not a moment to wait.
     *
     * The usual sequence is: laptop sleeps, phone goes in a pocket, both come back
     * minutes or hours later. The reconnect loop is still running by then (it no
     * longer gives up), but it's on its slow cadence, and Android may have been
     * holding its sockets down while the app was backgrounded. Picking the phone up
     * is the user saying "now" — so retry immediately instead of making them watch a
     * spinner tick down, or tap a device they never meant to disconnect from.
     */
    override fun onResume() {
        super.onResume()
        vm.kickReconnect()
        vm.onForeground()   // update check; throttled to once a day
    }
}

@Composable
private fun RemoteApp(vm: RemoteViewModel = viewModel()) {
    val state by vm.state.collectAsState()
    val context = LocalContext.current
    val haptics = remember { Haptics(context) }
    val scope = rememberCoroutineScope()

    // Every action object below is built ONCE: rebuilding them on each state change
    // (health tick, slider frame) defeats Compose's skipping for the whole control screen.
    // Lambdas that depend on a setting read it from the ViewModel at call time
    // instead of capturing a value that would go stale.
    fun haptic(block: Haptics.() -> Unit) {
        if (vm.state.value.settings.haptics) haptics.block()
    }

    val scanQr: () -> Unit = remember(vm) {
        { startQrScan(context = context, onResult = vm::applyScannedUri, onError = vm::reportScanError) }
    }

    val settingsActions = remember(vm) {
        SettingsActions(
            onSensitivity = vm::setSensitivity,
            onNaturalScroll = vm::setNaturalScroll,
            onScrollStripLeft = vm::setScrollStripLeft,
            onHaptics = vm::setHaptics,
            onAcceleration = vm::setAcceleration,
            onUpdateCheck = vm::setUpdateCheck,
            onCheckUpdateNow = vm::checkForUpdatesNow,
            // The fallback when the app can't install a release itself. A device
            // with no browser at all would throw; a toast rather than the error
            // slot, because the control screen has no error banner.
            onOpenRelease = {
                try {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(vm.releasesUrl())))
                } catch (e: Exception) {
                    Toast.makeText(context, "Couldn't open a browser to show the release.",
                        Toast.LENGTH_LONG).show()
                }
            },
            onDownloadUpdate = vm::downloadUpdate,
            onCancelDownload = vm::cancelDownload,
            onInstallUpdate = {
                scope.launch {
                    val intent = vm.installIntent() ?: return@launch
                    try {
                        context.startActivity(intent)
                    } catch (e: Exception) {
                        Toast.makeText(context, "Couldn't open Android's installer.",
                            Toast.LENGTH_LONG).show()
                    }
                }
            },
        )
    }

    val controlActions = remember(vm) {
        ControlActions(
            onMove = vm::move,
            onScroll = { dx, dy -> haptic { scrollTick() }; vm.scroll(dx, dy) },
            onZoom = { steps -> haptic { scrollTick() }; vm.zoom(steps) },
            onClick = { haptic { leftClick() }; vm.click() },
            onRightClick = { haptic { rightClick() }; vm.rightClick() },
            onMiddleClick = { haptic { leftClick() }; vm.middleClick() },
            onSwitchStep = { forward -> haptic { scrollTick() }; vm.switchAppStep(forward) },
            onSwitchEnd = vm::switchAppEnd,
            onDragStart = { haptic { leftClick() }; vm.dragStart() },
            onDragEnd = vm::dragEnd,
            onVolume = vm::setVolume,
            onBrightness = vm::setBrightness,
            onMedia = vm::media,
            onKeyboardInput = vm::onKeyboardInput,
            onSpecialKey = vm::specialKey,
            onCombo = vm::combo,
            onSystem = vm::system,
            settings = settingsActions,
            onButtonTap = { haptic { tap() } },
            onDismissGestureHint = vm::dismissGestureHint,
            onDisconnect = vm::disconnect,
        )
    }

    when (state.conn) {
        ConnState.Connected -> ControlScreen(state = state, a = controlActions)
        ConnState.Reconnecting -> ReconnectingScreen(
            name = state.deviceName.ifBlank { "the laptop" },
            hint = state.error,
            scanError = state.scanError,
            onCancel = vm::disconnect,
            onBack = vm::stopReconnecting,
            onScanQr = scanQr,
        )
        else -> ConnectionScreen(
            state = state,
            onName = vm::onName,
            onIp = vm::onIp,
            onPort = vm::onPort,
            onToken = vm::onToken,
            onConnectManual = vm::connectManual,
            onConnectSaved = vm::connectSaved,
            onDeleteDevice = vm::deleteDevice,
            onScanQr = scanQr,
            onRescan = vm::rescan,
            onCancelConnect = vm::cancelConnect,
            settings = settingsActions,
        )
    }

    // Above every screen, not inside the connect screen: the Reconnecting screen's
    // Scan QR (the usual way to re-pair) raises it too.
    state.pendingReplace?.let { p ->
        ReplacePairingDialog(p.existing.name, vm::confirmReplace, vm::cancelReplace)
    }
}

/** A scanned QR that would swap a saved laptop's pairing asks first: a QR can be
 *  planted, and the swap would send everything typed to whoever made it. */
@Composable
private fun ReplacePairingDialog(name: String, onConfirm: () -> Unit, onCancel: () -> Unit) {
    AlertDialog(
        onDismissRequest = onCancel,
        title = { Text("Replace the pairing for $name?") },
        text = {
            Text("This QR code has a different pairing than the one saved for $name. " +
                "That's expected if you pressed New code or reinstalled LazeR on the " +
                "laptop. If you didn't, cancel.")
        },
        confirmButton = { TextButton(onClick = onConfirm) { Text("Replace") } },
        dismissButton = { TextButton(onClick = onCancel) { Text("Cancel") } },
    )
}

/**
 * [hint] is the diagnosis: what the laptop said (re-paired, QR only), what the
 * phone knows (it put the laptop to sleep), or — once an outage has gone on long
 * enough to explain — the likely network cause. It does NOT mean the retry has
 * stopped: the loop keeps going underneath, so a laptop that wakes up later
 * reconnects on its own with nothing to tap.
 *
 * Scan QR is always offered, not only once the hint appears: a re-paired laptop
 * can never be reached with the stored key. Back: see [RemoteViewModel.stopReconnecting].
 */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ReconnectingScreen(
    name: String,
    hint: String?,
    scanError: String?,
    onCancel: () -> Unit,
    onBack: () -> Unit,
    onScanQr: () -> Unit,
) {
    BackHandler(onBack = onBack)
    // Scrolls: in landscape a long network diagnosis would otherwise push the title
    // and the Cancel button off both ends. The min height keeps it centred when it fits.
    BoxWithConstraints(Modifier.fillMaxSize()) {
    Column(
        modifier = Modifier.verticalScroll(rememberScrollState()).fillMaxWidth()
            .heightIn(min = maxHeight).padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        // Expressive morphing loading indicator (the shape-shifting polygon).
        LoadingIndicator(modifier = Modifier.size(48.dp))
        Text(
            "Reconnecting to $name…",
            style = MaterialTheme.typography.titleMedium,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 20.dp),
        )
        Text(
            hint ?: "Connection dropped. Make sure the laptop and Wi-Fi are still on.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 8.dp),
        )
        if (hint != null) {
            Text(
                "Still trying — it will reconnect by itself once the laptop is back.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 12.dp),
            )
        }
        if (hint != null) {
            Button(onClick = onScanQr, modifier = Modifier.padding(top = 20.dp)) {
                Text("Scan QR")
            }
        } else {
            OutlinedButton(onClick = onScanQr, modifier = Modifier.padding(top = 24.dp)) {
                Text("Scan QR")
            }
        }
        if (scanError != null) {
            Text(
                scanError,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
        OutlinedButton(onClick = onCancel, modifier = Modifier.padding(top = 8.dp)) {
            Text("Cancel")
        }
    }
    }
}
