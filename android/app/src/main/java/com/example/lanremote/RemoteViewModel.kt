package com.example.lanremote

import android.app.Application
import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.lanremote.data.ApkUpdater
import com.example.lanremote.data.Device
import com.example.lanremote.data.DeviceStore
import com.example.lanremote.data.DiscoveredHost
import com.example.lanremote.data.Discovery
import com.example.lanremote.data.DiscoveryStatus
import com.example.lanremote.data.Settings
import com.example.lanremote.data.SettingsStore
import com.example.lanremote.data.UpdateChecker
import com.example.lanremote.data.savedMatch
import com.example.lanremote.net.ConnectOutcome
import com.example.lanremote.net.ConnectResult
import com.example.lanremote.net.PairingError
import com.example.lanremote.net.PairingException
import com.example.lanremote.net.Protocol
import com.example.lanremote.net.RemoteClient
import com.example.lanremote.net.SecureChannel
import com.example.lanremote.net.ipv4Octets
import com.example.lanremote.net.parsePairingUri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.hypot
import kotlin.math.min

enum class ConnState { Disconnected, Connecting, Connected, Reconnecting }

/** Dotted-quad to a 32-bit int, or null if it isn't a plain IPv4 literal. */
private fun ipv4ToInt(s: String): Int? {
    val o = ipv4Octets(s) ?: return null
    return (o[0] shl 24) or (o[1] shl 16) or (o[2] shl 8) or o[3]
}

// Pointer acceleration: smoothed speed at/above which the gain saturates, and the
// max multiplier applied to a fast flick. Slow drags stay near 1× for precision.
private const val ACCEL_REF_PX = 12f
private const val ACCEL_MAX = 2.2f
private const val MOVE_GAP_RESET_MS = 120L   // idle gap ⇒ treat the next move as a fresh gesture
// How long beginReconnect retries at its fast cadence before it (a) explains what's
// probably wrong and (b) drops to RETRY_MAX_MS. It no longer STOPS at this point —
// it used to, and that was the bug behind "I have to reconnect after every sleep":
// the laptop is unreachable for far longer than any bounded window while it's
// asleep, so the phone had always given up by the time it woke. A permanently
// failing handshake (a re-paired laptop, whose new key the stored one can never
// match) is handled by showing the message, not by abandoning the retry.
private const val RECONNECT_EXPLAIN_MS = 90_000L
// Retry cadence once an outage is clearly not a blip. Slow enough to be free on
// battery overnight, brisk enough that a laptop waking up is picked up promptly —
// and the foreground/network hints in kickReconnect short-circuit the wait anyway.
private const val RETRY_MAX_MS = 15_000L
// Automatic update checks: after a failed attempt, wait this long before the next
// one. Taps on Check now / Try again skip it.
private const val RETRY_AFTER_FAILURE_MS = 60 * 60_000L
// Speed-adaptive delta smoothing (one-euro style): near-still input is low-pass
// filtered to kill capacitive jitter; fast flicks pass straight through so the cursor
// never lags. Blend ramps from SMOOTH_FLOOR (slow) to 1.0 (fast).
private const val SMOOTH_FLOOR = 0.45f       // min blend at rest — lower = smoother, more lag
private const val SMOOTH_REF_PX = 7f         // per-event speed at which smoothing fully disengages
// How long a fresh discovery run shows "Looking for laptops…" before an empty list
// reads as "none found". NSD answers within a second or two on a healthy LAN.
private const val DISCOVERY_QUIET_MS = 8_000L
// A reconnect this soon after the phone put the laptop to sleep is the sleep, not
// a network problem, and is described that way.
private const val SLEEP_EXPLAINS_MS = 30 * 60_000L

/** A scanned QR that would replace the pairing of a saved laptop, waiting for the
 *  user to confirm. [existing] is the saved record it replaces. */
data class PendingPairing(val existing: Device, val name: String, val ip: String,
                          val port: Int, val token: String, val key: String)

data class UiState(
    // The typed-code form only. Deliberately NOT the connected device, or "Enter
    // manually" would come pre-filled with a QR-paired laptop's token and "Connect &
    // save" could overwrite its pairing.
    val name: String = "",
    val ip: String = "",
    val port: String = Protocol.DEFAULT_PORT.toString(),
    val token: String = "",
    // The laptop being connected to / driven / reconnected to.
    val deviceName: String = "",
    val conn: ConnState = ConnState.Disconnected,
    val volume: Float = 50f,
    val muted: Boolean? = null,        // null: the laptop doesn't report it
    val brightness: Float = 50f,
    val brightnessAvailable: Boolean = false,
    // NOTE: the keyboard's staging text deliberately does NOT live here. It used to,
    // and that was the bug: this whole object is rewritten on every health-loop tick
    // (volume/brightness sync, every 1.5-4s), which re-fed a plain String into a
    // fully-controlled TextField and discarded whatever the IME was still holding as
    // uncommitted composing text. The field owns its own editing state now — see
    // KeyboardPanel — and the ViewModel keeps only the diff, not the display value.
    val error: String? = null,
    // A QR that couldn't be used. Kept apart from [error] so a bad scan on the
    // Reconnecting screen doesn't overwrite its diagnosis.
    val scanError: String? = null,
    val pendingReplace: PendingPairing? = null,
    val savedDevices: List<Device> = emptyList(),
    val discovered: List<DiscoveredHost> = emptyList(),
    val discoveryStatus: DiscoveryStatus = DiscoveryStatus.Idle,
    // The current run has searched for DISCOVERY_QUIET_MS and found nothing.
    val discoveryQuiet: Boolean = false,
    val settings: Settings = Settings(),
    // Tag of a newer release, or null when we're current / haven't found out / the
    // check is switched off. Drives the connect-screen card and the Settings badge.
    val updateTag: String? = null,
    // How the last update check went, for the Settings sheet only. The connect
    // screen still stays quiet on failure, but Settings must say what happened:
    // a bare switch made "up to date" and "couldn't reach GitHub" look identical,
    // so a user on an old version had no way to tell the check wasn't working.
    // Named "status", not "check", so it can't be confused with the on/off switch
    // in settings.updateCheck.
    val updateStatus: UpdateStatus = UpdateStatus.Idle,
    val lastUpdateCheckMs: Long = 0L,   // last SUCCESSFUL check, epoch millis; 0 = never
    val appVersion: String = "",        // installed versionName; blank if unreadable
    val gestureHintSeen: Boolean = true,
    val download: UpdateDownload = UpdateDownload.Idle,
)

/** Idle covers both "never checked" and "checked fine" — [UiState.lastUpdateCheckMs]
 *  tells those apart. Failed means the most recent attempt got no usable answer. */
enum class UpdateStatus { Idle, Checking, Failed }

/** The in-app download of a newer release. */
sealed interface UpdateDownload {
    data object Idle : UpdateDownload
    /** [total] is -1 when the server didn't say. */
    data class Running(val bytes: Long, val total: Long) : UpdateDownload
    /** Downloaded and verified; Install hands it to Android. */
    data object Ready : UpdateDownload
    /** [canRetry] = false when this release can only be installed from the release
     *  page (it has no published checksum). */
    data class Failed(val message: String, val canRetry: Boolean = true) : UpdateDownload
}

class RemoteViewModel(app: Application) : AndroidViewModel(app) {

