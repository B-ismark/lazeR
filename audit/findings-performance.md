# Reliability and performance findings — 2026-09-23

Nothing here was run on a device, and the test suites were not run. Android races are
inferred from code.

---

## R-1 [MEDIUM] A held drag button is never released when the phone leaves or the remote pauses
**File:** `server/remote_server.py:1421-1425` (MDOWN/MUP), `:2460-2471` (`drop_client`), `:648-656` (`appswitch_reset`), `:2751` (control verbs dropped while paused)
**Issue:** MDOWN presses `Button.left`. `drop_client` and the local-takeover path only
call `appswitch_reset()`, which releases Alt and nothing else. MUP is in
`POINTER_VERBS ⊂ CONTROL_VERBS`, so it is **discarded** while the remote is paused. The
phone sends MUP once over UDP (`RemoteViewModel.kt:766`), while ASW-end is sent twice
for this very reason (`:774-775`).
**Evidence:** [code] `drop_client` releases only Alt; the pause gate at `:2751` drops every `CONTROL_VERBS` member.
**Scenario:** The user is in "Hold drag" and grabs the laptop's own mouse. The remote
pauses, the phone's release is dropped, and the button stays logically down: the
physical mouse drags or selects until the next click. The same happens if Wi-Fi drops
mid-hold.
**Suggested fix:** Track held buttons server-side and release them wherever
`appswitch_reset` runs. Let MUP through the pause gate. Resend MUP from the phone.

## R-2 [MEDIUM] A stale connect attempt on the phone can tear down the current one
**File:** `android/.../net/RemoteClient.kt:46-77`, `:153`, `:292-299`; trigger `MainActivity.kt:152-155` → `RemoteViewModel.kt:383`
**Issue:** `RemoteClient` keeps one shared `socket`/`channel`. `connect()` calls
`close()`, then reassigns them. On any exception, including a `SocketException` from a
socket closed underneath it (`doHandshake` catches only `SocketTimeoutException`), it
calls `close()` again, which closes whatever the fields hold *now*.
**Scenario:** On the Reconnecting screen the user taps Scan QR. The reconnect job is
cancelled, but its blocking `client.connect` keeps running on an IO thread. The new
connect closes the old socket; the old thread wakes and closes the new socket or nulls
the new channel, and a plaintext HELLO can then go out (see S-6). The scan fails with
"didn't answer"; a retry works. `disconnect()` → queued `closeSocket()` has the same shape.
**Suggested fix:** A per-connection session object (socket, channel, sender). Cleanup
closes only its own session.

## R-3 [MEDIUM] A second server instance still binds 50505, and some double-launches look like nothing happened
**File:** `server/remote_server.py:1645` (`SO_REUSEADDR`), `:3909`, `:3919`, `:4125`
**Issue:** On Windows, `SO_REUSEADDR` lets a second UDP socket bind the same port. A
terminal instance launched while another runs warns "two servers will fight over UDP
50505 (drops/reconnects). Close the other one." and then **binds anyway**: the "multi-server
storm" already recorded in project memory. A GUI launched while a `--no-gui` instance
runs sends SHOW, prints "opened the existing window" to a windowed exe (no console),
and exits, while the terminal ignores SHOW. The user's double-click appears to do
nothing. If the single-instance bind fails, the GUI proceeds unguarded.
**Suggested fix:** Refuse to start a second server; on Windows use `SO_EXCLUSIVEADDRUSE`
instead of `SO_REUSEADDR`, after confirming the sleep/resume rebind still works. Have a
terminal owner answer SHOW with a reply the GUI can show.

## R-4 [LOW] A supervised restart leaves stale state behind
**File:** `server/remote_server.py:2797-2809`, `:2412`, `:2423`, `:2775`
**Issue:** The `failures` backoff counter is never reset, so after 5 crashes over the
process's lifetime every later restart waits 5 s. A restart clears `client` without
emitting "disconnected", so the GUI keeps showing "Phone connected". It doesn't release
held input, and the old socket closes only when it is garbage-collected.
**Suggested fix:** Reset `failures` after a healthy interval. Emit "disconnected",
release held input, and close the socket in a `finally`.

## R-5 [LOW] Shared state touched from several threads without locks
**File:** `server/remote_server.py`: `rotate_secrets` (Tk thread) against `serve_loop`
reading `wire.*`; `_alt_held` (`:618`, `:633-635`) written by both the hook thread and the
UDP thread; `announce_async` (`:2445-2454`) racing `_stop_mdns` (`:3957`)
**Issue:** The practical effects are small: the GUI shows "connected" for up to 12 s
after Regenerate; a narrow race can leave Alt held; an mDNS registration can outlive
shutdown, and two announce workers can run after a loop restart (a leaked Zeroconf
instance).
**Suggested fix:** Lock around mutations of `Wire` and the Alt flag, and move the
announce busy flag to module level.

## R-6 [LOW] The low-latency WifiLock stays held after a failed connect from the Reconnecting screen
**File:** `RemoteViewModel.kt:408-412`
**Issue:** The connect-failure branch sets Disconnected but never calls `holdWifi(false)`.
Disconnect, `onCleared` and the 90 s tail all release the lock correctly. On API 29+ the
lock only takes effect in the foreground, which limits the battery cost.
**Suggested fix:** Release the lock on the failure path.

## R-7 [LOW] `Discovery.found` is an unsynchronized map
**File:** `android/.../data/Discovery.kt:16`, `:26`, `:40`, `:80`, `:94`
**Issue:** A plain `LinkedHashMap` is mutated from NSD binder threads, cleared from Main,
and iterated in `emit`, so a `ConcurrentModificationException` is possible. `stop()` also
resets `resolving` while a resolve is in flight.
**Suggested fix:** Use a concurrent or synchronized map, and drop callbacks that belong
to an earlier `start()`.

## P-1 [LOW] Allocations per MOVE packet on the phone
**File:** `SecureChannel.kt:61` and the `send()` path
**Issue:** `Cipher.getInstance("AES/GCM/NoPadding")` runs for every packet, with about 6
allocations plus a Runnable and a String per event. At 120–240 Hz this is GC churn, not
a measured bottleneck.
**Suggested fix:** Reuse one cipher on the sender thread and re-init it with each IV.
Measure before and after.

## P-2 [LOW] The whole control screen recomposes on every slider-drag frame and every health tick
**File:** `MainActivity.kt:97` (a new `ControlActions` with about 27 lambdas per state change), `RemoteViewModel.kt:784`
**Issue:** The actions object isn't remembered or `@Stable`, and volume and brightness
are read at the top of the tree. Trackpad motion itself does **not** recompose (sound).
**Suggested fix:** `remember` the actions object and move the slider state reads down
into the sliders.

(Trivial: `sock.settimeout(1.0)` is called on every loop iteration at `:2617`.)

---

## Sound
- COM: every pycaw path goes through `_endpoint()`, which calls `CoInitialize`.
- The PING/VGET/BGET/HELLO/AUTH branches fail soft.
- `LocalInputGuard.rearm` installs the new hooks before dropping the old ones.
- WAKE_LOCK is declared.
- The phone's reconnect loop never gives up.
- Every socket read runs on IO.
- The phone doesn't coalesce moves; this is deliberate (`RemoteViewModel.kt:740-743`).
