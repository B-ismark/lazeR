package com.example.lanremote.ui

import android.app.Activity
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
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
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
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
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberModalBottomSheetState
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
import androidx.compose.ui.text.style.TextAlign
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
private const val ZOOM_STEP_PX = 36f     // two-finger spread change per ctrl+wheel zoom notch

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
                colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
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
// Controls: volume + segmented Media/Keyboard (basic, always visible).
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ControlsPanel(state: UiState, a: ControlActions) {
    var tab by rememberSaveable { mutableIntStateOf(0) } // 0 = Media, 1 = Keyboard

    Column {
        LevelCard(
            icon = Icons.AutoMirrored.Filled.VolumeUp,
            label = "Volume",
            value = state.volume,
            onChange = a.onVolume,
            onStep = { d -> a.onButtonTap(); a.onVolume((state.volume + d).coerceIn(0f, 100f)) },
        )

        // Brightness lives in the Advanced sheet (Tune icon) to keep this page short.

        Spacer(Modifier.height(12.dp))

        SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
            SegmentedButton(
                selected = tab == 0, onClick = { tab = 0 },
                shape = SegmentedButtonDefaults.itemShape(index = 0, count = 2),
                icon = {},
                label = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.Headphones, contentDescription = null,
                            modifier = Modifier.size(18.dp))
                        Text("  Media")
                    }
                },
            )
            SegmentedButton(
                selected = tab == 1, onClick = { tab = 1 },
                shape = SegmentedButtonDefaults.itemShape(index = 1, count = 2),
                icon = {},
                label = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.Keyboard, contentDescription = null,
                            modifier = Modifier.size(18.dp))
                        Text("  Keyboard")
                    }
                },
            )
        }

        Spacer(Modifier.height(12.dp))

        AnimatedContent(
            targetState = tab,
            transitionSpec = { fadeIn() togetherWith fadeOut() },
            label = "panel",
        ) { which ->
            if (which == 0) MediaPanel(a)
            else KeyboardPanel(state, a)
        }
    }
}

/** Volume / brightness card: icon + percent, with −/＋ nudge buttons flanking the slider. */
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
            Icon(icon, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
            Text("  $label  ${value.toInt()}%", style = MaterialTheme.typography.titleMedium)
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            FilledTonalIconButton(onClick = { onStep(-5f) }, modifier = Modifier.size(40.dp)) {
                Icon(Icons.Filled.Remove, contentDescription = "$label down")
            }
            Slider(value = value, onValueChange = onChange, valueRange = 0f..100f,
                modifier = Modifier.weight(1f).padding(horizontal = 8.dp))
            FilledTonalIconButton(onClick = { onStep(5f) }, modifier = Modifier.size(40.dp)) {
                Icon(Icons.Filled.Add, contentDescription = "$label up")
            }
        }
    }
}

