package com.example.lanremote.ui

import android.app.Activity
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.animation.core.animate
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.offset
import androidx.compose.material.icons.filled.FastForward
import androidx.compose.material.icons.filled.FastRewind
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import kotlinx.coroutines.launch
import kotlin.math.roundToInt
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardReturn
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.automirrored.outlined.Backspace
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.BrightnessHigh
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material.icons.filled.Fullscreen
import androidx.compose.material.icons.filled.FullscreenExit
import androidx.compose.material.icons.filled.Headphones
import androidx.compose.material.icons.filled.Keyboard
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.filled.PauseCircle
import androidx.compose.material.icons.filled.Remove
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material.icons.filled.WarningAmber
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonShapes
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.example.lanremote.UiState
import kotlin.math.abs
import kotlin.math.hypot

private const val SCROLL_STEP_PX = 16f   // smaller = finer scroll + more haptic detents
private const val SWIPE_APP_PX = 120f    // three-finger horizontal travel per app switch — short hop, fits small screens
private const val SWIPE_NAV_PX = 150f    // two-finger horizontal travel per browser back/forward — deliberate, not jittery scroll
private const val ZOOM_STEP_PX = 22f     // two-finger spread change per ctrl+wheel zoom notch (finer = smoother zoom)
// The 2-finger gesture latches once ACCUMULATED evidence crosses a threshold — not per
// frame, which mis-fired zoom on a back-swipe (one wobbly first frame was enough). We
// sum net spread (gap change) vs net centroid travel over the opening frames, then pick
// the bigger. ZOOM_BIAS makes spread win only when it clearly dominates, so an ordinary
// horizontal swipe (fingers drift together, gap ~steady) reliably lands on back/forward.
private const val PINCH_LATCH_PX = 14f   // accumulated |gap change| that can commit to zoom
private const val SCROLL_LATCH_PX = 10f  // accumulated centroid travel that can commit to scroll/nav
private const val ZOOM_BIAS = 1.4f       // spread must beat travel by this factor to latch zoom

