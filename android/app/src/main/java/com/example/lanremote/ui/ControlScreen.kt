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
import androidx.compose.material.icons.filled.KeyboardDoubleArrowLeft
import androidx.compose.material.icons.filled.KeyboardDoubleArrowRight
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
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.automirrored.outlined.Backspace
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.BrightnessHigh
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
import androidx.compose.material.icons.filled.Tune
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
import androidx.compose.runtime.DisposableEffect
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
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.example.lanremote.UiState
import com.example.lanremote.WHEEL_UNITS_PER_DETENT
import androidx.compose.ui.input.pointer.PointerId
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

private const val SCROLL_STEP_PX = 16f   // finger travel per detent — sets scroll gain + haptic cadence
// Gesture travel is measured in wheel units, not detents, so a frame's motion can be sent
// at its true resolution instead of being rounded to the nearest 120-unit hop. Same overall
// gain as before (SCROLL_STEP_PX of travel is still one detent), just no longer quantised.
private const val WHEEL_UNITS_PER_PX = WHEEL_UNITS_PER_DETENT / SCROLL_STEP_PX
private const val SWIPE_APP_PX = 120f    // three-finger horizontal travel per app switch — short hop, fits small screens
private const val ZOOM_STEP_PX = 22f     // two-finger spread change per ctrl+wheel zoom notch (finer = smoother zoom)

// ── zoom-vs-pan ───────────────────────────────────────────────────────────────
// Every earlier version of this ran a MAGNITUDE RACE: change in finger separation
// (`dGap`) versus centroid travel, whoever's bigger wins. That is unfixable, and the
// reason is geometric. Two fingers rest side by side, so the gap vector is ~(G, 0):
// a horizontal speed difference between the fingers feeds `dGap` at 1:1, while a
// vertical one feeds it at only d²/2G — for G=200px, d=3px that is 3.0px vs 0.02px,
// a 133x asymmetry. A hand sliding sideways pivots at the wrist and the fingers
// genuinely splay, so a *pure horizontal pan* produces a large, sustained, REAL gap
// change. Captured from this app's own trackpad (adb getevent, 14 deliberate
// side-drags): gap moved 295->329px over one 360px pan, mean |dGap| 3-6px/frame,
// peaks past 18px. No deadzone can filter that — it isn't noise, it's the gesture.
// The magnitude race then loses at the END of a swipe: the fingers decelerate, so
// centroid travel collapses while the splay persists, the ratio inverts, and the
// gesture flips to zoom. Measured: 2 of those 14 pans mis-fired zoom (10 frames), and
// because a flip also clears the pan accumulator, each flip silently ate up to a full
// SCROLL_STEP_PX of travel plus every frame it stayed flipped — the "stutter".
//
// The fix is a different QUESTION, not a better threshold. For per-finger motion
// vectors v0/v1, with common c=(v0+v1)/2 and differential d=(v1-v0)/2:
//     v0 · v1 = |c|² - |d|²
// so dot(v0,v1) > 0 means translation dominates — a pan — no matter how much the gap
// changed. Normalised, that dot is the cosine between the fingers' paths, which is
// scale-free and needs no per-speed tuning. Measured on the same captures: real
// pinches sit at cos ≈ -0.99, pans at cos > 0. Clean separation, so the verdict can
// be LATCHED for the gesture (no mid-drag flips, hence no dropped pan travel) rather
// than re-derived every frame from a decaying window.
// Validated against the captures: 0/14 pans mis-read as pinch, 3/3 real pinches
// caught. This is ChromeOS's touchpad approach (ImmediateInterpreter::FingersAngle +
// ZoomFingersAreConsistent); Android's ScaleGestureDetector has no pan guard at all.
// Thresholds are finger physiology, so they are physical sizes (dp ~ 1/160in), not px.
private val PINCH_MIN_TRAVEL = 13.dp     // a finger must travel this far before its direction is trustworthy (~2mm)
private val PINCH_MIN_SEP = 45.dp        // below this the finger axis is too short to split zoom from twist (~7mm)
private const val PINCH_MAX_COS = -0.4f  // pinch needs the fingers >=114 deg apart...
private const val PAN_MIN_COS = -0.2f    // ...and anything above this is definitely a pan; between = undecided
private const val PINCH_MOV_RATIO = 0.4f // the slower finger must travel >=40% as far as the faster
private const val PINCH_FRAMES = 3       // consecutive opposing frames before zoom engages
// Deliberately NO decision deadline. Undecided already pans (see below), so waiting costs
// nothing — while settling early permanently kills a slow pinch: two fingers resting a
// moment before pinching, or a pinch pivoting around one near-stationary finger, which the
// captures show taking ~15 frames to prove itself. A 100ms deadline here measurably lost
// one of the three recorded pinches. Real pans don't need it: they all latch on dot>0
// within 3-8 frames anyway.