@Composable
private fun MediaPanel(a: ControlActions) {
    fun fire(action: String) { a.onButtonTap(); a.onMedia(action) }
    SectionCard {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(14.dp, Alignment.CenterHorizontally),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            FilledTonalIconButton(
                onClick = { fire("prev") },
                modifier = Modifier.size(64.dp), shape = RoundedCornerShape(20.dp),
            ) { Icon(Icons.Filled.SkipPrevious, "Previous", modifier = Modifier.size(32.dp)) }
            FilledIconButton(
                onClick = { fire("play_pause") },
                modifier = Modifier.size(92.dp), shape = RoundedCornerShape(32.dp),
            ) { Icon(Icons.Filled.PauseCircle, "Play / Pause", modifier = Modifier.size(46.dp)) }
            FilledTonalIconButton(
                onClick = { fire("next") },
                modifier = Modifier.size(64.dp), shape = RoundedCornerShape(20.dp),
            ) { Icon(Icons.Filled.SkipNext, "Next", modifier = Modifier.size(32.dp)) }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun KeyboardPanel(state: UiState, a: ControlActions) {
    fun special(name: String) { a.onButtonTap(); a.onSpecialKey(name) }
    SectionCard {
        OutlinedTextField(
            value = state.keyboardText, onValueChange = a.onKeyboardInput,
            label = { Text("Type on laptop") }, singleLine = true,
            keyboardOptions = KeyboardOptions(autoCorrectEnabled = false),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilledTonalIconButton(onClick = { special("backspace") }) {
                Icon(Icons.AutoMirrored.Outlined.Backspace, contentDescription = "Backspace")
            }
            FilledTonalButton(onClick = { special("space") }) { Text("Space") }
            FilledTonalButton(onClick = { special("tab") }) { Text("Tab") }
            FilledTonalButton(onClick = { special("esc") }) { Text("Esc") }
            FilledTonalButton(onClick = { special("enter") }) { Text("Enter") }
        }
    }
}

// ---------------------------------------------------------------------------
// Click bar: explicit Left / Middle / Right + hold-to-drag.
// ---------------------------------------------------------------------------
@Composable
private fun ClickBar(a: ControlActions) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        FilledTonalButton(onClick = a.onClick, modifier = Modifier.weight(1f)) { Text("Left") }
        FilledTonalButton(onClick = a.onMiddleClick, modifier = Modifier.weight(1f)) { Text("Middle") }
        FilledTonalButton(onClick = a.onRightClick, modifier = Modifier.weight(1f)) { Text("Right") }
        HoldDragButton(a.onDragStart, a.onDragEnd, modifier = Modifier.weight(1.2f))
    }
}