/** All the actions the control screen can fire. Bundled to keep the signature sane. */
class ControlActions(
    val onMove: (Float, Float) -> Unit,
    val onScroll: (Int, Int) -> Unit,   // dx, dy steps
    val onZoom: (Int) -> Unit,          // two-finger pinch: +1 = zoom in (spread), -1 = zoom out (pinch)
    val onClick: () -> Unit,
    val onRightClick: () -> Unit,
    val onMiddleClick: () -> Unit,
    val onSwitchStep: (Boolean) -> Unit,  // three-finger notch: true = next app, false = previous
    val onSwitchEnd: () -> Unit,          // fingers lifted: commit the highlighted app
    val onBrowserNav: (Boolean) -> Unit,  // two-finger horizontal swipe: true = forward (→), false = back (←)
    val onDragStart: () -> Unit,
    val onDragEnd: () -> Unit,
    val onVolume: (Float) -> Unit,
    val onBrightness: (Float) -> Unit,
    val onMedia: (String) -> Unit,
    val onKeyboardInput: (String) -> Unit,
    val onSpecialKey: (String) -> Unit,
    val onCombo: (String) -> Unit,
    val onSystem: (String) -> Unit,
    val onPresentation: (String) -> Unit,
    val onPaste: (String) -> Unit,
    val onSensitivity: (Float) -> Unit,
    val onNaturalScroll: (Boolean) -> Unit,
    val onHaptics: (Boolean) -> Unit,
    val onAcceleration: (Boolean) -> Unit,
    val onButtonTap: () -> Unit,        // light haptic for generic button presses
    val onDisconnect: () -> Unit,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ControlScreen(state: UiState, a: ControlActions) {
    var fullscreen by rememberSaveable { mutableStateOf(false) }
    var showAdvanced by rememberSaveable { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(false) }

    BackHandler(enabled = true) {
        if (fullscreen) fullscreen = false else a.onDisconnect()
    }

    // Immersive while the trackpad is expanded.
    val view = LocalView.current
    LaunchedEffect(fullscreen) {
        val window = (view.context as? Activity)?.window ?: return@LaunchedEffect
        val controller = WindowCompat.getInsetsController(window, view)
        if (fullscreen) {
            controller.hide(WindowInsetsCompat.Type.systemBars())
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        } else {
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
    }

    if (fullscreen) {
        FullscreenTrackpad(state, a, onExit = { fullscreen = false })
        return
    }

    Scaffold(
        topBar = {
            CenterAlignedTopAppBar(
                title = { Text("LazeR") },
                navigationIcon = {
                    IconButton(onClick = a.onDisconnect) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = "Back · disconnect")
                    }
                },
                actions = {
                    IconButton(onClick = { showAdvanced = true }) {
                        Icon(Icons.Filled.Tune, contentDescription = "Advanced")
                    }
                    IconButton(onClick = { showSettings = true }) {
                        Icon(Icons.Filled.Settings, contentDescription = "Settings")
                    }
                    IconButton(onClick = { fullscreen = true }) {
                        Icon(Icons.Filled.Fullscreen, contentDescription = "Expand trackpad")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                ),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .imePadding()
                .padding(horizontal = 16.dp, vertical = 8.dp),
        ) {
            if (state.relayWhileLocal) {
                RelayWarningCard()
                Spacer(Modifier.height(12.dp))
            }
            ControlsPanel(state, a)
            Spacer(Modifier.height(12.dp))
            TrackpadCard(
                modifier = Modifier.fillMaxWidth().weight(1f).heightIn(min = 220.dp),
                a = a,
                natural = state.settings.naturalScroll,
            )
            Spacer(Modifier.height(10.dp))
            ClickBar(a)
        }
    }

    if (showAdvanced) {
        AdvancedSheet(state, a, onDismiss = { showAdvanced = false })
    }
    if (showSettings) {
        SettingsSheet(state, a, onDismiss = { showSettings = false })
    }
}

// ---------------------------------------------------------------------------
// Controls: volume + a connected Media/Keyboard toggle group (always visible).
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3Api::class, ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ControlsPanel(state: UiState, a: ControlActions) {
    var tab by rememberSaveable { mutableIntStateOf(0) } // 0 = Media, 1 = Keyboard

    Column {
        // Expressive connected button group replaces the segmented row: the selected
        // half swells, the other compresses — the M3 Expressive toggle feel.
        ButtonGroup(
            overflowIndicator = { },
            modifier = Modifier.fillMaxWidth(),
        ) {
            toggleableItem(
                checked = tab == 0,
                label = "Media",
                onCheckedChange = { tab = 0 },
                icon = { Icon(Icons.Filled.Headphones, contentDescription = null,
                    modifier = Modifier.size(18.dp)) },
                weight = 1f,
            )
            toggleableItem(
                checked = tab == 1,
                label = "Keyboard",
                onCheckedChange = { tab = 1 },
                icon = { Icon(Icons.Filled.Keyboard, contentDescription = null,
                    modifier = Modifier.size(18.dp)) },
                weight = 1f,
            )
        }

        Spacer(Modifier.height(12.dp))

        AnimatedContent(
            targetState = tab,
            transitionSpec = { fadeIn() togetherWith fadeOut() },
            label = "panel",
        ) { which ->
            if (which == 0) MediaPanel(state, a)
            else KeyboardPanel(state, a)
        }
    }
}

/** Shown when we're linked through the rendezvous relay while actually on the laptop's
 *  Wi-Fi — the fast direct LAN path was blocked (usually the laptop's firewall / a
 *  Public network profile). Without this the degraded link looks like a normal
 *  connection while quietly dropping control packets. Dismissible per session. */
@Composable
private fun RelayWarningCard() {
    var show by remember { mutableStateOf(true) }
    if (!show) return
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.errorContainer,
        ),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            modifier = Modifier.padding(start = 16.dp, top = 12.dp, bottom = 12.dp, end = 4.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Icon(Icons.Filled.WarningAmber, contentDescription = null,
                tint = MaterialTheme.colorScheme.onErrorContainer,
                modifier = Modifier.size(22.dp))
            Column(Modifier.weight(1f).padding(start = 12.dp)) {
                Text("Connected over the relay",
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onErrorContainer)
                Text("You're on the same Wi-Fi as the laptop, but the fast direct link is " +
                        "blocked — so this runs slow and may drop out. Set the laptop's network " +
                        "to Private, or allow LazeR through its firewall.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onErrorContainer)
            }
            IconButton(onClick = { show = false }) {
                Icon(Icons.Filled.Close, contentDescription = "Dismiss",
                    tint = MaterialTheme.colorScheme.onErrorContainer)
            }
        }
    }
}

/** Volume / brightness card: icon + percent, with −/＋ nudge buttons flanking the slider. */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun LevelCard(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    label: String,
    value: Float,
    onChange: (Float) -> Unit,
    onStep: (Float) -> Unit,
) {
    SectionCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            // Expressive MaterialShapes flourish: the level icon sits in a 9-sided
            // "cookie" tonal badge instead of a bare glyph.
            Box(
                modifier = Modifier
                    .size(36.dp)
                    .clip(MaterialShapes.Cookie9Sided.toShape())
                    .background(MaterialTheme.colorScheme.primaryContainer),
                contentAlignment = Alignment.Center,
            ) {
                Icon(icon, contentDescription = null,
                    tint = MaterialTheme.colorScheme.onPrimaryContainer,
                    modifier = Modifier.size(20.dp))
            }
            Text("  $label  ${value.toInt()}%", style = MaterialTheme.typography.titleMedium)
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            PressIconButton(onClick = { onStep(-5f) }, size = 44.dp, shape = RoundedCornerShape(14.dp)) {
                Icon(Icons.Filled.Remove, contentDescription = "$label down")
            }
            Slider(value = value, onValueChange = onChange, valueRange = 0f..100f,
                modifier = Modifier.weight(1f).padding(horizontal = 8.dp))
            PressIconButton(onClick = { onStep(5f) }, size = 44.dp, shape = RoundedCornerShape(14.dp)) {
                Icon(Icons.Filled.Add, contentDescription = "$label up")
            }
        }
    }
}

