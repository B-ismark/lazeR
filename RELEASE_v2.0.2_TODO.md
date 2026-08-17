# TODO: publish v2.0.2 — must be done on the "key B" laptop

**Delete this file once the release is out** (last step below).

Everything for v2.0.2 is merged and green on `main`. The release was **deliberately
not published**, because the laptop it was prepared on cannot sign an installable
APK. This is the handoff.

---

## Why this was deferred

LazeR's release APKs have been signed by **two different** debug keystores. Measured
with `apksigner verify --print-certs` on the published assets (2026-08-17):

| Release | APK signer SHA-1 | Key |
|---|---|---|
| v1.5.0 | `45e9b7d6fd8e0c1c0d97c93ec6ad49c0b52267d4` | **B** |
| v1.5.1 | `a6e85371f497a4b9fe432b6414d90863fb94b8ac` | A |
| v2.0.0 | `45e9b7d6…` | **B** |
| v2.0.1 | `45e9b7d6…` | **B** |

Every current user is on **key B**. The laptop this work was done on holds **key A**,
so building there produces an APK Android rejects as a signature mismatch — users
would have to uninstall first, losing paired devices and settings. With the version
bumped to 2.0.2 the in-app checker would have actively *prompted* an update that
cannot install, which is why publishing was held rather than risked.

All Android debug certs share the DN `C=US, O=Android, CN=Android Debug`. The DN
proves nothing — **compare fingerprints, not names.**

---

## Step 0 — confirm you are on the right machine (do this first)

```powershell
keytool -list -v -keystore "$env:USERPROFILE\.android\debug.keystore" -storepass android
```

The `SHA1:` line under *Certificate fingerprints* **must** be:

```
45:E9:B7:D6:FD:8E:0C:1C:0D:97:C9:3E:C6:AD:49:C0:B5:22:67:D4
```

- Matches → correct laptop, continue.
- Shows `A6:E8:53:71:F4:97:A4:B9:FE:43:2B:64:14:D9:08:63:FB:94:B8:AC` → this is the
  **key A** machine. **Stop.** Publishing from here is the exact thing being avoided.
- Neither → a third keystore. Stop and ask; do not publish.

While you are on this machine, **copy `~/.android/debug.keystore` somewhere durable.**
The A→B→A→B alternation across v1.5.0, v1.5.1 and v2.0.0 means past upgrades probably
broke this way silently. This keeps recurring until the key is backed up.

## Step 1 — sync and verify the tree

```powershell
git checkout main
git pull --ff-only
git log --oneline -1     # expect c7c2c4e or later
git status --short       # expect clean
```

Confirm the version is already at 2.0.2 in both places (a test pins them to each
other, and `publish_release.ps1` asserts the tag matches `versionName`):

- `server/remote_server.py` → `APP_VERSION = "2.0.2"`
- `android/app/build.gradle.kts` → `versionName = "2.0.2"`, `versionCode = 20002`

## Step 2 — prerequisites

- `gh auth status` must be authenticated, with push rights to `B-ismark/lazeR`.
- `server/.venv` must exist: `cd server && uv venv && uv pip install -r requirements.txt`
  (bare `python` on these machines is the broken MS Store stub).
- Android **platform-35** and **build-tools 35.0.0**:
  `sdkmanager "platforms;android-35" "build-tools;35.0.0"`. Note the key-A laptop only
  had build-tools 34.0.0 and AGP tolerated it; if the APK build fails here, this is
  the first thing to check.
- Do **not** bump `material3` past `1.5.0-alpha12` — see `android/EXPRESSIVE_MIGRATION.md`.

## Step 3 — run the tests

```powershell
server\.venv\Scripts\python.exe -m unittest discover -s server\tests -t server\tests
```

Expect **144 tests, OK**. `ServeLoopResilience.test_second_plaintext_phone_is_turned_away_and_reported`
is independently flaky (a socket test, ~1 failure in 5 runs) — if only that one fails,
re-run before investigating.

## Step 4 — publish

Run **from the repo root** (`-File` resolves relative to your current directory):

```powershell
powershell -ExecutionPolicy Bypass -File tools\publish_release.ps1 -Tag v2.0.2 `
    -Title "LazeR v2.0.2 - firewall fix" -Notes (Get-Content -Raw RELEASE_NOTES_v2.0.2.md)
