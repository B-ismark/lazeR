# LazeR

Phone-as-trackpad. Android (Kotlin + Jetpack Compose, **Material 3 Expressive**) client
+ Python laptop server. Mouse move/click/scroll, pinch-zoom, system volume, display
brightness, media keys, keyboard typing and app-switch gestures.

Direct UDP on `50505` over the local network. Secure pairing (QR) is AES-256-GCM
with a replay-proof handshake.

```
android/     Jetpack Compose client
server/      Python server (pynput + socket)
PROTOCOL.md  wire format shared by both
```

## How it works

- The laptop window shows a **QR code** carrying its LAN IP, a pairing **token** and
  a 256-bit **key**, and listens on UDP `50505`.
- The phone scans it and runs an encrypted challenge-response handshake
  (`HELLO`→`CHAL`→`AUTH`→`OK`).
- The laptop pins that phone as the sole controller. Every later packet must be
  sealed with the key, carry an increasing counter **and** come from the pinned
  source, or it is dropped. (Typing the code by hand instead is an unencrypted,
  opt-in fallback — see Security notes.)
- Trackpad drags → `MOVE dx dy` (lossy UDP, low latency, with optional pointer
  acceleration). Tap → `CLICK`. Volume slider → `VOL 0-100`; brightness slider →
  `BRIGHT 0-100` (both two-way synced). Media buttons → `MEDIA play_pause|next|prev`.
- Keyboard panel types text (`KEY`); the Shortcuts sheet fires shortcuts
  (`COMBO ctrl c`, …) for copy/cut/paste and undo/redo.
- Two fingers → `SCROLL dx dy` on **both** axes, like a real trackpad; the app at the
  far end decides what that means (canvas pan, document scroll, or a browser's own
  swipe-nav). Pinch → `ZOOM` (ctrl+wheel). Three fingers left/right → cycle apps
  (`ASW next` / `prev`, Alt held the whole gesture).
- Haptic feedback on taps/keys, and gentle battery use — the phone polls briskly
  while you're interacting and backs off when idle.

See [PROTOCOL.md](PROTOCOL.md) for the packet format.

---

## Run the server (laptop)

**Windows, no Python:** download `LazeR.exe` from the
[latest release](https://github.com/B-ismark/lazeR/releases/latest) and run it. It
bundles everything.

**From source** (Python 3.8+):

```bash
cd server
uv venv && uv pip install -r requirements.txt    # or: python -m pip install -r requirements.txt
python remote_server.py
```

A window opens with the QR code to scan. Only one LazeR runs at a time: a second
launch brings the first one's window forward (or says it's running headless), and
a launch that finds UDP `50505` taken by something else says so and exits.

Useful flags (`--help` lists them all):

```bash
python remote_server.py --no-gui        # terminal mode: prints the IP, code and a text QR
python remote_server.py --minimized     # start hidden in the tray
python remote_server.py --regenerate    # new code + key; every phone must rescan
python remote_server.py --resume        # clear a panic latch in a running LazeR
```

`LazeR.exe` takes the same flags. It has no console of its own, so run it from a
Command Prompt or PowerShell to see their output.

Platform notes:
- **Windows** — volume uses `pycaw` (installed via requirements). Media/mouse work out of the box.
- **macOS** — volume uses `osascript` (built in). Grant the terminal **Accessibility**
  permission (System Settings → Privacy & Security → Accessibility) so `pynput` can move the mouse.
- **Linux** — volume uses `amixer` or `pactl` (install `alsa-utils` or `pulseaudio-utils`).
  On Wayland, `pynput` mouse control may need an X11 session.

**Firewall.** Phones reach the server over **inbound UDP `50505`**, which Windows
Defender Firewall blocks by default (the server still looks healthy locally because
loopback bypasses the firewall — only the phone times out). On Windows the GUI
detects this and shows **Allow through firewall**; one click adds the rule after a
single UAC prompt. Headless or to pre-seed it:

```bash
python remote_server.py --setup-firewall   # self-elevates, adds the rule, exits
```

On macOS/Linux, allow inbound UDP `50505` in your firewall if prompted.

**Work-managed PCs: the rule can be added and still do nothing.** Some company
policies tell Windows to **ignore locally-added firewall rules** on a given network
type — Public, usually. LazeR's rule is then created and reads back as enabled and
correct, but Windows discards it and phones keep timing out. Discovery breaks with
it, so the app finds no laptop and retries whatever address it last saved. LazeR
detects this and says so instead of reporting a healthy firewall. To confirm it
yourself:

