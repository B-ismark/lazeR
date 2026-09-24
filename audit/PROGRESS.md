# Audit-fix status (branch `claude/audit-fixes`, uncommitted)

A hand-off note: re-derive before trusting it. The findings are in [AUDIT.md](../AUDIT.md)
and `audit/findings-*.md`; the fixes are the branch diff.

## State
All 59 audit findings, both 2026-09-24 reviews' findings, and the items the first
review left open are fixed in the working tree, and the in-app APK downloader is in.
Nothing is known open.

What the last two rounds changed:
- **QR hosts** (`isPairableHost`, `net/Protocol.kt`): any unicast IPv4/IPv6 literal
  or `.local` name, public and CGNAT included. IPv6 is parsed to bytes
  (`ipv6Bytes`), so IPv4-mapped/-compatible forms can't sneak loopback, unspecified
  or multicast past it. IPv4 octets with a leading zero are refused
  (`ipv4Octets`): Android dials `012` as octal 10.
- **A QR rescan resets `bound`** (`mergeDevice(rescanned = true)`), also when the
  scan was made on the Reconnecting screen and lands via the retry loop
  (`beginReconnect(persist, rescanned)`).
- **Server handshake:** a challenge keeps the nonce that opened it plus the latest 3,
  and a verified AUTH gets one OK per nonce, so replayed HELLOs, however many, can't
  take the phone's OK.
- **`--resume`:** the running copy answers `RESUMED`/`DENIED`; the CLI exits 0/1/2
  (resumed / refused or none running / unconfirmed). A bare RESUME (an older
  `--resume`) gets a warning naming that cause.
- **Mute:** `VOL n [0|1]` from Windows (pycaw `GetMute`); the phone shows a toggle
  when the laptop reports mute, the plain button when it doesn't. Reset when the
  phone connects to another laptop. One reading is skipped after a lost VGET, since
  its late reply would carry pre-tap state. A failed mute read no longer drops the
  volume, and no output device skips the mute read.
- **Discovery:** the list clears when a Wi-Fi or Ethernet network is lost (watched
  directly: behind a VPN the default network never changes). Results are
  delivered under the lock, so a stopped run can't write back. NSD's
  already-active refusal is retried (5 × 300 ms).
- **Diagnosis:** the phone's own network is checked before "asleep"; its addresses
  come from Wi-Fi/Ethernet networks by transport, never a VPN or mobile data; hosts
  that aren't IPv4 literals skip the subnet check. A same-named laptop's refusal at
  a new address beats the saved address's silence.
- **Reconnect hint:** re-derived every attempt, but a definite refusal (re-paired,
  needs QR, outdated) stands until another replaces it.
- **Settings rows** are one `toggleable` with `Role.Switch`.
- **SIO_UDP_CONNRESET never took effect** (since before this branch): Python's
  socket module has no such constant, and the `AttributeError` was swallowed, on
  the server and in the tests alike. Now set via `WSAIoctl` (`udp_resets_off`).
  The server was safe anyway (`serve_loop` catches the reset); the tests weren't,
  which is the old ~10% WinError 10054 flake. Its cause was the first packet
  beating the bind; `_start` now waits for `serving`. The earlier "6 of 6 green"
  was luck.
- **Copy:** the Settings update text and `UpdateChecker` KDoc no longer claim the
  check is the only internet use; "v2" wire naming → L3; "L2 until 2.2.0".

## Gates (working tree)
- Server: 224 tests OK (1 skip, Windows-only), 3 of 3 runs, ~34 s; the former
  flake 0 of 30 in isolation (it failed 5 of 50 before).
- Android: 96 unit tests, 0 failures; `assembleRelease` builds. (Gradle's daemon
  died of host out-of-memory twice with only 1.7 GB commit free; the build then ran
  with `-Pkotlin.compiler.execution.strategy=in-process` after stopping my leftover
  Kotlin daemon.)
- Mutation checks, this round: server 9 of 9 red (incl. the reset helper doing
  nothing and `open_socket` skipping it), Android 5 of 5 red (the host parser:
  mapped, ::/80, leading zeros, `::` for zero groups, multicast).
- Clean copy of just the commit-bound files (no `.lazer_*`, no
  `local.properties`), on the final tree: server 224 OK, Android 96/0/0 plus
  `assembleRelease`.
- The Gradle wrapper checksum matches services.gradle.org; workflow YAML parses
  and all 7 pinned action SHAs are tagged releases.
- On the phone (Pixel 6 Pro, release build, laptop on its older LazeR.exe):
  - auto-connects; Settings shows the new update text; Mute is the plain button;
  - bionic reads `012.0.0.1` as 10.0.0.1 (`ping`), which is why leading zeros go;
  - Wi-Fi off with a VPN up (only IPv4: `tun0` 172.18.x): "This phone has no Wi-Fi
    address" (the VPN address is no longer counted);
  - Wi-Fi off behind that VPN clears "Found on your network" (it stayed before the
    Wi-Fi callback);
  - Wi-Fi off mid-session → Reconnecting → Wi-Fi on → reconnects by itself.

## Not verified
- On the phone:
  - QR scanning, the replace prompt, and a scan on the Reconnecting screen (P2);
  - the sticky refusal hint and "asleep" ordering (need a re-paired or sleeping
    laptop);
  - typing; TalkBack; small screens;
  - an end-to-end in-app download and install (no published release has
    `LazeR.apk.sha256` yet);
  - the mute toggle and its per-laptop reset against a laptop that reports mute.
- The `--resume` exit codes from the CLI itself (only the mapping is tested; running
  it here would poke the real running LazeR).
- Not exercised at all: the elevated firewall command; the release workflow and
  Dependabot running; mute on macOS and Linux; a real campus or CGNAT network;
  NSD's already-active refusal.
- Compatibility: an old phone against the new laptop is covered only by the server's
  no-nonce tests.