```

Or paste the notes from the bottom of this file into the GitHub UI afterwards.

**Be aware:** this script kills OneDrive, plus any running `LazeR` and `java`
process, to free the build locks. Save your work first. It builds `LazeR.exe` and the
hardened release APK, then creates the release with both assets.

It also prompts for confirmation because CLAUDE.md's policy is *major releases only*.
This patch release is a deliberate, user-approved exception: the fix cannot reach
anyone without a tag bump, because the update checker only notifies when the newest
release tag is strictly newer than the installed version.

## Step 5 — verify what actually got published

```powershell
gh release download v2.0.2 --pattern "LazeR.apk" -D "$env:TEMP\v202check"
& "$env:LOCALAPPDATA\Android\Sdk\build-tools\35.0.0\apksigner.bat" verify --print-certs "$env:TEMP\v202check\LazeR.apk"
```

- Signer SHA-1 **must** be `45e9b7d6fd8e0c1c0d97c93ec6ad49c0b52267d4`. If it is
  `a6e85371…`, the wrong keystore was used — delete the release and stop.
- `gh release view v2.0.2` should list **both** `LazeR.exe` and `LazeR.apk`.
- Release notes should mention the firewall fix.

Then confirm delivery works: launch the new `LazeR.exe`, and its update pill should
read *up to date (v2.0.2)*. A server still on 2.0.1 should offer v2.0.2.

## Step 6 — clean up

Delete this file and commit:

```powershell
git rm RELEASE_v2.0.2_TODO.md
git commit -m "chore: drop the v2.0.2 release handoff, release is out"
git push
```

---

## What v2.0.2 actually contains

One user-facing fix, plus repo hygiene. Merged as PR #4.

**The firewall rule was created with `profile=private,domain`.** Windows classifies
most Wi-Fi as **Public** — corporate SSIDs and plenty of home routers included — so on
the networks people actually use the rule was inert and the port stayed shut. It also
failed *silently*: `firewall_rule_exists()` matched the rule by name only, so the GUI
showed a healthy firewall while every phone timed out with nothing to diagnose.

- Rule is now created with `profile=any`.
- Rule name is versioned (`LazeR inbound UDP 50505 v2`), because pre-fix installs
  carry a `private,domain` rule under the old name and matching it would keep
  reporting a firewall that drops every packet. The repair path deletes the old one.
- Purge + add share one elevated `cmd.exe`, so it stays a single UAC prompt.
- `dist/run_windows.ps1` had the same Private-only assumption; fixed too.
- README and START_HERE both claimed the port was *never* opened on Public networks.
  That was a security claim the fix falsified, so both were corrected.
- Four `WindowsVolumeBackend` tests had failed on **every** CI run since PR #3, so
  `main` was red at v2.0.1. `comtypes` was never stubbed (only `pycaw`), and the stubs
  were scoped to the `make_volume()` call while `_endpoint()` re-imports comtypes on
  every re-acquisition. Both fixed; CI is green.

No functional Android change in this release — the fix is entirely server-side. The
APK is rebuilt only so its `versionName` stays in step with the tag.

---

## Draft release notes (paste into the release)

```markdown
## Fixed: phones timing out even with the firewall "allowed"

LazeR's firewall rule was only ever opened on **Private** and **Domain** networks.
Windows classifies most Wi-Fi as **Public** — including corporate networks and plenty
of home routers — so on those the rule did nothing and the port stayed closed. The
window still reported the firewall as fine, which made this close to undiagnosable:
the server looked healthy locally (loopback bypasses the firewall) while every phone
timed out.

The rule now covers all network types, and LazeR replaces the old inert rule when you
click **Allow through firewall** (one UAC prompt, as before).

Opening the port is not the exposure it sounds like: the port was never the security
boundary. Every packet is gated by your per-session pairing token over AES-256-GCM
with a replay-proof handshake, so a reachable port without the QR code gets an
attacker nothing. The docs claimed the opposite scoping and have been corrected.

**If phones still time out** after updating, the remaining suspects are a VPN that
blocks local network traffic, or client isolation on the Wi-Fi access point (common on
corporate and guest networks) — neither of which LazeR can override.

## Also

- Fixed four tests that failed on every CI run since v2.0.1, so CI is green again.
- No functional changes to the phone app; this fix is entirely on the laptop side.
```
