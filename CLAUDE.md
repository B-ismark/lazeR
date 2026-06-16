# LazeR — agent guide

Phone-as-trackpad over LAN. Android (Kotlin/Compose) client + Python server. UDP
on `50505`, gated by a per-session token; secure wire is AES-256-GCM (QR pairing).
See [README.md](README.md) and [PROTOCOL.md](PROTOCOL.md) for the full picture.

## Releases — IMPORTANT POLICY

**Only publish a new GitHub Release for MAJOR releases.** Routine changes (bug
fixes, small features, docs, refactors) just get committed and pushed — do **not**
cut a release or re-upload the `LazeR.exe` / `LazeR.apk` assets for them.

A "major release" = a meaningful user-facing milestone (new headline feature set,
protocol change phones must adopt, or an explicit version bump the user asks for).
When in doubt, ask the user before publishing — don't release on every change.

To publish a major release, use the helper (builds both artifacts, handles the
OneDrive build-lock, creates or refreshes the release):

```
powershell -ExecutionPolicy Bypass -File tools\publish_release.ps1 -Tag v1.2.0 -Title "LazeR v1.2 — <headline>"
```

It prompts for confirmation (major-only policy) unless `-Yes`, and needs `gh`
authenticated (`gh auth login`). It re-runs `--clobber` if the tag already exists.

## Build artifacts

- **Windows server exe:** `tools/build_exe.ps1` → `dist/LazeR.exe` (PyInstaller
  onefile, bundles Python + deps). The script prefers `server/.venv` (incl. uv
  venvs, which have no pip — it falls back to `uv pip`).
- **Android APK:** from `android/`, `./gradlew assembleRelease` (hardened, R8,
  signed with the local `~/.android/debug.keystore` "sideload" config) →
  `app/build/outputs/apk/release/`. `assembleDebug` for a quick debuggable build.

## Environment gotchas (Windows)

- **This repo lives under OneDrive.** OneDrive sync locks `build/` mid-compile and
  fails Gradle/PyInstaller (`Access denied` / `Unable to delete`). Before a build:
  stop OneDrive (`Stop-Process -Name OneDrive -Force`), delete the stale build dir,
  build, then relaunch OneDrive (`C:\Program Files\Microsoft OneDrive\OneDrive.exe`).
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
