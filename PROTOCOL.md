# Remote Control Wire Protocol

Transport: **UDP**, single port (default `50505`). One UTF-8 text line per datagram.
Every datagram is a single packet. No framing beyond the datagram boundary.

## Packet grammar

```
<TOKEN> <VERB> [args...]
```

- `<TOKEN>` — the shared secret string shown in the laptop window. Present on **every**
  **v1 (plaintext)** packet (handshake and all control packets); packets with a wrong or
  missing token get no reply at all, so the typed code can't be guessed by watching for
  one. On the **secure wire (L3)** no token rides the wire at all — a valid GCM tag *is*
  the authentication, because it proves the sender holds the key that only the QR ever
  carried.
- The server parses at most three fields: `token`, `verb`, and a single `rest` string.
  So `KEY hello world` delivers the literal text `hello world` (spaces preserved).

### Verbs

| Packet                        | Meaning                                              | Reply              |
|-------------------------------|------------------------------------------------------|--------------------|
| `<TOKEN> HELLO [<cnonce>]`    | Handshake, step 1. v1: pins sender + `OK`. Secure: draws a challenge; the optional client nonce binds the replies (below). | `OK` (v1) / `CHAL <nonce> [<cnonce>]` (secure) |
| `<TOKEN> AUTH <nonce>`        | Handshake, step 2 (secure only). Echo the `CHAL` nonce to be pinned. | `OK [<cnonce>]`    |
| `<TOKEN> MOVE <dx> <dy>`      | Relative cursor move, signed ints (px).              | none (lossy)       |
| `<TOKEN> SCROLL <dx> <dy>`    | Scroll wheel, signed ints (steps). +dy = up.         | none (lossy)       |
| `<TOKEN> ZOOM <steps>`        | Ctrl+wheel zoom (pinch). +steps = in, − = out.       | none (lossy)       |
| `<TOKEN> CLICK`               | Left mouse click.                                    | none               |
| `<TOKEN> RCLICK`              | Right mouse click.                                   | none               |
| `<TOKEN> MCLICK`              | Middle mouse click.                                  | none               |
| `<TOKEN> MDOWN`               | Press & hold left button (drag-lock start).          | none               |
| `<TOKEN> MUP`                 | Release left button (drag-lock end). Idempotent; the phone sends it twice, since a lost one would leave the button held. | none |
| `<TOKEN> PING`                | Liveness probe (reconnect watchdog).                 | `PONG`             |
| `<TOKEN> VOL <0-100>`         | Set system volume to absolute percent.               | none               |
| `<TOKEN> VGET`                | Ask server for current system volume.                | `VOL <0-100> [0\|1]` |
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

**Key padding.** On the secure wire the server strips trailing NUL characters from
every decrypted packet. A phone that has seen this server bind its handshake (so knows
it strips them) pads `KEY` and `KEYSP` bodies with NULs to a multiple of 32 UTF-8
bytes. GCM adds no padding of its own, so without this the ciphertext length gives away
how much was typed in each chunk, and which special key was pressed.

The server holds a pressed button (`MDOWN`) only while a phone is driving: a drop, an
idle timeout, `BYE`, a local-takeover pause, the panic chord, a wake and a loop restart
all release it.

`COMBO` modifiers: `ctrl`, `alt`, `shift`, `win`/`cmd`. The final token is the
key — a single literal char, a `KEYSP` name, or `f1`–`f12`.

`ASW` drives the OS app switcher as a held session. The first `next`/`prev` presses
and **holds** `Alt`, then taps `Tab` (`next`) or `Shift+Tab` (`prev`); each further
`next`/`prev` taps again while `Alt` stays down, so the switcher cycles forward
through every window. `end` releases `Alt` and commits the highlighted app. The
server also auto-releases `Alt` on `BYE` or a new `HELLO`, so a dropped connection
mid-gesture can't leave `Alt` stuck. Maps to the Windows three-finger touchpad swipe.

### Replies (server → client)