/** While held, the left button stays pressed — drag on the trackpad to move/select. */
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
 *  back-forward · 3 fingers left/right = switch apps · tap/hold = click.
 *  [naturalScroll] = true → content follows fingers (touchscreen feel). */
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
): Modifier = this
    .pointerInput(Unit) {
        val acc = floatArrayOf(0f, 0f)  // x, y
        var swipeAcc = 0f               // three-finger horizontal travel since last notch
        var navAcc = 0f                 // two-finger horizontal travel since last back/forward
        var zoomAcc = 0f                // two-finger spread change since last zoom notch
        var switching = false           // Alt-Tab session open on the laptop
        awaitPointerEventScope {
            while (true) {
                val event = awaitPointerEvent()
                val pressed = event.changes.filter { it.pressed }
                when {
                    // Three fingers left/right cycle apps: each notch taps Tab while Alt is
                    // held on the laptop — full Windows-style forward cycle. Once a switch is
                    // open, hold through finger-count flicker (3→2→3 is common on capacitive
                    // screens): don't scroll/move and don't release Alt until every finger
                    // lifts. Only the `else` branch commits (releases Alt).
                    pressed.size == 3 || (switching && pressed.isNotEmpty()) -> {
                        if (pressed.size == 3) {
                            val three = pressed.take(3)
                            val dx = three.sumOf {
                                (it.position.x - it.previousPosition.x).toDouble()
                            }.toFloat() / 3f
                            swipeAcc += dx
                            while (swipeAcc >= SWIPE_APP_PX) { onSwitchStep(true); switching = true; swipeAcc -= SWIPE_APP_PX }
                            while (swipeAcc <= -SWIPE_APP_PX) { onSwitchStep(false); switching = true; swipeAcc += SWIPE_APP_PX }
                        }
                        acc[0] = 0f; acc[1] = 0f; navAcc = 0f; zoomAcc = 0f
                        pressed.forEach { it.consume() }
                    }
                    pressed.size == 2 -> {
                        val two = pressed.take(2)
                        // Pinch test first: change in the gap between the two fingers. If
                        // the spread is changing faster than the pair is translating, it's
                        // a zoom (ctrl+wheel) — suppress scroll/nav so the two don't fight.
                        val a0 = two[0]; val a1 = two[1]
                        val curGap = hypot((a0.position.x - a1.position.x).toDouble(),
                            (a0.position.y - a1.position.y).toDouble()).toFloat()
                        val prevGap = hypot((a0.previousPosition.x - a1.previousPosition.x).toDouble(),
                            (a0.previousPosition.y - a1.previousPosition.y).toDouble()).toFloat()
                        val dGap = curGap - prevGap
                        val dx = two.sumOf { (it.position.x - it.previousPosition.x).toDouble() }.toFloat() / 2f
                        val dy = two.sumOf { (it.position.y - it.previousPosition.y).toDouble() }.toFloat() / 2f
                        if (abs(dGap) > abs(dx) && abs(dGap) > abs(dy)) {
                            zoomAcc += dGap
                            while (zoomAcc >= ZOOM_STEP_PX) { onZoom(1); zoomAcc -= ZOOM_STEP_PX }    // spread → zoom in
                            while (zoomAcc <= -ZOOM_STEP_PX) { onZoom(-1); zoomAcc += ZOOM_STEP_PX }  // pinch → zoom out
                            acc[1] = 0f; navAcc = 0f
                        } else {
                            zoomAcc = 0f
                            // m = -1 makes content follow the fingers (natural); +1 = reverse.
                            val m = if (naturalScroll()) -1 else 1
                            // Vertical → scroll. Horizontal → browser back/forward (like a
                            // Windows touchpad two-finger swipe). Per-event axis check keeps a
                            // vertical scroll from drifting into an accidental nav.
                            acc[1] += dy
                            while (acc[1] <= -SCROLL_STEP_PX) { onScroll(0, m); acc[1] += SCROLL_STEP_PX }
                            while (acc[1] >= SCROLL_STEP_PX) { onScroll(0, -m); acc[1] -= SCROLL_STEP_PX }
                            if (abs(dx) > abs(dy)) navAcc += dx
                            while (navAcc >= SWIPE_NAV_PX) { onBrowserNav(true); navAcc -= SWIPE_NAV_PX }   // swipe right → forward
                            while (navAcc <= -SWIPE_NAV_PX) { onBrowserNav(false); navAcc += SWIPE_NAV_PX }  // swipe left → back
                        }
                        pressed.forEach { it.consume() }
                    }
                    pressed.size == 1 -> {
                        val ch = pressed[0]
                        val dx = ch.position.x - ch.previousPosition.x
                        val dy = ch.position.y - ch.previousPosition.y
                        if (dx != 0f || dy != 0f) { onMove(dx, dy); ch.consume() }
                    }
                    else -> {
                        acc[0] = 0f; acc[1] = 0f; swipeAcc = 0f; navAcc = 0f; zoomAcc = 0f
                        if (switching) { onSwitchEnd(); switching = false }
                    }
                }
            }
        }
    }
    .pointerInput(Unit) {
        detectTapGestures(onTap = { onClick() }, onLongPress = { onRightClick() })
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
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .trackpadInput(a.onMove, a.onScroll, a.onZoom, a.onClick, a.onRightClick,
                        a.onSwitchStep, a.onSwitchEnd, a.onBrowserNav, { natural }),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    "Drag to move · Two fingers scroll, pinch to zoom, swipe ⇄ for back/forward · Three fingers switch apps\nTap = click · Hold = right-click",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.55f),
                    textAlign = TextAlign.Center,
                )
            }
            Spacer(Modifier.width(8.dp))
            ScrollStrip(a.onScroll)
        }
    }
}

