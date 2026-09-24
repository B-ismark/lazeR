# Security findings — 2026-09-23

Scope: wire protocol and crypto (server `Wire`, phone `SecureChannel`/`RemoteClient`),
secret storage, Android manifest/backup, QR, update check, local server surface,
build and release supply chain.

Verification key: **[repro]** reproduced by running code · **[code]** confirmed by
reading the cited lines · **[inferred]** follows from code, not exercised.

---

## S-1 [HIGH] One unauthenticated datagram kills the receive loop when encryption is off
**File:** `server/remote_server.py:879` (called unguarded from `serve_loop` at `:2637`)
**Issue:** The plaintext (v1) path compares the token with
`secrets.compare_digest(parts[0], self.token)` on *str* values outside any `try`.
`compare_digest` raises `TypeError` for non-ASCII strings. The packet is decoded with
`"ignore"`, so any UTF-8 first word reaches it. This breaks the CLAUDE.md rule
"nothing on the UDP thread may raise". The secure-only branch just above (`:865-870`)
is wrapped and is safe.
**Evidence:** [repro] `secrets.compare_digest('é', 'ABC123')` → `TypeError comparing strings with non-ASCII characters is not supported`.
The subagent reproduced it through `Wire(..., require_secure=False).parse('é HELLO'.encode(), …)`.
**Reachability:** Off by default. It is reachable after `--allow-plaintext` **or** after the
GUI's "Require encryption" toggle is switched off (`:3451`). Users who pair by typing the
code must do one of those (see U-1 in findings-ux-flows.md).
**Scenario:** Any LAN host with no token sends `b"\xc3\xa9 X"`. The loop dies, and
`serve_forever` restarts it with `_stop.wait(min(1.0 * failures, 5.0))`. `failures` is
never reset (`:2797-2809`), so after five packets each one costs about 5 s of outage and
drops the phone. One packet every 5 s keeps the laptop unusable.
**Suggested fix:** Compare bytes (`data.split(b" ", 2)[0]` against `token.encode()`), or
wrap the v1 branch like the secure one. Reset `failures` after a healthy run. Add hostile
plaintext bytes to the `serve_loop` tests; the existing `HOSTILE` list at
`tests/test_wire.py:519` only feeds the activity-label tests.

