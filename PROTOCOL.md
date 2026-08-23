# Remote Control Wire Protocol

Transport: **UDP**, single port (default `50505`). One UTF-8 text line per datagram.
Every datagram is a single packet. No framing beyond the datagram boundary.

## Packet grammar

```
<TOKEN> <VERB> [args...]
```

- `<TOKEN>` — the shared secret string shown by the server at startup. Present on **every**
  **v1 (plaintext)** packet (handshake and all control packets); packets with a wrong or
  missing token are silently dropped. On the **v2/v3 secure wire** no token rides the wire
  at all — a valid GCM tag *is* the authentication, because it proves the sender holds the
  key that only the QR ever carried.
- The server parses at most three fields: `token`, `verb`, and a single `rest` string.
  So `KEY hello world` delivers the literal text `hello world` (spaces preserved).

### Verbs

| Packet                        | Meaning                                              | Reply              |
|-------------------------------|------------------------------------------------------|--------------------|
| `<TOKEN> HELLO`               | Handshake, step 1. v1: pins sender + `OK`. v2: draws a challenge. | `OK` (v1) / `CHAL <nonce>` (v2) |
| `<TOKEN> AUTH <nonce>`        | Handshake, step 2 (v2 only). Echo the `CHAL` nonce to be pinned. | `OK`               |
| `<TOKEN> MOVE <dx> <dy>`      | Relative cursor move, signed ints (px).              | none (lossy)       |
| `<TOKEN> SCROLL <dx> <dy>`    | Scroll wheel, signed ints (steps). +dy = up.         | none (lossy)       |
| `<TOKEN> ZOOM <steps>`        | Ctrl+wheel zoom (pinch). +steps = in, − = out.       | none (lossy)       |
| `<TOKEN> CLICK`               | Left mouse click.                                    | none               |
| `<TOKEN> RCLICK`              | Right mouse click.                                   | none               |
| `<TOKEN> MCLICK`              | Middle mouse click.                                  | none               |
| `<TOKEN> MDOWN`               | Press & hold left button (drag-lock start).          | none               |
| `<TOKEN> MUP`                 | Release left button (drag-lock end).                 | none               |
| `<TOKEN> PING`                | Liveness probe (reconnect watchdog).                 | `PONG`             |
| `<TOKEN> VOL <0-100>`         | Set system volume to absolute percent.               | none               |
| `<TOKEN> VGET`                | Ask server for current system volume.                | `VOL <0-100>`      |
| `<TOKEN> BRIGHT <0-100>`      | Set display brightness to absolute percent.          | none               |
| `<TOKEN> BGET`                | Ask server for current display brightness.           | `BRI <0-100>`      |
| `<TOKEN> MEDIA <action>`      | `play_pause` \| `next` \| `prev`.                    | none               |
| `<TOKEN> KEY <text>`          | Type the literal UTF-8 text (spaces preserved).      | none               |
| `<TOKEN> KEYSP <name>`        | Press one special key (see below).                   | none               |
| `<TOKEN> COMBO <mods..> <key>`| Hold modifiers, tap key. e.g. `ctrl c`, `alt tab`.   | none               |
| `<TOKEN> ASW <action>`        | App-switch session: `next`\|`prev`\|`end`.           | none               |
| `<TOKEN> SYS <action>`        | `lock` \| `sleep` \| `mute`.                         | none               |
| `<TOKEN> BYE`                 | Client disconnects; server forgets it.               | none               |

`KEYSP <name>` names: `enter`, `backspace`, `space`, `tab`, `esc`, `delete`,
`up`, `down`, `left`, `right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`.

`COMBO` modifiers: `ctrl`, `alt`, `shift`, `win`/`cmd`. The final token is the
key — a single literal char, a `KEYSP` name, or `f1`–`f12`.

