# Platform Audit — 2026-09-23

LazeR at `main` @ `5462025`, covering the Android client (Kotlin/Compose), the Python
server (Tk GUI + CLI), the launchers and docs, and CI/release.

This is a report only: no code was changed. The audit template's web-app dimensions
were adapted to this product:
- **Security** is the wire, secrets, Android and supply chain.
- **Data integrity** is persisted state (pairing secrets, saved devices, published address).
- **Performance** is reliability plus the cursor hot path.
- **UX & flows** is its own file, because it was the primary ask.

## How findings were verified
- Five narrow read-only reviews ran in parallel.
- Every High and most Medium findings were then re-checked against the code by hand.
- Reproduced by running code:
  - **S-1**: `compare_digest` raises `TypeError`.
  - **S-2**: `icacls` on the real secret file.
  - **S-8**: the `WinError 10040` path.
  - A `pip-audit` of today's resolution of `requirements.txt` (no known vulnerabilities).
- Everything else is from reading code.

**Not verified:** nothing was run on a phone or emulator, and the laptop GUI was not
opened. No screen was looked at, TalkBack was not tried, neither test suite was run, and
the Android race conditions (R-2, R-7) and the replay attack (S-4) were not exercised.
Layout claims (for example an error line landing off-screen) are inferred from code.

## Summary
| Severity | Count |
|---|---|
| Critical | 0 |
| High | 3 |
| Medium | 21 |
| Low | 35 |
| **Total** | **59** |

There are no Critical findings. The secure wire is well built: nonces, replay
protection into the server, bounded handshake state, constant-time comparisons,
Keystore-sealed storage on the phone, and backups excluded. The serious problems sit
around it: the opt-in plaintext path, the first-reply case on the phone, and file
permissions.

