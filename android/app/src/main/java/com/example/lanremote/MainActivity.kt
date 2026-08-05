package com.example.lanremote

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
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
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent {
            LanRemoteTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    RemoteApp()
                }
            }
        }
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
                // Scroll now streams fine-grained wheel units, many per gesture frame, so
                // it can't drive haptics any more — that would be a continuous buzz. The
                // UI reports detent crossings separately and those keep the old feel.
                onScroll = vm::scroll,
                onScrollDetent = {
                    if (state.settings.haptics) haptics.scrollTick()
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

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ReconnectingScreen(name: String, onCancel: () -> Unit) {
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
            "Connection dropped. Make sure the laptop and Wi-Fi are still on.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 8.dp),
        )
        OutlinedButton(onClick = onCancel, modifier = Modifier.padding(top = 24.dp)) {
            Text("Cancel")
        }
    }
}