## S-2 [MEDIUM] Pairing secrets inherit the folder's permissions on Windows
**File:** `server/remote_server.py:736-741`, `:757-768`, `:1788-1792`
**Issue:** `.lazer_key` and `.lazer_token` are written with a plain `open(..., "w")`.
`os.chmod(0o600)` runs only off Windows. The files sit next to the exe (`_APP_DIR`).
**Evidence:** [repro] `icacls dist\.lazer_key` shows `NT AUTHORITY\Authenticated Users:(I)(M)` and `BUILTIN\Users:(I)(RX)`.
**Scenario:** On a shared PC, with the exe outside the user profile (for example `D:\`,
as here), another account can read both files and drive the pointer and keyboard from
its own phone. Or it can replace the key, and the next launch hands control to it.
Inside `%USERPROFILE%` (for example Downloads) the inherited permissions are owner-only,
so the risk depends on where the exe lives.
**Suggested fix:** On Windows, create the files with an owner-only, non-inherited
access list (current user + SYSTEM), set atomically at creation.

## S-3 [MEDIUM] Manually paired phones hand their token, and then every keystroke, to any host that advertises `_lazer._udp`
**File:** `android/.../RemoteViewModel.kt:486-503` (`connectResolving`), `net/RemoteClient.kt:127`, `:150`
**Issue:** When the saved IP fails, the phone tries every mDNS-discovered host with the
saved token. For a keyless (manual-entry) device that is a cleartext `"$token HELLO"`, and
the v1 path accepts a plain `OK` from anyone. The comment at `:482-483` says "The wrong
host simply fails the authenticated handshake, so trying them is safe". That holds only
for devices paired by QR. The reconnect loop calls this with no give-up.
**Evidence:** [code] The loop `for (h in candidates) { if (client.connect(h.ip, h.port, dev.token, dev.key, …)) return dev.copy(ip = h.ip …) }`, followed by `store.upsert` of the new IP.
**Scenario:** While the laptop sleeps, a LAN attacker publishes a fake mDNS record.
The phone sends it the token (no sniffing needed, so switched or enterprise Wi-Fi
doesn't help), accepts its `OK`, saves its IP permanently, and then streams every typed
character to it in plaintext. The attacker can also use the token against an
`--allow-plaintext` laptop.
**Suggested fix:** Never fall back to discovered hosts for a keyless device, or ask the
user first. Never persist a new IP learned through a v1 handshake.

## S-4 [MEDIUM] The phone accepts a replayed server reply from an old session as the first reply of a new one
**File:** `android/.../net/SecureChannel.kt:92-94`, `net/RemoteClient.kt:144`
**Issue:** The client pins the server's session id from whatever first sealed reply it
receives (`if (pinned == null) { srvSid = rsid; recvCtr = ctr }`). The key is persistent,
and `CHAL`/`OK` are not bound to the client's HELLO or session id, so any genuine sealed
server packet captured earlier passes. PROTOCOL.md claims "The client likewise pins the
server's sid on the first reply ... so replies can't be replayed to it." That holds only
after the first reply.
**Scenario:** A LAN host with captured replies races a replayed `OK`. The phone shows
Connected, pins the attacker's session id, and then rejects the real server. Combined
with S-3's IP persistence, the wrong IP can be saved. The attacker cannot decrypt or
forge input, so the effect is denial of service, misdirection, and metadata leakage.
[inferred] Not exercised on a device.
**Suggested fix:** Bind replies to the handshake. Put a client nonce (or the client
session id) in HELLO, echo it in CHAL/OK or the reply AAD, and reject replies that don't
echo it. This is a wire change, so bump the dialect.

## S-5 [MEDIUM] The release supply chain is not pinned
**Files:** `.github/workflows/release.yml:63-80,116,127-175`, `tools/build_exe.ps1:43-54`,
`server/requirements.txt`, `android/gradle/wrapper/gradle-wrapper.properties`, `tools/publish_release.ps1`
**Issue:** Several related gaps:
- **Actions pinned by tag, not SHA, in the signing job.** In the `apk` job,
  `android-actions/setup-android@v3` and `gradle/actions/setup-gradle@v4` run while the
  decoded keystore is on disk. The keystore password is the well-known `android`
  (documented), so the file alone is enough to sign.
- **The release exe bundles unpinned Python dependencies.** `build_exe.ps1` installs bare
  names, and `requirements.txt` uses only `>=` floors with no hashes.
- **No `distributionSha256Sum`** in the Gradle wrapper properties.
- **`publish_release.ps1` (the local fallback) never checks the APK signer.** The CI path
  does, at `release.yml:106-113`.
**Scenario:** A moved tag (the tj-actions pattern) exfiltrates `LAZER_KEYSTORE_B64`, and
the attacker can then ship APKs that install as updates on every user's phone. That is
unrecoverable without every user uninstalling. A compromised PyPI release ships inside
an exe that injects input.
**Evidence:** [code] `pip-audit` on today's resolution of `requirements.txt` shows **no
known vulnerabilities**. The risk is future drift, not a current CVE.
**Suggested fix:** Pin actions to commit SHAs (at least in `apk`), with Dependabot to
bump them. Use a hashed lock file (`uv pip compile --generate-hashes`) in both
`build_exe.ps1` and CI. Add `distributionSha256Sum`. Add the signer check to
`publish_release.ps1`.
**Related (informational):** The `release` environment has no required reviewer, and
`can_admins_bypass: true`, so anyone with push access can sign and publish by pushing a
`v*` tag. `ci.yml` has no `permissions:` block; the repo default is `read`, but an
explicit block is cheap.

## S-6 [LOW] A QR with a malformed key silently falls back to the plaintext wire
**File:** `RemoteViewModel.kt:365`, `SecureChannel.kt:118-124`, `RemoteClient.kt:55-56`
**Issue:** `keyFromBase64` returns null for a `k` that is present but invalid, and
`channel = rawKey?.let { SecureChannel(it) }` then sends a plaintext HELLO carrying the
token. PROTOCOL.md covers only a *missing* `k`.
**Scenario:** A truncated or crafted QR puts the token on the air. A default server
refuses it; an `--allow-plaintext` server runs the session unencrypted.
**Suggested fix:** Fail closed. A non-blank `k` that won't decode should show
"Unrecognized QR code".

## S-7 [LOW] The QR host isn't restricted to the LAN, and a scanned QR silently replaces a saved device
**File:** `RemoteViewModel.kt:358-359`, `DeviceStore.kt:154-156`
**Issue:** The phone accepts any `uri.host`, including a public hostname, and `upsert`
overwrites the matching `ip:port` record without asking.
**Scenario:** A planted QR routes all input, typed passwords included, to an internet
host. Or it quietly swaps a saved laptop's token and key.
**Suggested fix:** Accept only private, link-local or `.local` hosts. Confirm before
replacing a saved device whose key differs.

## S-8 [LOW] Oversized datagrams stall the receive loop (default config, no auth)
**File:** `server/remote_server.py:2620`, `:2627-2633`
**Issue:** `recvfrom(2048)` raises `OSError [WinError 10040]` on a larger datagram, and
the generic `except OSError` sleeps 100 ms.
**Scenario:** About 10 packets/s over 2048 bytes (~30 KB/s) keeps the loop mostly
asleep. Moves drop, pings time out, and the phone falls into reconnect churn. A LAN
flood can always degrade service; this makes it roughly 100 times cheaper.
[inferred] The per-packet error was reproduced; end-to-end throughput loss was not measured.
**Suggested fix:** On WSAEMSGSIZE, `continue` with no sleep, or read into a 64 KB buffer.

## S-9 [LOW] On Linux/macOS, the token file is never restricted and the key is restricted only after it is written
**File:** `server/remote_server.py:1791`, `:738-741`, `:765-768`
**Issue:** The token file is created with the umask (usually 0644). The key is written
first and then chmodded.
**Suggested fix:** Create both with `os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)`.

## S-10 [LOW] The single-instance port accepts an unauthenticated RESUME
**File:** `server/remote_server.py:1694`, `:1759-1762`
**Issue:** Loopback TCP 50506 clears the panic latch for any local caller. On a
multi-session machine (fast user switching, RDP), loopback is shared across sessions.
The impact is bounded, since a local process in the same session can inject input anyway.
**Suggested fix:** Require a secret from a user-private file, or use a per-user named pipe.

## S-11 [LOW] External programs, including an elevated one, are launched by bare name
**File:** `server/remote_server.py:2264` (elevated `cmd.exe`), `:2041-2042`, `:2183`, `:2198`, `:306`, `:334`
**Issue:** `CreateProcess` searches the application directory and the current directory
before System32, so a `cmd.exe` or `netsh.exe` planted next to the exe could be picked up.
Argument construction itself is safe: all arguments are constants.
[inferred] Not tested for exploitability.
**Suggested fix:** Use absolute `%SystemRoot%\System32\…` paths.

## S-12 [LOW] A replayed AUTH can burn the phone's pending challenge
**File:** `server/remote_server.py:936-941`
**Issue:** `self._chal.pop(addr, None)` consumes the challenge on any AUTH with a valid
tag, match or not. A spoofed replay between CHAL and the real AUTH makes the real one
fail, and the phone retries. This only stalls the handshake.
**Suggested fix:** Consume the challenge only on a match, or key challenges by (addr, client session id).

## S-13 [LOW] KEY packets leak typing length and timing
**File:** `RemoteViewModel.kt:819`, `:919`
**Issue:** GCM adds no padding, and each IME commit is its own packet, so a passive
observer learns chunk lengths and inter-key timing (the classic keystroke-timing side channel).
**Suggested fix:** Pad KEY bodies to fixed size buckets.

## S-14 [LOW] Unused permission `CHANGE_WIFI_MULTICAST_STATE`
**File:** `android/app/src/main/AndroidManifest.xml:10`
**Issue:** [code] Grep finds no `createMulticastLock` anywhere, and NsdManager doesn't
need the permission.
**Suggested fix:** Remove it, then confirm discovery still works on a device.

---

## Checked and sound
- **Server nonces:** a random 8-byte session id per process, and the counter re-draws the
  session id instead of wrapping. The phone gets a fresh session id per connect and checks
  its counter bound.
- **Replay into the server:** requires the pinned address, the magic, the session id, and
  a strictly increasing counter. The watermark advances only for pinned packets.
- **Handshake:** a single-use challenge with a 6 s TTL. Challenge state is bounded
  (`CHAL_PER_IP=8`, `CHAL_MAX=256`, heaviest-source eviction). Unauthenticated packets get
  no reply, apart from about 1.6x on a *replayed* HELLO.
- **Constant-time comparisons:** `compare_digest` is used for the token and the challenge;
  the library checks GCM tags.
- **No secrets in logs:** KEY content is not logged (only its length).
- **No downgrade while plaintext is off:** a missing `cryptography` means nothing is
  accepted, so it fails closed.
- **Android storage:** tokens and keys sit in an AES-GCM blob sealed by an Android
  Keystore key. `allowBackup=false`, and the data extraction rules exclude the device
  store from cloud backup and device-to-device transfer.
- **Manifest:** only the launcher activity is exported. No deep links, no cleartext
  override, release builds not debuggable, R8 on.
- **Update check is notify-only on both halves:** a constant HTTPS URL, only `tag_name`
  is read, rendered as plain text, and the only link opened is a constant.
- **Server subprocesses:** list form everywhere, with constant strings. No filenames come
  from the network.
- **Git history:** no `.lazer_*`, keystore or `.jks` file was ever committed.
- **Dependencies:** `pip-audit` finds no known vulnerabilities (today's resolution).

## Intentional, documented — not reported
- Release signing with the debug keystore (password `android`).
- The global 60 s pause of plaintext pairing after a flood, which any LAN host can
  trigger. It is global on purpose, so a flooder can't frame the phone's address.
- An authenticated phone has full keyboard and mouse control, which amounts to code
  execution by design. There are no file, URL or shell verbs.
- The firewall rule on Profile=Any.
- The Keystore key doesn't require user authentication, and there is a plaintext
  fallback if the Keystore is unavailable.
- A 6-character token (about 31 bits); it matters only under `--allow-plaintext`, where
  rate limiting applies.