    private val client = RemoteClient()
    private val store = DeviceStore(app)
    private val discovery = Discovery(app)
    private val settingsStore = SettingsStore(app)
    private val updater = ApkUpdater(app)
    private var latestRelease: UpdateChecker.Release? = null
    private var downloadJob: Job? = null
    private var verifiedSha: String? = null
    private var downloadGen = 0L          // the newest downloadUpdate(); see its set()
    private var downloadTag: String? = null   // the release the Ready file is
    private var healthJob: Job? = null
    private var reconnectJob: Job? = null
    private var connectJob: Job? = null
    private var discoveryQuietJob: Job? = null
    private var sleepRequestedMs = 0L         // when the phone last sent SYS sleep...
    private var sleptDeviceId: String? = null //   ...and to which laptop
    private var lastUserVolumeMs: Long = 0
    private var lastUserMuteMs: Long = 0
    private var lastUserBrightnessMs: Long = 0
    private var lastInteractionMs: Long = 0   // drives adaptive health-poll backoff
    private var current: Device? = null   // device we're connected to / reconnecting
    // "Try again now" signal into the reconnect loop's wait. CONFLATED because these
    // are hints, not a queue — several arriving at once should cost one extra attempt,
    // not one per sender.
    private val reconnectKick = Channel<Unit>(Channel.CONFLATED)

    // Held for the duration of a live session. Trackpad packets are tiny, frequent UDP
    // datagrams; when the phone's Wi-Fi radio drops into power-save between beacons (how
    // aggressively is negotiated per-AP, so identical setups feel different router to
    // router) those packets get batched and the cursor micro-stutters. A low-latency
    // Wi-Fi lock asks the radio to stay awake in a low-latency mode for as long as it's
    // held — the same knob real-time games use — flattening that variance across networks.
    // Requires android.permission.WAKE_LOCK — NOT CHANGE_WIFI_STATE, the usual wrong
    // guess. Without it acquire() throws a SecurityException that holdWifi swallows, so
    // the lock silently never engages and the cursor lag comes back. Keep WAKE_LOCK in
    // the manifest. Released on disconnect.
    private val wifiLock: WifiManager.WifiLock? by lazy {
        val wm = getApplication<Application>().applicationContext
            .getSystemService(Context.WIFI_SERVICE) as? WifiManager
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        else
            @Suppress("DEPRECATION") WifiManager.WIFI_MODE_FULL_HIGH_PERF
        wm?.createWifiLock(mode, "LazeR:session")?.apply { setReferenceCounted(false) }
    }

    /** Hold (or drop) the low-latency Wi-Fi lock for the session. Idempotent + best-effort. */
    private fun holdWifi(on: Boolean) {
        try {
            val lock = wifiLock ?: return
            if (on) { if (!lock.isHeld) lock.acquire() }
            else if (lock.isHeld) lock.release()
        } catch (_: Exception) {
            // radio state is best-effort — never let it break the session
        }
    }

    private fun touch() { lastInteractionMs = System.currentTimeMillis() }

    private val _state = MutableStateFlow(
        UiState(
            savedDevices = store.load(),
            settings = settingsStore.load(),
            lastUpdateCheckMs = settingsStore.lastUpdateCheckMs,
            appVersion = installedVersion().orEmpty(),
            gestureHintSeen = settingsStore.gestureHintSeen,
        )
    )
    val state: StateFlow<UiState> = _state.asStateFlow()

    // Wakes the retry the moment Wi-Fi is usable again, instead of leaving it to the
    // backoff. The two halves go down together far more often than either goes down
    // alone — the phone roams, the router reboots, the laptop sleeps and the phone
    // drops to mobile data — so "a network just became available" is the single best
    // signal that another attempt is worth making right now.
    private val connectivity: ConnectivityManager? =
        app.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager

    private val netCallback = object : ConnectivityManager.NetworkCallback() {
        // Called directly, not dispatched onto viewModelScope: kickReconnect only
        // reads a StateFlow and calls Channel.trySend, both thread-safe and
        // non-blocking. Hopping to the main thread would just delay the very
        // "try again right now" signal this callback exists to deliver.
        override fun onAvailable(network: Network) = kickReconnect()
    }

