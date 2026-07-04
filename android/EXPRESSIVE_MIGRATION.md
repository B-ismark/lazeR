# M3 → M3 Expressive migration (Android client)

The Compose client moved from baseline Material 3 to **Material 3 Expressive**:
springier motion, shape-morph buttons, connected button groups, morphing loading
indicators, a floating toolbar, and MaterialShapes accents. Gesture/trackpad logic
was untouched — this was a chrome-only change.

## Version constraints (read before bumping anything)

M3 Expressive APIs (`MaterialExpressiveTheme`, `MotionScheme`, `ButtonGroup`,
`LoadingIndicator`, `HorizontalFloatingToolbar`, `MaterialShapes`, the expressive
button/icon shape-morph overloads) were **removed from `material3` 1.4.0 stable** and
live **only in the `material3` 1.5.0-alpha line** behind
`@OptIn(ExperimentalMaterial3ExpressiveApi::class)`.

The alpha line's Compose floor climbs fast, which drives `compileSdk`:

| material3 | Compose (ui/foundation) | compileSdk floor |
|-----------|-------------------------|------------------|
| 1.5.0-alpha01 … **alpha12** | 1.8.x | **35** |
| 1.5.0-alpha16 | 1.11.x | 36 |
| 1.5.0-alpha20 … alpha23 | 1.12.x | 37 + AGP 9 |

We pin **`androidx.compose.material3:material3:1.5.0-alpha12`** — the newest alpha
still on the Compose 1.8/1.9 train — so the toolchain stays at compileSdk 35 / AGP 8.7.3
/ Gradle 8.9. Going to alpha16+ means bumping compileSdk (36/37), AGP (8.9/9), the
Gradle wrapper, and installing a new platform. Don't bump material3 past alpha12
without doing that whole chain.

### Resolved toolchain

| Piece | Before | After |
|-------|--------|-------|
| AGP | 8.5.2 | **8.7.3** |
| Kotlin + compose plugin | 2.0.20 | **2.1.0** |
| Gradle wrapper | 8.9 | 8.9 (unchanged) |
| compileSdk | 34 | **35** |
| targetSdk | 34 | 34 (unchanged — no Android 15 runtime opt-in) |
| Compose BOM | 2024.09.02 | **2025.10.00** (Compose 1.9.x) |
| material3 | (BOM) 1.3.0 | **1.5.0-alpha12** (explicit override) |

`material3` is version-overridden explicitly; everything else (ui, foundation,
animation, icons) still comes from the BOM. alpha12's declared floor is Compose 1.8.1,
and the BOM's 1.9.x is a binary-compatible superset (stable→stable minor), so the
override coexists with the BOM cleanly.

## What changed, per component

| Area | Before | After |
|------|--------|-------|
| Theme | `MaterialTheme` | `MaterialExpressiveTheme` + `MotionScheme.expressive()` — `Theme.kt` |
| Media/Keyboard tabs | `SingleChoiceSegmentedButtonRow` | `ButtonGroup { toggleableItem }` (connected toggle) |
| Click bar (main) | `Row` of `FilledTonalButton` | `ButtonGroup { clickableItem }` (Left/Middle/Right) + hold-drag |
| Media transport, ±nudges, paste | hand-rolled `PressIconButton` (scale-on-press) | expressive `FilledIconButton`/`FilledTonalIconButton` with `IconButtonShapes(shape, pressedShape = Circle)` — the container morphs its corners on press |
| Spinners (Scan, Connect&save, Reconnecting) | `CircularProgressIndicator` | `LoadingIndicator` (morphing polygon) |
| Keyboard keys (Space/Tab/Esc/Enter/**New line**/backspace), chips, fullscreen clicks | plain | expressive pill `FilledTonalButton(shapes = ButtonDefaults.shapes())`. Backspace is a pill (not a shrunk icon-button — that clipped the glyph off-centre). **New line** = `COMBO shift enter` (soft newline). |
| Scan-QR primary button | `Button(shape=…)` | `Button(shapes = ButtonDefaults.shapes())` |
| Fullscreen bottom controls | separate exit button + full-width click row | `HorizontalFloatingToolbar` (clicks + hold + exit float below the pad) |
| Volume/brightness icon | bare glyph | `MaterialShapes.Cookie9Sided.toShape()` tonal badge |

The old `PressIconButton` manual scale animation (`animateFloatAsState` + `Modifier.scale`)
was deleted — the expressive shape morph replaces it.

## Gotchas hit

- **Release lint crashes.** The lint bundled with AGP 8.7.3 throws
  `IncompatibleClassChangeError` in `NonNullableMutableLiveDataDetector` when analyzing
  a project with the material3 alpha. It's a lint-tooling bug, not a code defect.
  Worked around with `android { lint { checkReleaseBuilds = false } }` in
  `app/build.gradle.kts` (debug lint still runs). Revisit when leaving the alpha.
- **`@OptIn` spread.** `clickableItem`/`toggleableItem`/`ButtonGroup`/`LoadingIndicator`/
  `HorizontalFloatingToolbar`/`ButtonDefaults.shapes()`/`IconButtonShapes`/`toShape()`
  all need `@OptIn(ExperimentalMaterial3ExpressiveApi::class)` (a couple also want
  `ExperimentalMaterial3Api`). Applied per-composable.
- **Experimental churn.** Every expressive API is alpha — signatures can move between
  alpha releases. That's the standing tax for shipping Expressive before it stabilizes.

## Not yet verified

Build is green end-to-end (compileDebug/compileRelease + R8 + package). **On-device
visual/interaction parity has not been checked** — no device/emulator was attached at
migration time. Install `app-release.apk` on a phone and eyeball: the button-group
toggles, the icon-button press morph, the loading indicators, and the fullscreen
floating toolbar. The one residual runtime risk is the alpha12-built-against-1.8.x /
run-against-1.9.x skew (low — Compose keeps binary compat across stable minors).

## Build

Same as before (see [../CLAUDE.md](../CLAUDE.md) for the off-OneDrive build dance):

```
./gradlew assembleRelease --init-script <offsync-init.gradle> --project-cache-dir <tmp>
```

Needs Android **platform-35** + **build-tools 35.0.0** installed (`sdkmanager
"platforms;android-35" "build-tools;35.0.0"`).
