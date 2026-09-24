package com.example.lanremote.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.example.lanremote.UiState
import com.example.lanremote.UpdateDownload
import com.example.lanremote.UpdateStatus
import kotlinx.coroutines.delay

/** What Settings can change. Shared by the control screen and the connect screen,
 *  so the update check can be switched off before pairing. */
class SettingsActions(
    val onSensitivity: (Float) -> Unit,
    val onNaturalScroll: (Boolean) -> Unit,
    val onScrollStripLeft: (Boolean) -> Unit,
    val onHaptics: (Boolean) -> Unit,
    val onAcceleration: (Boolean) -> Unit,
    val onUpdateCheck: (Boolean) -> Unit,
    val onCheckUpdateNow: () -> Unit,   // ask GitHub now, skipping the daily throttle
    val onOpenRelease: () -> Unit,      // the release page in a browser (fallback)
    val onDownloadUpdate: () -> Unit,
    val onCancelDownload: () -> Unit,
    val onInstallUpdate: () -> Unit,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun SettingsSheet(state: UiState, a: SettingsActions, onDismiss: () -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        // Scrolls: sideways, or on a short phone, the sheet is taller than the window,
        // and Updates (where the Settings badge sends people) is at the very bottom.
        Column(
            Modifier
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp)
                .padding(bottom = 28.dp),
        ) {
            SheetTitle("Pointer")
            Text("Cursor speed  ${"%.1f".format(state.settings.sensitivity)}×",
                style = MaterialTheme.typography.bodyMedium)
            Slider(
                value = state.settings.sensitivity,
                onValueChange = a.onSensitivity,
                valueRange = 0.6f..3.0f,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            ToggleRow("Pointer acceleration", "Fast flicks move the cursor farther",
                state.settings.acceleration, a.onAcceleration)

            Spacer(Modifier.height(16.dp))
            SheetTitle("Scrolling")
            ToggleRow("Natural scrolling", "Content follows your fingers",
                state.settings.naturalScroll, a.onNaturalScroll)
            ToggleRow("Scroll bar on the left", "Puts the scroll strip on the left of the " +
                "trackpad — handy for left-handed use",
                state.settings.scrollStripLeft, a.onScrollStripLeft)

            Spacer(Modifier.height(16.dp))
            SheetTitle("Feedback")
            ToggleRow("Haptic feedback", "Vibrate on clicks and scroll",
                state.settings.haptics, a.onHaptics)

            Spacer(Modifier.height(20.dp))
            SheetTitle("Updates")
            ToggleRow(
                "Check for new versions",
                "Asks GitHub once a day whether a newer release exists. Nothing is " +
                    "downloaded until you tap Download & install.",
                state.settings.updateCheck, a.onUpdateCheck,
            )
            // No version, nothing to compare: the check can't run, so offer no button.
            if (state.settings.updateCheck && state.appVersion.isNotBlank()) {
                UpdateStatusLine(state, a)
            }
        }
    }
}

/**
 * The Settings gear, with a small dot when a newer release is out.
 *
 * The app reconnects to the last laptop on launch, so daily users go straight to
 * the pad and never see the connect-screen card. The dot sits on the way to the one
 * place that explains it (Settings → Updates) and goes away once they've updated or switched checks off.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun SettingsIcon(updateAvailable: Boolean) {
    BadgedBox(badge = { if (updateAvailable) Badge() }) {
        Icon(
            Icons.Filled.Settings,
            contentDescription = if (updateAvailable) "Settings, update available" else "Settings",
        )
    }
}

/**
 * Download → Install for a newer release, with the release page as the fallback.
 * Shared by the connect-screen card and Settings → Updates.
 */