```powershell
Get-NetFirewallProfile -PolicyStore ActiveStore | Select Name, AllowLocalFirewallRules
```

`AllowLocalFirewallRules: False` on the profile you're connected to is the tell. Fix
it by setting that Wi-Fi to **Private** under *Settings → Network & internet → Wi-Fi
→ (your network) → Network profile type*, or ask IT to allow inbound UDP `50505` by
policy. LazeR cannot override it — the setting exists specifically to stop apps
doing that.

**Start with Windows.** LazeR does not auto-launch out of the box. Turn on the
**Start with Windows** toggle in the desktop GUI, or from the command line. It starts
in the tray, so the QR (which carries the pairing key) isn't left on the screen at
login:

```bash
python remote_server.py --enable-startup    # register; appears in Task Manager → Startup apps
python remote_server.py --disable-startup    # remove it
```

This registers a per-user entry under `HKCU\…\Run` (named `LazeR`), so LazeR
launches at login and shows up under **Task Manager → Startup apps** and
**Settings → Apps → Startup** (where you can also toggle it).

**VPN.** A split-tunnel VPN is fine. A VPN that **full-tunnels or blocks LAN
traffic** will stop the phone reaching the laptop even with the firewall open — no
app can override that. Enable **"allow local network"** in the VPN client, or
disconnect it on the LAN you're controlling from. The server flags an active VPN
in the activity log to make this obvious.

---

## Run the client (Android)

Open `android/` in Android Studio (Giraffe+), let it sync, run on a device on the
**same Wi-Fi** as the laptop. Or from the command line:

```bash
cd android
gradle wrapper            # first time only, generates the wrapper
./gradlew installDebug    # device/emulator connected via adb
```

