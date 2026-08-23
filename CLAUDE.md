# LazeR — agent guide

Phone-as-trackpad. Android (Kotlin/Compose) client + Python server. LAN-only UDP on
`50505`, gated by a per-session token; secure wire is AES-256-GCM (QR pairing) with a
challenge-response handshake (`HELLO`→`CHAL`→`AUTH`, replay-proof). Encryption is
required by default — `--allow-plaintext` opts into the legacy manual-code wire.
The wire dialect is `L3` (`sid(8)|counter(4)`); the legacy `L2` dialect was removed
after its one-release grace window.
See [README.md](README.md) and [PROTOCOL.md](PROTOCOL.md) for the full picture.

## Update checks — the only internet access

Both halves check GitHub's public releases API for a newer tag and show a
notice (server: a "Version" pill in Details; phone: a card on the connect
screen). **Notify-only** — never downloads or installs. Anonymous GET, no token,
silent on any failure. Off via `--no-update-check` / **Advanced → Updates**.

`APP_VERSION` in `remote_server.py` **must match** `versionName` in
`android/app/build.gradle.kts` — a test asserts it, since a stale value would
either nag forever or never nag. Bump both when you bump either.

This is the ONLY outbound internet request in the product; everything else is
LAN-only. Keep it that way — if a feature needs the internet, that's a design
discussion, not an implementation detail.

## Releases — IMPORTANT POLICY

**Only publish a new GitHub Release for MAJOR releases.** Routine changes (bug
fixes, small features, docs, refactors) just get committed and pushed — do **not**
cut a release or re-upload the `LazeR.exe` / `LazeR.apk` assets for them.

A "major release" = a meaningful user-facing milestone (new headline feature set,
protocol change phones must adopt, or an explicit version bump the user asks for).
When in doubt, ask the user before publishing — don't release on every change.

