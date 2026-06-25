# Remote Control Wire Protocol

Transport: **UDP**, single port (default `50505`). One UTF-8 text line per datagram.
Every datagram is a single packet. No framing beyond the datagram boundary.

## Packet grammar

```
<TOKEN> <VERB> [args...]
```

- `<TOKEN>` — the shared secret string shown by the server at startup. Present on **every**
  packet (handshake and all control packets). Packets with a wrong/missing token are
  silently dropped.
- The server parses at most three fields: `token`, `verb`, and a single `rest` string.
  So `KEY hello world` delivers the literal text `hello world` (spaces preserved).

### Verbs

| Packet                        | Meaning                                              | Reply              |
|-------------------------------|------------------------------------------------------|--------------------|
| `<TOKEN> HELLO`               | Handshake. Registers sender IP:port as the client.   | `OK`               |
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
| `<TOKEN> CLIP <text>`         | Set laptop clipboard to `text`, then paste (Ctrl+V). | none               |
| `<TOKEN> MEDIA <action>`      | `play_pause` \| `next` \| `prev`.                    | none               |
| `<TOKEN> KEY <text>`          | Type the literal UTF-8 text (spaces preserved).      | none               |
| `<TOKEN> KEYSP <name>`        | Press one special key (see below).                   | none               |
| `<TOKEN> COMBO <mods..> <key>`| Hold modifiers, tap key. e.g. `ctrl c`, `alt tab`.   | none               |
| `<TOKEN> ASW <action>`        | App-switch session: `next`\|`prev`\|`end`.           | none               |
| `<TOKEN> SYS <action>`        | `lock` \| `sleep` \| `mute`.                         | none               |
| `<TOKEN> PRES <action>`       | Slides: `start`\|`end`\|`next`\|`prev`\|`blank`.     | none               |
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
- `VOL <0-100>` — current laptop volume, sent in answer to `VGET`. Lets the phone
  keep its slider in sync with the laptop's real volume (two-way).
- `BRI <0-100>` — current laptop display brightness, sent in answer to `BGET`. Lets
  the phone keep its brightness slider in sync (two-way, same shape as `VOL`).
- `PONG` — answer to `PING`; lets the phone confirm the laptop is still alive and
  trigger an auto-reconnect when it goes silent.

## Wire formats

Two datagram encodings coexist; the server auto-detects per packet.

### v2 — secure (default for QR pairing)

```
packet = "L2" (2) | sid (4) | counter (8, big-endian) | AES-256-GCM(ciphertext+tag)
nonce  = sid | counter                 (12 bytes)
AAD    = "L2" | sid | counter          (the packet's first 14 bytes)
plaintext = "<VERB> [args]"            (the v1 line minus the token)
```

- The 256-bit key is shared **only** via the QR (`&k=` below); never on the wire,
  never over mDNS. A valid GCM tag *is* the authentication — it proves the sender
  holds the key, so no token rides v2 packets.
- `sid` is a random 4-byte per-session id the client picks at connect; `counter`
  is a per-session monotonic uint64 (first `HELLO` = 1, then +1 per send).
- **Replay/forgery:** the server pins the `sid` of the accepted `HELLO` and requires
  every later packet to carry that `sid` with a **strictly greater** counter — so
  replayed or reordered packets are dropped, and forged packets fail the tag.
- Replies (`OK`/`PONG`/`VOL n`) are encrypted the same way with the server's own
  `sid`/counter.
- Confidentiality: keystrokes and all args are encrypted, not just authenticated.

### v1 — plaintext (legacy, trusted-LAN only)

`"<TOKEN> <VERB> [args]"` as before. Used when pairing by **manually typed code**
(no key). Offers no confidentiality and is replayable/spoofable on the wire — fine
on a trusted home network, unsafe on open Wi-Fi. The server's **Require encryption**
toggle (or `--secure-only`) rejects v1 entirely.

## Security model

1. Server boots, loads/generates a persistent token **and** a 256-bit key, prints
   them + LAN IP. The key goes only into the on-screen QR.
2. **Secure (v2):** the client encrypts `HELLO` under the key. A valid tag pins that
   client's `(ip, port)` + `sid` as the sole controller. Every later packet must
   carry a valid tag, the pinned `sid`, and an increasing counter, or it is dropped.
   Re-pinning (reconnect from a new port) is safe because only the key holder can
   produce a valid tag.
3. **Plaintext (v1):** token match pins `(ip, port)`; later packets need the token
   **and** the pinned source. A plaintext re-pin from a new address is accepted (so
   reconnects work) but logged as a warning — turn on Require encryption to forbid it.
4. **Brute-force / flood:** a high rate of rejected packets raises a warning.
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
