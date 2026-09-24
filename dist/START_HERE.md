# LazeR — turn your phone into a laptop trackpad

Drag the phone like a trackpad, scroll, click/right-click, set volume, hit media
keys, and type — all over your Wi-Fi. No account, and nothing leaves your network
except an optional check for new versions.

A release is two files, both on the
[release page](https://github.com/B-ismark/lazeR/releases/latest):

```
LazeR.exe   the laptop app (Windows; bundles everything, nothing to install)
LazeR.apk   the Android app
```

## 1. Laptop

Double-click **`LazeR.exe`**. A window opens with a QR code. Windows may ask to allow
it through the firewall — allow it, or click **Allow through firewall** in the
window later (one admin prompt).

**Start with Windows:** open **Show details** and flip **Start with Windows** on.
LazeR then starts in the tray at every login.

**No Windows, or rather run from source?** `cd server`, then
`uv venv && uv pip install -r requirements.txt` and `python remote_server.py`
(see the README for macOS/Linux notes).

## 2. Phone

Copy **`LazeR.apk`** to your Android phone and tap it (allow "install unknown apps"
when prompted). Later versions can be downloaded and installed from inside the app.

## 3. Connect

Phone and laptop on the **same Wi-Fi**. Open **LazeR** on the phone, tap **Scan QR
to connect**, and scan the code in the laptop window. That's it: the laptop is saved,
and the app reconnects to it by itself from then on — after a sleep, a Wi-Fi drop or
an IP change too.

Typing the code by hand instead is unencrypted and off by default — see Security
below.

- **Trackpad** (bottom): drag = move, tap = left-click, **two-finger tap =
  right-click**. Holding does nothing on purpose, so a resting hand never clicks.
- **Two fingers**: scroll on both axes, like a real trackpad. **Pinch** = zoom.
  **Three fingers** left/right = switch apps. (Zoom and Switch app are also under
  **Shortcuts**.)
- **Scroll strip** on the trackpad's right edge — or the left, via **Settings →
  Scroll bar on the left**.
- **Volume** slider — synced both ways with the laptop. Tap its speaker icon to
  mute; on a Windows laptop the icon shows when it is muted, and moving the volume
  unmutes it.
- **Media / Keyboard** — toggle between them with the buttons up top.
- **Turn the phone sideways** for a wide trackpad: Media and Keys move to a rail on
  the left edge, and tapping one opens its panel beside the pad.

## Notes
- The **pairing is persistent** — saved on the laptop and reused across restarts, so a
  saved phone keeps connecting without re-pairing.
- **Survives sleep** — after the laptop wakes, it rebinds and re-announces itself, and
  the phone finds it again, even at a new IP.
- **New versions** — a dot on the phone's **Settings** button means one is out;
  **Settings → Updates** can download and install it. The laptop window's **Version**
  line in Show details says when the laptop app is out of date; download `LazeR.exe`
  from the release page and update both together.

## If the phone won't connect

**On a work laptop, set the Wi-Fi to Private.** This is the fix for the most
confusing failure there is. Some company policies tell Windows to **ignore firewall
rules that apps add for themselves** on networks labelled *Public* — and Windows
labels most Wi-Fi Public, work networks included. LazeR's rule then gets created and
looks perfectly correct, and Windows throws it away anyway. The phone times out, and
because the same block also kills discovery, the app finds no laptop and keeps
retrying the last address it remembers — so it looks like a Wi-Fi problem.

Fix it in **Settings → Network & internet → Wi-Fi → (your network) → Network profile
type → Private**. LazeR can't do this for you; the setting exists precisely to stop
apps overriding it. Newer LazeR versions say so in the window instead of showing a
healthy firewall. To see it yourself:

```powershell
Get-NetFirewallProfile -PolicyStore ActiveStore | Select Name, AllowLocalFirewallRules
```

`AllowLocalFirewallRules: False` on the profile you're using is the giveaway. If it's
your own PC and you'd rather not mark the network Private, ask IT to allow inbound
UDP 50505 by policy instead.

**Other things that block it, in order of likelihood:**

- **The phone is on a stale saved laptop.** If a saved entry shows an address on a
  different network from the phone's own, delete it and scan the QR again.
- **A VPN on either device** that blocks local network traffic. Turn on "allow local
  network" in the VPN, or disconnect it. A split-tunnel VPN is fine. Note the phone
  side too: ad-blockers like AdGuard run as a local VPN.
- **Client isolation** on the Wi-Fi access point — common on guest and corporate
  networks. Nothing on either device can override it.
- **2.4 GHz vs 5 GHz as separate names**, or a guest network. Those are different
  networks even though they're the same router; the app tells you when it detects it.

Don't bother testing with **ping** — Windows ignores incoming pings by default, so a
failed ping means nothing here.

## Security
- **Scan the QR for encryption.** QR pairing uses an **AES-256-GCM** encrypted,
  authenticated, replay-protected channel — your keystrokes and clicks can't be
  sniffed, spoofed, or replayed by others on the Wi-Fi. The window shows
  **Connected · encrypted** when it's on.
- **Encryption is on by default, so scan the QR.** Typing the code by hand uses an
  unencrypted connection and is now refused unless you allow it: in **Show details**,
  flip **Require encryption** off (or start with `--allow-plaintext`). If a phone
  tries the typed code while it's required, both the phone and the window say so. The
  toggle resets to On at every launch.
- **Your mouse always wins.** Touch the laptop's own mouse/touchpad/keyboard and the
  remote pauses instantly; it resumes a couple of seconds after you stop. Press
  **Ctrl+Alt+Shift+L** (panic) to hard-stop the remote — it stays off until you click
  **Resume** in the window. Use this if anything ever feels wrong.
- **Regenerate** the pairing code anytime (Show details → Pairing code → Regenerate,
  or **New code** on the Connected card) to kick every paired phone and force a fresh
  scan.
- **Firewall:** LazeR's firewall rule (the **Allow through firewall** button) opens
  UDP 50505 on **all** network types. Limiting it to Private would break the common
  case — Windows labels most Wi-Fi Public, including work and many home networks, so
  the port would stay shut and the phone would time out. Your pairing code and
  encryption are what keep others out, not a closed port. Still, treat plaintext
  mode as trusted-LAN only.
