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
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.SystemUpdate
import androidx.compose.material.icons.filled.Wifi
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ElevatedButton
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import com.example.lanremote.data.DiscoveryStatus

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
    onDeleteDevice: (Device) -> Unit,
    onScanQr: () -> Unit,
    onRescan: () -> Unit,
    onCancelConnect: () -> Unit,
    settings: SettingsActions,
) {
    val connecting = state.conn == ConnState.Connecting

    // Progressive disclosure: the rare paths start collapsed so the home screen
    // reads as "scan the QR" and nothing else. The network list starts open: with
    // nothing saved it's the most useful thing on the screen.
    var showDiscovered by rememberSaveable { mutableStateOf(true) }
    var showManual by rememberSaveable { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(false) }
    // Delete asks before it acts — see the dialog at the bottom of this screen.
    var pendingDelete by remember { mutableStateOf<Device?>(null) }

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
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "LazeR",
                    style = MaterialTheme.typography.displaySmall,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.weight(1f),
                )
                // Settings before pairing too: the update check (the app's only
                // internet use) can be switched off before it ever runs again.
                IconButton(onClick = { showSettings = true }) {
                    SettingsIcon(updateAvailable = state.updateTag != null)
                }
            }

            // Below the title, above everything actionable: seen on the way to
            // connecting without standing between the user and the QR button.
            state.updateTag?.let { UpdateCard(it, state, settings) }

            // Saved devices — the fast path, kept visible when present.
            if (state.savedDevices.isNotEmpty()) {
                SectionLabel("Saved devices")
                state.savedDevices.forEach { dev ->
                    SavedRow(
                        device = dev,
                        enabled = !connecting,
                        onClick = { onConnectSaved(dev) },
                        onDelete = { pendingDelete = dev },
                    )
                }
            }

            // Discovered laptops; see [HostRow].
            ExpandHeader(
                title = "Found on your network" +
                    if (state.discovered.isNotEmpty()) "  (${state.discovered.size})" else "",
                expanded = showDiscovered,
                onToggle = { showDiscovered = !showDiscovered },
                trailing = {
                    TextButton(onClick = { onRescan(); showDiscovered = true }) {
                        Icon(Icons.Filled.Refresh, contentDescription = null,
                            modifier = Modifier.size(18.dp))
                        Text("  Refresh")
                    }
                },
            )
            AnimatedVisibility(visible = showDiscovered) {
                Column {
                    if (state.discovered.isNotEmpty()) {
                        state.discovered.forEach { host ->
                            val saved = state.savedDevices.firstOrNull {
                                it.ip == host.ip && it.port == host.port
                            }
                            HostRow(host, saved != null, enabled = !connecting) {
                                if (saved != null) onConnectSaved(saved) else onScanQr()
                            }
                        }
                    } else {
                        DiscoveryEmpty(state)
                    }
                }
            }

            // Typed code — works only when the laptop allows unencrypted pairing,
            // which it doesn't by default, so it's labelled as the exception it is.
            ExpandHeader(
                title = "Pair with a typed code (advanced)",
                expanded = showManual,
                onToggle = { showManual = !showManual },
            )
            AnimatedVisibility(visible = showManual) {
                ManualCard(state, connecting, onName, onIp, onPort, onToken, onConnectManual)
            }
            Spacer(Modifier.height(16.dp))
        }

        // Primary action pinned at the bottom — easy thumb reach. Errors sit right
        // above it, where they can't scroll off the end of a long saved list.
        Surface(tonalElevation = 3.dp, color = MaterialTheme.colorScheme.surface) {
            Column(
                Modifier
                    .fillMaxWidth()
                    .navigationBarsPadding()
                    .padding(horizontal = 20.dp)
                    .padding(top = 12.dp, bottom = 16.dp),
            ) {
                val message = state.scanError ?: state.error
                if (message != null && !connecting) {
                    Text(
                        message,
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.padding(bottom = 12.dp),
                    )
                }
                if (connecting) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        LoadingIndicator(modifier = Modifier.size(32.dp))
                        Text(
                            "  Connecting to ${state.deviceName}…",
                            style = MaterialTheme.typography.titleMedium,
                            modifier = Modifier.weight(1f),
                        )
                        OutlinedButton(onClick = onCancelConnect) { Text("Cancel") }
                    }
                } else {
                    Button(
                        onClick = onScanQr,
                        shapes = ButtonDefaults.shapes(),
                        modifier = Modifier.fillMaxWidth().height(56.dp),
                    ) {
                        Icon(Icons.Filled.QrCodeScanner, contentDescription = null,
                            modifier = Modifier.size(22.dp))
                        Text("  Scan QR to connect", style = MaterialTheme.typography.titleMedium)
                    }
                }
            }
        }
    }

    // Deleting a saved device discards its stored pairing — recovering means
    // getting at the laptop and rescanning its QR. One misclick on the small
    // trash icon must not cost that, so the delete asks first.
    pendingDelete?.let { dev ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("Remove ${dev.name}?") },
            text = { Text("You'll need to scan the laptop's QR code to pair again.") },
            confirmButton = {
                TextButton(onClick = {
                    pendingDelete = null
                    onDeleteDevice(dev)
                }) { Text("Remove") }
            },
            dismissButton = {
                TextButton(onClick = { pendingDelete = null }) { Text("Cancel") }
            },
        )
    }

    if (showSettings) {
        SettingsSheet(state, settings, onDismiss = { showSettings = false })
    }
}

