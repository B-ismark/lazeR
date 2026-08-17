# LazeR — turn your phone into a laptop trackpad

Drag the phone like a trackpad, scroll, click/right-click, set volume, hit media
keys, and type — all over your Wi-Fi. No account, no internet, no install on a store.

This folder is the **whole thing**: the laptop server + the phone app + a script
that sets everything up.

```
LazeR.apk        the Android app (install on your phone)
server/          the Python server (runs on your laptop)
run_windows.ps1  one-shot setup + run for Windows
run_unix.sh      one-shot setup + run for macOS / Linux
```

## 1. Laptop — run the server

**No Python? Use `LazeR.exe`.** If the folder has **`LazeR.exe`**, just double-click
it — it bundles Python and everything else, nothing to install. (Build it yourself
from source with `tools\build_exe.ps1`.)

**With Python:** right-click `run_windows.ps1` → **Run with PowerShell**.
(or in a terminal: `powershell -ExecutionPolicy Bypass -File run_windows.ps1`)

This also drops a clickable **LazeR icon on your Desktop** — after the first run
just double-click that to start the app (no terminal). To make the shortcut
without running the full setup, run `Create LazeR Shortcut.ps1`.

**Start with Windows:** open **Show details** in the window and flip **Start with
Windows** on — LazeR then launches at every login. (Or run `Add to Startup.ps1`;
remove with `Add to Startup.ps1 -Remove`.)

**macOS / Linux:** `bash run_unix.sh`

The script installs Python deps, opens the firewall (Windows asks for admin once),
and starts the server. It prints something like:

```
  Laptop IP : 192.168.1.20
  Port      : 50505
  Token     : A1B2C3
```

You need **Python 3.8+** installed first. If it's missing the script tells you how.

## 2. Phone — install the app

Copy **`LazeR.apk`** to your Android phone and tap it (allow "install unknown apps"
when prompted).

> On USB with debugging on? Run `run_windows.ps1 -InstallApk` (or
> `bash run_unix.sh --install-apk`) and it pushes the app for you.

## 3. Connect

Phone and laptop on the **same Wi-Fi**. Open **LazeR**, then either:

- **Scan QR** (easiest) — the server prints a QR at startup; scan it and you're in.
- **Pick from "Found on your network"** — the app auto-discovers the laptop; tap it,
  type the token once, and it's saved.
- **Enter manually** — IP, port `50505`, token.

Saved laptops show on the start screen — tap one to reconnect instantly (the server's
token is persistent, so it keeps working across restarts).

- **Trackpad** (bottom): drag = move, tap = left-click, **two-finger tap =
  right-click**. Holding does nothing on purpose, so a resting hand never clicks.
- **Two fingers**: vertical = scroll · **horizontal swipe = browser back / forward**
  (like a Windows touchpad). **Three fingers** left/right = switch apps.
- **Scroll strip** on the trackpad's right edge.
- **Volume** slider — synced both ways with the laptop.
- **Media / Keyboard** — toggle between them with the buttons up top.

## Notes
- A **window opens** showing the QR, IP, port, token (with a Copy button), a live
  connection status, and an activity log. Run with `--no-gui` for headless/terminal mode.
- The **token is persistent** — saved on the laptop and reused across restarts, so a
  saved phone keeps connecting without re-pairing.
- **Survives sleep** — after the laptop wakes, the server rebinds and re-announces
  itself automatically (if your IP changed, the window shows the new QR — just rescan).
- Volume on Windows uses `pycaw` (installed by the script); macOS uses `osascript`;
  Linux uses `amixer`/`pactl`.

## Security
- **Scan the QR for encryption.** QR pairing uses an **AES-256-GCM** encrypted,
  authenticated, replay-protected channel — your keystrokes and clicks can't be
  sniffed, spoofed, or replayed by others on the Wi-Fi. The window shows
  **Connected · encrypted** when it's on.
- **Encryption is on by default, so scan the QR.** Typing the code by hand uses an
  unencrypted connection and is now refused unless you allow it: in **Show details**,
  flip **Require encryption** off (or start with `--allow-plaintext`). If a phone
  tries the typed code while it's required, the activity log tells you.
- **Your mouse always wins.** Touch the laptop's own mouse/touchpad/keyboard and the
  remote pauses instantly; it resumes a couple of seconds after you stop. Press
  **Ctrl+Alt+Shift+L** (panic) to hard-stop the remote — it stays off until you click
  **Resume** in the window. Use this if anything ever feels wrong.
- **Regenerate** the pairing code anytime (Show details → Pairing code → Regenerate)
  to kick every paired phone and force a fresh scan.
- **Firewall:** setup opens UDP 50505 on **all** network types. Limiting it to
  Private sounded safer but broke the common case — Windows labels most Wi-Fi
  Public, including work and many home networks, so the port stayed shut and the
  phone just timed out. Your pairing code and encryption are what keep others out,
  not a closed port. Still, treat plaintext mode as trusted-LAN only.