@Composable
private fun ScrollStrip(onScroll: (Int, Int) -> Unit) {
    val acc = remember { floatArrayOf(0f) }
    Box(
        modifier = Modifier
            .width(66.dp)
            .fillMaxHeight()
            .clip(RoundedCornerShape(28.dp))
            .background(MaterialTheme.colorScheme.secondaryContainer)
            .pointerInput(Unit) {
                detectVerticalDragGestures { change, dy ->
                    change.consume()
                    acc[0] += dy
                    // Inverted: drag down = scroll up, drag up = scroll down.
                    while (acc[0] <= -SCROLL_STEP_PX) { onScroll(0, -1); acc[0] += SCROLL_STEP_PX }
                    while (acc[0] >= SCROLL_STEP_PX) { onScroll(0, 1); acc[0] -= SCROLL_STEP_PX }
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        Column(
            modifier = Modifier.fillMaxHeight().padding(vertical = 14.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.SpaceBetween,
        ) {
            Icon(Icons.Filled.KeyboardArrowUp, contentDescription = "Scroll up",
                tint = MaterialTheme.colorScheme.onSecondaryContainer)
            Box(
                modifier = Modifier
                    .width(6.dp).height(64.dp)
                    .clip(RoundedCornerShape(50))
                    .background(MaterialTheme.colorScheme.onSecondaryContainer.copy(alpha = 0.45f)),
            )
            Icon(Icons.Filled.KeyboardArrowDown, contentDescription = "Scroll down",
                tint = MaterialTheme.colorScheme.onSecondaryContainer)
        }
    }
}

// ---------------------------------------------------------------------------
// Fullscreen trackpad
// ---------------------------------------------------------------------------
@Composable
private fun FullscreenTrackpad(state: UiState, a: ControlActions, onExit: () -> Unit) {
    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface)) {
        // Full-bleed trackpad — no scroll strip here; two-finger drag scrolls.
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .background(MaterialTheme.colorScheme.surfaceContainerHighest)
                .trackpadInput(a.onMove, a.onScroll, a.onZoom, a.onClick, a.onRightClick,
                    a.onSwitchStep, a.onSwitchEnd, a.onBrowserNav, { state.settings.naturalScroll }),
        ) {
            Text(
                "Drag to move · Two fingers scroll, pinch to zoom, swipe ⇄ for back/forward · Three fingers switch apps\nTap = click · Hold = right-click",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.45f),
                textAlign = TextAlign.Center,
                modifier = Modifier.align(Alignment.Center),
            )
            FilledTonalIconButton(
                onClick = onExit,
                modifier = Modifier.align(Alignment.TopEnd).statusBarsPadding().padding(16.dp),
            ) { Icon(Icons.Filled.FullscreenExit, contentDescription = "Exit fullscreen") }
        }
        // Click bar pinned at the bottom, clear of the gesture nav area.
        Box(
            Modifier
                .navigationBarsPadding()
                .padding(horizontal = 12.dp)
                .padding(top = 8.dp, bottom = 20.dp),
        ) {
            ClickBar(a)
        }
    }
}

// ---------------------------------------------------------------------------
// Advanced sheet: shortcuts, system, presentation.
// ---------------------------------------------------------------------------
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun AdvancedSheet(state: UiState, a: ControlActions, onDismiss: () -> Unit) {
    var paste by rememberSaveable { mutableStateOf("") }

    @Composable
    fun chip(label: String, action: () -> Unit) = ChipBtn(label) { a.onButtonTap(); action() }

    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.padding(horizontal = 20.dp).padding(bottom = 28.dp)) {
            // Brightness (only when the laptop reports a backend) — kept here so the
            // main control page stays compact.
            if (state.brightnessAvailable) {
                SheetTitle("Brightness")
                LevelCard(
                    icon = Icons.Filled.BrightnessHigh,
                    label = "Brightness",
                    value = state.brightness,
                    onChange = a.onBrightness,
                    onStep = { d -> a.onButtonTap(); a.onBrightness((state.brightness + d).coerceIn(0f, 100f)) },
                )
                Spacer(Modifier.height(20.dp))
            }

            SheetTitle("Paste text")
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
                FilledIconButton(
                    onClick = {
                        if (paste.isNotEmpty()) { a.onButtonTap(); a.onPaste(paste); paste = "" }
                    },
                    modifier = Modifier.size(52.dp),
                ) { Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send paste") }
            }

            Spacer(Modifier.height(20.dp))
            SheetTitle("Shortcuts")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                chip("Copy") { a.onCombo("ctrl c") }
                chip("Paste") { a.onCombo("ctrl v") }
                chip("Cut") { a.onCombo("ctrl x") }
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
            SheetTitle("Settings")

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
            ToggleRow("Natural scrolling", "Content follows your fingers",
                state.settings.naturalScroll, a.onNaturalScroll)
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

@Composable
private fun ChipBtn(label: String, onClick: () -> Unit) {
    FilledTonalButton(onClick = onClick) { Text(label) }
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
