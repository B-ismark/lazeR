package com.example.lanremote

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.lanremote.ui.ConnectionScreen
import com.example.lanremote.ui.ControlActions
import com.example.lanremote.ui.ControlScreen
import com.example.lanremote.ui.theme.LanRemoteTheme
import com.example.lanremote.util.Haptics
import com.example.lanremote.util.startQrScan

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
    }
}

@Composable
private fun RemoteApp(vm: RemoteViewModel = viewModel()) {
    val state by vm.state.collectAsState()
    val context = LocalContext.current
    val haptics = remember { Haptics(context) }

    when (state.conn) {
        ConnState.Connected -> ControlScreen(
            state = state,
            a = ControlActions(
                onMove = vm::move,
                onScroll = { dx, dy ->
                    if (state.settings.haptics) haptics.scrollTick()
                    vm.scroll(dx, dy)
                },
                onZoom = { steps ->
                    if (state.settings.haptics) haptics.scrollTick()
                    vm.zoom(steps)
                },
                onClick = {
                    if (state.settings.haptics) haptics.leftClick()
                    vm.click()
                },
                onRightClick = {
                    if (state.settings.haptics) haptics.rightClick()
                    vm.rightClick()
                },
                onMiddleClick = {
                    if (state.settings.haptics) haptics.leftClick()
                    vm.middleClick()
                },
                onSwitchStep = { forward ->
                    if (state.settings.haptics) haptics.scrollTick()
                    vm.switchAppStep(forward)
                },
                onSwitchEnd = vm::switchAppEnd,
                onDragStart = {
                    if (state.settings.haptics) haptics.leftClick()
                    vm.dragStart()
                },
                onDragEnd = vm::dragEnd,
                onVolume = vm::setVolume,
                onBrightness = vm::setBrightness,
                onMedia = vm::media,
                onKeyboardInput = vm::onKeyboardInput,
                onSpecialKey = vm::specialKey,
                onCombo = vm::combo,
                onSystem = vm::system,
                onSensitivity = vm::setSensitivity,
                onNaturalScroll = vm::setNaturalScroll,
                onHaptics = vm::setHaptics,
                onAcceleration = vm::setAcceleration,
                onUpdateCheck = vm::setUpdateCheck,
                onButtonTap = { if (state.settings.haptics) haptics.tap() },
                onDisconnect = vm::disconnect,
            ),
        )
        ConnState.Reconnecting -> ReconnectingScreen(
            name = state.name.ifBlank { state.ip },
            hint = state.error,
            onCancel = vm::disconnect,
        )
        else -> ConnectionScreen(
            state = state,
            onName = vm::onName,
            onIp = vm::onIp,
            onPort = vm::onPort,
            onToken = vm::onToken,
            onConnectManual = vm::connectManual,
            onConnectSaved = vm::connectSaved,
            onUseDiscovered = vm::useDiscovered,
            onDeleteDevice = vm::deleteDevice,
            onScanQr = {
                startQrScan(
                    context = context,
                    onResult = vm::applyScannedUri,
                    onError = vm::reportError,
                )
            },
            onRescan = vm::rescan,
            onOpenRelease = {
                // Hand off to the browser; we never fetch or install the APK
                // ourselves. A device with no browser at all would throw, so the
                // failure is reported rather than crashing the app.
                try {
                    context.startActivity(
                        Intent(Intent.ACTION_VIEW, Uri.parse(vm.releasesUrl()))
                    )
                } catch (e: Exception) {
                    vm.reportError("Couldn't open the browser to show the release.")
                }
            },
        )
    }
}

/**
 * [hint] is the diagnosis the reconnect loop reaches once an outage has gone on long
 * enough to be worth explaining. It replaces the generic line rather than joining it,
 * and it does NOT mean the retry has stopped — the loop keeps going underneath, so a
 * laptop that wakes up later reconnects on its own with nothing to tap.
 */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ReconnectingScreen(name: String, hint: String?, onCancel: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(32.dp),
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
        OutlinedButton(onClick = onCancel, modifier = Modifier.padding(top = 24.dp)) {
            Text("Cancel")
        }
    }
}