@Composable
private fun MediaPanel(state: UiState, a: ControlActions) {
    fun fire(action: String) { a.onButtonTap(); a.onMedia(action) }
    fun seek(name: String) { a.onButtonTap(); a.onSpecialKey(name) }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        // Volume now lives with the media transport instead of at the top of the page.
        LevelCard(
            icon = Icons.AutoMirrored.Filled.VolumeUp,
            label = "Volume",
            value = state.volume,
            onChange = a.onVolume,
            onStep = { d -> a.onButtonTap(); a.onVolume((state.volume + d).coerceIn(0f, 100f)) },
        )
        SectionCard {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                // Rewind / fast-forward send Left / Right arrow — the focused app decides
                // the step (YouTube ~5s, Netflix/VLC/Disney+ ~10s). Generic chevrons, no
                // seconds label that would be wrong half the time. Each button morphs its
                // corner (rounded → circle) on press — the M3 Expressive icon-button feel.
                PressIconButton(onClick = { seek("left") }, size = 44.dp, shape = RoundedCornerShape(16.dp)) {
                    Icon(Icons.Filled.FastRewind, "Rewind", modifier = Modifier.size(24.dp))
                }
                PressIconButton(onClick = { fire("prev") }, size = 50.dp, shape = RoundedCornerShape(18.dp)) {
                    Icon(Icons.Filled.SkipPrevious, "Previous", modifier = Modifier.size(26.dp))
                }
                PressIconButton(onClick = { fire("play_pause") }, size = 62.dp, shape = RoundedCornerShape(22.dp), filled = true) {
                    Icon(Icons.Filled.PauseCircle, "Play / Pause", modifier = Modifier.size(36.dp))
                }
                PressIconButton(onClick = { fire("next") }, size = 50.dp, shape = RoundedCornerShape(18.dp)) {
                    Icon(Icons.Filled.SkipNext, "Next", modifier = Modifier.size(26.dp))
                }
                PressIconButton(onClick = { seek("right") }, size = 44.dp, shape = RoundedCornerShape(16.dp)) {
                    Icon(Icons.Filled.FastForward, "Fast forward", modifier = Modifier.size(24.dp))
                }
            }
        }
    }
}