    // The Wi-Fi and Ethernet networks themselves, not the default one: behind a VPN
    // the default network is the VPN, which doesn't change when Wi-Fi drops or
    // switches, so watching it left laptops from a network the phone had left on the
    // list. A default NetworkRequest excludes VPNs.
    private val lanCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = kickReconnect()
        override fun onLost(network: Network) {
            viewModelScope.launch { forgetDiscovered() }
        }
    }

    /** A Wi-Fi or Ethernet network went away, so the laptops found on it are gone
     *  from here: stop listing them, and look again if a scan was running. */
    private fun forgetDiscovered() {
        val scanning = _state.value.discoveryStatus != DiscoveryStatus.Idle
        stopDiscovery()   // first, so no result from the old network lands after the clear
        update { it.copy(discovered = emptyList()) }
        if (scanning) startDiscovery()
    }

    // The update check in flight, if any. Declared above init on purpose: init starts
    // the launch check, and an initializer written below it would run afterwards and
    // null the job out.
    private var updateJob: Job? = null
    // When the last automatic attempt started, successful or not. A failure doesn't
    // stamp the daily throttle, so without this a network that blocks GitHub would
    // be asked again every time the app came back to the front.
    private var lastUpdateAttemptMs = 0L

    init {
        startDiscovery()
        try {
            connectivity?.registerDefaultNetworkCallback(netCallback)
            connectivity?.registerNetworkCallback(
                NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .addTransportType(NetworkCapabilities.TRANSPORT_ETHERNET)
                    .build(),
                lanCallback)
        } catch (_: Exception) {
            // Callback registration is best-effort — the timed retry still runs.
        }
        // Silently re-try the last device on launch, if any. A failure that retrying
        // can fix goes into the reconnect loop: Android often kills the app
        // overnight, and the laptop may still be waking when the user opens it.
        val lastId = settingsStore.lastDeviceId
        store.load().firstOrNull { it.id == lastId }?.let { connectDevice(it, save = false, auto = true) }
        checkForUpdate()   // no-op when switched off or inside the throttle window
        // A downloaded APK is only ever known about in memory, so one on disk now is
        // left over from an install or a killed process: about 10 MB of cache.
        viewModelScope.launch(Dispatchers.IO) { updater.clear() }
    }

    /** Restart mDNS discovery — clears the list and looks again for laptops. */
    fun rescan() = startDiscovery()

    private fun startDiscovery() {
        discoveryQuietJob?.cancel()
        update { it.copy(discoveryQuiet = false) }
        discovery.start(
            onChange = { hosts -> update { it.copy(discovered = hosts) } },
            onStatus = { st -> update { it.copy(discoveryStatus = st) } },
        )
        discoveryQuietJob = viewModelScope.launch {
            delay(DISCOVERY_QUIET_MS)
            update { it.copy(discoveryQuiet = true) }
        }
    }

    private fun stopDiscovery() {
        discoveryQuietJob?.cancel()
        discovery.stop()
    }

    // --- settings ---
    fun setSensitivity(v: Float) = updateSettings { it.copy(sensitivity = v) }
    fun setNaturalScroll(v: Boolean) = updateSettings { it.copy(naturalScroll = v) }
    fun setScrollStripLeft(v: Boolean) = updateSettings { it.copy(scrollStripLeft = v) }
    fun setHaptics(v: Boolean) = updateSettings { it.copy(haptics = v) }
    fun setAcceleration(v: Boolean) = updateSettings { it.copy(acceleration = v) }

    /** Turning the check off also clears any banner already on screen — leaving it
     *  up would look like the setting hadn't taken. Turning it on re-checks now
     *  rather than waiting out the throttle, so the toggle gives visible feedback. */
    fun setUpdateCheck(v: Boolean) {
        updateSettings { it.copy(updateCheck = v) }
        if (v) {
            checkForUpdate(force = true)
        } else {
            // Stop a request that's still out, so its answer can't put a badge back
            // up after the user switched checks off.
            updateJob?.cancel()
            cancelDownload()
            update { it.copy(updateTag = null, updateStatus = UpdateStatus.Idle) }
        }
    }

    /** Settings' "Check now" / "Try again": ask GitHub immediately, skipping the
     *  once-a-day throttle. Only reachable by a tap, so it can't hammer the API. */
    fun checkForUpdatesNow() = checkForUpdate(force = true)

    /** App came to the front. Launch alone isn't enough: a phone that keeps LazeR in
     *  memory for days would never check again. The daily throttle still applies. */
    fun onForeground() = checkForUpdate()

    /** The app's own versionName, read from the installed package.
     *
     *  Deliberately not BuildConfig.VERSION_NAME: that needs `buildFeatures {
     *  buildConfig = true }`, and build.gradle.kts is version-locked around the M3
     *  Expressive alphas (see CLAUDE.md), so this avoids touching it at all. */
    private fun installedVersion(): String? = try {
        val ctx = getApplication<Application>()
        ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName
    } catch (e: Exception) {
        null
    }

    /**
     * Check GitHub for a newer release, if enabled and the throttle allows.
     *
     * Shows the cached answer first so a known update appears immediately on launch
     * instead of only after a network round trip. A failure never interrupts: it
     * only shows as the status line in Settings → Updates.
     */
    private fun checkForUpdate(force: Boolean = false) {
        if (!_state.value.settings.updateCheck) return
        // Unreadable version: nothing to compare against. Settings hides the status
        // line in this case, so there's no Check now button left doing nothing.
        val mine = _state.value.appVersion.ifBlank { return }
        // Cached result: instant, offline-safe, and re-validated below if due.
        settingsStore.lastKnownTag?.let { cached ->
            if (UpdateChecker.isNewer(cached, mine)) update { it.copy(updateTag = cached) }
        }
        val now = System.currentTimeMillis()
        if (!force && !settingsStore.updateCheckDue(now)) return
        if (!force && now - lastUpdateAttemptMs in 0 until RETRY_AFTER_FAILURE_MS) return
        if (updateJob?.isActive == true) return   // one request at a time
        lastUpdateAttemptMs = now
        update { it.copy(updateStatus = UpdateStatus.Checking) }
        updateJob = viewModelScope.launch {
            // Cancelled (checks switched off) → latestRelease's withContext throws on
            // return, so nothing below runs.
            val release = UpdateChecker.latestRelease()
            val tag = release?.tag
            if (release == null || tag == null) {
                // The throttle stamp is left alone, so the next launch tries again.
                update { it.copy(updateStatus = UpdateStatus.Failed) }
                return@launch
            }
            latestRelease = release
            // A downloaded file is for the tag it was fetched for. Once the notice
            // names another (a newer release, or none because this one is now
            // installed), drop it rather than install the wrong version under it.
            val shown = if (UpdateChecker.isNewer(tag, mine)) tag else null
            if (downloadTag != null && downloadTag != shown) cancelDownload()
            val at = System.currentTimeMillis()
            settingsStore.lastUpdateCheckMs = at
            settingsStore.lastKnownTag = tag
            // Recompute rather than trusting the cache: this also CLEARS the banner
            // once the user has actually updated, which is the only way it goes away.
            update {
                it.copy(
                    updateTag = shown,
                    updateStatus = UpdateStatus.Idle,
                    lastUpdateCheckMs = at,
                )
            }
        }
    }

    /** The release page, for a browser: the fallback when the app can't install. */
    fun releasesUrl(): String = UpdateChecker.RELEASES_PAGE

    /**
     * Download the newer release's APK and verify it against the checksum published
     * next to it. Only ever on a tap, never automatically: the update CHECK is
     * automatic, the download is the user's call.
     */
    fun downloadUpdate() {
        if (downloadJob?.isActive == true) return
        // Every state write below checks this is still the newest download: a
        // cancelled one can't be interrupted mid-read, so its late progress or
        // result would overwrite whatever came after it.
        val gen = ++downloadGen
        fun set(d: UpdateDownload) {
            if (gen == downloadGen) update { it.copy(download = d) }
        }
        set(UpdateDownload.Running(0, -1))
        downloadJob = viewModelScope.launch {
            val release = latestRelease?.takeIf { it.tag == _state.value.updateTag }
                ?: UpdateChecker.latestRelease()?.also { latestRelease = it }
            if (release == null) {
                set(UpdateDownload.Failed(
                    "Couldn't reach GitHub. Check the connection and try again."))
                return@launch
            }
            // Fetched afresh when the notice came from the cache: make sure it is
            // still newer than what's installed before downloading it.
            if (!UpdateChecker.isNewer(release.tag, _state.value.appVersion)) {
                set(UpdateDownload.Failed("This phone already has the newest LazeR.",
                    canRetry = false))
                return@launch
            }
            val apk = release.apkUrl
            val shaUrl = release.shaUrl
            if (apk == null || shaUrl == null) {
                set(UpdateDownload.Failed(
                    "This release can't be installed from inside the app. Open the " +
                        "release page to download it.", canRetry = false))
                return@launch
            }
            val sha = updater.fetchSha256(shaUrl)
            if (sha == null) {
                set(UpdateDownload.Failed(
                    "Couldn't read the release's checksum. Try again, or open the " +
                        "release page."))
                return@launch
            }
            val result = updater.download(apk, sha) { bytes, total ->
                set(UpdateDownload.Running(bytes, total))
            }
            if (gen != downloadGen) return@launch
            when (result) {
                ApkUpdater.Download.Ok -> {
                    verifiedSha = sha
                    downloadTag = release.tag
                    set(UpdateDownload.Ready)
                }
                is ApkUpdater.Download.Failed -> set(
                    if (result.reason == "cancelled") UpdateDownload.Idle
                    else UpdateDownload.Failed(downloadFailureMessage(result.reason)))
            }
        }
    }

    fun cancelDownload() {
        downloadGen++
        downloadTag = null
        verifiedSha = null
        updater.cancel()
        downloadJob?.cancel()
        updater.clear()
        update { it.copy(download = UpdateDownload.Idle) }
    }

    /** What the activity should launch to install: Android's installer, or first
     *  the "allow installs from LazeR" screen. Null means there's nothing to do
     *  (the state already says why). */
    suspend fun installIntent(): android.content.Intent? {
        val sha = verifiedSha ?: return null
        return when (val r = updater.installIntent(sha)) {
            is ApkUpdater.Install.Launch -> r.intent
            is ApkUpdater.Install.NeedsPermission -> r.intent
            is ApkUpdater.Install.Failed -> {
                verifiedSha = null
                update { it.copy(download = UpdateDownload.Failed(
                    "The downloaded file changed or went missing. Download it again.")) }
                null
            }
        }
    }

    private fun downloadFailureMessage(reason: String): String = when (reason) {
        "digest", "short" ->
            "The download didn't match the release's checksum, so it was thrown away. " +
                "Try again."
        "too-big" -> "The download was larger than any LazeR release, so it was stopped."
        "slow" -> "The download was too slow and was stopped. Try again on a better connection."
        "network" -> "The download was interrupted. Check the connection and try again."
        else -> "Couldn't download the update ($reason). Try again, or open the release page."
    }

    private inline fun updateSettings(block: (Settings) -> Settings) {
        val s = block(_state.value.settings)
        settingsStore.save(s)
        update { it.copy(settings = s) }
    }

    fun dismissGestureHint() {
        settingsStore.gestureHintSeen = true
        update { it.copy(gestureHintSeen = true) }
    }

    // --- form fields ---
    fun onName(v: String) = update { it.copy(name = v, error = null) }

    /**
     * Normalize the IP field instead of trusting the IME.
     *
     * The field uses a decimal keyboard so a separator key is always available
     * (a plain number pad on many IMEs has no "." at all, which made manual
     * entry impossible). Decimal separators are LOCALE-dependent — plenty of
     * keyboards render a comma — and pasted values carry stray spaces, so both
     * are normalized here rather than letting a valid-looking-but-unparseable
     * string reach connect().
     *
     * Letters and hyphens survive on purpose: connect() resolves through
     * InetAddress.getByName, so a hostname ("laptop", "laptop.local") is a legal
     * value here, and a digits-only filter silently shredded a pasted one into
     * something unresolvable.
     */
    fun onIp(v: String) = update {
        it.copy(ip = v.replace(',', '.')
            .filter { c -> c.isLetterOrDigit() || c == '.' || c == '-' },
            error = null)
    }
    fun onPort(v: String) = update { it.copy(port = v.filter { c -> c.isDigit() }, error = null) }
    fun onToken(v: String) = update { it.copy(token = v.trim().uppercase(), error = null) }

    // --- connect entry points ---
    fun connectManual() {
        // Typed code has no key ⇒ legacy plaintext, which the laptop refuses unless
        // it allows it. The saved record keeps any key it already had (mergeDevice).
        val s = _state.value
        val ip = s.ip.trim()
        if (ip.isBlank() || s.token.isBlank()) {
            update { it.copy(error = "Enter the laptop's IP and its pairing code.") }
            return
        }
        val port = s.port.toIntOrNull() ?: Protocol.DEFAULT_PORT
        connectDevice(newDevice(s.name, ip, port, s.token, ""), save = true)
    }

    fun connectSaved(device: Device) = connectDevice(device, save = false)

    /** Parse a scanned `lazer://ip:port/?token=..&name=..&k=..` URI and connect. */
    fun applyScannedUri(raw: String) {
        val p = parsePairingUri(raw).getOrElse { e ->
            val msg = when ((e as? PairingException)?.error) {
                PairingError.NoToken -> "That QR code has no pairing code in it."
                PairingError.BadHost ->
                    "That QR code doesn't name a laptop's address, so LazeR won't use it."
                else -> "Unrecognized QR code — scan the one in the LazeR window on the laptop."
            }
            update { it.copy(scanError = msg) }
            return
        }
        // A key that is present but unreadable must not fall back to the plaintext
        // wire: that would put the pairing code on the air in the clear.
        if (p.key.isNotBlank() && SecureChannel.keyFromBase64(p.key) == null) {
            update { it.copy(scanError = "Unrecognized QR code — its encryption key is damaged.") }
            return
        }
        update { it.copy(scanError = null) }
        val dev = newDevice(p.name, p.host, p.port, p.token, p.key)
        val existing = savedMatch(_state.value.savedDevices, dev)
        if (existing != null && existing.key.isNotBlank() && existing.key != p.key) {
            // Someone could have planted this QR; don't silently swap a saved
            // laptop's pairing for it.
            update {
                it.copy(pendingReplace = PendingPairing(existing, p.name, p.host, p.port,
                    p.token, p.key))
            }
            return
        }
        connectDevice(dev, save = true, auto = scannedWhileReconnecting(), scanned = true)
    }

    fun confirmReplace() {
        val p = _state.value.pendingReplace ?: return
        update { it.copy(pendingReplace = null) }
        connectDevice(Device(id = p.existing.id, name = p.name, ip = p.ip, port = p.port,
            token = p.token, key = p.key), save = true, auto = scannedWhileReconnecting(),
            scanned = true)
    }

    /** A QR scanned from the Reconnecting screen must not turn the retry-forever
     *  loop into one attempt: if the laptop is still asleep, keep retrying. */
    private fun scannedWhileReconnecting() = _state.value.conn == ConnState.Reconnecting

    fun cancelReplace() = update { it.copy(pendingReplace = null) }

    /** Stop a connect in progress (the Cancel button while connecting). */
    fun cancelConnect() {
        // A tap on a Cancel button still on screen after the connect landed.
        if (_state.value.conn != ConnState.Connecting) return
        connectJob?.cancel()
        // disconnect, not just cancelConnect: a handshake that finished on the IO
        // thread just before the cancel has already installed its session.
        client.disconnect()
        current = null
        // The Reconnecting screen may have been holding the radio awake, and its
        // long tail stops discovery between attempts.
        holdWifi(false)
        update { it.copy(conn = ConnState.Disconnected) }
        startDiscovery()
    }

    private fun newDevice(name: String, ip: String, port: Int, token: String, key: String) =
        Device(id = "$ip:$port", name = name.ifBlank { ip }, ip = ip, port = port,
            token = token, key = key)

    private fun connectDevice(
        dev0: Device, save: Boolean, auto: Boolean = false, scanned: Boolean = false,
    ) {
        // Keep the saved record's id and bound flag, so lastDeviceId and the
        // downgrade guard follow the laptop rather than its address. A scan drops
        // the flag: see mergeDevice's rescanned.
        val existing = savedMatch(_state.value.savedDevices, dev0)
        val dev = if (existing != null) {
            // A typed code for a laptop already paired by QR, with the same code:
            // use the saved key rather than dropping to the plaintext wire.
            val key = if (dev0.key.isBlank() && existing.token == dev0.token) existing.key
            else dev0.key
            dev0.copy(id = existing.id, key = key,
                bound = !scanned && existing.bound && existing.key == key)
        } else dev0
        // Another laptop's mute state isn't this one's; the first VGET fills it in.
        val keepMute = current?.id == dev.id
        current = dev
        reconnectJob?.cancel()
        // Cancel the watchdog too: a health loop left alive could declare the link
        // dead mid-connect and drag us into beginReconnect on top of the attempt.
        healthJob?.cancel()
        connectJob?.cancel()
        // Clear an old scan error too: it outranks the connect's own diagnosis on the
        // connect screen, so a stale "Unrecognized QR code" would hide it.
        update { it.copy(conn = ConnState.Connecting, error = null, scanError = null,
            deviceName = dev.name, muted = if (keepMute) it.muted else null) }
        connectJob = viewModelScope.launch {
            val (connected, outcome) = connectResolving(dev, 2000)
            if (outcome.result == ConnectResult.Cancelled) return@launch
            if (connected != null) {
                onConnected(dev, connected, persist = save, rescanned = scanned)
            } else if (auto && outcome.result == ConnectResult.NoAnswer) {
                // A scan handed to the retry loop is still a scan: save it when it lands.
                beginReconnect(persist = save, rescanned = scanned)
            } else {
                // The Reconnecting screen may have been holding the radio awake.
                holdWifi(false)
                update {
                    it.copy(conn = ConnState.Disconnected,
                        error = connectFailureMessage(dev, outcome.result))
                }
            }
        }
    }

    /** A connect or reconnect succeeded: remember what changed and go live. */
    private fun onConnected(
        asked: Device, connected0: Device, persist: Boolean, rescanned: Boolean = false,
    ) {
        var connected = connected0
        // Persist when explicitly saving, when the stored address was stale and we
        // reached the laptop at a new IP via mDNS (so the next tap dials the right
        // place), or when it has just shown it binds its handshake.
        val moved = connected.ip != asked.ip || connected.port != asked.port
        val learned = connected.bound && !asked.bound
        if (persist || moved || learned) {
            // Outside update{}: its lambda re-runs on a lost race, and this is a
            // Keystore round trip and a prefs write.
            val list = store.upsert(connected, rescanned)
            update { it.copy(savedDevices = list) }
            // The merge may have folded this into a record with another id; follow
            // it, or lastDeviceId points at nothing and the next launch can't find it.
            savedMatch(list, connected)?.let { connected = connected.copy(id = it.id) }
        }
        current = connected
        settingsStore.lastDeviceId = connected.id
        if (connected.id == sleptDeviceId) sleptDeviceId = null   // it's awake again
        stopDiscovery()    // no need to keep scanning Wi-Fi while controlling
        holdWifi(true)     // pin the radio low-latency for the session
        touch()
        update { it.copy(conn = ConnState.Connected, error = null, deviceName = connected.name) }
        startHealthLoop()
    }

    /**
     * Explain a failed connection instead of listing every possible cause.
     *
     * "Couldn't reach X — check it's on, same Wi-Fi, token correct" covered four
     * unrelated failures at once, and the most common one is invisible from the
     * phone: a router whose 2.4 GHz and 5 GHz SSIDs are separate networks, so the
     * phone and the laptop never share a subnet. We already know our own addresses,
     * so we can tell that apart from "same network, nothing answered".
     */
    private fun connectFailureMessage(dev: Device, result: ConnectResult): String {
        when (result) {
            ConnectResult.NeedsQr -> return "${dev.name} only accepts QR pairing, because " +
                "it requires encryption. Scan the QR code in the LazeR window on the laptop."
            ConnectResult.WrongKey -> return "${dev.name} has been re-paired since this " +
                "phone last connected. Scan the new QR code in the LazeR window."
            ConnectResult.Outdated -> return "${dev.name} answered in an older format. " +
                "Update LazeR on the laptop, or scan its QR code to keep using that version."
            else -> {}
        }
        val mine = localIPv4s()
        val tail = if (dev.key.isBlank()) " A typed code only works while the laptop " +
            "allows unencrypted pairing — scanning its QR is more reliable."
        else " If the laptop was re-paired, scan its new QR code."
        // The phone's own network first: a Sleep explains silence, not a phone that
        // has left the laptop's Wi-Fi. Only an IPv4 literal can be subnet-checked.
        return when {
            mine.isEmpty() ->
                "This phone has no Wi-Fi address — join the laptop's network and retry."
            ipv4ToInt(dev.ip) != null && mine.none { it.sharesSubnetWith(dev.ip) } ->
                "Your phone is on ${mine.first().address} but ${dev.ip} is on a " +
                    "different network, so they can't reach each other. Put both on the " +
                    "same Wi-Fi — note that a router's 2.4 GHz and 5 GHz names are " +
                    "sometimes separate networks, and guest networks always are."
            sleptRecently(dev) ->
                "${dev.name} is asleep. Wake it and LazeR reconnects by itself."
            else ->
                "${dev.ip}${if (ipv4ToInt(dev.ip) != null) " is on your network but" else ""}" +
                    " didn't answer. Check LazeR is running " +
                    "on the laptop and that you allowed its firewall prompt — the LazeR " +
                    "window warns when inbound UDP is blocked. Some routers also block " +
                    "device-to-device traffic (\"client isolation\")." + tail
        }
    }

    private fun sleptRecently(dev: Device) = dev.id == sleptDeviceId &&
        System.currentTimeMillis() - sleepRequestedMs < SLEEP_EXPLAINS_MS

    private data class LocalV4(val address: String, val prefix: Int) {
        /** True if [target] falls inside this interface's subnet. Uses the interface's
         *  REAL prefix length rather than assuming /24, so a /16 or /22 LAN — common
         *  on larger home mesh setups and offices — isn't misreported as "different
         *  network". */
        fun sharesSubnetWith(target: String): Boolean {
            val a = ipv4ToInt(address) ?: return false
            val b = ipv4ToInt(target) ?: return false
            if (prefix !in 1..32) return false
            val mask = if (prefix == 32) -1 else (-1 shl (32 - prefix))
            return (a and mask) == (b and mask)
        }
    }

    /** True if [ip] is an address a laptop could plausibly be reachable at from here.
     *  Rejects loopback and link-local; anything we can't parse is left alone so a
     *  hostname or IPv6 literal still gets its chance. */
    private fun usableHost(ip: String): Boolean {
        val v4 = ipv4ToInt(ip) ?: return true
        val a = (v4 ushr 24) and 0xFF
        val b = (v4 ushr 16) and 0xFF
        return a != 127 && a != 0 && !(a == 169 && b == 254)
    }

    /** This phone's IPv4 addresses on Wi-Fi or Ethernet, whatever their range (a
     *  campus LAN can be public or CGNAT space). Chosen by the network's transport,
     *  not the interface name or address range: mobile data and VPNs aren't a LAN
     *  the laptop is on, even when they hand out 10.x addresses. */
    @Suppress("DEPRECATION")   // allNetworks: its replacement needs a callback
    private fun localIPv4s(): List<LocalV4> = try {
        val cm = connectivity ?: return emptyList()
        cm.allNetworks.asSequence()
            .filter { n ->
                val c = cm.getNetworkCapabilities(n) ?: return@filter false
                !c.hasTransport(NetworkCapabilities.TRANSPORT_VPN) &&
                    (c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                        c.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))
            }
            .flatMap { n -> cm.getLinkProperties(n)?.linkAddresses.orEmpty().asSequence() }
            .filter { la ->
                val a = la.address
                a is java.net.Inet4Address && !a.isLoopbackAddress && !a.isLinkLocalAddress
            }
            .mapNotNull { la -> la.address.hostAddress?.let { LocalV4(it, la.prefixLength) } }
            .toList()
    } catch (e: Exception) {
        emptyList()
    }

    /**
     * Connect to [dev]'s stored address; if that fails, try every laptop currently
     * visible via mDNS (its IP may have moved on a DHCP lease / reboot).
     *
     * Only for a QR-paired device: a wrong host can't complete the keyed handshake,
     * so trying strangers costs nothing. A typed-code device sends its token in the
     * clear and accepts a plain OK from anyone, so it never tries a discovered host —
     * anything on the LAN advertising `_lazer._udp` would otherwise be handed the
     * token and then every keystroke.
     *
     * @return the device that answered (its id preserved, ip/port refreshed, bound
     *   flag updated) or null, with the outcome that decides the message: the saved
     *   address's own refusal wins over the discovered hosts' silence, and the same
     *   laptop's refusal at a new address wins over the saved address's silence.
     */
    private suspend fun connectResolving(dev: Device, timeoutMs: Long): Pair<Device?, ConnectOutcome> {
        // The QR and the store only hold keys that decoded; a record that no longer
        // does fails closed instead of going out on the plaintext wire.
        val raw = if (dev.key.isBlank()) null
        else SecureChannel.keyFromBase64(dev.key)
            ?: return null to ConnectOutcome(ConnectResult.WrongKey)
        fun done(o: ConnectOutcome, ip: String, port: Int) =
            dev.copy(ip = ip, port = port, bound = dev.bound || o.bound) to o
        val first = client.connect(dev.ip, dev.port, dev.token, raw, timeoutMs,
            requireBound = dev.bound)
        if (first.result == ConnectResult.Connected) return done(first, dev.ip, dev.port)
        if (first.result == ConnectResult.Cancelled || raw == null) return null to first
        val candidates = _state.value.discovered
            .filterNot { it.ip == dev.ip && it.port == dev.port }
            // Drop addresses that can't be a laptop on this LAN. A server that
            // re-announced itself while its Wi-Fi was still coming up used to publish
            // the 127.0.0.1 fallback, and dialing that means dialing this phone —
            // which burns a full handshake timeout per attempt for an address that
            // can never answer. Cheap to skip, and it also filters link-local junk.
            .filter { usableHost(it.ip) }
        var outcome = first
        for (h in candidates) {
            val o = client.connect(h.ip, h.port, dev.token, raw, timeoutMs,
                requireBound = dev.bound)
            if (o.result == ConnectResult.Connected) return done(o, h.ip, h.port)
            if (o.result == ConnectResult.Cancelled) return null to o
            // The saved address was silent, but this laptop, announced under its own
            // name at a new address, refused (re-paired, say): that refusal is the
            // diagnosis. Another name's refusal is a stranger's laptop and says
            // nothing about this one.
            if (outcome.result == ConnectResult.NoAnswer &&
                o.result != ConnectResult.NoAnswer && sameLaptop(h.name, dev.name)) outcome = o
        }
        return null to outcome
    }

    /** [mdnsName] is the laptop's hostname as the server announces it: only letters,
     *  digits, `-` and `_` survive (`start_mdns` in remote_server.py). */
    private fun sameLaptop(mdnsName: String, qrName: String): Boolean {
        val safe = qrName.filter { it.isLetterOrDigit() || it == '-' || it == '_' }
        return safe.isNotEmpty() && safe.equals(mdnsName, ignoreCase = true)
    }

    /** Watchdog: poll volume (doubles as liveness); on repeated misses, reconnect. */
    private fun startHealthLoop() {
        healthJob?.cancel()
        healthJob = viewModelScope.launch {
            var misses = 0
            var tick = 0
            // A VGET that timed out can still be answered late, and the next VGET
            // would read that reply: the laptop's state from before a tap made since.
            // Each lost VGET yields at most one such reply, so skip one reading.
            var skipVolReading = false
            while (isActive) {
                val volTimeout = 400
                val pingTimeout = 500
                // Ride out brief transients (a Wi-Fi airtime blip or a momentary
                // server-loop stall) on the SAME socket instead of thrashing a healthy
                // session into a reconnect. A reconnect is expensive — new socket, new
                // port, a visible "stopped then came back" glitch — so only declare the
                // link dead after several consecutive misses. Paired with the fast
                // re-probe below, dead detection is still ~2.5s while a 1–2s blip is
                // absorbed with no drop at all.
                val maxMisses = 5
                val v = client.queryVolume(volTimeout)
                val alive = if (v != null) {
                    val now = System.currentTimeMillis()
                    val use = !skipVolReading
                    skipVolReading = false
                    if (use && now - lastUserVolumeMs > 1200) {
                        update { it.copy(volume = v.level.toFloat()) }
                    }
                    // Same guard for mute, so a poll answered just before a tap
                    // reached the laptop doesn't flip the button back.
                    if (use && now - lastUserMuteMs > 1200) update { it.copy(muted = v.muted) }
                    true
                } else {
                    skipVolReading = true
                    client.ping(pingTimeout)   // confirm before declaring it dead
                }
                if (alive) {
                    misses = 0
                    // Brightness changes rarely and the read is costly on the laptop, so
                    // once the control is showing we sync it every ~4th tick. But UNTIL
                    // it's showing (fresh launch / after process death resets the flag)
                    // probe EVERY tick: BGET is lossy and a single dropped reply must not
                    // leave the slider hidden for many seconds. A laptop with no backend
                    // never answers, so it stays hidden. NO latch off — any reply reveals
                    // it. The availability flag is independent of the value-sync guard
                    // below (a recent user drag must not suppress *showing* the control).
                    val probe = !_state.value.brightnessAvailable || tick % 4 == 0
                    if (probe) {
                        val b = client.queryBrightness()
                        if (b != null) {
                            val syncVal = System.currentTimeMillis() - lastUserBrightnessMs > 1200
                            update {
                                it.copy(
                                    brightnessAvailable = true,
                                    brightness = if (syncVal) b.toFloat() else it.brightness,
                                )
                            }
                        }
                    }
                } else if (++misses >= maxMisses) {
                    beginReconnect()
                    return@launch
                }
                tick++
                // Adaptive backoff: poll briskly (1.5s) while you're actively using the
                // trackpad/sliders for responsive volume sync + fast disconnect notice;
                // ease off to 4s when idle to spare the radio + battery. Worst-case
                // disconnect detection when idle ≈ 8–12s, still fine. But once a miss is
                // seen, re-probe FAST (0.5s) so a transient is confirmed-recovered (misses
                // back to 0) or confirmed-dead within ~2.5s — recovery feels instant, not
                // "came back after a while".
                val active = System.currentTimeMillis() - lastInteractionMs < 5000
                delay(if (misses > 0) 500L else if (active) 1500L else Protocol.IDLE_POLL_MS)
            }
        }
    }

    /** Keep retrying the current device until it answers or the user backs out.
     *
     *  mDNS runs during reconnect so a laptop that came back on a new IP can still be
     *  found and re-pinned (the saved address is refreshed on success).
     *
     *  This used to STOP after 90s and drop to the connect screen. The reasoning was
     *  sound — a re-paired laptop can never complete the handshake, so an endless
     *  spinner with no explanation is worse than an error — but the window is far
     *  shorter than the outage it has to survive. A laptop asleep over lunch,
     *  overnight, or through a reboot is unreachable for far longer than 90s, so the
     *  phone had always given up by the time it came back, and every single sleep
     *  ended in a manual reconnect.
     *
     *  So the retry no longer ends: it slows down (2s while it's plausibly a blip, out
     *  to RETRY_MAX_MS once the outage is clearly long) and surfaces the actionable
     *  message on the reconnect screen instead of navigating away. The user keeps the
     *  Cancel button either way, which is the deliberate exit; what's gone is the
     *  automatic one that fired exactly when the laptop was still asleep. */
    private fun beginReconnect(persist: Boolean = false, rescanned: Boolean = false) {
        healthJob?.cancel()
        val dev = current ?: return disconnect()
        // Straight after a Sleep the phone sent, say so now: that IS the reason, and
        // waiting 90 s to blame the network would be wrong.
        update {
            it.copy(conn = ConnState.Reconnecting, deviceName = dev.name, scanError = null,
                error = if (sleptRecently(dev)) connectFailureMessage(dev, ConnectResult.NoAnswer)
                else null)
        }
        reconnectJob?.cancel()
        startDiscovery()
        reconnectJob = viewModelScope.launch {
            val startMs = System.currentTimeMillis()
            var explained = false
            var lastDefinite: ConnectResult? = null
            while (isActive) {
                val (c, outcome) = connectResolving(dev, 1200)
                if (c != null) {
                    onConnected(dev, c, persist, rescanned)
                    return@launch
                }
                // The attempt is over; don't leave the scan running through the
                // wait. In the long tail it is restarted just before the next one.
                if (explained) stopDiscovery()
                val elapsed = System.currentTimeMillis() - startMs
                val definite = outcome.result == ConnectResult.WrongKey ||
                    outcome.result == ConnectResult.NeedsQr ||
                    outcome.result == ConnectResult.Outdated
                if (elapsed > RECONNECT_EXPLAIN_MS && !explained) {
                    // Long enough that this isn't a blip: say what's likely wrong and
                    // what would fix it — while STILL retrying underneath, so a laptop
                    // that wakes up an hour later reconnects with nothing to tap.
                    explained = true
                    // Stop pinning the radio awake once we're in the long tail —
                    // holding a low-latency Wi-Fi lock through a multi-hour outage
                    // costs real battery for a link that isn't there. Stop the mDNS
                    // scan for the same reason: leaving it running for hours would
                    // undo exactly the saving the line above is making. It gets
                    // restarted around each attempt below, so a laptop that comes
                    // back at a new address is still found.
                    holdWifi(false)
                    stopDiscovery()
                }
                if (definite) lastDefinite = outcome.result
                if (lastDefinite != null || explained) {
                    // A definite result is the laptop saying why; retrying can still
                    // succeed (someone flips its setting back). It stands until another
                    // one replaces it: a later attempt whose reply was lost says
                    // nothing new, and letting it win made the banner flicker.
                    // Otherwise re-derived every attempt, not once: over a long outage
                    // the cause changes — the Sleep the phone sent stops explaining it,
                    // the phone changes network — and a message set once would say
                    // "asleep" all night.
                    val msg = connectFailureMessage(dev, lastDefinite ?: outcome.result)
                    if (_state.value.error != msg) update { it.copy(error = msg) }
                }
                // Fast while it's likely transient, then ease off. Same shape as the
                // health loop's backoff, and it keeps a long outage from polling the
                // radio every 2s all night. A kick (app foregrounded, network back)
                // cuts the wait short so the retry lands at the moment it's most
                // likely to succeed instead of up to RETRY_MAX_MS later.
                val wait = if (elapsed > RECONNECT_EXPLAIN_MS) RETRY_MAX_MS else 2000L
                withTimeoutOrNull(wait) { reconnectKick.receive() }
                if (explained) {
                    // Scan only around the attempt itself. Results arrive
                    // asynchronously, so this attempt uses what the previous
                    // window found and the next one benefits from this window —
                    // which converges within a couple of retries while leaving the
                    // radio alone for the ~15s in between.
                    startDiscovery()
                }
            }
        }
    }

    /** The app came back to the foreground, or the phone rejoined a network.
     *
     *  Either is a strong hint that the thing we've been failing to reach may be
     *  reachable NOW, and waiting out the backoff wastes the one moment the user is
     *  actually looking at the screen. Nudges the running retry to try again
     *  immediately — deliberately NOT a restart of the loop, which would reset the
     *  elapsed clock and so keep re-arming the Wi-Fi lock and re-suppressing the
     *  diagnosis every time the app was reopened. A no-op unless we're reconnecting. */
    fun kickReconnect() {
        if (_state.value.conn != ConnState.Reconnecting) return
        reconnectKick.trySend(Unit)
    }

    fun deleteDevice(device: Device) {
        if (settingsStore.lastDeviceId == device.id) settingsStore.lastDeviceId = null
        update { it.copy(savedDevices = store.delete(device.id)) }
    }

    /** The QR scanner itself failed (no Play services, camera refused, ...). */
    fun reportScanError(msg: String) = update { it.copy(scanError = msg) }

    /** Back on the Reconnecting screen: stop retrying, but — unlike Cancel or
     *  Disconnect — still reconnect to this laptop on the next launch. Back is also
     *  how people leave an app, and that shouldn't forget the laptop. */
    fun stopReconnecting() = disconnect(forget = false)

    fun disconnect() = disconnect(forget = true)

    private fun disconnect(forget: Boolean) {
        healthJob?.cancel()
        reconnectJob?.cancel()
        connectJob?.cancel()
        // An intentional leave: don't auto-reconnect next launch.
        if (forget) settingsStore.lastDeviceId = null
        current = null
        client.disconnect()
        holdWifi(false)                     // let the radio power-save again
        // Clear the error too. The reconnect loop now writes its diagnosis into
        // that slot while STAYING on the reconnect screen, so leaving it set here
        // would drop the user onto the device list with a red banner about a
        // device they deliberately left — and it would survive a rescan.
        update { it.copy(conn = ConnState.Disconnected, error = null) }
        startDiscovery()   // scan again for the connection screen
    }

    // --- pointer ---
    // Per-gesture smoothing + sub-pixel carry so high report-rate screens (e.g.
    // 120 Hz) don't make the cursor jitter: raw per-event speed swings frame to
    // frame, and truncating the scaled delta each event drops fractional movement.
    private var accX = 0f
    private var accY = 0f
    private var emaSpeed = 0f
    private var lastMoveMs = 0L
    private var smX = 0f          // smoothed dx
    private var smY = 0f          // smoothed dy
    private var smInit = true     // seed the filter on the first move of a gesture

    fun move(dx: Float, dy: Float) {
        if (dx == 0f && dy == 0f) return
        val s = _state.value.settings
        // A fresh gesture after an idle gap: clear stale speed + remainder so the
        // first move isn't flung by the previous flick's momentum.
        val now = System.currentTimeMillis()
        if (now - lastMoveMs > MOVE_GAP_RESET_MS) { emaSpeed = 0f; accX = 0f; accY = 0f; smInit = true }
        lastMoveMs = now
        lastInteractionMs = now

        // Adaptive low-pass on the raw delta: heavy at low speed (removes jitter), off
        // at speed (no lag). Seeded on the gesture's first move to avoid an undershoot.
        val speed = hypot(dx.toDouble(), dy.toDouble()).toFloat()
        if (smInit) {
            smX = dx; smY = dy; smInit = false
        } else {
            val a = SMOOTH_FLOOR + (1f - SMOOTH_FLOOR) * min(speed / SMOOTH_REF_PX, 1f)
            smX += a * (dx - smX)
            smY += a * (dy - smY)
        }
        val fx = smX
        val fy = smY

        var mx = fx * s.sensitivity
        var my = fy * s.sensitivity
        if (s.acceleration) {
            // EMA-smooth the per-event speed so the gain ramps instead of jittering;
            // slow drags stay ~1:1 for precision, fast flicks reach ACCEL_MAX.
            val inst = hypot(fx.toDouble(), fy.toDouble()).toFloat()
            emaSpeed = emaSpeed * 0.6f + inst * 0.4f
            val gain = 1f + min(emaSpeed / ACCEL_REF_PX, 1f) * (ACCEL_MAX - 1f)
            mx *= gain; my *= gain
        }
        // Carry the sub-pixel remainder instead of truncating each event.
        accX += mx; accY += my
        val ix = accX.toInt()
        val iy = accY.toInt()
        if (ix != 0 || iy != 0) {
            // Send each event's delta immediately. Small, frequent deltas keep the
            // cursor smooth under network jitter; batching them into fewer, larger
            // sends made the motion *jumpier* (each arriving hop is bigger), so we
            // don't coalesce.
            client.move(ix, iy)
            accX -= ix
            accY -= iy
        }
    }

    fun scroll(dx: Int, dy: Int) {
        if (dx == 0 && dy == 0) return
        touch()
        client.scroll(dx, dy)   // direction decided in the UI (per-surface)
    }

    fun zoom(steps: Int) {
        if (steps == 0) return
        touch()
        client.zoom(steps)   // two-finger pinch → ctrl+wheel on the laptop
    }

    fun click() { touch(); client.click() }
    fun rightClick() { touch(); client.rightClick() }
    fun middleClick() { touch(); client.middleClick() }
    fun dragStart() = client.mouseDown()
    fun dragEnd() = client.mouseUp()
    fun combo(spec: String) = client.combo(spec)

    // Three-finger swipe → cycle apps like Windows: Alt stays held across the gesture,
    // each notch taps Tab (forward) / Shift+Tab (back), lifting fingers commits.
    fun switchAppStep(forward: Boolean) = client.appSwitch(if (forward) "next" else "prev")
    fun switchAppEnd() {
        // UDP is lossy and a dropped "end" leaves Alt held; resend (server end is idempotent).
        client.appSwitch("end")
        client.appSwitch("end")
    }

    fun system(action: String) {
        if (action == "mute") {
            // Show the toggle at once; the next volume poll confirms it.
            lastUserMuteMs = System.currentTimeMillis()
            update { s -> s.copy(muted = s.muted?.let { !it }) }
        }
        if (action == "sleep") {
            sleepRequestedMs = System.currentTimeMillis()
            sleptDeviceId = current?.id
        }
        client.system(action)
    }

    // --- volume ---
    fun setVolume(v: Float) {
        lastUserVolumeMs = System.currentTimeMillis()
        touch()
        // The laptop unmutes when its volume is set above 0 (set_win), so show that
        // now rather than after the next poll. Same test as the value sent below.
        val unmutes = v.toInt() > 0 && _state.value.muted == true
        if (unmutes) lastUserMuteMs = lastUserVolumeMs
        update { it.copy(volume = v, muted = if (unmutes) false else it.muted) }
        client.setVolume(v.toInt())
    }

    // --- brightness ---
    fun setBrightness(v: Float) {
        lastUserBrightnessMs = System.currentTimeMillis()
        touch()
        update { it.copy(brightness = v) }
        client.setBrightness(v.toInt())
    }

    // --- media ---
    fun media(action: String) { touch(); client.media(action) }

    // --- keyboard ---
    /**
     * Send the difference between the field's previous and current text.
     *
     * Both sides are passed in by the caller rather than read from state: the text
     * field owns its own contents (so selection and the IME composing region survive
     * recomposition), and this is a pure "what changed" translation. Keeping a copy
     * here as the source of truth is what let an unrelated state update clobber the
     * field mid-word.
     */
    fun onKeyboardInput(old: String, new: String) {
        touch()
        for (op in keyboardOps(old, new)) when (op) {
            KeyOp.Backspace -> client.keySpecial("backspace")
            KeyOp.NewLine -> client.combo("shift enter")
            is KeyOp.Type -> client.key(op.text)
        }
    }

    fun specialKey(name: String) {
        touch()
        client.keySpecial(name)
    }

    override fun onCleared() {
        healthJob?.cancel()
        reconnectJob?.cancel()
        stopDiscovery()
        client.disconnect()
        holdWifi(false)   // safety net: never leak the Wi-Fi lock if the VM dies mid-session
        updater.cancel()
        try {
            connectivity?.unregisterNetworkCallback(netCallback)
        } catch (_: Exception) {
            // never registered, or already gone
        }
        try {
            connectivity?.unregisterNetworkCallback(lanCallback)
        } catch (_: Exception) {
        }
        super.onCleared()
    }

    /**
     * Apply [block] atomically.
     *
     * This was `_state.value = block(_state.value)` — a read-modify-write, and not
     * every caller is on the main thread: Discovery's NSD callbacks arrive on a
     * binder thread, so a discovery update could read a snapshot taken before a
     * keystroke's write and then put it back, silently reverting the newer field.
     * compare-and-set retries instead of losing the race.
     */
    private fun update(block: (UiState) -> UiState) {
        while (true) {
            // Not named `current` — that's the connected-Device member field.
            val snapshot = _state.value
            if (_state.compareAndSet(snapshot, block(snapshot))) return
        }
    }
}

