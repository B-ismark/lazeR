# LAN Remote

Phone-as-trackpad over Wi-Fi. Android (Kotlin + Jetpack Compose, Material You)
client + Python laptop server. Mouse move/click, system volume, media keys.
All traffic is UDP on the local network, gated by a per-session token.

```
android/   Jetpack Compose client
server/    Python server (pynput + socket)
PROTOCOL.md  wire format shared by both
```

## How it works

- Server boots, prints its **LAN IP** + a random **token**, listens on UDP `50505`.
- Phone enters that IP + token, sends a `HELLO` handshake.
- Server pins that phone's IP:port as the sole controller. Every packet must carry
  the token **and** come from the pinned source, or it is silently dropped.
- Trackpad drags → `MOVE dx dy` (lossy UDP, low latency). Tap → `CLICK`.
  Slider → `VOL 0-100`. Buttons → `MEDIA play_pause|next|prev`.
- Two fingers vertical → scroll. Two fingers horizontal swipe → browser back/forward
  (`COMBO alt left` / `alt right`), like a Windows touchpad. Three fingers left/right →
  cycle apps (`ASW next` / `prev`, Alt held the whole gesture).

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

Open UDP `50505` in the OS firewall if prompted.

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

`minSdk 24`, `targetSdk 34`. Only `INTERNET` permission — no volume-button hooks,
no special device permissions.

---

## Security notes

- **Encrypted by default (QR pairing).** Scanning the QR establishes an
  **AES-256-GCM** channel: every datagram is encrypted, authenticated by its GCM
  tag (which also proves the sender holds the key — no token on the wire), and
  carries a monotonic counter the server enforces for **replay protection**. See the
  v2 wire in [PROTOCOL.md](PROTOCOL.md).
- **Manual code = plaintext v1**, for trusted LANs only. `--secure-only` (or the
  **Require encryption** toggle) rejects it.
- Acceptance is bound to one source IP:port + session; a high rate of rejected
  packets raises a brute-force/flood warning.
- **Local input wins.** Physical mouse/keyboard on the laptop (detected via
  non-injected low-level hooks) pauses the remote; **Ctrl+Alt+Shift+L** latches it off
  until you resume. So even a successful intruder can't fight your own hand.
- The pairing **token + key are persistent** (`server/.lazer_token`, `.lazer_key`)
  and reused across launches; **Regenerate** in the UI rotates both and kicks phones.
- Open/public Wi-Fi: the setup firewall rule is **Private-profile only**. Treat
  plaintext mode as untrusted there; prefer QR + Require encryption.