@Composable
internal fun UpdateDownloadControls(state: UiState, a: SettingsActions, onColor: androidx.compose.ui.graphics.Color) {
    when (val d = state.download) {
        UpdateDownload.Idle -> Row(verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = a.onDownloadUpdate) { Text("Download & install") }
            TextButton(onClick = a.onOpenRelease) { Text("Release page") }
        }
        is UpdateDownload.Running -> Column {
            if (d.total > 0) {
                LinearProgressIndicator(
                    progress = { (d.bytes.toFloat() / d.total).coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth(),
                )
            } else {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    if (d.bytes == 0L) "Starting download…"
                    else "Downloading… ${mb(d.bytes)}" + if (d.total > 0) " of ${mb(d.total)}" else "",
                    style = MaterialTheme.typography.bodySmall, color = onColor,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = a.onCancelDownload) { Text("Cancel") }
            }
        }
        UpdateDownload.Ready -> Column {
            Text("Downloaded and checked. Android will ask you to confirm the install.",
                style = MaterialTheme.typography.bodySmall, color = onColor)
            Spacer(Modifier.height(8.dp))
            Button(onClick = a.onInstallUpdate) { Text("Install") }
        }
        is UpdateDownload.Failed -> Column {
            Text(d.message, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error)
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (d.canRetry) Button(onClick = a.onDownloadUpdate) { Text("Try again") }
                TextButton(onClick = a.onOpenRelease) { Text("Release page") }
            }
        }
    }
}

private fun mb(bytes: Long) = "%.1f MB".format(bytes / 1_048_576.0)

/**
 * What the update check actually found, under its switch. Four states:
 * available (with Download), checking, couldn't reach GitHub (with Try again), and
 * up to date (with when, and Check now), so a check blocked by the network doesn't
 * look like being current.
 */
@Composable
private fun UpdateStatusLine(state: UiState, a: SettingsActions) {
    val tag = state.updateTag
    val version = "You have ${state.appVersion}"
    if (tag != null) {
        Card(
            shape = RoundedCornerShape(20.dp),
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.secondaryContainer),
            modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
        ) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("LazeR $tag is available",
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer)
                Text(
                    "$version. Update the laptop app too — they ship together.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer,
                )
                Spacer(Modifier.height(8.dp))
                UpdateDownloadControls(state, a, MaterialTheme.colorScheme.onSecondaryContainer)
            }
        }
        return
    }

    // Re-read the clock every half minute so "checked just now" doesn't stay frozen
    // while the sheet is open.
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(30_000)
            now = System.currentTimeMillis()
        }
    }

    val failed = state.updateStatus == UpdateStatus.Failed
    val (line, detail, action) = when {
        state.updateStatus == UpdateStatus.Checking ->
            Triple("Checking…", version, null)
        // Also covers GitHub answering with an error (rate limit, no release), so
        // it doesn't claim the network is at fault.
        failed ->
            Triple(
                "Couldn't check for updates",
                "No usable answer from GitHub. Your network may be blocking it, or " +
                    "GitHub is busy. Nothing is wrong with this phone.",
                "Try again",
            )
        state.lastUpdateCheckMs > 0L ->
            Triple(
                "You're up to date",
                "$version · checked ${checkedAgo(state.lastUpdateCheckMs, now)}",
                "Check now",
            )
        else -> Triple("Not checked yet", version, "Check now")
    }
    Row(
        Modifier.fillMaxWidth().padding(bottom = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(line, style = MaterialTheme.typography.bodyMedium,
                color = if (failed)
                    MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
            Text(detail, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (action != null) {
            TextButton(onClick = a.onCheckUpdateNow) { Text(action) }
        } else {
            CircularProgressIndicator(Modifier.padding(end = 12.dp).size(20.dp), strokeWidth = 2.dp)
        }
    }
}

/**
 * "just now", "5 min ago", "3 hours ago", "yesterday", "12 days ago".
 *
 * Written out rather than DateUtils: the rest of the app's copy is English, and
 * DateUtils switches to an absolute date after a week and to "in 3 hours" when the
 * clock has moved backwards, neither of which reads after "checked". A timestamp in
 * the future (clock corrected backwards) counts as just now.
 */
internal fun checkedAgo(atMs: Long, nowMs: Long): String {
    val min = (nowMs - atMs) / 60_000
    val hours = min / 60
    val days = hours / 24
    return when {
        min < 1 -> "just now"
        min < 60 -> "$min min ago"
        hours == 1L -> "an hour ago"
        hours < 24 -> "$hours hours ago"
        days == 1L -> "yesterday"
        else -> "$days days ago"
    }
}

@Composable
internal fun ToggleRow(title: String, subtitle: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    // The whole row is the switch, so TalkBack reads the title with its state and
    // a tap on the text toggles it too.
    Row(
        Modifier.fillMaxWidth()
            .toggleable(value = checked, role = Role.Switch, onValueChange = onChange)
            .padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked = checked, onCheckedChange = null)
    }
}

@Composable
internal fun SheetTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(bottom = 10.dp))
}
