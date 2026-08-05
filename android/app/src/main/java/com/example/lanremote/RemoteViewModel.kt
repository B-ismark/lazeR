package com.example.lanremote

import android.app.Application
import android.content.Context
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.lanremote.data.Device
import com.example.lanremote.data.DeviceStore
import com.example.lanremote.data.DiscoveredHost
import com.example.lanremote.data.Discovery
import com.example.lanremote.data.Settings
import com.example.lanremote.data.SettingsStore
import com.example.lanremote.data.UpdateChecker
import com.example.lanremote.net.RemoteClient
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
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
    val parts = s.split(".").mapNotNull { it.toIntOrNull() }
    if (parts.size != 4 || parts.any { it !in 0..255 }) return null
    return (parts[0] shl 24) or (parts[1] shl 16) or (parts[2] shl 8) or parts[3]
}

// Pointer acceleration: smoothed speed at/above which the gain saturates, and the
// max multiplier applied to a fast flick. Slow drags stay near 1× for precision.
private const val ACCEL_REF_PX = 12f
private const val ACCEL_MAX = 2.2f
private const val MOVE_GAP_RESET_MS = 120L   // idle gap ⇒ treat the next move as a fresh gesture
// How long beginReconnect keeps auto-retrying before it gives up and shows an
// actionable error instead of an endless spinner. Generous enough to ride out a
// laptop sleep/resume or a full reboot hands-free, but bounded so a permanently
// failing handshake (e.g. the laptop was re-paired and now has a different key —
// which can NEVER succeed with the stored key) doesn't strand the user on the
// "Reconnecting" screen forever.
private const val RECONNECT_GIVEUP_MS = 90_000L
// Speed-adaptive delta smoothing (one-euro style): near-still input is low-pass
// filtered to kill capacitive jitter; fast flicks pass straight through so the cursor
// never lags. Blend ramps from SMOOTH_FLOOR (slow) to 1.0 (fast).
private const val SMOOTH_FLOOR = 0.45f       // min blend at rest — lower = smoother, more lag
private const val SMOOTH_REF_PX = 7f         // per-event speed at which smoothing fully disengages

/** Wheel units in one detent — Windows' WHEEL_DELTA. The UI measures scrolling in units
 *  so it can express a gesture at full resolution; only the wire format cares about
 *  detents, and only when talking to a laptop too old to understand [RemoteClient.scrollUnits]. */
const val WHEEL_UNITS_PER_DETENT = 120f

data class UiState(
    val name: String = "",
    val ip: String = "",
    val port: String = "50505",
    val token: String = "",
    val conn: ConnState = ConnState.Disconnected,
    val volume: Float = 50f,
    val brightness: Float = 50f,
    val brightnessAvailable: Boolean = false,
    // NOTE: the keyboard's staging text deliberately does NOT live here. It used to,
    // and that was the bug: this whole object is rewritten on every health-loop tick
    // (volume/brightness sync, every 1.5-4s), which re-fed a plain String into a
    // fully-controlled TextField and discarded whatever the IME was still holding as
    // uncommitted composing text. The field owns its own editing state now — see
    // KeyboardPanel — and the ViewModel keeps only the diff, not the display value.
    val error: String? = null,
    val savedDevices: List<Device> = emptyList(),
    val discovered: List<DiscoveredHost> = emptyList(),
    val settings: Settings = Settings(),
    // Tag of a newer release, or null when we're current / haven't found out / the
    // check is switched off. One nullable field rather than a status enum: "up to
    // date" and "couldn't reach GitHub" both render as nothing at all, so the UI
    // has no reason to tell them apart.
    val updateTag: String? = null,
)

class RemoteViewModel(app: Application) : AndroidViewModel(app) {