To publish a major release, use the helper (builds both artifacts, handles the
OneDrive build-lock, creates or refreshes the release) — **run it from the repo
root**, since `-File` resolves against the current directory and PowerShell starts
in `%USERPROFILE%`, where `tools\` doesn't exist:

```
powershell -ExecutionPolicy Bypass -File tools\publish_release.ps1 -Tag v1.2.0 -Title "LazeR v1.2 — <headline>"
```

From anywhere else, give `-File` the full path instead. Only the invocation path
matters; the script resolves everything it touches from its own location.

It prompts for confirmation (major-only policy) unless `-Yes`, and needs `gh`
authenticated (`gh auth login`). It re-runs `--clobber` if the tag already exists.

## Build artifacts

- **Windows server exe:** `tools/build_exe.ps1` → `dist/LazeR.exe` (PyInstaller
  onefile, bundles Python + deps). The script prefers `server/.venv` (incl. uv
  venvs, which have no pip — it falls back to `uv pip`).
- **Android APK:** from `android/`, `./gradlew assembleRelease` (hardened, R8,
  signed with the local `~/.android/debug.keystore` "sideload" config) →
  `app/build/outputs/apk/release/`. `assembleDebug` for a quick debuggable build.
  Needs Android **platform-35** + **build-tools 35.0.0** (`sdkmanager
  "platforms;android-35" "build-tools;35.0.0"`).

### M3 Expressive version lock (don't break this)

The UI is **Material 3 Expressive**, whose APIs live ONLY in the `material3`
**1.5.0-alpha** line (they were pulled from 1.4.0 stable). `app/build.gradle.kts` pins
**`material3:1.5.0-alpha12`** explicitly (overriding the Compose BOM) — the newest alpha
still on Compose 1.8/1.9, which keeps **compileSdk 35 / AGP 8.7.3 / Gradle 8.9**. alpha16
jumps to Compose 1.11 (compileSdk 36), alpha20+ to 1.12 (compileSdk 37 + AGP 9). **Do not
bump material3 past alpha12** without doing that whole toolchain chain (+ new platform).
Toolchain also moved: AGP 8.5.2→8.7.3, Kotlin 2.0.20→2.1.0, compileSdk 34→35 (targetSdk
stays 34). Release lint is disabled (`lint { checkReleaseBuilds = false }`) — AGP 8.7.3's
lint crashes (`IncompatibleClassChangeError`) on the alpha libs. Full detail:
[android/EXPRESSIVE_MIGRATION.md](android/EXPRESSIVE_MIGRATION.md).

### Cursor lag / low-latency WifiLock (don't remove WAKE_LOCK)

The client holds a **low-latency WifiLock** (`WIFI_MODE_FULL_LOW_LATENCY`, falling back to
`FULL_HIGH_PERF` below API 29) for the whole session (`RemoteViewModel`: acquire on connect,
release on disconnect / `onCleared`). Without it the phone radio drops into WiFi power-save
between beacons, making the cursor feel **sluggish / trailing** on some routers even on a
flawless 5 GHz link — the tell is a **bimodal LAN ping (≈4 ms ↔ 100–170 ms, 0 % loss)** with
great RSSI/rate. Acquiring a WifiLock **requires `android.permission.WAKE_LOCK`** (NOT
`CHANGE_WIFI_STATE`, the common wrong guess); without it `acquire()` throws a swallowed
`SecurityException` and the lock silently never engages. **Keep WAKE_LOCK.**

### Sleep / resume recovery (don't reintroduce these)

Recovering from a laptop sleep must need **no restart and no re-tap**. Four rules
keep that true:

- **Nothing on the UDP thread may raise.** `serve_loop` is the only thread serving
  the phone; anything escaping it left the laptop deaf with the window still green.
  `serve_forever` restarts it as a backstop, but new code above the handler guard
  (the HELLO/AUTH/PING/VGET/BGET branches) must handle its own errors. The original
  offender was `get_volume()`: a pycaw `IAudioEndpointVolume` captured once at import
  goes stale on resume or an output-device change, and VGET is the phone's liveness
  probe — so the first poll after every sleep killed the loop. It's re-acquired on
  demand now, and may return `None`.
- **Never publish `lan_ip()` directly.** It falls back to `127.0.0.1` so the GUI
  always renders; publishing that into mDNS or the QR poisons discovery until a
  restart. Use `usable_lan_ip()`, which returns `None` instead, and announce via
  `announce_network()`.
- **Re-read the address on a timer, not only after a sleep gap.** Roaming and DHCP
  changes move us with no gap at all (`NET_WATCH_S`).
- **The phone's reconnect loop has no give-up.** It backs off but never stops, since
  every bounded window is shorter than a real sleep. `kickReconnect()` short-circuits
  the backoff on app-foreground and network-available.

Windows low-level hooks are revoked across a wake/lock and report nothing when they
are, so `LocalInputGuard.rearm()` re-installs them on every wake — installing the new
pair *before* dropping the old, since a failed re-install would otherwise throw away
two working hooks and silently kill local-takeover detection for the session.

### Two Win32 traps this codebase has already paid for

- **`SetCursorPos` is not input.** It moves the pointer without advancing
  `GetLastInputInfo`, so Windows keeps the pointer image hidden (it only draws it
  when it believes a mouse is in use) and the idle timer keeps running. pynput moves
  via `SetCursorPos`, so remote control drove an *invisible* cursor and let the
  display blank mid-session. Fixed with a zero-delta `SendInput` when the pointer is
  hidden (`make_pointer_waker`) and `SetThreadExecutionState` while the phone is
  driving (`make_idle_suppressor`) — not by switching movement to `SendInput`, which
  would put Windows' pointer ballistics on top of our own tuned deltas.
- **A `WH_KEYBOARD_LL` hook reports side-specific modifier codes** — `VK_LSHIFT`
  (0xA0), not `VK_SHIFT` (0x10). The panic chord matched generic codes against hook
  output for its whole life and therefore never fired once. Modifiers are now read
  with `GetAsyncKeyState`, which answers for the generic codes and is side-agnostic.

**COM is per-thread.** comtypes initializes only the importing thread, so anything
touching pycaw from the UDP thread must `CoInitialize()` first or it fails every time
with `CoInitialize has not been called` — which silently turned the volume-endpoint
recovery above into a no-op until it was caught.

## Environment gotchas (Windows)

- **This repo lives under OneDrive**, which locks `build/` mid-compile and fails
  Gradle/PyInstaller (`Access is denied` / `Unable to delete`). Killing OneDrive is
  unreliable — it auto-restarts and re-locks. The real fix (already baked into the
  scripts) is to put build output OUTSIDE the synced tree:
  - `build_exe.ps1` sets PyInstaller `--workpath`/`--specpath` to `$env:TEMP`.
  - `publish_release.ps1` builds the APK with a generated `--init-script` that
    redirects `layout.buildDirectory` + a `--project-cache-dir`, both under `$env:TEMP`.
  - Manual Gradle build off-sync:
    `./gradlew assembleRelease --init-script <init.gradle> --project-cache-dir <tmp>`
    where the init sets `allprojects { layout.buildDirectory.set(...) }` to a temp path.
  The APK then lands under `%TEMP%\lazeR-build\app\outputs\apk\release\`, not `android/app/build/`.
- **Python:** use the uv-managed venv at `server/.venv` (bare `python` is the broken
  MS Store stub). Create/refresh: `cd server && uv venv && uv pip install -r requirements.txt`.
- **PowerShell 5.1 + native tools:** don't run build CLIs under
  `$ErrorActionPreference='Stop'` and avoid `2>&1` on native exes — both turn normal
  stderr into a terminating `NativeCommandError`. Gate on `$LASTEXITCODE` instead.
- **Firewall:** phones reach the server over inbound UDP 50505, blocked by default
  (loopback bypasses it, so the server looks fine locally while phones time out).
  The server self-adds the rule (`--setup-firewall`, or the GUI "Allow through
  firewall" button, one UAC). A full-tunnel/LAN-blocking VPN can still block it —
  not overridable; user must allow local LAN in the VPN.
- **A managed PC can make our firewall rule INERT, and everything still looks fine.**
  Policy can set *"Apply local firewall rules: No"* on a profile
  (`AllowLocalPolicyMerge = 0`); only policy-delivered rules then count. Our rule is
  created, `netsh show rule` lists it, and `Get-NetFirewallRule` reports it
  Enabled/Allow/Profile=Any in the **ActiveStore** — while Windows drops every packet.
  Tells: `Get-NetFirewallProfile -PolicyStore ActiveStore` shows
  `AllowLocalFirewallRules: False` for the profile you're on, and mDNS dies too, so
  phone discovery finds nothing and can't correct a stale saved IP (the phone then
  retries a long-dead address forever and blames the network). It's read from **two**
  registry roots — `SOFTWARE\Policies\Microsoft\WindowsFirewall\<Profile>` (GPO) and
  `SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\Mdm\<Profile>`
  (Intune/MDM). Checking only the GPO one finds nothing on an Intune-managed laptop.
  `<Profile>` is `DomainProfile` / `StandardProfile` (= **Private**) / `PublicProfile`.
  `firewall_rule_is_inert()` detects it and the UI says so instead of showing a green
  pill. Only real fix from the user side: mark the network **Private**, or have IT
  push the rule. Public is the profile that gets locked down in practice.
- **`Get-NetFirewallRule` defaults to the PersistentStore**, which does NOT include
  GPO/MDM rules. Query `-PolicyStore ActiveStore` when you want what's *in force*.
- **Windows ignores inbound ping by default** — every `Echo Request` rule ships
  disabled. A phone that can't ping the laptop proves nothing; don't chase it. ARP
  resolving the phone's MAC is better evidence that the LAN path works.

## Secrets

`.lazer_token` / `.lazer_key` (per install, incl. the exe's own next to `dist/`)
are gitignored — never commit them.

They live next to the **running** binary (`_APP_DIR`), so `server/.lazer_*` and
`dist/.lazer_*` are two different identities: switching between
`python remote_server.py` and `dist/LazeR.exe` invalidates the phone's saved pairing.
The phone just reports a failed connect, which is indistinguishable from a firewall or
network problem. To test from source against an already-paired phone, copy
`dist/.lazer_*` into `server/` rather than re-pairing — and keep any backup **outside**
the repo, since `.gitignore` matches those two names exactly and a `.lazer_token.bak`
is therefore NOT ignored.