/**
 * What to send the laptop to turn [old] into [new], as (backspaces, text to type).
 *
 * The laptop's caret sits at the end of what we've sent, so an edit becomes:
 * delete back to the last character the two strings share, then type the rest.
 * Autocorrect or a swipe suggestion replacing the last word therefore costs a
 * word's worth of backspaces. It used to rewind the WHOLE field and retype it on
 * any edit that wasn't a pure append or delete: dozens of one-per-packet
 * backspaces the user watched scrub across the laptop, and losing the single
 * retype packet on the lossy wire could erase everything they had typed.
 */
internal fun keyboardDelta(old: String, new: String): Pair<Int, String> {
    // commonPrefixWith never ends on half a surrogate pair, so an emoji is
    // always deleted and retyped whole.
    val common = old.commonPrefixWith(new).length
    // Count code points, not UTF-16 units: one backspace on the laptop removes a
    // single-code-point emoji (two units here) whole. Emoji built from several
    // code points (flags, skin tones, ZWJ families) are where targets disagree:
    // Chromium-based apps delete the whole cluster with one backspace, so there
    // the extra backspaces eat into the text before it. No single count fits
    // every app; per code point is right for the common case.
    return old.codePointCount(common, old.length) to new.substring(common)
}

/** [s] minus its last character, never splitting a surrogate pair. The
 *  on-screen Backspace trims the buffer with this, so the buffer and the laptop
 *  lose the same thing: half an emoji left behind would cost one more backspace
 *  later and leave the two out of step. */
