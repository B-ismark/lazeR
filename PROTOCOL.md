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
- `CHAL <nonce>` — answer to a **v2** `HELLO`: a one-time, base64url random nonce the
  client must echo in `AUTH` before it is pinned (see the handshake note under v2).
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
- **Handshake (challenge-response, v2).** A valid GCM tag proves key possession but
  NOT freshness, so a captured `HELLO`+control stream could otherwise be replayed by
  anyone who lacks the key (e.g. the untrusted rendezvous, which sees all relayed
  ciphertext). So a v2 `HELLO` is **not** pinned on arrival: the server replies
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
on a trusted home network, unsafe on open Wi-Fi. The server's **Require encryption**
toggle (or `--secure-only`) rejects v1 entirely.

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
  - With remote access enabled the URI also carries `&r=<rdv-host:port>`, so a
    scanned phone learns where to reach this laptop when it's off the LAN.

## Remote access (off-LAN / NAT traversal)

On the same LAN the phone reaches the laptop's private IP directly. Off-LAN, that
IP is unreachable, so a phone and its saved laptop find each other through a public
**rendezvous coordinator** (`rendezvous/rendezvous_server.py`; deploy notes in
`rendezvous/deploy.md`). Requires a **key** — remote access is v2-only; plaintext
(manual-code) devices stay LAN-only. Enable on the laptop with
`--rendezvous <host[:port]>` (remembered across launches; `off` to disable).
**Enabling a rendezvous forces secure-only** (plaintext is refused on every path),
since off-LAN the endpoint is internet-reachable where a v1 token would be
brute-forceable/observable.

**The coordinator is untrusted.** It never sees the AES key and cannot control or
decrypt a laptop, and — because control is pinned via the fresh challenge-response —
it **cannot replay a captured session** either. The worst a hostile/compromised
rendezvous can do is learn a public IP, redirect/refuse a connection (DoS), or be
used as a 1:1 relay reflector (bounded by its global rate cap). All control stays
end-to-end encrypted (v2) whether the path is direct or relayed. (A residual: the
`room` is a plaintext bearer, so an on-path observer who sees a `REG` can DoS/redirect
that session — never decrypt it. Closing that needs a return-routability check on
`REG`, tracked as a follow-up.)

### Room id

Both paired devices derive the same opaque id from the key they already share, and
send only *that* to the rendezvous:

```
room = base64url( HMAC-SHA256(key, "lazer-rdv-v1")[:16] )      # 22 chars, no padding
```

### Rendezvous wire (UDP, its own port — default 50510)

All control is UTF-8 text lines; relayed data is raw v2 datagrams.

| Packet (client → rdv)      | Meaning                                             |
|----------------------------|-----------------------------------------------------|
| `REG <role> <room>`        | Register/refresh my endpoint. role `H`=laptop, `P`=phone. |
| `RELAY <role> <room>`      | Switch this room to relay mode.                     |
| `BYE <role> <room>`        | Forget my endpoint.                                 |

| Packet (rdv → client)      | Meaning                                             |
|----------------------------|-----------------------------------------------------|
| `SELF <ip> <port>`         | Your reflexive (public) address — STUN-lite.        |
| `PEER <ip> <port>`         | The other role's public endpoint.                   |
| `RELAY OK`                 | Relay is active for your room.                       |

Any datagram whose first two bytes are `L2` (the v2 magic) is treated as **relay
data**, not control: the rdv looks the sender up by address and forwards the bytes
verbatim to the other role's endpoint in the same room. The data path **never
amplifies** (out size = in size) and forwards only between two endpoints registered
under the same room; a 128-bit `room` is the bearer. Registration is trusted by
(spoofable) source address, so a spoofed `REG` could register a victim and have the
data path aimed at it — a 1:1 reflector, not an amplifier, and bounded by the rdv's
global rate cap. The rdv also caps its tables and expires unpaired rooms fast so a
junk-`REG` flood can't exhaust memory or lock out new sessions.

### Connect sequence

1. **Register.** The laptop `REG H`s every ~20s (also a NAT keepalive), so its
   public endpoint is always current at the rendezvous.
2. **Introduce.** When a phone `REG P`s, the rendezvous replies `PEER` to the phone
   **and** pushes `PEER` to the laptop, so each learns the other's public endpoint.
3. **Hole-punch.** Both fire UDP at the other's endpoint at once: the laptop sends a
   sustained punch burst (openers at ~30ms for a few seconds); the phone sends
   encrypted `HELLO`s (v2) and completes the `CHAL`→`AUTH`→`OK` handshake once a
   packet gets through. All of this rides the laptop's **same** `50505` socket, so the
   NAT mapping the phone hits is the one carrying control traffic. (The laptop only
   punches toward a **global** peer address a `PEER` line names — never loopback/
   private — so a forged `PEER` can't aim it at an internal host.)
4. **Relay fallback.** If the punch doesn't open a path within a few seconds
   (symmetric / carrier-grade NAT), the phone sends `RELAY P` and re-sends its
   `HELLO`s **to the rendezvous**, which forwards them (still encrypted) to the
   laptop and relays the replies back. Higher latency, but works everywhere.

Once `OK` arrives, the session is an ordinary v2 wire (§v2 above) over whichever
path won — the phone's socket simply targets the peer (direct) or the rendezvous
(relay), and everything else in this protocol is unchanged.

> **Windows note.** Sending UDP to an endpoint with no listener (routine while
> punching) makes the OS raise `WSAECONNRESET` on the socket's next receive. The
> server disables that report (`SIO_UDP_CONNRESET` off) and ignores the error, so a
> normal punch never tears down the receive loop.