/**
 * Expressive icon button. The M3 Expressive spec gives icon buttons a *shape morph*:
 * the container animates from its resting [shape] to [pressedShape] while held (here a
 * round-cornered square that pops to a circle), driven by the theme's motion scheme.
 * That replaces the old hand-rolled scale-down animation.
 */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun PressIconButton(
    onClick: () -> Unit,
    size: Dp,
    shape: Shape,
    filled: Boolean = false,
    pressedShape: Shape = CircleShape,
    content: @Composable () -> Unit,
) {
    val shapes = IconButtonShapes(shape = shape, pressedShape = pressedShape)
    val mod = Modifier.size(size)
    if (filled) {
        FilledIconButton(onClick = onClick, shapes = shapes, modifier = mod) { content() }
    } else {
        FilledTonalIconButton(onClick = onClick, shapes = shapes, modifier = mod) { content() }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun KeyboardPanel(state: UiState, a: ControlActions) {
    fun special(name: String) { a.onButtonTap(); a.onSpecialKey(name) }
    fun combo(c: String) { a.onButtonTap(); a.onCombo(c) }
    SectionCard {
        OutlinedTextField(
            value = state.keyboardText, onValueChange = a.onKeyboardInput,
            label = { Text("Type on laptop") }, singleLine = true,
            keyboardOptions = KeyboardOptions(autoCorrectEnabled = false),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        // One row, equal-width keys (weight) so all six fit on a single line regardless of
        // screen width. Backspace and New line are icon keys to stay compact; the centre
        // four are text. Tight content padding lets the labels breathe in the narrow cells.
        val keyPad = PaddingValues(horizontal = 4.dp)
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            FilledTonalButton(onClick = { special("backspace") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Icon(Icons.AutoMirrored.Outlined.Backspace, contentDescription = "Backspace",
                    modifier = Modifier.size(20.dp))
            }
            FilledTonalButton(onClick = { special("space") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Space", maxLines = 1)
            }
            FilledTonalButton(onClick = { special("tab") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Tab", maxLines = 1)
            }
            FilledTonalButton(onClick = { special("esc") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Esc", maxLines = 1)
            }
            FilledTonalButton(onClick = { special("enter") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Enter", maxLines = 1)
            }
            // Shift+Enter: soft newline without submitting — near-universal (chat apps,
            // editors), unlike Alt+Enter which varies by app. Shown as the return glyph.
            FilledTonalButton(onClick = { combo("shift enter") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Icon(Icons.AutoMirrored.Filled.KeyboardReturn,
                    contentDescription = "New line (Shift+Enter)", modifier = Modifier.size(20.dp))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Click bar: a connected Left / Middle / Right button group + hold-to-drag.
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3Api::class, ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ClickBar(
    a: ControlActions,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Connected group: whichever button you press swells and its neighbours give way
        // — the signature M3 Expressive button-group interaction.
        ButtonGroup(
            overflowIndicator = { },
            modifier = Modifier.weight(3f),
        ) {
            clickableItem(onClick = a.onClick, label = "Left", weight = 1f)
            clickableItem(onClick = a.onMiddleClick, label = "Middle", weight = 1f)
            clickableItem(onClick = a.onRightClick, label = "Right", weight = 1f)
        }
        HoldDragButton(a.onDragStart, a.onDragEnd, modifier = Modifier.weight(1.2f))
        // Optional trailing affordance (e.g. the fullscreen-exit button) sits after the
        // hold-drag button without stealing width from the connected group.
        trailing?.invoke()
    }
}

/** While held, the left button stays pressed — drag on the trackpad to move/select. */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun HoldDragButton(
    onDragStart: () -> Unit,
    onDragEnd: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val src = remember { MutableInteractionSource() }
    val pressed by src.collectIsPressedAsState()
    var wasPressed by remember { mutableStateOf(false) }
    LaunchedEffect(pressed) {
        if (pressed) { onDragStart(); wasPressed = true }
        else if (wasPressed) { onDragEnd(); wasPressed = false }
    }
    OutlinedButton(onClick = {}, interactionSource = src, modifier = modifier) {
        Text(if (pressed) "Drag…" else "Hold drag")
    }
}

// ---------------------------------------------------------------------------
// Trackpad + scroll strip
// ---------------------------------------------------------------------------

/** 1 finger = move · 2 fingers = scroll (vertical) / horizontal swipe = browser
 *  back-forward, pinch = zoom · 3 fingers left/right = switch apps.
 *  Tap = left-click · two-finger tap = right-click. Hold does nothing, so a resting
 *  hand never fires a click. [naturalScroll] = true → content follows fingers.
 *
 *  Everything — moves, scroll, zoom, and both taps — runs in ONE pointer loop (no
 *  separate detectTapGestures) so a two-finger tap can't also fire a stray left-click. */
private fun Modifier.trackpadInput(
    onMove: (Float, Float) -> Unit,
    onScroll: (Int, Int) -> Unit,
    onZoom: (Int) -> Unit,
    onClick: () -> Unit,
    onRightClick: () -> Unit,
    onSwitchStep: (Boolean) -> Unit,
    onSwitchEnd: () -> Unit,
    onBrowserNav: (Boolean) -> Unit,
    naturalScroll: () -> Boolean,
): Modifier = this.pointerInput(Unit) {
    val slop = viewConfiguration.touchSlop
    val tapMs = viewConfiguration.longPressTimeoutMillis
    val acc = floatArrayOf(0f, 0f)  // scroll x, y accumulators
    var swipeAcc = 0f               // three-finger horizontal travel since last notch
    var navAcc = 0f                 // two-finger horizontal travel since last back/forward
    var zoomAcc = 0f                // two-finger spread change since last zoom notch
    var switching = false           // Alt-Tab session open on the laptop
    // Per-touch session state — drives tap classification + gesture latches.
    var active = false              // a touch sequence is in progress
    var downMs = 0L                 // when the first finger landed
    var maxFingers = 0              // peak simultaneous fingers this sequence
    var travel = 0f                 // total finger travel this sequence
    var moved = false               // fired a move/scroll/zoom/nav/switch ⇒ not a tap
    var twoHold = 0                 // consecutive 2-finger frames (flicker guard)
    var pinchMode = 0               // 0 undecided · 1 zoom · -1 scroll/nav — latched until <2 fingers
    var latchGap = 0f               // accumulated net gap change while undecided
    var latchPanX = 0f              // accumulated net centroid x-travel while undecided
    var latchPanY = 0f              // accumulated net centroid y-travel while undecided
    awaitPointerEventScope {
        while (true) {
            val event = awaitPointerEvent()
            val pressed = event.changes.filter { it.pressed }
            val n = pressed.size
            if (n > 0 && !active) {                 // sequence starts
                active = true
                downMs = event.changes.first().uptimeMillis
                maxFingers = 0; travel = 0f; moved = false; twoHold = 0; pinchMode = 0
                latchGap = 0f; latchPanX = 0f; latchPanY = 0f
                acc[0] = 0f; acc[1] = 0f; swipeAcc = 0f; navAcc = 0f; zoomAcc = 0f
            }
            if (n > maxFingers) maxFingers = n
            when {
                // Three fingers cycle apps; hold through finger-count flicker until every
                // finger lifts. Only the all-up branch commits (releases Alt).
                n == 3 || (switching && n > 0) -> {
                    moved = true
                    if (n == 3) {
                        val three = pressed.take(3)
                        val dx = three.sumOf {
                            (it.position.x - it.previousPosition.x).toDouble()
                        }.toFloat() / 3f
                        swipeAcc += dx
                        while (swipeAcc >= SWIPE_APP_PX) { onSwitchStep(true); switching = true; swipeAcc -= SWIPE_APP_PX }
                        while (swipeAcc <= -SWIPE_APP_PX) { onSwitchStep(false); switching = true; swipeAcc += SWIPE_APP_PX }
                    }
                    acc[0] = 0f; acc[1] = 0f; navAcc = 0f; zoomAcc = 0f; twoHold = 0
                    pressed.forEach { it.consume() }
                }
                n == 2 -> {
                    twoHold++
                    // Swallow the FIRST 2-finger frame: filters the 1↔2 flicker capacitive
                    // screens throw mid-drag, so a stray blip can't jerk a scroll or break a move.
                    if (twoHold < 2) {
                        pressed.forEach { it.consume() }
                    } else {
                        val two = pressed.take(2)
                        val a0 = two[0]; val a1 = two[1]
                        val curGap = hypot((a0.position.x - a1.position.x).toDouble(),
                            (a0.position.y - a1.position.y).toDouble()).toFloat()
                        val prevGap = hypot((a0.previousPosition.x - a1.previousPosition.x).toDouble(),
                            (a0.previousPosition.y - a1.previousPosition.y).toDouble()).toFloat()
                        val dGap = curGap - prevGap
                        val dx = two.sumOf { (it.position.x - it.previousPosition.x).toDouble() }.toFloat() / 2f
                        val dy = two.sumOf { (it.position.y - it.previousPosition.y).toDouble() }.toFloat() / 2f
                        travel += abs(dx) + abs(dy)
                        // Latch into zoom OR scroll once one clearly dominates, then stay there
                        // until the fingers lift — no per-frame flip-flop (that was the judder).
                        // Decide on ACCUMULATED evidence, not a single frame: sum net spread vs
                        // net travel over the opening frames so one wobbly first frame (uneven
                        // finger landing, slight rotation) can't mis-latch a back-swipe to zoom.
                        if (pinchMode == 0) {
                            latchGap += dGap
                            latchPanX += dx
                            latchPanY += dy
                            val spread = abs(latchGap)
                            val pan = hypot(latchPanX.toDouble(), latchPanY.toDouble()).toFloat()
                            if (spread >= PINCH_LATCH_PX && spread > pan * ZOOM_BIAS) pinchMode = 1
                            else if (pan >= SCROLL_LATCH_PX) pinchMode = -1
                        }
                        if (pinchMode == 1) {
                            moved = true
                            zoomAcc += dGap
                            while (zoomAcc >= ZOOM_STEP_PX) { onZoom(1); zoomAcc -= ZOOM_STEP_PX }    // spread → zoom in
                            while (zoomAcc <= -ZOOM_STEP_PX) { onZoom(-1); zoomAcc += ZOOM_STEP_PX }  // pinch → zoom out
                        } else if (pinchMode == -1) {
                            moved = true
                            // m = -1 makes content follow the fingers (natural); +1 = reverse.
                            val m = if (naturalScroll()) -1 else 1
                            acc[1] += dy
                            while (acc[1] <= -SCROLL_STEP_PX) { onScroll(0, m); acc[1] += SCROLL_STEP_PX }
                            while (acc[1] >= SCROLL_STEP_PX) { onScroll(0, -m); acc[1] -= SCROLL_STEP_PX }
                            if (abs(dx) > abs(dy)) navAcc += dx
                            while (navAcc >= SWIPE_NAV_PX) { onBrowserNav(true); navAcc -= SWIPE_NAV_PX }   // swipe right → forward
                            while (navAcc <= -SWIPE_NAV_PX) { onBrowserNav(false); navAcc += SWIPE_NAV_PX }  // swipe left → back
                        }
                        pressed.forEach { it.consume() }
                    }
                }
                n == 1 -> {
                    twoHold = 0
                    val ch = pressed[0]
                    val dx = ch.position.x - ch.previousPosition.x
                    val dy = ch.position.y - ch.previousPosition.y
                    travel += abs(dx) + abs(dy)
                    if (dx != 0f || dy != 0f) {
                        onMove(dx, dy)
                        if (travel > slop) moved = true   // past slop ⇒ a drag, not a tap
                        ch.consume()
                    }
                }
                else -> {
                    // All fingers up: classify the finished sequence as a tap, if it was one.
                    if (active) {
                        val up = event.changes.maxOfOrNull { it.uptimeMillis } ?: downMs
                        val quick = (up - downMs) < tapMs
                        if (!moved && travel < slop && quick) {
                            if (maxFingers >= 2) onRightClick() else onClick()
                        }
                        if (switching) { onSwitchEnd(); switching = false }
                        active = false
                    }
                    acc[0] = 0f; acc[1] = 0f; swipeAcc = 0f; navAcc = 0f; zoomAcc = 0f
                    twoHold = 0; pinchMode = 0
                    latchGap = 0f; latchPanX = 0f; latchPanY = 0f
                }
            }
        }
    }
}

/** Draws a dense hexagonal dot lattice — the tactile grid texture of the trackpad. */
private fun Modifier.hexDots(color: Color): Modifier = drawBehind {
    val s = 20.dp.toPx()             // horizontal dot spacing
    val rh = s * 0.8660254f          // row height (sin 60°) → hexagonal packing
    val r = 1.6.dp.toPx()            // dot radius
    var row = 0
    var y = 0f
    while (y <= size.height + rh) {
        val xOff = if (row % 2 == 0) 0f else s / 2f
        var x = xOff
        while (x <= size.width + s) {
            drawCircle(color = color, radius = r, center = Offset(x, y))
            x += s
        }
        y += rh
        row++
    }
}

@Composable
private fun TrackpadCard(modifier: Modifier, a: ControlActions, natural: Boolean) {
    Card(
        modifier = modifier,
        shape = RoundedCornerShape(28.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHighest,
        ),
    ) {
        Row(Modifier.fillMaxSize().padding(8.dp)) {
            // Bare tactile field: hex-dot lattice on a recessed (darker) surface, no
            // hint text. The grid + inset colour do the talking.
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .clip(RoundedCornerShape(22.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .hexDots(MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.22f))
                    .trackpadInput(a.onMove, a.onScroll, a.onZoom, a.onClick, a.onRightClick,
                        a.onSwitchStep, a.onSwitchEnd, a.onBrowserNav, { natural }),
            )
            Spacer(Modifier.width(14.dp))
            ScrollStrip(a.onScroll)
        }
    }
}

/** A real scrollbar: a recessed track with a raised thumb that follows the drag and
 *  springs back to centre on release. It's a RATE scroller (the laptop's scroll
 *  position is unknown), so the thumb is a relative grip, not a document map. */
@Composable
private fun ScrollStrip(onScroll: (Int, Int) -> Unit) {
    val acc = remember { floatArrayOf(0f) }
    val scope = rememberCoroutineScope()
    val density = LocalDensity.current
    val thumbHalfPx = with(density) { 30.dp.toPx() }   // half the 60dp thumb
    val padPx = with(density) { 40.dp.toPx() }          // keep the thumb clear of the chevrons
    var boxH by remember { mutableIntStateOf(0) }
    var thumbOff by remember { mutableFloatStateOf(0f) }   // px from centre, + = down
    val onSecondary = MaterialTheme.colorScheme.onSecondaryContainer

    Box(
        modifier = Modifier
            .width(52.dp)
            .fillMaxHeight()
            .clip(RoundedCornerShape(26.dp))
            .background(MaterialTheme.colorScheme.secondaryContainer)
            .onSizeChanged { boxH = it.height }
            .pointerInput(Unit) {
                detectVerticalDragGestures(
                    onDragEnd = { scope.launch { animate(thumbOff, 0f) { v, _ -> thumbOff = v } } },
                    onDragCancel = { scope.launch { animate(thumbOff, 0f) { v, _ -> thumbOff = v } } },
                ) { change, dy ->
                    change.consume()
                    acc[0] += dy
                    // Inverted: drag down = scroll up, drag up = scroll down.
                    while (acc[0] <= -SCROLL_STEP_PX) { onScroll(0, -1); acc[0] += SCROLL_STEP_PX }
                    while (acc[0] >= SCROLL_STEP_PX) { onScroll(0, 1); acc[0] -= SCROLL_STEP_PX }
                    // Thumb rides the finger within the track, clamped to the groove.
                    val max = (boxH / 2f - thumbHalfPx - padPx).coerceAtLeast(0f)
                    thumbOff = (thumbOff + dy).coerceIn(-max, max)
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        // Recessed groove.
        Box(
            modifier = Modifier
                .fillMaxHeight()
                .padding(vertical = 34.dp)
                .width(8.dp)
                .clip(RoundedCornerShape(50))
                .background(onSecondary.copy(alpha = 0.16f)),
        )
        // Raised thumb.
        Box(
            modifier = Modifier
                .offset { IntOffset(0, thumbOff.roundToInt()) }
                .width(14.dp).height(60.dp)
                .clip(RoundedCornerShape(50))
                .background(onSecondary.copy(alpha = 0.85f)),
        )
        // Direction hints, pinned top/bottom.
        Column(
            modifier = Modifier.fillMaxHeight().padding(vertical = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.SpaceBetween,
        ) {
            Icon(Icons.Filled.KeyboardArrowUp, contentDescription = "Scroll up",
                tint = onSecondary.copy(alpha = 0.8f))
            Icon(Icons.Filled.KeyboardArrowDown, contentDescription = "Scroll down",
                tint = onSecondary.copy(alpha = 0.8f))
        }
    }
}

// ---------------------------------------------------------------------------
// Fullscreen trackpad
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun FullscreenTrackpad(state: UiState, a: ControlActions, onExit: () -> Unit) {
    // Frame tone matches the compact card so the recessed pad reads the same way.
    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.surfaceContainerHighest)) {
        // Same tactile pad + real scrollbar as the compact view, now full-bleed. (Two-finger
        // drag still scrolls too — the strip is just an explicit alternative.)
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .statusBarsPadding()
                .padding(12.dp),
        ) {
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .clip(RoundedCornerShape(24.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .hexDots(MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.22f))
                    .trackpadInput(a.onMove, a.onScroll, a.onZoom, a.onClick, a.onRightClick,
                        a.onSwitchStep, a.onSwitchEnd, a.onBrowserNav, { state.settings.naturalScroll }),
            )
            Spacer(Modifier.width(14.dp))
            ScrollStrip(a.onScroll)
        }
        // Same click bar as the home page — the connected Left/Middle/Right group +
        // hold-drag — reused in the dead space below the pad instead of a bespoke
        // floating toolbar, so both views feel identical. Exit rides along as the
        // trailing affordance. Sits clear of the gesture-nav area.
        ClickBar(
            a = a,
            modifier = Modifier
                .navigationBarsPadding()
                .padding(horizontal = 12.dp, vertical = 12.dp),
            trailing = {
                FilledTonalIconButton(onClick = onExit) {
                    Icon(Icons.Filled.FullscreenExit, contentDescription = "Exit fullscreen")
                }
            },
        )
    }
}

// ---------------------------------------------------------------------------
// Advanced sheet: shortcuts, system, presentation.
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class,
    ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun AdvancedSheet(state: UiState, a: ControlActions, onDismiss: () -> Unit) {
    var paste by rememberSaveable { mutableStateOf("") }

    @Composable
    fun chip(label: String, action: () -> Unit) = ChipBtn(label) { a.onButtonTap(); action() }

    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.padding(horizontal = 20.dp).padding(bottom = 28.dp)) {
            // Display — brightness, only when the laptop reports a backend. Grouped up top
            // as a level control (mirrors Volume's card on the main page).
            if (state.brightnessAvailable) {
                SheetTitle("Display")
                LevelCard(
                    icon = Icons.Filled.BrightnessHigh,
                    label = "Brightness",
                    value = state.brightness,
                    onChange = a.onBrightness,
                    onStep = { d -> a.onButtonTap(); a.onBrightness((state.brightness + d).coerceIn(0f, 100f)) },
                )
                Spacer(Modifier.height(20.dp))
            }

            // Clipboard — the paste-to-laptop field alongside the copy/cut/paste combos,
            // since they're all clipboard actions.
            SheetTitle("Clipboard")
            // Sends the text to the laptop clipboard and pastes it in one shot —
            // far faster than per-character typing for URLs, snippets, passwords.
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = paste, onValueChange = { paste = it },
                    label = { Text("Text to paste on laptop") },
                    leadingIcon = { Icon(Icons.Filled.ContentPaste, contentDescription = null) },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(autoCorrectEnabled = false),
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                PressIconButton(
                    onClick = {
                        if (paste.isNotEmpty()) { a.onButtonTap(); a.onPaste(paste); paste = "" }
                    },
                    size = 52.dp,
                    shape = RoundedCornerShape(18.dp),
                    filled = true,
                ) { Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send paste") }
            }
            Spacer(Modifier.height(10.dp))
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                chip("Copy") { a.onCombo("ctrl c") }
                chip("Cut") { a.onCombo("ctrl x") }
                chip("Paste") { a.onCombo("ctrl v") }
            }

            // Editing — history actions, separated from clipboard so each group is one idea.
            Spacer(Modifier.height(20.dp))
            SheetTitle("Editing")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                chip("Undo") { a.onCombo("ctrl z") }
                chip("Redo") { a.onCombo("ctrl y") }
            }

            Spacer(Modifier.height(20.dp))
            SheetTitle("System")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                chip("Lock") { a.onSystem("lock") }
                chip("Sleep") { a.onSystem("sleep") }
                chip("Mute") { a.onSystem("mute") }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Settings sheet.
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SettingsSheet(state: UiState, a: ControlActions, onDismiss: () -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.padding(horizontal = 20.dp).padding(bottom = 28.dp)) {
            // Pointer — everything that shapes cursor motion.
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

            Spacer(Modifier.height(16.dp))
            SheetTitle("Feedback")
            ToggleRow("Haptic feedback", "Vibrate on clicks and scroll",
                state.settings.haptics, a.onHaptics)
        }
    }
}

@Composable
private fun ToggleRow(title: String, subtitle: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun SheetTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(bottom = 10.dp))
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun ChipBtn(label: String, onClick: () -> Unit) {
    FilledTonalButton(onClick = onClick, shapes = ButtonDefaults.shapes()) { Text(label) }
}

@Composable
private fun SectionCard(content: @Composable androidx.compose.foundation.layout.ColumnScope.() -> Unit) {
    Card(
        shape = RoundedCornerShape(24.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
        ),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(20.dp), content = content)
    }
}