- `OK [<cnonce>]` — handshake accepted; sender is now the registered controller.
  Echoes the client nonce when the `HELLO` carried one.
- `CHAL <nonce> [<cnonce>]` — answer to a secure `HELLO`: a one-time, base64url random
  nonce the client must echo in `AUTH` before it is pinned, followed by the client's own
  nonce when it sent one (see the handshake notes below).
- `ERR secure-required` / `ERR bad-key` — **plaintext** hints, rate-limited per sender:
  a typed-code `HELLO` whose token is right while encryption is required, or a secure
  packet that fails to decrypt (almost always a phone paired before the laptop was
  re-paired). They carry nothing secret, and a wrong token still gets silence. Being
  unauthenticated, the phone treats them only as the reason to show if the handshake
  fails, never as a reason to stop or forget a pairing.
- `VOL <0-100> [0|1]` — current laptop volume, sent in answer to `VGET`. Lets the
  phone keep its slider in sync with the laptop's real volume (two-way). The second
  word is the mute state (`1` = muted), sent only when the audio backend reports it
  (Windows); an older phone reads only the number.
- `BRI <0-100>` — current laptop display brightness, sent in answer to `BGET`. Lets
  the phone keep its brightness slider in sync (two-way, same shape as `VOL`).
- `PONG` — answer to `PING`; lets the phone confirm the laptop is still alive and
  trigger an auto-reconnect when it goes silent.

## Wire formats

Two datagram encodings exist; the server auto-detects per packet.

### L3 — secure (default for QR pairing)

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
| `L2`  | `sid(4)` \| `counter(8)` | 2^32          | removed in 2.2.0 |

**Why the split moved.** The key is *persistent* across launches while the `sid` is
random per session, so a `sid` collision means GCM nonce reuse under one key — which
leaks the authentication key, not merely a plaintext. A 4-byte `sid` put that at the
birthday bound of 2^32: roughly 1.2% odds by 10 000 sessions and 39% by 65 000. Every
reconnect mints a session and the phone's watchdog reconnects on any drop, so those
counts are reachable over a device's lifetime. Moving four bytes from the counter to
the `sid` buys 2^64 at no practical cost — a 4-byte counter still allows 4.29e9
packets in a single session, and exhausting it re-keys the `sid` rather than wrapping.

**Compatibility.** The L2 dialect was accepted
until 2.2.0 so a phone updated ahead of its laptop kept pairing; it is now
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
  own `sid`/counter, and the **client** pins the server's `sid` and then requires a
  strictly-greater counter.
- **Binding the replies to this handshake.** The key outlives every session, so any
  `CHAL` or `OK` captured earlier still decrypts; pinning "the first reply" would let
  one replayed from an old session win the race, and the phone would then refuse the
  real laptop. So the phone's `HELLO` carries a fresh random client nonce (16–64 chars
  of `[A-Za-z0-9_-]`), the server echoes it as the last word of `CHAL` and `OK`, and
  the phone pins only the `OK` that echoes its own nonce. A `HELLO` is replayable, so
  a challenge remembers the client nonce that opened it plus the last few others sent
  for it, and the verified `AUTH` gets one `OK` per nonce; replays slipped in
  mid-handshake, however many, can't take the phone's `OK` away. A server older than this
  ignores the nonce and answers unbound (`CHAL <nonce>`, `OK`), which a new phone
  accepts — until it has once seen that laptop answer bound. From then on it refuses
  unbound answers from it, so a replayed old-format reply can't be used to downgrade.
  Scanning the laptop's QR again resets that: the flag then follows what that scan's
  handshake showed, so rolling the laptop back to an older build needs only a rescan.
  An old phone sends no nonce and gets exactly the old replies.
- Confidentiality: keystrokes and all args are encrypted, not just authenticated.

### v1 — plaintext (legacy, trusted-LAN only)

