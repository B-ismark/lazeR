# UX, redundancy and user-flow findings — 2026-09-23

Covers the Android client (Compose) and the laptop side: the Tk GUI, CLI flags,
launchers and docs. Every finding comes from reading code and docs. **Nothing was looked
at on a screen**: no device, no emulator, no running GUI, and no TalkBack pass.

## Surface inventory (short)

**Phone:**
- **Connection screen:** update card, saved devices (tap to connect, trash to delete),
  "Found on your network" with **Scan**, collapsed "Enter manually", an error line, and a
  pinned "Scan QR to connect".
- **Reconnecting screen:** spinner. Scan QR appears only after 90 s. Cancel.
- **Control screen (portrait):**
  - Top bar: back arrow (disconnect), Tune (Advanced), gear with dot (Settings), Fullscreen.
  - Media/Keys toggle.
  - Trackpad with scroll strip.
  - L/M/R click bar and "Hold drag".
- **Landscape deck:** a rail with the same actions.
- **Advanced sheet:** brightness; copy, cut, paste, undo, redo; Lock, Sleep, Mute.
- **Settings sheet:** speed, acceleration, natural scroll, strip side, haptics, updates.
- **Gestures:**
  - 1 finger: tap = left click.
  - 2 fingers: tap = right click; pan = scroll; pinch = zoom.
  - 3 fingers: swipe = Alt-Tab.

**Laptop:**
- **Header:** static "● server running".
- **Hero card:** hostname and status.
- **Banners:** pause banner (Resume / Quit) and firewall banner (Allow / Dismiss).
- **Connect card:** QR, token chip + Copy, "No scanner? Enter this code".
- **Connected card:** "Show QR code".
- **Collapsed "Show details":**
  - IP / port / "Full code" / "Also at".
  - Five pills.
  - Require encryption, Regenerate, the panic hint, Start with Windows.
  - The **activity log**.
- **Tray:** Show / Quit.
- **CLI:** `--no-gui --secure-only --no-update-check --allow-plaintext --resume
  --setup-firewall --enable-startup --disable-startup`.
- **Launch surfaces (7):**
  - `LazeR.exe`
  - `LazeR.bat`
  - `run_windows.ps1`
  - `run_unix.sh`
  - `Create LazeR Shortcut.ps1`
  - `Add to Startup.ps1`
  - the GUI "Start with Windows" toggle (Run key)

---

# A. Redundant or conflicting surfaces

## U-1 [HIGH] Typed-code pairing is offered in six places, fails by default, and is blamed on the firewall
**Files:**
- Phone:
  - `RemoteViewModel.kt:333-337` (`connectManual` always sends plaintext, `key = ""`)
  - `:346-352` (`useDiscovered`)
  - `:436-440` (failure text)
  - `ConnectionScreen.kt:143-146`, `:158-165`, `:286`
- Laptop:
  - `remote_server.py:4102` (secure by default)
  - `:2644-2646` (refusal explained only in the log)
  - `:3291` ("No scanner? Enter this code in the app")
- Docs: `dist/START_HERE.md:59-61`, `README.md:18-21`, `:124-126`

**Issue:** Encryption is required by default, and the typed-code path is plaintext, so
the server **silently drops** it. Yet it is still offered in six places:
- the phone's "Enter manually" card
- the phone's "Found on your network → Use"
- the laptop's main connect card
- START_HERE (as two of its three ways to connect)
- README's "How it works"
- README's client steps

When it fails:
- **Phone:** "*${ip} is on your network but didn't answer. Check LazeR is running on the
  laptop and that you allowed its firewall prompt…*". The manual card says only that
  manual entry is plaintext, never that the laptop must allow it.
