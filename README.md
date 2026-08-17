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

- Server boots, prints its **LAN IP** + a random **token**, listens on UDP `50505`.
- Phone enters that IP + token, sends a `HELLO` handshake.
- Server pins that phone's IP:port as the sole controller. Every packet must carry
  the token **and** come from the pinned source, or it is silently dropped.
- Trackpad drags → `MOVE dx dy` (lossy UDP, low latency, with optional pointer
  acceleration). Tap → `CLICK`. Volume slider → `VOL 0-100`; brightness slider →
  `BRIGHT 0-100` (both two-way synced). Media buttons → `MEDIA play_pause|next|prev`.
- Keyboard panel types text (`KEY`); the Advanced sheet fires shortcuts
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

Needs Python 3.8+.

```bash
cd server
python -m pip install -r requirements.txt
python remote_server.py
```

It prints something like:

```
  Laptop IP : 192.168.1.20
  Port      : 50505
  Token     : A1B2C3
```

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

**Start with Windows.** LazeR does not auto-launch out of the box. Turn on the
**Start with Windows** toggle in the desktop GUI, or from the command line:

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

In the app:
1. Enter the laptop **IP**, leave **Port** at `50505`, enter the **token**.
2. Tap **Connect**. On success you land on the control screen.
3. Drag the trackpad to move the cursor; tap it to left-click. Use the slider for
   volume and the buttons for media. Top-right **✕** disconnects.

`minSdk 24`, `targetSdk 34`, `compileSdk 35`. UI is Material 3 Expressive (springy
motion, shape-morph buttons, connected button groups, morphing loading indicators) —
see [android/EXPRESSIVE_MIGRATION.md](android/EXPRESSIVE_MIGRATION.md) for version
constraints (material3 is pinned to a 1.5.0-alpha; don't bump it blindly).
Permissions: `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`,
`CHANGE_WIFI_MULTICAST_STATE` (mDNS discovery), `WAKE_LOCK` (the low-latency
WifiLock that keeps the cursor smooth — see
[android/EXPRESSIVE_MIGRATION.md](android/EXPRESSIVE_MIGRATION.md)) and `VIBRATE`.
No CAMERA permission: QR scanning uses Google's on-device code scanner, which runs
in Play Services. No volume-button hooks.

---

## Security notes

- **Encrypted by default (QR pairing).** Scanning the QR establishes an
  **AES-256-GCM** channel: every datagram is encrypted, authenticated by its GCM
  tag (which also proves the sender holds the key — no token on the wire), and
  carries a monotonic counter the server enforces for **replay protection**. The
  handshake is **challenge-response** (`HELLO`→`CHAL`→`AUTH`), so a captured session
  can't be replayed even by a party that saw the ciphertext. See the v2 wire in
  [PROTOCOL.md](PROTOCOL.md).
- **Encryption is required by default.** Plaintext (v1) pairing by typed code is
  refused unless you opt in with `--allow-plaintext`, or turn **Require encryption**
  off in the GUI. QR pairing always gives the phone a key, so the safe wire is what
  you get without having to know to ask for it. If a phone tries the typed code
  while encryption is required, the activity log says so rather than leaving you
  with an unexplained timeout.
- Acceptance is bound to one source IP:port + session; a high rate of rejected
  packets raises a brute-force/flood warning.
- **Local input wins.** Physical mouse/keyboard on the laptop (detected via
  non-injected low-level hooks) pauses the remote; **Ctrl+Alt+Shift+L** latches it off
  until you resume. So even a successful intruder can't fight your own hand.
- The pairing **token + key are persistent** (`server/.lazer_token`, `.lazer_key`)
  and reused across launches; **Regenerate** in the UI rotates both and kicks phones.
- Open/public Wi-Fi: the firewall rule LazeR adds covers **all** profiles, Public
  included. It used to be scoped to Private and Domain, which sounded safer and
  wasn't: Windows classifies most Wi-Fi as Public — corporate SSIDs and plenty of
  home routers — so the rule was inert exactly where people were trying to use it,
  and the port silently stayed shut. The port was never the security boundary
  anyway; the per-session token, AES-256-GCM and the replay-proof handshake are, so
  a reachable port without the QR gets an attacker nothing. Treat plaintext mode as
  untrusted on open Wi-Fi; prefer QR pairing, which is the default.
- **Control traffic never leaves the LAN.** There is exactly one outbound internet
  request in the whole product, and it isn't control-related — see below.

### The one internet request: update checks

LazeR is LAN-only; v2.0 removed off-LAN access entirely. The single exception is an
update check, because a sideloaded app has no store to notify you and would
otherwise sit on a stale version indefinitely.

- **What it does:** one HTTPS `GET` to
  `api.github.com/repos/B-ismark/lazeR/releases/latest`, reads the release tag, and
  compares it with the running version.
- **Anonymous:** a plain GET with no token, cookie or device identifier. The only
  header is the `User-Agent` GitHub requires. Nothing about you or your laptop is
  sent — GitHub sees a request, as it would for any public URL.
- **Notify-only:** it never downloads or installs anything. It shows a link to the
  release page; you download as usual. Self-updating would mean the app fetching and
  installing a binary it can't verify, which is a much larger trust ask.
- **Silent on failure:** offline, rate-limited or GitHub down all mean "don't know",
  which shows nothing rather than an error.
- **Frequency:** server, once per launch. Phone, at most once a day.
- **Turning it off:** server — `--no-update-check`. Phone — **Advanced → Updates →
  Check for new versions**. Off means the code is never called at all.

Both halves check independently and show their own notice, since the `.exe` and the
`.apk` are installed separately and can drift apart.