`ASW` drives the OS app switcher as a held session. The first `next`/`prev` presses
and **holds** `Alt`, then taps `Tab` (`next`) or `Shift+Tab` (`prev`); each further
`next`/`prev` taps again while `Alt` stays down, so the switcher cycles forward
through every window. `end` releases `Alt` and commits the highlighted app. The
server also auto-releases `Alt` on `BYE` or a new `HELLO`, so a dropped connection
mid-gesture can't leave `Alt` stuck. Maps to the Windows three-finger touchpad swipe.

### Replies (server → client)

- `OK` — handshake accepted; sender is now the registered controller.
- `CHAL <nonce>` — answer to a **v2** `HELLO`: a one-time, base64url random nonce the
  client must echo in `AUTH` before it is pinned (see the handshake note under v2).
- `VOL <0-100>` — current laptop volume, sent in answer to `VGET`. Lets the phone
  keep its slider in sync with the laptop's real volume (two-way).
- `BRI <0-100>` — current laptop display brightness, sent in answer to `BGET`. Lets
  the phone keep its brightness slider in sync (two-way, same shape as `VOL`).
- `PONG` — answer to `PING`; lets the phone confirm the laptop is still alive and
  trigger an auto-reconnect when it goes silent.

## Wire formats

Two datagram encodings exist; the server auto-detects per packet.

### v2/v3 — secure (default for QR pairing)

```
packet = MAGIC (2) | nonce (12) | AES-256-GCM(ciphertext+tag)
AAD    = the packet's first 14 bytes
plaintext = "<VERB> [args]"            (the v1 line minus the token)
```

Two dialects differ **only** in how the 12-byte nonce is split. The header is 14
bytes either way, so framing, AAD and every other rule below are identical:

| Magic | nonce split              | session space | status |
|-------|--------------------------|---------------|--------|
| `L3`  | `sid(8)` \| `counter(4)` | 2^64          | **current** |
| `L2`  | `sid(4)` \| `counter(8)` | 2^32          | removed — accepted through v2.x for un-updated phones |

**Why the split moved.** The key is *persistent* across launches while the `sid` is
random per session, so a `sid` collision means GCM nonce reuse under one key — which
leaks the authentication key, not merely a plaintext. A 4-byte `sid` put that at the
birthday bound of 2^32: roughly 1.2% odds by 10 000 sessions and 39% by 65 000. Every
reconnect mints a session and the phone's watchdog reconnects on any drop, so those
counts are reachable over a device's lifetime. Moving four bytes from the counter to
the `sid` buys 2^64 at no practical cost — a 4-byte counter still allows 4.29e9
packets in a single session, and exhausting it re-keys the `sid` rather than wrapping.

