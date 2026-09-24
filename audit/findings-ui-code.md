# Code quality and test findings — 2026-09-23

UI and UX findings are in `findings-ux-flows.md`. This file covers dead code, stale
comments, duplication and tests. The test suites were **not** run for this audit.

---

## C-1 [MEDIUM] Some tests can never fail
**File:** `server/tests/test_wire.py:1447-1454`, `:532-538`, `:1333-1360`
**Issue:**
- `test_no_wake_hold_before_any_remote_activity` sets `rs._last_remote_ts[0] = None`
  and then asserts that it is `None`. That is a tautology: it never checks the module's
  initial value or the `want_awake` logic in `serve_loop`.
- `test_every_control_verb_is_known_to_the_dispatcher` checks 4 hard-coded memberships.
  It cannot catch the case its name claims to guard: a verb added to `CONTROL_VERBS` but
  never handled.
- The PanicChord and `test_rearm_is_safe_before_the_hook_thread_exists` tests check only
  constants and attributes. The install-before-unhook order that CLAUDE.md insists on,
  and `chord_held` itself, are untested.

**Evidence:** [code]
```python
rs._last_remote_ts[0] = None
last = rs._last_remote_ts[0]
self.assertIsNone(last, ...)
```
**Why Medium:** CLAUDE.md, rule 3: "An assertion never seen failing is decoration, and its green is worse than no test because it is believed."
**Suggested fix:** Assert against the module's real initial state and the `serve_loop`
decision. Derive the verb list from the dispatcher. Test the rearm order with fake
hook install and unhook functions.

## C-2 [MEDIUM] Coverage gaps around the paths that failed in this audit
**Server, untested:**
- Plaintext v1 parsing with hostile bytes. This is exactly the gap behind S-1.
- Releasing held input on drop and on pause (R-1).
- The idle drop after `CLIENT_IDLE_S`, and BYE → `drop_client`.
- Local-takeover pause, the 2 s auto-resume, the panic latch, and wake clearing the pause.
- `BrightnessService` coalescing.

**Android, untested:** `RemoteClient` (handshake retry, `drainStale`, disconnect ordering)
and `RemoteViewModel` (reconnect backoff, kick, WifiLock symmetry, move smoothing).
Existing tests cover only `SecureChannel`, `keyboardOps`, `UpdateChecker`,
`backZoneClearance` and `CheckedAgo`.

**Suggested fix:** Start with a hostile-plaintext `parse` test and a drop-releases-button
test. Both are cheap and guard real defects.

## C-3 [LOW] Secret generation and writing is duplicated three times
**File:** `server/remote_server.py:736-743` (`load_or_create_key`), `:756-770` (`rotate_secrets`), `:1788-1794` (`load_or_create_token`)
**Issue:** Three copies of "generate, write, maybe chmod", each swallowing errors
differently. That is how S-2, S-9 and D-1 got in.
**Suggested fix:** One `write_secret(path, data)` that creates the file owner-only and
raises on failure.

## C-4 [LOW] Dead code
**Evidence:** [code] `grep -rn` over the repo, excluding `.venv` and the untracked `dist/server` copy:
- `_emit_action` (`server/remote_server.py:2396`) has only its definition. `main:4138-4147` re-implements it inline.
- `nudgeVolume` / `nudgeBrightness` (`RemoteViewModel.kt:788`, `:798`) have only their
  definitions. The UI does the arithmetic inline (`ControlScreen.kt:471`, `:1355`).

**Suggested fix:** Delete them, or have the inline copies call them.

## C-5 [LOW] Comments describing removed or incorrect behaviour
**Files:**
- **Relay comments.** `RemoteViewModel.kt:354` (the `&r=` relay parameter), `RemoteClient.kt:195-196` ("over the relay…"), `RemoteViewModel.kt:741-743` and `remote_server.py:898` describe a relay. There is no relay code in the tree.
- **`RemoteClient.kt:14-15`.** Says every packet carries the token. False on the secure wire.
- **`RemoteClient.kt:62`, `:81`.** Say the server answers OK to every HELLO/AUTH. It answers only a successful AUTH.
- **`ConnectionScreen.kt:127`.** Says "collapsed by default", but `showDiscovered = true` at `:84`.
- **`RemoteViewModel.kt:482-483`.** Says "trying them is safe", which is false for keyless devices (S-3).

**Issue:** CLAUDE.md rule 5 applies: prose describing deleted behaviour becomes the next
reader's source of truth.
**Suggested fix:** Fix or delete each one.

## C-6 [LOW] Update-check parsing edge cases
**File:** `android/.../data/UpdateChecker.kt:38`, `server/remote_server.py:1625`, around `:1587`
**Issue:**
- Both halves cap the response at 64 KB, so a release with very long notes or many
  assets is truncated and shows "couldn't check".
- The server's `parse_version` uses `str.isdigit()`, which accepts "²". The following
  `int()` then raises outside the try and kills the check thread.

Only the repo owner controls this input, so the practical risk is low.
**Suggested fix:** Read `tag_name` from a streamed parse, or raise the cap. Use
`isdecimal()`/ASCII-only digits.

## C-7 [LOW] The launcher scripts' dependency lists have drifted from `requirements.txt`
**File:** `dist/run_windows.ps1:42`, `dist/run_unix.sh:30`
**Issue:** Both omit `psutil`. Without it, `candidate_ips()` returns `[]`, so source runs
lose VPN-adapter avoidance for the advertised IP and lose VPN detection. See also U-2:
these scripts may be retired anyway.
**Suggested fix:** `pip install -r requirements.txt` (a hashed lock file; see S-5).

## Noted, no finding
`remote_server.py` is 4.2k lines. The audit found no concrete harm from the size beyond
the duplication in C-3, so no split is recommended on those grounds alone.