Or install `LazeR.apk` from the
[latest release](https://github.com/B-ismark/lazeR/releases/latest).

In the app:
1. Tap **Scan QR to connect** and scan the code in the laptop window. The laptop is
   saved; next time the app reconnects to it by itself.
2. Drag the trackpad to move the cursor; tap it to left-click. Use the slider for
   volume and the buttons for media. The back arrow (tap twice) disconnects.

If a connection fails the app says why when the laptop can tell it: the laptop
needs the QR (encryption is required), or it was re-paired since the phone last
connected. Otherwise it points at the network.

`minSdk 24`, `targetSdk 34`, `compileSdk 35`. UI is Material 3 Expressive (springy
motion, shape-morph buttons, connected button groups, morphing loading indicators) —
see [android/EXPRESSIVE_MIGRATION.md](android/EXPRESSIVE_MIGRATION.md) for version
constraints (material3 is pinned to a 1.5.0-alpha; don't bump it blindly).
Permissions: `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`, `WAKE_LOCK`
(the low-latency WifiLock that keeps the cursor smooth — see
[android/EXPRESSIVE_MIGRATION.md](android/EXPRESSIVE_MIGRATION.md)), `VIBRATE`, and
`REQUEST_INSTALL_PACKAGES` (installing an update you chose to download; see below).
mDNS discovery goes through `NsdManager`, which needs no multicast permission.
No CAMERA permission: QR scanning uses Google's on-device code scanner, which runs
in Play Services. No volume-button hooks.

---

## Security notes

- **Encrypted by default (QR pairing).** Scanning the QR establishes an
  **AES-256-GCM** channel: every datagram is encrypted, authenticated by its GCM
  tag (which also proves the sender holds the key — no token on the wire), and
  carries a monotonic counter the server enforces for **replay protection**. The
  handshake is **challenge-response** (`HELLO`→`CHAL`→`AUTH`), so a captured session
  can't be replayed even by a party that saw the ciphertext, and the laptop's replies
  echo a nonce from the phone's `HELLO`, so an old reply can't be replayed at the
  phone either. See the secure wire in [PROTOCOL.md](PROTOCOL.md).
- **Encryption is required by default.** Plaintext (v1) pairing by typed code is
  refused unless you opt in with `--allow-plaintext`, or turn **Require encryption**
  off in the GUI. The toggle isn't saved — each launch starts with encryption
  required unless `--allow-plaintext` is given. QR pairing always gives the phone a
  key, so the safe wire is what you get without having to know to ask for it. If a
  phone tries the typed code while encryption is required, both the phone and the
  laptop window say so rather than leaving you with an unexplained timeout.
- Acceptance is bound to one source IP:port + session; a high rate of rejected
  packets raises a brute-force/flood warning and briefly pauses **manual-code**
  (plaintext) acceptance — the only path a token guess exists against. QR-paired
  phones keep working throughout, since the secure wire authenticates by GCM tag
  and never sees a token.
- The QR encodes the token **and** the key, so whoever photographs the screen owns
  the machine until you make a new code. An idle, unpaired window therefore hides the
  code after five minutes behind a click-to-show placeholder. Pairing is unchanged:
  one click brings it back.
- **Local input wins.** Physical mouse/keyboard on the laptop (detected via
  non-injected low-level hooks) pauses the remote; **Ctrl+Alt+Shift+L** latches it off
  until you resume. So even a successful intruder can't fight your own hand. Resume
  from the GUI window, or — handy when running headless — from a second terminal:

  ```bash
  python remote_server.py --resume     # or: LazeR.exe --resume
  ```
  It exits 0 once the running copy confirms, 1 if it refused or none is running, and
  2 if it didn't answer (a copy older than this one). Run it from the running copy's
  folder: it proves itself with that install's pairing code.
- The pairing **token + key are persistent** (`.lazer_token`, `.lazer_key` next to
  the running program: `server/` from source, the exe's folder for `LazeR.exe`), are
  readable only by your Windows account, and are reused across launches.
  **Regenerate** (in Show details), **New code** (on the Connected card), or
  `--regenerate` replaces both and disconnects the phone at once. If they can't be
  saved, LazeR says so and keeps the old ones rather than pretending.
- Open/public Wi-Fi: the firewall rule LazeR adds covers **all** profiles, Public
  included: Windows classifies most Wi-Fi as Public — corporate SSIDs and plenty of
  home routers — so a Private-only rule is inert exactly where people use it. The
  port is not the security boundary anyway; the pairing key, AES-256-GCM and the
  replay-proof handshake are, so a reachable port without the QR gets an attacker
  nothing. Treat plaintext mode as untrusted on open Wi-Fi; prefer QR pairing, which
  is the default.
- **Control traffic never leaves the LAN.** The only outbound internet requests are
  the update check and an update download you start yourself — see below.

### The only internet access: updates

LazeR is LAN-only; v2.0 removed off-LAN access entirely. The exceptions are the
update check and the phone's download of an update you ask for, because a sideloaded
app has no store to notify you and would otherwise sit on a stale version
indefinitely.

- **What it does:** one HTTPS `GET` to
  `api.github.com/repos/B-ismark/lazeR/releases/latest`, reads the release tag, and
  compares it with the running version.
- **Anonymous:** a plain GET with no token, cookie or device identifier. The only
  header is the `User-Agent` GitHub requires. Nothing about you or your laptop is
  sent — GitHub sees a request, as it would for any public URL.
- **It only checks.** The check reads the release's tag and asset links, never the
  files. The laptop only ever shows a notice. On the phone, **Download & install**
  fetches the new `LazeR.apk` only when you tap it: from this project's GitHub
  releases only, checked against the `LazeR.apk.sha256` published with it (and
  checked again just before install), then handed to Android's installer, which also
  refuses any APK not signed with LazeR's own key. The first time, Android asks you to
  allow installs from LazeR. **Release page** is always there as the manual route.
- **Quiet on failure:** offline, rate-limited or GitHub down all mean "don't know",
  which never interrupts you. On the phone, **Settings → Updates** is the one place
  that says so ("Couldn't check for updates", with **Try again**), so a blocked check
  can't pass for being up to date.
- **Where the phone shows it:** a card on the connect screen (with Download &
  install), a dot on the Settings button (the app usually reconnects straight to the
  pad, past that card), and the status line under **Settings → Updates**, which also offers
  **Check now**.
- **Frequency:** server, once per launch. Phone, at most once a day on its own
  (checked on launch and whenever the app comes back to the front), or whenever you
  tap **Check now**.
- **Turning it off:** server — `--no-update-check`. Phone — **Settings → Updates →
  Check for new versions**, reachable from the connect screen too, before pairing.
  Off means the code is never called at all.

Both halves check independently and show their own notice, since the `.exe` and the
`.apk` are installed separately and can drift apart.
