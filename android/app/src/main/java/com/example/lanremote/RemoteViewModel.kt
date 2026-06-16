package com.example.lanremote

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.lanremote.data.Device
import com.example.lanremote.data.DeviceStore
import com.example.lanremote.data.DiscoveredHost
import com.example.lanremote.data.Discovery
import com.example.lanremote.data.Settings
import com.example.lanremote.data.SettingsStore
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

// Pointer acceleration: smoothed speed at/above which the gain saturates, and the
// max multiplier applied to a fast flick. Slow drags stay near 1× for precision.
private const val ACCEL_REF_PX = 12f
private const val ACCEL_MAX = 2.2f
private const val MOVE_GAP_RESET_MS = 120L   // idle gap ⇒ treat the next move as a fresh gesture

data class UiState(
    val name: String = "",
    val ip: String = "",
    val port: String = "50505",
    val token: String = "",
    val conn: ConnState = ConnState.Disconnected,
    val volume: Float = 50f,
    val brightness: Float = 50f,
    val brightnessAvailable: Boolean = false,
    val keyboardText: String = "",
    val error: String? = null,
    val savedDevices: List<Device> = emptyList(),
    val discovered: List<DiscoveredHost> = emptyList(),
    val settings: Settings = Settings(),
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
        connect(device.name, device.ip, device.port, device.token, device.key, save = false)
    }

    fun useDiscovered(host: DiscoveredHost) {
        // Fill IP/port from discovery; token still required (then saved).
        update {
            it.copy(name = host.name, ip = host.ip, port = host.port.toString(),
                error = "Enter the token for ${host.name}")
        }
    }

    /** Parse a scanned `lazer://ip:port/?token=..&name=..` URI and connect. */
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
            val ok = client.connect(ip, port, token, key)
            if (ok) {
                if (save) update { it.copy(savedDevices = store.upsert(dev)) }
                settingsStore.lastDeviceId = dev.id
                discovery.stop()   // no need to keep scanning Wi-Fi while controlling
                touch()
                update { it.copy(conn = ConnState.Connected) }
                startHealthLoop()
            } else {
                update {
                    it.copy(conn = ConnState.Disconnected,
                        error = "Couldn't reach $ip — check it's on, same Wi-Fi, token correct")
                }
            }
        }
    }

    /** Watchdog: poll volume (doubles as liveness); on repeated misses, reconnect. */
    private fun startHealthLoop() {
        healthJob?.cancel()
        healthJob = viewModelScope.launch {
            var misses = 0
            var brightnessOk = true   // stop polling brightness once a laptop reports none
            var tick = 0
            while (isActive) {
                val v = client.queryVolume()
                val alive = if (v != null) {
                    if (System.currentTimeMillis() - lastUserVolumeMs > 1200) {
                        update { it.copy(volume = v.toFloat()) }
                    }
                    true
                } else {
                    client.ping()   // confirm before declaring it dead
                }
                if (alive) {
                    misses = 0
                    // Brightness changes rarely and the read is costly on the laptop —
                    // sync it every ~4th tick, not every tick. (Volume doubles as the
                    // liveness probe, so it stays every tick.)
                    if (brightnessOk && tick % 4 == 0) {
                        val b = client.queryBrightness()
                        if (b != null) {
                            if (System.currentTimeMillis() - lastUserBrightnessMs > 1200) {
                                update { it.copy(brightness = b.toFloat(), brightnessAvailable = true) }
                            }
                        } else {
                            brightnessOk = false
                            update { it.copy(brightnessAvailable = false) }
                        }
                    }
                } else if (++misses >= 2) {
                    beginReconnect()
                    return@launch
                }
                tick++
                // Adaptive backoff: poll briskly (1.5s) while you're actively using the
                // trackpad/sliders for responsive volume sync + fast disconnect notice;
                // ease off to 4s when idle to spare the radio + battery. Worst-case
                // disconnect detection when idle ≈ 8–12s, still fine.
                val active = System.currentTimeMillis() - lastInteractionMs < 5000
                delay(if (active) 1500L else 4000L)
            }
        }
    }

    /** Keep retrying the current device until it answers or the user backs out. */
    private fun beginReconnect() {
        healthJob?.cancel()
        val dev = current ?: return disconnect()
        update { it.copy(conn = ConnState.Reconnecting) }
        reconnectJob?.cancel()
        reconnectJob = viewModelScope.launch {
            while (isActive) {
                if (client.connect(dev.ip, dev.port, dev.token, dev.key, timeoutMs = 1200)) {
                    update { it.copy(conn = ConnState.Connected, error = null) }
                    startHealthLoop()
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
        update { it.copy(conn = ConnState.Disconnected, keyboardText = "") }
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

    fun move(dx: Float, dy: Float) {
        if (dx == 0f && dy == 0f) return
        val s = _state.value.settings
        // A fresh gesture after an idle gap: clear stale speed + remainder so the
        // first move isn't flung by the previous flick's momentum.
        val now = System.currentTimeMillis()
        if (now - lastMoveMs > MOVE_GAP_RESET_MS) { emaSpeed = 0f; accX = 0f; accY = 0f }
        lastMoveMs = now
        lastInteractionMs = now

        var mx = dx * s.sensitivity
        var my = dy * s.sensitivity
        if (s.acceleration) {
            // EMA-smooth the per-event speed so the gain ramps instead of jittering;
            // slow drags stay ~1:1 for precision, fast flicks reach ACCEL_MAX.
            val inst = hypot(dx.toDouble(), dy.toDouble()).toFloat()
            emaSpeed = emaSpeed * 0.6f + inst * 0.4f
            val gain = 1f + min(emaSpeed / ACCEL_REF_PX, 1f) * (ACCEL_MAX - 1f)
            mx *= gain; my *= gain
        }
        // Carry the sub-pixel remainder instead of truncating each event.
        accX += mx; accY += my
        val ix = accX.toInt()
        val iy = accY.toInt()
        if (ix != 0 || iy != 0) {
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

    // Two-finger horizontal swipe → browser back/forward, like a Windows touchpad.
    // forward (swipe →) = Alt+Right, back (swipe ←) = Alt+Left.
    fun browserNav(forward: Boolean) = client.combo(if (forward) "alt right" else "alt left")

    fun system(action: String) = client.system(action)
    fun presentation(action: String) = client.presentation(action)

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

    // --- clipboard ---
    fun pasteText(text: String) {
        if (text.isNotEmpty()) client.clipboardPaste(text)
    }

    // --- media ---
    fun media(action: String) { touch(); client.media(action) }

    // --- keyboard ---
    fun onKeyboardInput(new: String) {
        val old = _state.value.keyboardText
        when {
            new == old -> Unit
            new.length > old.length && new.startsWith(old) ->
                client.key(new.substring(old.length))
            new.length < old.length && old.startsWith(new) ->
                repeat(old.length - new.length) { client.keySpecial("backspace") }
            else -> {
                repeat(old.length) { client.keySpecial("backspace") }
                if (new.isNotEmpty()) client.key(new)
            }
        }
        update { it.copy(keyboardText = new) }
    }

    fun specialKey(name: String) {
        client.keySpecial(name)
        if (name == "enter" || name == "esc") update { it.copy(keyboardText = "") }
    }

    override fun onCleared() {
        healthJob?.cancel()
        reconnectJob?.cancel()
        discovery.stop()
        client.disconnect()
        super.onCleared()
    }

    private inline fun update(block: (UiState) -> UiState) {
        _state.value = block(_state.value)
    }
}