/** Searching, couldn't search, or searched and found nothing: three states, three messages. */
@Composable
private fun DiscoveryEmpty(state: UiState) {
    when {
        state.discoveryStatus == DiscoveryStatus.Failed -> EmptyHint(
            "This phone can't search the network for laptops right now. Scan the QR " +
                "code in the LazeR window on the laptop instead."
        )
        state.discoveryStatus == DiscoveryStatus.Searching && !state.discoveryQuiet ->
            Card(
                shape = RoundedCornerShape(20.dp),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceContainerHigh),
                modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
            ) {
                Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    Text("  Looking for laptops…",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        else -> EmptyHint(
            "No laptop found. Make sure the phone and laptop are on the same Wi-Fi " +
                "and LazeR is running on the laptop, then tap Refresh — or just scan " +
                "the QR code below. Some networks block this search; the QR still works."
        )
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
            Text(
                "Only works when the laptop allows unencrypted pairing: in the LazeR " +
                    "window, Show details → turn Require encryption off. The code travels " +
                    "unencrypted, so use this only on a network you trust. Scanning the " +
                    "QR is encrypted and needs no setting.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
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
                    // Decimal (not Number): plain number pads on many IMEs have no
                    // "." key at all, which made typing an IP impossible. The comma
                    // some locales render in place of "." is normalized in onIp.
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
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
                label = { Text("Pairing code") }, placeholder = { Text("A1B2C3") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(
                    capitalization = KeyboardCapitalization.Characters,
                    keyboardType = KeyboardType.Text,
                ),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            ElevatedButton(
                onClick = onConnectManual,
                enabled = !connecting,
                modifier = Modifier.fillMaxWidth().height(50.dp),
                shape = RoundedCornerShape(16.dp),
            ) {
                Text("Connect & save")
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

/**
 * "Update available" banner, with the in-app Download → Install. Same Card shape
 * and container role as the rest of the screen's rows, one step up in emphasis
 * (secondaryContainer) so it reads as information rather than a problem.
 * Dismissible for the session, because a banner you cannot silence on the app's
 * home screen is a nag.
 */
@Composable
private fun UpdateCard(tag: String, state: UiState, a: SettingsActions) {
    var dismissed by rememberSaveable(tag) { mutableStateOf(false) }
    if (dismissed) return
    Spacer(Modifier.height(16.dp))
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.secondaryContainer),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            modifier = Modifier.padding(start = 16.dp, top = 12.dp, bottom = 4.dp, end = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                Icons.Filled.SystemUpdate, contentDescription = null,
                tint = MaterialTheme.colorScheme.onSecondaryContainer,
                modifier = Modifier.size(22.dp),
            )
            Column(Modifier.weight(1f).padding(start = 12.dp)) {
                Text(
                    "LazeR $tag is available",
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer,
                )
                Text(
                    "Update the laptop app too — they ship together.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer,
                )
            }
            IconButton(onClick = { dismissed = true }) {
                Icon(
                    Icons.Filled.Close, contentDescription = "Dismiss",
                    tint = MaterialTheme.colorScheme.onSecondaryContainer,
                )
            }
        }
        Column(Modifier.padding(start = 16.dp, end = 16.dp, bottom = 12.dp)) {
            UpdateDownloadControls(state, a, MaterialTheme.colorScheme.onSecondaryContainer)
        }
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

/** A laptop found on the network. [saved] = it's one of the saved devices, so a tap
 *  connects; otherwise the only way to pair it is its QR, and the row says so
 *  instead of filling hidden fields for a typed code the laptop would refuse. */
@Composable
private fun HostRow(host: DiscoveredHost, saved: Boolean, enabled: Boolean, onClick: () -> Unit) {
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.secondaryContainer),
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)
            .clickable(enabled = enabled, onClick = onClick),
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
                Text(if (saved) "${host.ip} · saved" else "${host.ip}:${host.port}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalAlignment = Alignment.CenterVertically) {
                if (!saved) {
                    Icon(Icons.Filled.QrCodeScanner, contentDescription = null,
                        tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(18.dp))
                }
                Text(if (saved) "Connect" else "Scan QR", color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.labelLarge)
            }
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
            .clickable(enabled = enabled, onClickLabel = "Connect", onClick = onClick),
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
                Text("${device.ip}:${device.port}" +
                    if (device.key.isBlank()) " · typed code" else "",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            IconButton(onClick = onDelete) {
                Icon(Icons.Filled.Delete, contentDescription = "Remove ${device.name}",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}