## Top 10 issues (in remediation order)
1. **[HIGH] One unauthenticated packet kills the receive loop when encryption is off.**
   Any LAN host can keep the laptop down with one datagram every 5 s once plaintext is
   allowed (CLI flag or GUI toggle). → [findings-security.md, S-1](audit/findings-security.md#s-1-high-one-unauthenticated-datagram-kills-the-receive-loop-when-encryption-is-off)
2. **[HIGH] Typed-code pairing is offered in six places, fails by default, and is blamed
   on the firewall.** Both the phone and the laptop offer a path the default config
   silently drops. → [findings-ux-flows.md, U-1](audit/findings-ux-flows.md)
3. **[HIGH] The phone can't tell connect-failure reasons apart.** Re-paired, refused,
   firewall and asleep all read as "didn't answer… firewall". This is the main support
   dead end, and it is fixed together with #2. → [U-6](audit/findings-ux-flows.md)
4. **[MEDIUM] Manually paired phones send their token, and then every keystroke, to any
   mDNS advertiser.** The "trying them is safe" comment is false for keyless devices.
   → [S-3](audit/findings-security.md)
5. **[MEDIUM] A held drag button is never released on disconnect or local takeover.** The
   physical mouse keeps dragging until the next click. → [findings-performance.md, R-1](audit/findings-performance.md)
6. **[MEDIUM] Pairing secrets inherit the folder's permissions on Windows.** Observed as
   `Authenticated Users:(M)` on `dist\.lazer_key`. → [S-2](audit/findings-security.md)
7. **[MEDIUM] "Regenerate" says it worked when the write failed, and the kicked phone gets
   back in after a restart.** → [findings-data.md, D-1](audit/findings-data.md)
8. **[MEDIUM] The signing job runs third-party actions pinned by tag; the release exe
   bundles unpinned dependencies.** A hijacked tag would leak the key that every
   installed APK trusts. → [S-5](audit/findings-security.md)
9. **[MEDIUM] A second server instance still binds UDP 50505.** This is the recorded
   "multi-server storm", and some double-launches look like nothing happened.
   → [R-3](audit/findings-performance.md)
10. **[MEDIUM] Auto-connect at launch tries once and never enters the reconnect loop.**
    After process death, the user must re-tap. That breaks CLAUDE.md's "no re-tap" rule.
    → [U-8](audit/findings-ux-flows.md)

Next in line: the phone accepting a replayed server reply as the first handshake reply
(S-4); a stale connect tearing down a fresh one (R-2); laptop warnings visible only in a
collapsed log (U-7); tests that can't fail (C-1).

## Findings by dimension
### Security → [audit/findings-security.md](audit/findings-security.md)
1 High, 4 Medium, 9 Low. Includes a "checked and sound" list and the intentional design
decisions that were not reported.

### Data / state integrity → [audit/findings-data.md](audit/findings-data.md)
1 Medium, 5 Low. Covers the failed-rotation revert, an encrypted pairing downgraded by
the manual form, duplicate device rows, `lan_ip()` published at startup, and cross-half
constants with no test.

### Reliability & performance → [audit/findings-performance.md](audit/findings-performance.md)
3 Medium, 6 Low. The hot path is sound (trackpad motion doesn't recompose). The issues
are held input, connection lifecycle races and single-instance handling.

### UX, redundancy & user flows → [audit/findings-ux-flows.md](audit/findings-ux-flows.md)
2 High, 11 Medium, 10 Low. Split into **A. Redundant or conflicting surfaces** and
**B. Flow gaps**. It includes a surface inventory and a list of things that *look*
redundant but are justified.

Headline redundancies:
- **Pairing paths:** typed-code pairing is offered in six places and works in none of
  them by default.
- **Launchers:** seven launch or autostart surfaces, four of which run a copy of the
  server that is untracked and stale on this machine.
- **Autostart:** two mechanisms that contradict each other.
- **Laptop status:** static "server running" and encryption indicators; the connected
  state shown three times.
- **Phone labels:** two different buttons both called "Scan".

### Code quality & tests → [audit/findings-ui-code.md](audit/findings-ui-code.md)
2 Medium, 5 Low. Covers tests that can't fail, coverage gaps that line up with the
defects found here, duplicated secret handling, dead code, and stale relay comments.

### Visual → not performed
There is no web build, and the audit template's browser flow doesn't apply. A real
visual pass needs:
- the APK on a device or emulator, in portrait, landscape, light and dark, with TalkBack
- the Tk window on Windows, including at 125% and 150% scaling

Neither was done, so every layout finding is inferred from code.

## Deferred / won't fix (intentional, documented)
- **Release signing with the debug keystore (password `android`)** is documented in CLAUDE.md.
  Its risk is covered under S-5 rather than reported separately.
- **The global 60 s pause on plaintext pairing after a flood** can be triggered by any LAN
  host. It is global on purpose, so a flooder can't frame the phone's address.
- **An authenticated phone has full keyboard and mouse control** (effectively code
  execution); that is the product.
- **The firewall rule on Profile=Any.**
- **The phone doesn't coalesce moves**, deliberately (`RemoteViewModel.kt:740-743`).
- **Elevated windows can't be driven (UIPI).** The user has already chosen to leave this
  as-is (project memory).
- **The Require-encryption toggle resets to On at launch.** Likely intentional (safe
  default); only the missing documentation is reported (D-5).
- **The Keystore key has no user-authentication requirement, and there is a plaintext
  fallback if the Keystore is unavailable.** Documented trade-offs in `DeviceStore.kt`.
- **The `remote_server.py` size (4.2k lines).** No concrete harm found beyond C-3, so no split is recommended.

## Totals and effort
- **Total findings:** 59 (0 Critical, 3 High, 21 Medium, 35 Low).
- **Top 3:**
  - S-1: receive-loop crash in plaintext mode.
  - U-1: dead-end typed-code pairing.
  - U-6: failures that can't be told apart.
- **Rough remediation effort:**

| Work | Estimate |
|---|---|
| S-1 + test | ~1 h |
| U-1 + U-6: distinct server refusal reply, phone messaging, UI/doc cleanup, tests on both halves | 6–8 h |
| S-3 | ~1 h |
| R-1 | ~1–2 h |
| S-2 (Windows access list via ctypes or icacls) | 2–3 h |
| D-1 | ~1 h |
| S-5 | 1–2 h |
| R-3 | ~2 h, plus a sleep/resume regression check |
| U-8 | ~1 h |
| **Top 10 subtotal** | **~17–22 h** |
| S-4 (wire change, dialect bump, golden vectors) | 4–6 h |
| All remaining Medium | ~15–20 h |
| All Low | ~15 h |
| **Everything** | **~55–65 h** |