**Compatibility.** The server replies in whichever dialect the client opened with
(otherwise the phone couldn't read its own `CHAL`). The L2 dialect was accepted
through v2.x so a phone updated ahead of its laptop kept pairing; it is now
removed — an L2 packet is unknown magic and is dropped like any other junk, so a
phone that never updated past v1.x must update to pair with this server.

- The 256-bit key is shared **only** via the QR (`&k=` below); never on the wire,
  never over mDNS. A valid GCM tag *is* the authentication — it proves the sender
  holds the key, so no token rides secure packets.
- `sid` is a random per-session id the client picks at connect (8 bytes);
  `counter` is a per-session monotonic integer filling the rest of the
  nonce (first `HELLO` = 1, then +1 per send).
- A session is identified by **dialect + sid** together, so a packet that switches
  dialect mid-session is refused like any other unpinned session.
- **Handshake (challenge-response).** A valid GCM tag proves key possession but NOT
  freshness, so a captured `HELLO`+control stream could otherwise be replayed by
  anyone who lacks the key — an on-path observer on the same network can record
  ciphertext without being able to forge it. So a `HELLO` is **not** pinned on
  arrival: the server replies
  `CHAL <nonce>` (a fresh single-use random nonce, encrypted), and only an `AUTH`
  that echoes that nonce (which requires the key to seal) pins the client — with the
  `AUTH` packet's `sid`/counter as the session baseline. A replayed `HELLO` just
  draws a new challenge the replayer can't answer; a replayed `AUTH` carries a stale
  nonce and is rejected. Challenges are single-use and expire in a few seconds.
- **Replay/forgery:** after pinning, every later packet must carry the pinned `sid`
  with a **strictly greater** counter, and must come **from the pinned client** —
  the counter watermark advances only for such packets, so a tag-valid replay from a
  stranger can't desync the real client. Forged packets fail the tag.
- Replies (`CHAL`/`OK`/`PONG`/`VOL n`) are encrypted the same way with the server's
  own `sid`/counter. The **client** likewise pins the server's `sid` on the first
  reply and requires a strictly-greater counter, so replies can't be replayed to it.
- Confidentiality: keystrokes and all args are encrypted, not just authenticated.

### v1 — plaintext (legacy, trusted-LAN only)

`"<TOKEN> <VERB> [args]"` as before. Used when pairing by **manually typed code**
(no key). Offers no confidentiality and is replayable/spoofable on the wire — fine
on a trusted home network, unsafe on open Wi-Fi. **The server rejects v1 by
default**; `--allow-plaintext` (or turning **Require encryption** off in the GUI)
permits it. A refused packet whose token actually matches is reported to the UI, so
a manual-code attempt gets an explanation instead of a silent timeout.

## Security model

1. Server boots, loads/generates a persistent token **and** a 256-bit key, prints
   them + LAN IP. The key goes only into the on-screen QR.
2. **Secure (v2):** the client encrypts `HELLO`; the server answers a one-time
   `CHAL` and pins that client's `(ip, port)` + `sid` only after a matching `AUTH`
   (challenge-response — see the v2 handshake note). This makes pinning **fresh**, so
   a captured session can't be replayed by a keyless attacker. Every later packet
   must carry a valid tag, the pinned `sid`, an increasing counter, **and** the pinned
   source, or it is dropped. Re-pinning (reconnect from a new port) is safe: it runs
   the full challenge, and only the key holder can complete it.
3. **Plaintext (v1):** token match pins `(ip, port)`; later packets need the token
   **and** the pinned source. A plaintext re-pin from a new address is accepted (so
   reconnects work) but logged as a warning — turn on Require encryption to forbid it.
4. **Brute-force / flood:** a high rate of rejected packets raises a warning and
   pauses manual-code (plaintext) acceptance briefly — the only path a token
   brute-force exists against. QR-paired phones are unaffected.
5. **Local takeover:** physical mouse/keyboard input on the laptop (detected via
   non-injected low-level hooks) pauses the remote so the user's own device always
   wins; `Ctrl+Alt+Shift+L` latches the remote OFF until the user resumes.

Movement/scroll are lossy by design: a dropped packet just means a slightly shorter
gesture. Discrete actions (CLICK/RCLICK/VOL/MEDIA/KEY) also ride UDP — fine for LAN.

## Discovery & pairing (out-of-band, not UDP control packets)

- **Persistent token.** The server stores its token in `server/.lazer_token` and
  reuses it across launches, so saved phones reconnect without re-pairing.
- **mDNS / Bonjour.** The server advertises service type `_lazer._udp.` on port
  `50505` with a `name` TXT property. The phone discovers laptops automatically; the
  advertisement carries **no token** (IP/port/name only).
- **QR code.** At startup the server prints a QR encoding a connection URI:
  ```
  lazer://<ip>:<port>/?token=<token>&name=<hostname>&k=<base64url-256-bit-key>
  ```
  Scanning it fills everything and connects on the **secure (v2)** wire in one tap.
  Both token and key travel only in the QR (shown on the laptop screen), never over
  mDNS. Manual entry has no `k`, so it uses plaintext v1.

> **Windows note.** Sending UDP to an endpoint with no listener — routine when we
> reply to a phone that has just vanished — makes the OS raise `WSAECONNRESET` on the
> socket's next receive. The server disables that report (`SIO_UDP_CONNRESET` off)
> and ignores the error, so a departed phone never tears down the receive loop.