/** Latched verdict for a two-finger gesture. Pan is the safe default: a stray scroll is
 *  far less jarring than a stray zoom, and it keeps panning responsive from frame one. */
private const val MODE_UNDECIDED = 0
private const val MODE_PAN = 1
private const val MODE_PINCH = 2

/** All the actions the control screen can fire. Bundled to keep the signature sane. */
class ControlActions(
    val onMove: (Float, Float) -> Unit,
    val onScroll: (Int, Int) -> Unit,   // dx, dy in wheel units (120 = one detent)
    val onScrollDetent: () -> Unit,     // crossed a detent boundary — for haptics only
    val onZoom: (Int) -> Unit,          // two-finger pinch: +1 = zoom in (spread), -1 = zoom out (pinch)
    val onClick: () -> Unit,
    val onRightClick: () -> Unit,
    val onMiddleClick: () -> Unit,
    val onSwitchStep: (Boolean) -> Unit,  // three-finger notch: true = next app, false = previous
    val onSwitchEnd: () -> Unit,          // fingers lifted: commit the highlighted app
    val onDragStart: () -> Unit,
    val onDragEnd: () -> Unit,
    val onVolume: (Float) -> Unit,
    val onBrightness: (Float) -> Unit,
    val onMedia: (String) -> Unit,
    val onKeyboardInput: (String, String) -> Unit,   // (previous text, new text) → send the delta
    val onSpecialKey: (String) -> Unit,
    val onCombo: (String) -> Unit,
    val onSystem: (String) -> Unit,
    val onSensitivity: (Float) -> Unit,
    val onNaturalScroll: (Boolean) -> Unit,
    val onHaptics: (Boolean) -> Unit,
    val onAcceleration: (Boolean) -> Unit,
    val onUpdateCheck: (Boolean) -> Unit,
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
    // Leaving the screen entirely never flipped `fullscreen` back, so a link that
    // dropped while expanded (the health loop declaring the laptop dead) left the
    // connection screen rendering underneath hidden system bars.
    DisposableEffect(Unit) {
        onDispose {
            val window = (view.context as? Activity)?.window
            if (window != null) {
                WindowCompat.getInsetsController(window, view)
                    .show(WindowInsetsCompat.Type.systemBars())
            }
        }
    }
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
            // KeyboardPanel takes no UiState on purpose — it owns its own text, so
            // nothing an unrelated state change does can disturb the IME session.
            if (which == 0) MediaPanel(state, a)
            else KeyboardPanel(a)
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
                // Seek back / forward send Left / Right arrow — the focused app decides
                // the step (YouTube ~5s, Netflix/VLC/Disney+ ~10s), so no seconds label:
                // a "10" would be wrong half the time. Double CHEVRONS, not the filled
                // FastRewind/FastForward triangles these used to be — those read as almost
                // the same shape as the SkipPrevious/SkipNext triangles beside them, and
                // people kept hitting seek when they meant next track. Each button morphs
                // its corner (rounded → circle) on press — the M3 Expressive feel.
                PressIconButton(onClick = { seek("left") }, size = 44.dp, shape = RoundedCornerShape(16.dp)) {
                    Icon(Icons.Filled.KeyboardDoubleArrowLeft, "Seek back",
                        modifier = Modifier.size(26.dp))
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
                    Icon(Icons.Filled.KeyboardDoubleArrowRight, "Seek forward",
                        modifier = Modifier.size(26.dp))
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
private fun KeyboardPanel(a: ControlActions) {
    // The text field owns its editing state, as a TextFieldValue rather than a String.
    //
    // This is the fix for text vanishing from the phone while the laptop kept it.
    // The value used to come from the shared UiState, which the health loop rewrites
    // every 1.5-4s for volume/brightness sync. A plain String can't carry selection
    // or the IME's *composing region*, so each of those unrelated recompositions
    // re-fed the field and Gboard's uncommitted (underlined) text was discarded —
    // while onKeyboardInput had already put those characters on the wire.
    //
    // Keeping the state local and typed means recomposition is now a no-op for the
    // field: it re-reads the same object, so the IME session is left untouched.
    var buffer by rememberSaveable(stateSaver = TextFieldValue.Saver) {
        mutableStateOf(TextFieldValue(""))
    }

    /** Replace the buffer, keeping the caret at the end. */
    fun setBuffer(text: String) {
        buffer = TextFieldValue(text, selection = TextRange(text.length))
    }

    // The buffer is a staging copy of what we've sent since the last commit, so the
    // diff in onKeyboardInput always has a truthful `old`. Keys that can be
    // represented as text edit it; keys that can't (enter/tab/esc/newline move focus
    // or commit) reset it, because after those the laptop's caret is somewhere the
    // buffer can no longer describe. Previously none of these touched it at all, so
    // the buffer drifted and the next keystroke diffed against a stale string —
    // firing a spurious backspace-and-retype burst.
    fun backspace() {
        a.onButtonTap()
        // Always send, even when our buffer is already empty: after an Enter the
        // buffer is cleared but the laptop still has text the user may want to delete.
        a.onSpecialKey("backspace")
        if (buffer.text.isNotEmpty()) setBuffer(buffer.text.dropLast(1))
    }

    fun space() {
        a.onButtonTap()
        a.onSpecialKey("space")
        setBuffer(buffer.text + " ")
    }

    fun commitKey(name: String) {
        a.onButtonTap()
        a.onSpecialKey(name)
        setBuffer("")
    }

    fun commitCombo(spec: String) {
        a.onButtonTap()
        a.onCombo(spec)
        setBuffer("")
    }

    SectionCard {
        OutlinedTextField(
            value = buffer,
            onValueChange = { next ->
                // Send the delta first, then adopt the new value verbatim so
                // selection and composition are preserved exactly as the IME set them.
                a.onKeyboardInput(buffer.text, next.text)
                buffer = next
            },
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
            FilledTonalButton(onClick = { backspace() }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Icon(Icons.AutoMirrored.Outlined.Backspace, contentDescription = "Backspace",
                    modifier = Modifier.size(20.dp))
            }
            FilledTonalButton(onClick = { space() }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Space", maxLines = 1)
            }
            FilledTonalButton(onClick = { commitKey("tab") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Tab", maxLines = 1)
            }
            FilledTonalButton(onClick = { commitKey("esc") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Esc", maxLines = 1)
            }
            FilledTonalButton(onClick = { commitKey("enter") }, shapes = ButtonDefaults.shapes(),
                contentPadding = keyPad, modifier = Modifier.weight(1f)) {
                Text("Enter", maxLines = 1)
            }
            // Shift+Enter: soft newline without submitting — near-universal (chat apps,
            // editors), unlike Alt+Enter which varies by app. Shown as the return glyph.
            FilledTonalButton(onClick = { commitCombo("shift enter") }, shapes = ButtonDefaults.shapes(),
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

/** 1 finger = move · 2 fingers = pan/scroll (both axes, like a real trackpad — the far
 *  end decides what that means: canvas pan in Figma, scroll in most apps, swipe-nav in a
 *  browser) · pinch = zoom · 3 fingers left/right = switch apps.
 *  Tap = left-click · two-finger tap = right-click. Hold does nothing, so a resting
 *  hand never fires a click. [naturalScroll] = true → content follows fingers.
 *
 *  Everything — moves, scroll, zoom, and both taps — runs in ONE pointer loop (no
 *  separate detectTapGestures) so a two-finger tap can't also fire a stray left-click. */
private fun Modifier.trackpadInput(
    onMove: (Float, Float) -> Unit,
    onScroll: (Int, Int) -> Unit,        // wheel units (120 = one detent)
    onScrollDetent: () -> Unit,          // detent boundary crossed — haptics only
    onZoom: (Int) -> Unit,
    onClick: () -> Unit,
    onRightClick: () -> Unit,
    onSwitchStep: (Boolean) -> Unit,
    onSwitchEnd: () -> Unit,
    naturalScroll: () -> Boolean,
): Modifier = this.pointerInput(Unit) {
    val slop = viewConfiguration.touchSlop
    val tapMs = viewConfiguration.longPressTimeoutMillis
    val acc = floatArrayOf(0f, 0f)  // pan x, y accumulators, in wheel units
    var detentAcc = 0f              // units since the last haptic tick
    var swipeAcc = 0f               // three-finger horizontal travel since last notch
    var zoomAcc = 0f                // two-finger spread change since last zoom notch
    var switching = false           // Alt-Tab session open on the laptop
    // Per-touch session state — drives tap classification + gesture latches.
    var active = false              // a touch sequence is in progress
    var downMs = 0L                 // when the first finger landed
    var maxFingers = 0              // peak simultaneous fingers this sequence
    var travel = 0f                 // total finger travel this sequence
    var moved = false               // dragged past slop ⇒ not a tap
    var twoHold = 0                 // consecutive 2-finger frames (flicker guard)
    var oneHold = 0                 // consecutive 1-finger frames (flicker guard)
    var threeHold = 0               // consecutive 3-finger frames (flicker guard)
    // Two-finger classifier state. The verdict is measured against where the two fingers
    // STARTED, not frame to frame: the cosine of two ~1px-quantised per-frame deltas is
    // mostly digitiser noise, while displacement-from-start grows into a clean signal.
    val minTravelSq = (PINCH_MIN_TRAVEL.toPx()).let { it * it }
    val minSepPx = PINCH_MIN_SEP.toPx()
    var mode = MODE_UNDECIDED       // latched pan/pinch verdict for the current finger pair
    var pinchFrames = 0             // consecutive frames the fingers have clearly opposed
    var id0 = PointerId(-1L)        // the pair being tracked — a change means a new gesture
    var id1 = PointerId(-1L)
    var b0x = 0f; var b0y = 0f      // baseline position of finger 0...
    var b1x = 0f; var b1y = 0f      // ...and finger 1
    var axUx = 0f; var axUy = 0f    // unit vector along the finger-to-finger axis at baseline
    var startSep = 0f               // finger separation at baseline
    awaitPointerEventScope {
        while (true) {
            val event = awaitPointerEvent()
            val pressed = event.changes.filter { it.pressed }
            val n = pressed.size
            if (n > 0 && !active) {                 // sequence starts
                active = true
                downMs = event.changes.first().uptimeMillis
                maxFingers = 0; travel = 0f; moved = false
                twoHold = 0; oneHold = 0; threeHold = 0
                mode = MODE_UNDECIDED; pinchFrames = 0
                id0 = PointerId(-1L); id1 = PointerId(-1L)
                acc[0] = 0f; acc[1] = 0f; swipeAcc = 0f; zoomAcc = 0f; detentAcc = 0f
            }
            if (n > maxFingers) maxFingers = n
            when {
                // Three fingers cycle apps; hold through finger-count flicker until every
                // finger lifts. Only the all-up branch commits (releases Alt).
                n == 3 || (switching && n > 0) -> {
                    threeHold++
                    // Same flicker guard the 2-finger branch gets. A sideways two-finger drag
                    // rotates the hand, so the pad picks up stray third contacts — and without
                    // this, one such frame wiped the pan accumulators mid-pan (losing up to a
                    // full step of travel) and fed its dx into swipeAcc, which can latch
                    // Alt-Tab for the rest of the gesture. Hold for a second frame first.
                    if (threeHold < 2 && !switching) {
                        pressed.forEach { it.consume() }
                    } else {
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
                        acc[0] = 0f; acc[1] = 0f; zoomAcc = 0f; twoHold = 0
                        pressed.forEach { it.consume() }
                    }
                }
                n == 2 -> {
                    twoHold++; oneHold = 0; threeHold = 0
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
                        if (travel > slop) moved = true   // past slop ⇒ a drag, not a tap

                        // (Re)baseline when the tracked pair changes — a finger lifting and
                        // landing again is a new gesture and must get a fresh verdict.
                        if (a0.id != id0 || a1.id != id1) {
                            id0 = a0.id; id1 = a1.id
                            b0x = a0.position.x; b0y = a0.position.y
                            b1x = a1.position.x; b1y = a1.position.y
                            startSep = hypot((b1x - b0x).toDouble(), (b1y - b0y).toDouble()).toFloat()
                            axUx = if (startSep > 1e-3f) (b1x - b0x) / startSep else 0f
                            axUy = if (startSep > 1e-3f) (b1y - b0y) / startSep else 0f
                            mode = MODE_UNDECIDED; pinchFrames = 0
                        }

                        // Classify off displacement from the baseline (see the constants block).
                        // Nothing here re-litigates a settled verdict — that is what stops the
                        // mid-drag flips that were eating pan travel.
                        if (mode == MODE_UNDECIDED) {
                            val d0x = a0.position.x - b0x; val d0y = a0.position.y - b0y
                            val d1x = a1.position.x - b1x; val d1y = a1.position.y - b1y
                            val m0 = d0x * d0x + d0y * d0y
                            val m1 = d1x * d1x + d1y * d1y
                            if (max(m0, m1) < minTravelSq) {
                                // Too little travel to read intent — keep waiting (and panning).
                            } else {
                                val dot = d0x * d1x + d0y * d1y
                                if (dot > 0f) {
                                    mode = MODE_PAN   // |common| > |differential| ⇒ pan, decisively
                                } else if (min(m0, m1) < minTravelSq) {
                                    // Opposing, but the slower finger hasn't moved far enough to
                                    // trust. WAIT rather than settle — calling pan here is what
                                    // breaks pinches, since one finger always crosses first.
                                } else {
                                    val cos = dot / sqrt((m0.toDouble() * m1.toDouble())).toFloat()
                                    val lo = min(m0, m1); val hi = max(m0, m1)
                                    // Rotation moves the fingers oppositely too, but TANGENTIALLY;
                                    // only motion along the finger axis is a zoom.
                                    var radialOk = true
                                    if (startSep > minSepPx) {
                                        val rx = d1x - d0x; val ry = d1y - d0y
                                        val radial = rx * axUx + ry * axUy
                                        val tangential = hypot((rx - radial * axUx).toDouble(),
                                            (ry - radial * axUy).toDouble()).toFloat()
                                        radialOk = abs(radial) >= tangential
                                    }
                                    if (cos >= PAN_MIN_COS || !radialOk) {
                                        mode = MODE_PAN
                                    } else if (cos <= PINCH_MAX_COS &&
                                        lo > hi * PINCH_MOV_RATIO * PINCH_MOV_RATIO) {
                                        if (++pinchFrames >= PINCH_FRAMES) {
                                            mode = MODE_PINCH
                                            acc[0] = 0f; acc[1] = 0f   // drop pan carry from the undecided frames
                                        }
                                    } else {
                                        pinchFrames = 0                // in the dead band, or one finger idling
                                    }
                                }
                            }
                        }

                        // Undecided pans immediately: scrolling stays responsive from frame one,
                        // and the worst case is a few px of scroll leaking into a pinch's opening.
                        if (mode == MODE_PINCH) {
                            zoomAcc += dGap
                            while (zoomAcc >= ZOOM_STEP_PX) { onZoom(1); zoomAcc -= ZOOM_STEP_PX }    // spread → zoom in
                            while (zoomAcc <= -ZOOM_STEP_PX) { onZoom(-1); zoomAcc += ZOOM_STEP_PX }  // pinch → zoom out
                        } else {
                            // Both axes, like a real trackpad — the app on the other end decides
                            // what scroll means (Figma pans the canvas, browsers scroll or swipe-
                            // navigate, most everything else just scrolls).
                            // m = -1 makes content follow the fingers (natural); +1 = reverse.
                            // Mind the wheel sign conventions, which differ per axis: a POSITIVE
                            // vertical step is the wheel rolling forward = scroll up = content
                            // moves down, but a POSITIVE horizontal step tilts right = viewport
                            // moves right = content moves LEFT. So the two axes need opposite
                            // signs to agree on "content follows the fingers". The horizontal
                            // pair used to mirror the vertical one, which left it inverted — it
                            // panned against the drag and ignored the natural-scroll setting.
                            val m = if (naturalScroll()) -1 else 1
                            // ONE packet per frame carrying the frame's exact travel, rather
                            // than a burst of whole-detent notches. Quantising here was the
                            // other half of the "jitter": a detent is 120 wheel units, so a
                            // normal drag fired ~31 discrete 120-unit hops a second, and the
                            // count per frame alternated (1,1,2,1,1,2...) as the accumulator
                            // crossed the boundary — uneven cadence even though the total
                            // distance was right. Sub-detent units make it a smooth stream,
                            // and per-frame batching means FEWER packets than before, not more.
                            // Fractional remainders carry, so no travel is lost or invented.
                            acc[0] += dx * WHEEL_UNITS_PER_PX * m
                            acc[1] += dy * WHEEL_UNITS_PER_PX * -m
                            val ux = acc[0].toInt()   // toward zero; remainder stays banked
                            val uy = acc[1].toInt()
                            if (ux != 0 || uy != 0) {
                                acc[0] -= ux
                                acc[1] -= uy
                                onScroll(ux, uy)
                                // Detents are now only a haptic concept: tick once per
                                // boundary crossed so the pad keeps its old feel.
                                detentAcc += abs(ux) + abs(uy)
                                while (detentAcc >= WHEEL_UNITS_PER_DETENT) {
                                    onScrollDetent()
                                    detentAcc -= WHEEL_UNITS_PER_DETENT
                                }
                            }
                        }
                        pressed.forEach { it.consume() }
                    }
                }
                n == 1 -> {
                    oneHold++; threeHold = 0
                    // A one-frame drop to a single contact in the middle of a two-finger pan is
                    // flicker, not intent. Treating it as a cursor move jerked the pointer
                    // mid-pan, and zeroing twoHold made the pan swallow ANOTHER frame on the way
                    // back in (so every blip cost two frames of travel). Hold both off until the
                    // single finger has actually persisted. A genuine one-finger drag is
                    // unaffected: maxFingers is 1, so it still moves from the first frame.
                    val settled = maxFingers < 2 || oneHold >= 2
                    if (settled) twoHold = 0
                    val ch = pressed[0]
                    val dx = ch.position.x - ch.previousPosition.x
                    val dy = ch.position.y - ch.previousPosition.y
                    if (!settled) {
                        ch.consume()
                    } else {
                        travel += abs(dx) + abs(dy)
                        if (dx != 0f || dy != 0f) {
                            onMove(dx, dy)
                            if (travel > slop) moved = true   // past slop ⇒ a drag, not a tap
                            ch.consume()
                        }
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
                    acc[0] = 0f; acc[1] = 0f; swipeAcc = 0f; zoomAcc = 0f
                    twoHold = 0; oneHold = 0; threeHold = 0
                    mode = MODE_UNDECIDED; pinchFrames = 0
                    id0 = PointerId(-1L); id1 = PointerId(-1L)
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
                    .trackpadInput(a.onMove, a.onScroll, a.onScrollDetent, a.onZoom, a.onClick, a.onRightClick,
                        a.onSwitchStep, a.onSwitchEnd, { natural }),
            )
            Spacer(Modifier.width(14.dp))
            ScrollStrip(a.onScroll, a.onScrollDetent)
        }
    }
}

/** A real scrollbar: a recessed track with a raised thumb that follows the drag and
 *  springs back to centre on release. It's a RATE scroller (the laptop's scroll
 *  position is unknown), so the thumb is a relative grip, not a document map. */
@Composable
private fun ScrollStrip(onScroll: (Int, Int) -> Unit, onDetent: () -> Unit) {
    val acc = remember { floatArrayOf(0f) }
    val detentAcc = remember { floatArrayOf(0f) }
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
                    // Inverted: drag down = scroll up, drag up = scroll down. Same
                    // wheel-unit stream as the trackpad (see WHEEL_UNITS_PER_PX) so the
                    // strip scrolls just as smoothly; detents survive only as haptics.
                    acc[0] += dy * WHEEL_UNITS_PER_PX
                    val u = acc[0].toInt()
                    if (u != 0) {
                        acc[0] -= u
                        onScroll(0, u)
                        detentAcc[0] += abs(u)
                        while (detentAcc[0] >= WHEEL_UNITS_PER_DETENT) {
                            onDetent()
                            detentAcc[0] -= WHEEL_UNITS_PER_DETENT
                        }
                    }
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
                    .trackpadInput(a.onMove, a.onScroll, a.onScrollDetent, a.onZoom, a.onClick, a.onRightClick,
                        a.onSwitchStep, a.onSwitchEnd, { state.settings.naturalScroll }),
            )
            Spacer(Modifier.width(14.dp))
            ScrollStrip(a.onScroll, a.onScrollDetent)
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

            // Editing — clipboard and history combos. The paste-to-laptop TEXT FIELD that
            // used to head this group is gone: a text field inside a ModalBottomSheet
            // fights the sheet over the IME insets, so focusing it made the sheet jitter
            // up and down. Nothing here needs typing, so the whole group is chips now and
            // the sheet has no text field at all to fight over.
            SheetTitle("Editing")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                chip("Copy") { a.onCombo("ctrl c") }
                chip("Cut") { a.onCombo("ctrl x") }
                chip("Paste") { a.onCombo("ctrl v") }
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

            // Called out as the internet setting on purpose: LazeR is otherwise
            // entirely LAN-only, so this is the one thing that leaves the network.
            Spacer(Modifier.height(20.dp))
            SheetTitle("Updates")
            ToggleRow(
                "Check for new versions",
                "Asks GitHub once a day whether a newer release exists — the only " +
                    "time LazeR uses the internet. Never downloads or installs " +
                    "anything; it just shows a link.",
                state.settings.updateCheck, a.onUpdateCheck,
            )
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
