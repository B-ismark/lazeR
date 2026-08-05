# LazeR — agent guide

Phone-as-trackpad. Android (Kotlin/Compose) client + Python server. LAN-only UDP on
`50505`, gated by a per-session token; secure wire is AES-256-GCM (QR pairing) with a
challenge-response handshake (`HELLO`→`CHAL`→`AUTH`, replay-proof). Encryption is
required by default — `--allow-plaintext` opts into the legacy manual-code wire.
Two dialects share that wire: `L3` (`sid(8)|counter(4)`, current) and `L2`
(`sid(4)|counter(8)`, accepted for one release then removed).
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
