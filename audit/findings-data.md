# State and persistence integrity findings — 2026-09-23

This product has no database. The "data integrity" dimension here covers persisted
state: pairing secrets, the phone's saved devices, settings, and published addresses.

---

## D-1 [MEDIUM] "Regenerate" says it worked when the write failed, and the next launch brings the old secrets back
**File:** `server/remote_server.py:757-770` (`rotate_secrets`), GUI `_regenerate` around `:3423-3434`
**Issue:** `rotate_secrets` swallows `OSError` on both writes, but still switches the
live `Wire` to the new token and key. The GUI then logs "New pairing code generated".
**Scenario:** The exe folder isn't writable, for example Program Files after an admin
first run. The user regenerates to kick a phone. The kick lasts only until the next
launch; then the old secrets reload and the kicked phone is back in. Phones re-paired
in between are locked out.
**Suggested fix:** If persisting fails, don't swap the live secrets, and show an error
on the main screen. The same generation code exists three times; see C-3.

## D-2 [LOW] The manual form holds the current device's token, and "Connect & save" can overwrite an encrypted pairing with no key
**File:** `RemoteViewModel.kt:339-341` (`connectSaved` copies name/ip/token into the form), `:333-337` (`connectManual` passes key `""`), `DeviceStore.kt:152-160`
**Issue:** After a saved device connects, "Enter manually" is pre-filled with its token.
If the laptop allows plaintext, "Connect & save" upserts the same `ip:port` record with
`key = ""`.
**Scenario:** A QR-paired laptop is silently downgraded to plaintext pairing.
**Suggested fix:** Keep the form state separate from the current device. Never replace
a saved key with a blank one.

## D-3 [LOW] Re-pairing at a new IP leaves a stale duplicate
**File:** `RemoteViewModel.kt:380` (`id = "$ip:$port"`), `DeviceStore.kt:154-156`
**Issue:** A QR scanned from a laptop whose IP has changed doesn't match the old record,
so it adds a second row with the same name and the old key.
**Suggested fix:** Key saved devices on a server identity carried in the QR (for example
a key fingerprint).

## D-4 [LOW] Startup publishes `lan_ip()` directly, which breaks a CLAUDE.md rule
**File:** `server/remote_server.py:4109` (`ip = lan_ip()`), which reaches `start_mdns` at `:3824` / `:4134` and the QR `build_uri` at `:2842` / `:3823`
**Issue:** CLAUDE.md: "Never publish `lan_ip()` directly … Use `usable_lan_ip()`". The
runtime paths comply; startup does not.
**Scenario:** Autostart at login runs before Wi-Fi associates. The 127.0.0.1 fallback or
a link-local address is announced and drawn into the QR, and a scan in that window
saves a bogus IP. It heals itself within `NET_WATCH_S` (5 s), and the phone filters
127.0.0.1 and link-local addresses from discovery (`usableHost`). What remains is mainly
a QR scanned in the first seconds.
**Suggested fix:** Use `usable_lan_ip()` at startup and defer publishing while it returns `None`.

## D-5 [LOW] The "Require encryption" toggle isn't saved
**File:** `server/remote_server.py:3450-3455`
**Issue:** The toggle changes only in-memory state and resets to On at every launch. Exe
users can't easily pass `--allow-plaintext`. Defaulting to safe is defensible, **likely
intentional**, but README:153 and START_HERE:137 don't mention the reset.
**Suggested fix:** Document the reset, or persist the choice explicitly with a visible warning.

## D-6 [LOW] Values both halves must agree on have no shared source or test
**Files:**
- Port `50505`: hard-coded four times on Android (`RemoteViewModel.kt:70, 336, 362`, `DeviceStore.kt:137`), against `PORT` on the server.
- mDNS type: `Discovery.kt:98` against `SERVICE_TYPE` at `remote_server.py:70`.
- Idle timing: `CLIENT_IDLE_S=12` (`:43`) against the phone's 4000 ms idle poll (`RemoteViewModel.kt:567`).
- QR format: the server's `build_uri` is tested, but the phone's `applyScannedUri` parsing is not.

**Issue:** Only `APP_VERSION`/`versionName` and the wire golden vectors are pinned by tests.
**Scenario:** If the phone's poll interval is stretched past about 12 s, the server
keeps dropping the phone as idle. If the QR format changes on one side, pairing breaks
with no failing test.
**Suggested fix:** Add golden tests for each: the same pattern already used for the wire bytes.
