package com.example.lanremote.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Wifi
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ElevatedButton
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.example.lanremote.ConnState
import com.example.lanremote.UiState
import com.example.lanremote.data.Device
import com.example.lanremote.data.DiscoveredHost

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun ConnectionScreen(
    state: UiState,
    onName: (String) -> Unit,
    onIp: (String) -> Unit,
    onPort: (String) -> Unit,
    onToken: (String) -> Unit,
    onConnectManual: () -> Unit,
    onConnectSaved: (Device) -> Unit,
    onUseDiscovered: (DiscoveredHost) -> Unit,
    onDeleteDevice: (Device) -> Unit,
    onScanQr: () -> Unit,
    onRescan: () -> Unit,
) {
    val connecting = state.conn == ConnState.Connecting

    // Progressive disclosure: the rare paths start collapsed so the home screen
    // reads as "scan the QR" and nothing else.
    var showDiscovered by rememberSaveable { mutableStateOf(true) }
    var showManual by rememberSaveable { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .imePadding(),
    ) {
        // Scrollable content fills the space above the pinned action.
        Column(
            modifier = Modifier
                .weight(1f)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp)
                .padding(top = 16.dp),
        ) {
            Text(
                "LazeR",
                style = MaterialTheme.typography.displaySmall,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.primary,
            )

            // Saved devices — the fast path, kept visible when present.
            if (state.savedDevices.isNotEmpty()) {
                SectionLabel("Saved devices")
                state.savedDevices.forEach { dev ->
                    SavedRow(
                        device = dev,
                        enabled = !connecting,
                        onClick = { onConnectSaved(dev) },
                        onDelete = { onDeleteDevice(dev) },
                    )
                }
            }

            // Discovered laptops — collapsed by default; expand to view + rescan.
            ExpandHeader(
                title = "Found on your network" +
                    if (state.discovered.isNotEmpty()) "  (${state.discovered.size})" else "",
                expanded = showDiscovered,
                onToggle = { showDiscovered = !showDiscovered },
                trailing = {
                    TextButton(onClick = { onRescan(); showDiscovered = true }) {
                        Icon(Icons.Filled.Refresh, contentDescription = null,
                            modifier = Modifier.size(18.dp))
                        Text("  Scan")
                    }
                },
            )
            AnimatedVisibility(visible = showDiscovered) {
                Column {
                    if (state.discovered.isNotEmpty()) {
                        state.discovered.forEach { host ->
                            HostRow(host) { onUseDiscovered(host) }
                        }
                    } else {
                        EmptyHint(
                            "No laptop found yet. Make sure the phone and laptop are on " +
                                "the same Wi-Fi and the LazeR app is running on the laptop, " +
                                "then tap Scan. You can also scan the QR below."
                        )
                    }
                }
            }

            // Manual entry — rarely needed, so tucked behind a collapsed header.
            ExpandHeader(
                title = "Enter manually",
                expanded = showManual,
                onToggle = { showManual = !showManual },
            )
            AnimatedVisibility(visible = showManual) {
                ManualCard(state, connecting, onName, onIp, onPort, onToken, onConnectManual)
            }

            if (state.error != null) {
                Spacer(Modifier.height(12.dp))
                Text(
                    state.error,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
            Spacer(Modifier.height(16.dp))
        }

        // Primary action pinned at the bottom — easy thumb reach.
        Surface(tonalElevation = 3.dp, color = MaterialTheme.colorScheme.surface) {
            Column(
                Modifier
                    .fillMaxWidth()
                    .navigationBarsPadding()
                    .padding(horizontal = 20.dp)
                    .padding(top = 12.dp, bottom = 16.dp),
            ) {
                Button(
                    onClick = onScanQr,
                    enabled = !connecting,
                    shapes = ButtonDefaults.shapes(),
                    modifier = Modifier.fillMaxWidth().height(56.dp),
                ) {
                    if (connecting) {
                        LoadingIndicator(
                            modifier = Modifier.size(24.dp),
                            color = MaterialTheme.colorScheme.onPrimary,
                        )
                        Text("  Connecting…", style = MaterialTheme.typography.titleMedium)
                    } else {
                        Icon(Icons.Filled.QrCodeScanner, contentDescription = null,
                            modifier = Modifier.size(22.dp))
                        Text("  Scan QR to connect", style = MaterialTheme.typography.titleMedium)
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ManualCard(
    state: UiState,
    connecting: Boolean,
    onName: (String) -> Unit,
    onIp: (String) -> Unit,
    onPort: (String) -> Unit,
    onToken: (String) -> Unit,
    onConnectManual: () -> Unit,
) {
    Card(
        shape = RoundedCornerShape(24.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            OutlinedTextField(
                value = state.name, onValueChange = onName,
                label = { Text("Name (optional)") }, singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Row {
                OutlinedTextField(
                    value = state.ip, onValueChange = onIp,
                    label = { Text("Laptop IP") }, placeholder = { Text("192.168.1.20") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.weight(2f),
                )
                Spacer(Modifier.width(8.dp))
                OutlinedTextField(
                    value = state.port, onValueChange = onPort,
                    label = { Text("Port") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = state.token, onValueChange = onToken,
                label = { Text("Token") }, placeholder = { Text("A1B2C3") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(
                    capitalization = KeyboardCapitalization.Characters,
                    keyboardType = KeyboardType.Text,
                ),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Manual entry is plaintext (no encryption) — use only on a trusted network. " +
                    "Scan the QR for an encrypted connection.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            ElevatedButton(
                onClick = onConnectManual,
                enabled = !connecting,
                modifier = Modifier.fillMaxWidth().height(50.dp),
                shape = RoundedCornerShape(16.dp),
            ) {
                if (connecting) {
                    LoadingIndicator(modifier = Modifier.size(20.dp))
                    Text("  Connecting…")
                } else {
                    Text("Connect & save")
                }
            }
        }
    }
}

/** Collapsible section header: a tappable title row with a chevron + optional action. */
@Composable
private fun ExpandHeader(
    title: String,
    expanded: Boolean,
    onToggle: () -> Unit,
    trailing: @Composable (() -> Unit)? = null,
) {
    Spacer(Modifier.height(8.dp))
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onToggle)
            .padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            title,
            style = MaterialTheme.typography.titleSmall,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.weight(1f),
        )
        trailing?.invoke()
        Icon(
            if (expanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
            contentDescription = if (expanded) "Collapse" else "Expand",
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun EmptyHint(text: String) {
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh),
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
    ) {
        Text(
            text,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(16.dp),
        )
    }
}

@Composable
private fun SectionLabel(text: String) {
    Spacer(Modifier.height(20.dp))
    Text(
        text,
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(bottom = 8.dp),
    )
}

@Composable
private fun HostRow(host: DiscoveredHost, onClick: () -> Unit) {
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.secondaryContainer),
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp).clickable(onClick = onClick),
    ) {
        Row(
            Modifier.padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(Icons.Filled.Wifi, contentDescription = null,
                tint = MaterialTheme.colorScheme.onSecondaryContainer)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(host.name, style = MaterialTheme.typography.titleMedium)
                Text("${host.ip}:${host.port}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer)
            }
            Text("Use", color = MaterialTheme.colorScheme.primary,
                style = MaterialTheme.typography.labelLarge)
        }
    }
}

@Composable
private fun SavedRow(
    device: Device,
    enabled: Boolean,
    onClick: () -> Unit,
    onDelete: () -> Unit,
) {
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh),
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)
            .clickable(enabled = enabled, onClick = onClick),
    ) {
        Row(
            Modifier.padding(start = 16.dp, top = 8.dp, bottom = 8.dp, end = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(Icons.Filled.Computer, contentDescription = null,
                tint = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(device.name, style = MaterialTheme.typography.titleMedium)
                Text("${device.ip}:${device.port}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Text("Tap to connect", color = MaterialTheme.colorScheme.primary,
                style = MaterialTheme.typography.labelMedium)
            IconButton(onClick = onDelete) {
                Icon(Icons.Filled.Delete, contentDescription = "Delete",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}