internal fun dropLastCodePoint(s: String): String =
    if (s.isEmpty()) s else s.substring(0, s.offsetByCodePoints(s.length, -1))

/** Line breaks in one form. A paste from a CRLF source carries "\r\n", and a
 *  bare '\r' typed as text reaches the laptop as Enter: the submit that the
 *  Shift+Enter mapping exists to avoid. */
private fun normalizeLineBreaks(s: String): String =
    if ('\r' !in s) s else s.replace("\r\n", "\n").replace('\r', '\n')

/** One thing to send the laptop while mirroring the phone's text field. */
internal sealed interface KeyOp {
    data object Backspace : KeyOp
    data object NewLine : KeyOp
    data class Type(val text: String) : KeyOp
}

/**
 * [keyboardDelta] as the keystrokes that carry it out.
 *
 * A line break typed on the phone becomes Shift+Enter, not a literal newline:
 * typed as text it arrives as Enter, which submits in chat apps and many forms.
 * Shift+Enter is the near-universal "new line without sending". Backspacing
 * over a line break needs nothing special: one backspace removes it on the
 * laptop too, including one that arrived as "\r\n".
 */
internal fun keyboardOps(old: String, new: String): List<KeyOp> {
    val (backspaces, typed) = keyboardDelta(normalizeLineBreaks(old), normalizeLineBreaks(new))
    return buildList {
        repeat(backspaces) { add(KeyOp.Backspace) }
        typed.split('\n').forEachIndexed { i, part ->
            if (i > 0) add(KeyOp.NewLine)
            if (part.isNotEmpty()) add(KeyOp.Type(part))
        }
    }
}