    private val client = RemoteClient()
    private val store = DeviceStore(app)
    private val discovery = Discovery(app)
    private val settingsStore = SettingsStore(app)
    private var healthJob: Job? = null
    private var reconnectJob: Job? = null
    private var lastUserVolumeMs: Long = 0
    private var lastUserBrightnessMs: Long = 0
    private var lastInteractionMs: Long = 0   // drives adaptive health-poll backoff
    private var current: Device? = null   // device we're connected to / reconnecting
    // Negotiated per session in startHealthLoop: does the laptop understand SCRU?
    private var hiResScroll = false
    // Leftover wheel units when we have to quantise to whole detents for an old server.
    private var scrollUnitAccX = 0f
    private var scrollUnitAccY = 0f

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
        UiState(savedDevices = store.load(), settings = settingsStore.load())
    )
    val state: StateFlow<UiState> = _state.asStateFlow()

    init {
        discovery.start { hosts ->
            update { it.copy(discovered = hosts) }
        }
        // Silently re-try the last device on launch, if any.
        val lastId = settingsStore.lastDeviceId
        store.load().firstOrNull { it.id == lastId }?.let { connectSaved(it) }
        checkForUpdate()   // no-op when switched off or inside the throttle window
    }

    /** Restart mDNS discovery — clears the list and looks again for laptops. */
    fun rescan() {
        discovery.start { hosts -> update { it.copy(discovered = hosts) } }
    }

    // --- settings ---
    fun setSensitivity(v: Float) = updateSettings { it.copy(sensitivity = v) }
    fun setNaturalScroll(v: Boolean) = updateSettings { it.copy(naturalScroll = v) }
    fun setHaptics(v: Boolean) = updateSettings { it.copy(haptics = v) }
    fun setAcceleration(v: Boolean) = updateSettings { it.copy(acceleration = v) }

    /** Turning the check off also clears any banner already on screen — leaving it
     *  up would look like the setting hadn't taken. Turning it on re-checks now
     *  rather than waiting out the throttle, so the toggle gives visible feedback. */
    fun setUpdateCheck(v: Boolean) {
        updateSettings { it.copy(updateCheck = v) }
        if (v) checkForUpdate(force = true) else update { it.copy(updateTag = null) }
    }

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
     * instead of only after a network round trip. Failures are silent by design —
     * see [UpdateChecker].
     */
    private fun checkForUpdate(force: Boolean = false) {
        if (!_state.value.settings.updateCheck) return
        val mine = installedVersion() ?: return
        // Cached result: instant, offline-safe, and re-validated below if due.
        settingsStore.lastKnownTag?.let { cached ->
            if (UpdateChecker.isNewer(cached, mine)) update { it.copy(updateTag = cached) }
        }
        val now = System.currentTimeMillis()
        if (!force && !settingsStore.updateCheckDue(now)) return
        viewModelScope.launch {
            val tag = UpdateChecker.latestTag() ?: return@launch   // silent on failure
            settingsStore.lastUpdateCheckMs = System.currentTimeMillis()
            settingsStore.lastKnownTag = tag
            // Recompute rather than trusting the cache: this also CLEARS the banner
            // once the user has actually updated, which is the only way it goes away.
            update { it.copy(updateTag = if (UpdateChecker.isNewer(tag, mine)) tag else null) }
        }
    }

    /** Open the releases page. Notify-only: we never download or install an APK. */
    fun releasesUrl(): String = UpdateChecker.RELEASES_PAGE

    private inline fun updateSettings(block: (Settings) -> Settings) {
        val s = block(_state.value.settings)
        settingsStore.save(s)
        update { it.copy(settings = s) }
    }

    // --- form fields ---
    fun onName(v: String) = update { it.copy(name = v, error = null) }
    fun onIp(v: String) = update { it.copy(ip = v.trim(), error = null) }
    fun onPort(v: String) = update { it.copy(port = v.filter { c -> c.isDigit() }, error = null) }
    fun onToken(v: String) = update { it.copy(token = v.trim().uppercase(), error = null) }

    // --- connect entry points ---
    fun connectManual() {
        // Manual code entry has no key ⇒ legacy plaintext (trusted networks only).
        val s = _state.value
        connect(s.name, s.ip, s.port.toIntOrNull() ?: 50505, s.token, "", save = true)
    }

    fun connectSaved(device: Device) {
        update { it.copy(name = device.name, ip = device.ip,
            port = device.port.toString(), token = device.token) }
        connect(device.name, device.ip, device.port, device.token, device.key,
            save = false)
    }

    fun useDiscovered(host: DiscoveredHost) {
        // Fill IP/port from discovery; token still required (then saved).
        update {
            it.copy(name = host.name, ip = host.ip, port = host.port.toString(),
                error = "Enter the token for ${host.name}")
        }
    }

    /** Parse a scanned `lazer://ip:port/?token=..&name=..&k=..&r=..` URI and connect. */
    fun applyScannedUri(raw: String) {
        val uri = try { Uri.parse(raw.trim()) } catch (e: Exception) { null }
        if (uri == null || uri.scheme != "lazer" || uri.host.isNullOrBlank()) {
            update { it.copy(error = "Unrecognized QR code") }
            return
        }
        val ip = uri.host!!
        val port = if (uri.port > 0) uri.port else 50505
        val token = uri.getQueryParameter("token")?.uppercase().orEmpty()
        val name = uri.getQueryParameter("name") ?: ip
        val key = uri.getQueryParameter("k").orEmpty()   // 256-bit secret ⇒ encrypted wire
        if (token.isBlank()) {
            update { it.copy(error = "QR code has no token") }
            return
        }
        update { it.copy(name = name, ip = ip, port = port.toString(), token = token) }
        connect(name, ip, port, token, key, save = true)
    }

    private fun connect(name: String, ip: String, port: Int, token: String,
                        key: String, save: Boolean) {
        if (ip.isBlank() || token.isBlank()) {
            update { it.copy(error = "Need an IP and token") }
            return
        }
        val dev = Device(id = "$ip:$port", name = name.ifBlank { ip },
            ip = ip, port = port, token = token, key = key)
        current = dev
        reconnectJob?.cancel()
        update { it.copy(conn = ConnState.Connecting, error = null) }
        viewModelScope.launch {
            val connected = connectResolving(dev, 2000)
            if (connected != null) {
                current = connected
                // Persist when explicitly saving, OR when the stored address was
                // stale and we reached the laptop at a new IP via mDNS — so next
                // tap dials the right place instead of failing again.
                val moved = connected.ip != dev.ip || connected.port != dev.port
                if (save || moved) update { it.copy(savedDevices = store.upsert(connected)) }
                settingsStore.lastDeviceId = connected.id
                discovery.stop()   // no need to keep scanning Wi-Fi while controlling
                holdWifi(true)     // pin the radio low-latency for the session
                touch()
                update { it.copy(conn = ConnState.Connected) }
                startHealthLoop()
            } else {
                update {
                    it.copy(conn = ConnState.Disconnected,
                        error = connectFailureMessage(dev))
                }
            }
        }
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
    private fun connectFailureMessage(dev: Device): String {
        val mine = localIPv4s()
        return when {
            mine.isEmpty() ->
                "This phone has no Wi-Fi address — join the laptop's network and retry."
            mine.none { it.sharesSubnetWith(dev.ip) } ->
                "Your phone is on ${mine.first().address} but ${dev.ip} is on a " +
                    "different network, so they can't reach each other. Put both on the " +
                    "same Wi-Fi — note that a router's 2.4 GHz and 5 GHz names are " +
                    "sometimes separate networks, and guest networks always are."
            else ->
                "${dev.ip} is on your network but didn't answer. Check LazeR is running " +
                    "on the laptop and that you allowed its firewall prompt — the LazeR " +
                    "window warns when inbound UDP is blocked. Some routers also block " +
                    "device-to-device traffic (\"client isolation\")."
        }
    }

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

    private fun localIPv4s(): List<LocalV4> = try {
        java.net.NetworkInterface.getNetworkInterfaces().asSequence()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { it.interfaceAddresses.asSequence() }
            .filter { it.address is java.net.Inet4Address && it.address.isSiteLocalAddress }
            .mapNotNull { ia ->
                ia.address.hostAddress?.let { LocalV4(it, ia.networkPrefixLength.toInt()) }
            }
            .toList()
    } catch (e: Exception) {
        emptyList()
    }

    /**
     * Connect to [dev]'s stored address; if that fails, try every laptop currently
     * visible via mDNS (its IP may have moved on a DHCP lease / reboot). The wrong
     * host simply fails the authenticated handshake, so trying them is safe.
     * @return the device that answered (its id preserved, ip/port refreshed), or null.
     */
    private suspend fun connectResolving(dev: Device, timeoutMs: Long): Device? {
        if (client.connect(dev.ip, dev.port, dev.token, dev.key, timeoutMs)) return dev
        val candidates = _state.value.discovered
            .filterNot { it.ip == dev.ip && it.port == dev.port }
        for (h in candidates) {
            if (client.connect(h.ip, h.port, dev.token, dev.key, timeoutMs)) {
                return dev.copy(ip = h.ip, port = h.port)
            }
        }
        return null
    }

    /** Watchdog: poll volume (doubles as liveness); on repeated misses, reconnect. */
    private fun startHealthLoop() {
        healthJob?.cancel()
        healthJob = viewModelScope.launch {
            // Renegotiate optional wire features for THIS session. Both the first connect
            // and every reconnect land here, and the laptop on the other end may not be
            // the one we last spoke to (or may have been upgraded since), so this must
            // never be cached across sessions. An old server stays silent and we fall
            // back to whole-detent scrolling.
            // Assume nothing until it answers: the query suspends for up to its timeout
            // while the session is ALREADY connected, so a scroll in that window would
            // otherwise go out in the previous laptop's dialect and be dropped on the
            // floor. Falling back for a few hundred ms is invisible; losing scroll isn't.
            hiResScroll = false
            hiResScroll = "hires" in client.queryCaps()
            scrollUnitAccX = 0f; scrollUnitAccY = 0f
            var misses = 0
            var tick = 0
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
                    if (System.currentTimeMillis() - lastUserVolumeMs > 1200) {
                        update { it.copy(volume = v.toFloat()) }
                    }
                    true
                } else {
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
                delay(if (misses > 0) 500L else if (active) 1500L else 4000L)
            }
        }
    }

    /** Keep retrying the current device until it answers, the user backs out, or the
     *  give-up window elapses. mDNS runs during reconnect so a laptop that came back on
     *  a new IP can still be found and re-pinned (the saved address is refreshed on
     *  success). After RECONNECT_GIVEUP_MS of continuous failure we stop and drop to
     *  Disconnected with an actionable message rather than spinning forever — the old
     *  loop had no exit, so a re-paired laptop (new key, handshake can never complete)
     *  or a laptop that stays unreachable left the app stuck on "Reconnecting". */
    private fun beginReconnect() {
        healthJob?.cancel()
        val dev = current ?: return disconnect()
        update { it.copy(conn = ConnState.Reconnecting) }
        reconnectJob?.cancel()
        discovery.start { hosts -> update { it.copy(discovered = hosts) } }
        reconnectJob = viewModelScope.launch {
            val startMs = System.currentTimeMillis()
            while (isActive) {
                val c = connectResolving(dev, 1200)
                if (c != null) {
                    current = c
                    if (c.ip != dev.ip || c.port != dev.port) {
                        update { it.copy(savedDevices = store.upsert(c)) }
                        settingsStore.lastDeviceId = c.id
                    }
                    discovery.stop()
                    holdWifi(true)
                    update { it.copy(conn = ConnState.Connected, error = null) }
                    startHealthLoop()
                    return@launch
                }
                if (System.currentTimeMillis() - startMs > RECONNECT_GIVEUP_MS) {
                    // Give up the auto-retry. Keep `current` and lastDeviceId intact so
                    // a tap (or next launch) can retry without re-adding the device, but
                    // release the radio lock and tell the user what to try next.
                    holdWifi(false)
                    update {
                        it.copy(conn = ConnState.Disconnected,
                            error = "Couldn't reconnect to ${dev.name}. " +
                                connectFailureMessage(dev) +
                                " If you re-paired the laptop, scan its new QR.")
                    }
                    return@launch
                }
                delay(2000)
            }
        }
    }

    fun deleteDevice(device: Device) {
        if (settingsStore.lastDeviceId == device.id) settingsStore.lastDeviceId = null
        update { it.copy(savedDevices = store.delete(device.id)) }
    }

    fun reportError(msg: String) = update { it.copy(error = msg) }

    fun disconnect() {
        healthJob?.cancel()
        reconnectJob?.cancel()
        settingsStore.lastDeviceId = null   // intentional leave: don't auto-reconnect next launch
        current = null
        client.disconnect()
        holdWifi(false)                     // let the radio power-save again
        update { it.copy(conn = ConnState.Disconnected) }
        discovery.start { hosts -> update { it.copy(discovered = hosts) } }   // scan again for the connection screen
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
            // cursor smooth even under relay jitter; batching them into fewer larger
            // sends made the motion *jumpier* (each arriving hop is bigger), so we
            // don't coalesce — not on LAN (low latency) nor on the relay.
            client.move(ix, iy)
            accX -= ix
            accY -= iy
        }
    }

    /**
     * Scroll by [dx]/[dy] WHEEL UNITS (120 = one detent). The UI always speaks units so
     * gesture code doesn't have to know what the laptop supports; picking a wire format
     * is this layer's job. Direction is decided in the UI (per-surface).
     *
     * On a `hires` server the units go straight out, so a pan is a smooth stream at the
     * true resolution of the gesture. Otherwise they're accumulated and emitted as whole
     * detents, with the remainder carried — never dropped, or a slow drag would lose
     * travel and a long one would drift short.
     */
    fun scroll(dx: Int, dy: Int) {
        if (dx == 0 && dy == 0) return
        touch()
        if (hiResScroll) {
            client.scrollUnits(dx, dy)
            return
        }
        scrollUnitAccX += dx
        scrollUnitAccY += dy
        val nx = (scrollUnitAccX / WHEEL_UNITS_PER_DETENT).toInt()   // toward zero
        val ny = (scrollUnitAccY / WHEEL_UNITS_PER_DETENT).toInt()
        if (nx != 0 || ny != 0) {
            scrollUnitAccX -= nx * WHEEL_UNITS_PER_DETENT
            scrollUnitAccY -= ny * WHEEL_UNITS_PER_DETENT
            client.scroll(nx, ny)
        }
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

    fun system(action: String) = client.system(action)

    // --- volume ---
    fun setVolume(v: Float) {
        lastUserVolumeMs = System.currentTimeMillis()
        touch()
        update { it.copy(volume = v) }
        client.setVolume(v.toInt())
    }

    fun nudgeVolume(delta: Float) = setVolume((_state.value.volume + delta).coerceIn(0f, 100f))

    // --- brightness ---
    fun setBrightness(v: Float) {
        lastUserBrightnessMs = System.currentTimeMillis()
        touch()
        update { it.copy(brightness = v) }
        client.setBrightness(v.toInt())
    }

    fun nudgeBrightness(delta: Float) =
        setBrightness((_state.value.brightness + delta).coerceIn(0f, 100f))

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
        when {
            new == old -> Unit
            new.length > old.length && new.startsWith(old) ->
                client.key(new.substring(old.length))
            new.length < old.length && old.startsWith(new) ->
                repeat(old.length - new.length) { client.keySpecial("backspace") }
            else -> {
                // Neither a pure append nor a pure delete (autocorrect or a swipe
                // replacing a whole word): rewind what we sent and retype it.
                repeat(old.length) { client.keySpecial("backspace") }
                if (new.isNotEmpty()) client.key(new)
            }
        }
    }

    fun specialKey(name: String) {
        touch()
        client.keySpecial(name)
    }

    override fun onCleared() {
        healthJob?.cancel()
        reconnectJob?.cancel()
        discovery.stop()
        client.disconnect()
        holdWifi(false)   // safety net: never leak the Wi-Fi lock if the VM dies mid-session
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