- **Laptop:** the one line that explains it ("*…Require encryption is on — scan the QR
  instead (or restart with --allow-plaintext)*") goes into the activity log, which lives
  inside the collapsed details panel. It also says "restart with a flag", although the
  GUI has a live toggle.

Related redundancy on the phone:
- "Use" on a discovered host only fills hidden fields and shows "*Enter the token for X*"
  in **error red**, below a collapsed card. The tap appears to do nothing.
- The discovered list isn't filtered against saved devices, so an already-paired laptop
  appears twice: once as a working saved row, once as a "Use" row that leads into this
  failure.

**Impact:** The second path that both screens offer is a dead end by default, and it is
misdiagnosed as a network problem. Phones without Google Play services can't use the
QR scanner, so they have no working first-time path at all.

**Suggested fix:**
- Make QR the only first-time path in the UI and the docs.
- On the laptop, hide the code chip (or mark it "disabled — encryption required") while
  encryption is required.
- On the phone, relabel manual entry as "Advanced: laptop must allow unencrypted
  pairing". "Use" on a discovered host should say "Scan this laptop's QR".
- Mark discovered hosts that match a saved device as "Saved", or hide them.
- Tell the phone about the refusal (see U-6).

## U-2 [MEDIUM] The `dist/` launcher family runs a copy of the server that isn't tracked or shipped, and is stale on this machine
**Files:** `dist/LazeR.bat:4-8`, `dist/run_windows.ps1:101,115`, `dist/run_unix.sh:61`, `dist/Create LazeR Shortcut.ps1:30`, `.gitignore:28`, `.github/workflows/release.yml:190-193`, `tools/build_exe.ps1:130`
**Issue:** Four tracked scripts run `dist/server/remote_server.py`, which is gitignored.
The release ships only `LazeR.exe` and `LazeR.apk`: not START_HERE.md, which
build_exe.ps1 says to ship, and not the scripts. START_HERE:9-14 describes a folder no
user receives. [code] The local untracked copy here is `APP_VERSION = "2.1.1"` against
`2.2.0` in `server/`. It also has a different identity (`dist/.lazer_*`), so a Desktop
shortcut created by these scripts runs an old server that the phone's saved pairing may
not match.
**Suggested fix:** Pick one: either ship the scripts with START_HERE as a bundle, or retire
them. Keep one Windows entry point (the exe) and one source path (`cd server`).

## U-3 [MEDIUM] Two autostart mechanisms that contradict each other
**Files:** `dist/Add to Startup.ps1` (Startup-folder `.lnk`), `remote_server.py:1895-1957` (Run key; `_remove_legacy_lnk`), `:4045` (help text)
**Issue:** START_HERE:30 recommends the `.lnk` script, which the server treats as
legacy. `startup_enabled()` reads only the Run key, so after running the script the GUI
toggle shows **Off** while LazeR starts at login anyway. The `--enable-startup` help says
"(Startup folder)", but the code writes `HKCU\…\Run`.
**Suggested fix:** Delete `Add to Startup.ps1`, fix the help text and START_HERE.

## U-4 [LOW] Laptop GUI: redundant or static status indicators
**File:** `remote_server.py:3005-3008`, `:3743`, `:3341`, `:3298`, `:3532`, `:3546-3551`
**Issue:**
- "● server running" in the header is hard-coded green. The code itself notes the loop
  can be dead while it's lit (`:2757-2759`).
- The Encryption pill is static.
- The connected state is shown three times: the hero dot/text, the "Phone connected"
  card, and a log line.
- The same token appears as the chip and as "Full code" in details, which suggests the
  chip is partial. It is called "Token", "Pairing code" and "code" in different places.
- The "Auto-discovery" pill turns green when zeroconf **imports**, not when registration
  succeeds.

**Suggested fix:** Tie the header dot to loop health, or remove it. Drive the discovery
pill from `net["zc"]`. Show the code once, under one name.

## U-5 [LOW] Phone: naming collisions and repeated labels
**File:** `ConnectionScreen.kt:134-137` / `:202`, `:475`, `ControlScreen.kt:1339-1383`
**Issue:**
- Two buttons are called "Scan": one runs an mDNS refresh, the other opens the camera.
  The empty-state copy uses both in one sentence.
- "Tap to connect" is repeated on every saved row, although the whole card is clickable.
- "Advanced" (Tune icon) holds actions, not settings, and sits next to "Settings".
- Mute lives under "System" in Advanced, while volume is on the main screen.
- Brightness is buried in Advanced.

**Suggested fix:** Rename the first "Scan" to "Refresh". Drop the per-row label. Rename
Advanced to "Shortcuts" or "More". Put Mute next to volume.

### Looks redundant but is justified (not reported)
- **The update notice appears three ways** (a card, a dot on Settings, a status line in
  Settings). Daily users auto-connect straight to the trackpad and never see the connect
  screen; the dot leads to the only place that shows failure status. Turning the check
  off clears all three.
- **The volume slider plus ± buttons:** fine versus quick adjustment.
- **The scroll strip plus two-finger scroll:** the strip is an explicit alternative.
- **Fullscreen exit via both a button and back:** standard.
- **The delete confirmation:** it protects the pairing.
- **`--secure-only`:** obsolete but harmless. It wins a conflict with `--allow-plaintext`,
  so old scripts keep working. Could print a deprecation notice.

---

# B. Flow gaps

## U-6 [HIGH] The phone can't tell connect-failure reasons apart
**File:** `RemoteClient.kt:38-164` (`connect` returns Boolean), `RemoteViewModel.kt:436-440`, `:617-625`
**Issue:** All of these produce the same "didn't answer… firewall… client isolation" text:
- wrong token or key (the laptop was re-paired)
- plaintext refused (U-1)
- firewall rule missing or inert under policy
- laptop asleep
- wrong IP

The hint "*If you re-paired the laptop, scan its new QR*" is appended only inside the
reconnect loop, never for a direct tap or the auto-connect at launch. CLAUDE.md already
calls this trap "indistinguishable from a firewall or network problem".
**Impact:** This is the main support dead end: users chase the network when the fix is
a rescan or a toggle.
**Suggested fix:** Have the server answer a *well-formed but refused* HELLO (known token,
plaintext refused; or secure HELLO, wrong key) with a distinct reply, rate-limited and
revealing nothing secret, so the phone can say "Laptop requires QR pairing" or "This
laptop was re-paired — scan its QR". At minimum, add the re-pair hint to every failure
message. Weigh the reply against the no-reply-to-strangers property (S-section, "sound").

## U-7 [MEDIUM] Every laptop-side warning goes only into a collapsed log, where per-action lines bury it
**File:** `remote_server.py:3789-3790` (warn → log only), `:3567` (log built inside collapsed details), `:2373` (`_ACTION_LABELS`), `:3739` (400-line cap)
**Issue:** These all appear nowhere else:
- plaintext refused
- "possible brute-force / flood"
- "Still no network address after waking"
- "Receive loop crashed and was restarted"
- blocked takeover attempts
- firewall advice

Nothing badges "Show details" when a warning arrives. Meanwhile every click, keystroke
count and volume step is logged too, so warnings scroll out of a 400-line buffer.
**Suggested fix:** Show warnings on the main screen (hero or banner), badge the details
toggle, and keep routine actions out of the log or filter them.

## U-8 [MEDIUM] Auto-connect at launch tries once and never enters the reconnect loop
**File:** `RemoteViewModel.kt:199-200`, `:409-412`, `:663-664`
**Issue:** At launch, `connectSaved` runs once. On failure the state goes to
Disconnected with an error, and `kickReconnect` does nothing unless the state is
Reconnecting.
**Scenario:** Android killed the app overnight. The user opens it while the laptop is
still waking, gets a firewall-flavoured error, and must tap the device by hand. That
breaks CLAUDE.md's "no restart and no re-tap" rule for the process-death case.
**Suggested fix:** Route a failed auto-connect at launch into the reconnect loop.

## U-9 [MEDIUM] The Reconnecting screen hides the re-pair action, lets a scan error overwrite the diagnosis, and closes the app on back
**File:** `MainActivity.kt:197-242` (Scan QR only when `hint != null`), `RemoteViewModel.kt:53` (`RECONNECT_EXPLAIN_MS = 90_000`), `MainActivity.kt:156` (`onError = vm::reportError`)
**Issue:**
- Scan QR is hidden for 90 s.
- A scan error ("Unrecognized QR code") writes into `state.error`, the same field that
  holds the hint, so it replaces the diagnosis. It then sits next to "Still trying —
  it will reconnect by itself…".
- There is no BackHandler, so system back exits the app instead of cancelling.

**Suggested fix:** Always offer Scan QR as a secondary action. Keep scan errors separate
from the hint. Map back to Cancel.

## U-10 [MEDIUM] Settings, including the only internet toggle, are reachable only while connected
**File:** `ControlScreen.kt:364-366` (SettingsSheet is invoked only here), `RemoteViewModel.kt:201` (update check runs at first launch)
**Issue:** A privacy-minded user can't turn off the GitHub check before pairing, and it
has already run by then. Update status and preferences can't be viewed from the connect
screen.
**Suggested fix:** Add a Settings entry to the connect screen.

## U-11 [MEDIUM] "Sleep" and "Lock" fire on one tap, and the aftermath is misdiagnosed
**File:** `ControlScreen.kt:1378` (`chip("Sleep") { a.onSystem("sleep") }`), `RemoteViewModel.kt:617-625`
**Issue:** There is no confirmation. After a deliberate Sleep the phone goes to
Reconnecting and, after 90 s, blames the firewall or network.
**Suggested fix:** Confirm Sleep, or give it an undo window. After a Sleep the user asked
for, say "Laptop is asleep", not a network diagnosis.

## U-12 [MEDIUM] The control screen never says which laptop is connected
**File:** `ControlScreen.kt:316` (hard-coded `Text("LazeR")`); the rail shows no name
**Issue:** `state.name` is available but unused there. Users with several saved laptops
can't confirm which one they're driving.
**Suggested fix:** Show the device name in the top bar and the rail.

## U-13 [MEDIUM] The exe is windowed, so its CLI flags and startup notices print to nowhere
**File:** `tools/build_exe.ps1:80` (`--windowed`); `print` everywhere; README:173 advertises `LazeR.exe --resume`
**Issue:** These produce no visible output:
- `--resume`, `--setup-firewall`, `--enable-startup`, `--help`
- the startup notices "[network] default route is X…", "[security] plaintext … ALLOWED"
  and "[discovery] mDNS failed"

`LazeR.exe --no-gui` starts a server with no window, no tray and no way to quit short of
Task Manager. The terminal panic hint tells users to run `LazeR.exe --resume`.
[inferred] From the PyInstaller flag, not observed.
**Suggested fix:** `AttachConsole(ATTACH_PARENT_PROCESS)` for the CLI actions, or a message
box. Route startup notices into the GUI. Refuse `--no-gui` in a windowed build, or give
it a tray icon.

## U-14 [MEDIUM] The firewall and VPN check runs once at launch, and its indicators go stale
**File:** `remote_server.py:2899` (`_check_firewall` only in `__init__`), `:3782` (netchange handler doesn't re-check), `:3113` (Dismiss), `:3144`, `:3152`, `:3248`
**Issue:**
- Moving to a Public or policy-locked network mid-session produces no banner. Project
  memory records this as the #1 real-world failure ("Public-WiFi relay trap").
- The VPN is mentioned only when the rule is *already* bad. That contradicts README:108-109,
  "The server flags an active VPN in the activity log".
- After Dismiss, the banner can't come back until restart.
- The pill says "blocked — click Allow above" even when the Allow button is hidden (policy
  lock) or dismissed.

**Suggested fix:** Re-probe on netchange and on wake. Make the pill text specific to the
state, and let the pill reopen the banner. Log the VPN whenever one is present.

## U-15 [MEDIUM] Off Windows, the "your mouse always wins" safety claim is false
**File:** `remote_server.py:1073` (`LocalInputGuard.start()` returns early off Windows), `:3604` (the panic hint shown on every platform), README:167-169, START_HERE:140-143
**Issue:** macOS and Linux have neither local takeover nor the panic chord, yet the GUI
shows "Panic: press Ctrl+Alt+Shift+L…" and both docs promise that local input wins, with
no caveat. CI stubs pynput, so no real non-Windows run is ever exercised.
`run_unix.sh` also has stale comments ("pynput only") and doesn't install psutil.
**Suggested fix:** Label macOS/Linux as experimental. Hide the panic hint there and state
the limitation.

## U-16 [LOW] Tray and autostart behaviour
**File:** `remote_server.py:3804-3806` (`_on_close` → `withdraw()`), `:3058` (the only in-window Quit is on the pause banner), `:1920` (autostart registers the bare exe), `:69` (`QR_HIDE_AFTER_MS`)
**Issue:**
- Closing the window hides it to the tray, with no notice the first time.
- Quit is reachable only from the tray, or from the pause banner.
- Start with Windows opens the full window at every login, with the QR (token + key)
  visible for up to 5 minutes on a possibly unattended screen.

**Suggested fix:** Show a one-time "still running in the tray" notice. Add Quit to the
main UI. Add a `--minimized` flag for the autostart entry.

## U-17 [LOW] The two disconnect paths have different safety
**File:** `ControlScreen.kt:254-268` (back: "Press back again to disconnect"), `:318-321` and rail `:1206-1209` (the arrow disconnects on one tap)
**Issue:** The double-back guard against accidental exit is bypassed by an arrow at the
top-left edge. Both paths clear `lastDeviceId`, so the next launch won't auto-connect.
**Suggested fix:** Same behaviour for both: a confirm step, or an undo snackbar.

## U-18 [LOW] Zoom and app-switch are gesture-only and taught nowhere in the app
**File:** `ControlScreen.kt:827-828` (pinch), `:777-786` (3-finger swipe), `:934-935` (the trackpad deliberately shows no hint text)
**Issue:** Right-click, scroll and drag all have on-screen alternatives; these two don't.
They are documented only in README:29.
**Suggested fix:** A one-time gesture hint. Optional Zoom ± and "Switch app" chips in Advanced.

## U-19 [LOW] Missing states on the connection screen
**File:** `ConnectionScreen.kt:148-152`, `Discovery.kt:29`, `ConnectionScreen.kt:167-174`, `:120`, `:189`, `RemoteViewModel.kt:487-503`
**Issue:**
- "No laptop found yet…" shows immediately, with no searching state, and
  `onStartDiscoveryFailed` is empty, so "searching", "failed" and "none" look identical.
- The error line sits at the end of the scroll content and can land off-screen with
  several saved devices. [inferred]
- There is no Cancel while connecting, and `connectResolving` can try many hosts at 2 s each.

**Suggested fix:** Show a searching indicator and a failure message, pin errors near the
action (or use a snackbar), and add a Cancel button.

## U-20 [LOW] Rotating the pairing is hard to find and incomplete
**File:** `remote_server.py:3423-3434`; argparse has no rotate option
**Issue:**
- Regenerate exists only inside the collapsed details panel. Headless users have no way
  to rotate except deleting the `.lazer_*` files.
- After Regenerate the hero stays "Connected · encrypted" until the 12 s idle drop.
- A failed write is reported as success; see D-1.

**Suggested fix:** Add a `--regenerate` flag. Update the hero immediately. Put a "Kick
phone / new code" action on the Connected card.

## U-21 [LOW] Copy that misleads or gives no next step
**Files:** `remote_server.py:3351`, `:3784`, `:3901`, README:84 ("just rescan"); `:3183`, `:4086` ("UAC declined?"); `:3639` (startup failure logged as `info`); `:2766` (raw verb and exception names); `:2450`; `dist/run_windows.ps1:69` ("allow Python through Windows Firewall")
**Issue:**
- **Reconnect copy:** it says rescanning is needed after an IP change, but the phone
  re-resolves over mDNS and saves the new IP (`RemoteViewModel.kt:396-400`). A rescan is
  needed only when mDNS is blocked.
- **Error strings:** several guess the cause, or show internal names such as "VGET
  failed and was ignored (TypeError: …)".
- **Firewall advice:** the Python-specific advice doesn't apply to exe users.

**Suggested fix:** Fix the reconnect wording. Give every error a next step. Map internal
verbs to user words.

## U-22 [LOW] Accessibility
**Files:**
- Laptop: every button is a `tk.Label` with a click binding (e.g. `:3052-3060`, `:3305-3308`); the window is not resizable (`:2849`).
- Phone: `ConnectionScreen.kt:478`; `ControlScreen.kt:669-671`, `:1064-1067`.

**Issue:**
- **Laptop:** no focus, no Tab, no Enter.
- **Phone:**
  - Delete is announced as "Delete" without the device name.
  - The compact click bar reads "L", "M", "R".
  - The scroll-strip chevrons are announced as "Scroll up/down" but aren't tappable.

Icon buttons otherwise have descriptions, and M3 enforces 48 dp targets.
**Suggested fix:**
- Laptop: use `tk.Button`/`ttk.Button`, or add focus and key bindings.
- Phone: "Delete <name>", "Left click" and so on, and either make the chevrons tappable
  or mark them decorative.

## U-23 [LOW] The docs drift from the code
**Files:** README.md, PROTOCOL.md, dist/START_HERE.md, CLAUDE.md:3
**Issue:**
- **Wire version:** README and PROTOCOL say "v2 wire", but the only accepted magic is
  L3 (`remote_server.py:715`). PROTOCOL:90 says L2 is "accepted through v2.x", yet it has
  already been removed in 2.2.0.
- **Token lifetime:** "per-session token" (README:182, CLAUDE.md:3, `run_windows.ps1:50`,
  `remote_server.py:1996`) contradicts "token + key are persistent" (README:175).
- **Token location:** README:175 and PROTOCOL:171 place the token files in `server/`,
  which is wrong for the exe.
- **QR delivery:** START_HERE says the server "prints" the QR and the IP/token. The GUI
  shows them in the window.
- **Security claims in PROTOCOL.md** that the code doesn't meet:
  - "wrong-token packets are silently dropped" (false in plaintext mode; S-1)
  - the reply-replay guarantee (false for the first reply; S-4)
  - a malformed `k` (not covered; S-6)
  - the flood pause is skipped when `aes is None` (not mentioned)

**Suggested fix:** One pass across the four docs. Fix the PROTOCOL.md security claims
together with S-1, S-4 and S-6.