`"<TOKEN> <VERB> [args]"` as before. Used when pairing by **manually typed code**
(no key). Offers no confidentiality and is replayable/spoofable on the wire — fine
on a trusted home network, unsafe on open Wi-Fi. **The server rejects v1 by
default**; `--allow-plaintext` (or turning **Require encryption** off in the window)
permits it. A refused `HELLO` whose token actually
matches is answered `ERR secure-required` and shown in the laptop window, so a
typed-code attempt gets an explanation on both screens instead of a silent timeout.


A typed-code phone never tries other laptops it discovers when its saved address
fails: it would be handing its token, and then every keystroke, in the clear to
anything on the LAN that advertises `_lazer._udp`.

## Security model

1. Server boots, loads/generates a persistent token **and** a 256-bit key, and shows
   them with the LAN IP in its window. The key goes only into the on-screen QR.
2. **Secure (L3):** the client encrypts `HELLO`; the server answers a one-time
   `CHAL` and pins that client's `(ip, port)` + `sid` only after a matching `AUTH`
   (challenge-response — see the handshake note under the secure wire). This makes pinning **fresh**, so
   a captured session can't be replayed by a keyless attacker. Every later packet
   must carry a valid tag, the pinned `sid`, an increasing counter, **and** the pinned
   source, or it is dropped. Re-pinning (reconnect from a new port) is safe: it runs
   the full challenge, and only the key holder can complete it.
3. **Plaintext (v1):** token match pins `(ip, port)`; later packets need the token
   **and** the pinned source. A plaintext re-pin from a new address is accepted (so
   reconnects work) but logged as a warning — turn on Require encryption to forbid it.
4. **Brute-force / flood:** a high rate of rejected packets raises a warning and
   pauses manual-code (plaintext) acceptance briefly — the only path a token
   brute-force exists against. QR-paired phones are unaffected. (With no key loaded —
   `cryptography` missing — there is no secure wire, and the pause is skipped.)
5. **Local takeover:** physical mouse/keyboard input on the laptop (detected via
   non-injected low-level hooks) pauses the remote so the user's own device always
   wins; `Ctrl+Alt+Shift+L` latches the remote OFF until the user resumes.

Movement/scroll are lossy by design: a dropped packet just means a slightly shorter
gesture. Discrete actions (CLICK/RCLICK/VOL/MEDIA/KEY) also ride UDP — fine for LAN.

## Discovery & pairing (out-of-band, not UDP control packets)

- **Persistent token and key**, reused across launches so saved phones reconnect
  without re-pairing; replaced by a new code or `--regenerate` (storage: README →
  Security notes).
- **mDNS / Bonjour.** The server advertises service type `_lazer._udp.` on port
  `50505` with a `name` TXT property. The phone discovers laptops automatically; the
  advertisement carries **no token** (IP/port/name only).
- **QR code.** The laptop window shows a QR encoding a connection URI:
  ```
  lazer://<ip>:<port>/?token=<token>&name=<hostname>&k=<base64url-256-bit-key>
  ```
  Scanning it fills everything and connects on the **secure (L3)** wire in one tap.
  Both token and key travel only in the QR (shown on the laptop screen), never over
  mDNS. Manual entry has no `k`, so it uses plaintext v1. The phone refuses a QR whose
  host is a DNS name or an address no other machine can have (loopback, unspecified,
  multicast, broadcast; an IPv4-mapped IPv6 address is judged by the IPv4 inside it),
  or an IPv4 octet with a leading zero (Android dials `012` as octal 10). Public and
  CGNAT addresses are accepted, since some campus LANs use them. It also refuses one
  whose `k` is present but won't decode — it never falls back to plaintext for a
  damaged key. A QR
  that would replace a saved laptop's key asks first. Both halves test this exact
  string (`SharedContract` / `ProtocolContractTest`).

> **Windows note.** Sending UDP to an endpoint with no listener — routine when we
> reply to a phone that has just vanished — makes the OS raise `WSAECONNRESET` on the
> socket's next receive. The server disables that report (`SIO_UDP_CONNRESET` off)
> and ignores the error, so a departed phone never tears down the receive loop.
