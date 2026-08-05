#!/usr/bin/env python3
"""
LazeR rendezvous — public UDP coordinator for cross-network (off-LAN) control.

It does three jobs, all over ONE UDP port, and it is deliberately *dumb*: it
never sees the AES key, never authenticates control, and cannot drive or decrypt
a laptop. It only helps two already-paired endpoints find each other and, when a
NAT refuses to be punched, forwards their (still end-to-end-encrypted) packets.

  1. STUN-lite   — echoes each sender its own public (ip, port) in `SELF`.
  2. Match       — pairs a laptop (role H) and a phone (role P) that present the
                   same `room` id, telling each the other's public endpoint so
                   they can UDP hole-punch a direct path.
  3. Relay       — if the punch fails (symmetric / carrier-grade NAT), it forwards
                   raw v2 datagrams between the two endpoints of a room. Payload is
                   AES-256-GCM ciphertext the relay can't read; it only routes.

Wire (all UTF-8 text lines except relayed data):

  client -> rdv   REG   <role> <room>      register/refresh endpoint (H | P)
  client -> rdv   RELAY <role> <room>      ask to switch this room to relay mode
  client -> rdv   BYE   <role> <room>      forget my endpoint (optional)
  rdv -> client   SELF  <ip> <port>        your reflexive address (diagnostic)
  rdv -> client   PEER  <ip> <port>        the other role's public endpoint
  rdv -> client   RELAY OK                 relay is active for your room

  <room> is base64url(HMAC-SHA256(key, "lazer-rdv-v1")[:16]) — a 128-bit bearer
  both paired devices derive from the key they already share. The rdv only ever
  sees this opaque id, never the key.

  Relay data path: any datagram whose first two bytes are b"L2" (the v2 secure
  magic) is treated as data, NOT control — the rdv looks the sender up by address
  and forwards the bytes verbatim to the other role's endpoint in the same room.
  The DATA path never amplifies (out size == in size) and forwards only between two
  endpoints registered under the same room.

  Threat-model caveats (control is trusted by source address, which UDP lets an
  attacker spoof; a room is a bearer with no key-proof — the rdv is key-blind):
    • A spoofed REG can register a victim as a room endpoint, so the DATA relay can
      be aimed at an arbitrary victim (1:1, no size gain — a reflector, not an
      amplifier). The GLOBAL rate bucket caps the total volume this can push.
    • The control path emits up to ~3 packets per REG (SELF + two PEER), so it is a
      small (<3x) amplifier if a live room is known.
    • Anyone who learns a room id (it rides the wire as plaintext; the rdv operator
      sees it) can evict/redirect/force-relay that session — DoS + metadata only,
      never payload decryption (that stays end-to-end under a key the rdv never has).
  Closing these fully needs a return-routability check on REG (challenge the claimed
  address before it's forward-eligible) — a client protocol change, tracked separately.

Run:  python3 rendezvous_server.py [--port 50510] [--host 0.0.0.0]
No dependencies beyond the standard library.
"""

import argparse
import base64
import socket
import sys
import time

DEFAULT_PORT = 50510
# Secure-wire magics. Anything opening with one of these is relay DATA, not
# control. L3 is the current dialect (sid(8)|counter(4)); L2 is the legacy one,
# still accepted for one release. Both must be listed here or relayed traffic
# from an updated phone would be misread as a control line and dropped.
V2_MAGIC = b"L2"
V3_MAGIC = b"L3"
DATA_MAGICS = (V2_MAGIC, V3_MAGIC)

# Lifetimes / limits. A laptop re-REGs every ~20s, so 90s of slack survives a
# missed beat; any traffic (data too) refreshes the entry, so live sessions never
# expire out from under themselves.
ENTRY_TTL_S = 90.0
UNPAIRED_TTL_S = 30.0       # a single-sided room (only H or only P) expires fast, so a
                           # flood of junk REGs can't hold the table full and lock out
                           # new sessions (they never pair, so they're pure ballast).
SWEEP_EVERY_S = 30.0
MAX_ROOMS = 5000            # backstop against a table-filling flood (evict oldest unpaired first)
MAX_LINE = 512              # control lines are tiny; ignore anything larger as data/junk
MAX_DATA = 2048             # relayed datagram cap (matches the server's recv buffer)

# Per-source token bucket: an anti-abuse backstop, NOT a shaper. Active trackpad
# use sends MOVE at the phone's touch rate (60–120/s) plus PINGs; on the relay
# path every one of those is an inbound datagram here, so the old 50/s ceiling
# dropped half the moves (choppy cursor) and starved PINGs (reconnect loop). Size
# it well above real interactive traffic; it only exists to cap a flood.
RATE_REFILL_PER_S = 500.0
RATE_BURST = 1000.0
BUCKET_IDLE_S = 120.0       # prune an idle per-IP bucket after this
MAX_BUCKETS = 4096          # hard cap on the per-IP bucket table (anti memory-flood)
# Per-IP rate limiting is spoof-defeatable on UDP (each forged source gets a fresh
# bucket), so a GLOBAL token bucket sits above it as a spoof-proof ceiling on total
# work the server will do — bounds both CPU and the volume a spoofed-source flood or
# forced-relay reflector can push through. Sized well above a handful of live sessions.
RATE_GLOBAL_PER_S = 4000.0
RATE_GLOBAL_BURST = 8000.0


def _log(msg):
    """Concise stdout line (systemd/journald timestamps it). Flush so `journalctl
    -f` shows it live during a diagnosis session."""
    print(f"[rdv] {msg}", flush=True)


class Room:
    __slots__ = ("H", "P", "relay")

    def __init__(self):
        self.H = None      # (addr, last_seen) — laptop
        self.P = None      # (addr, last_seen) — phone
        self.relay = False

    def slot(self, role):
        return self.H if role == "H" else self.P

    def set(self, role, addr, now):
        if role == "H":
            self.H = (addr, now)
        else:
            self.P = (addr, now)

    def fresh(self, role, now):
        s = self.slot(role)
        return s is not None and (now - s[1]) <= ENTRY_TTL_S


class Rendezvous:
    def __init__(self, sock):
        self.sock = sock
        self.rooms = {}                 # room(str) -> Room
        self.by_addr = {}               # addr(tuple) -> (room, role)
        self.buckets = {}               # ip(str) -> [tokens, last_refill]
        self._last_sweep = time.monotonic()
        self._gtok = RATE_GLOBAL_BURST  # global token bucket (spoof-proof ceiling)
        self._glast = time.monotonic()

    # ── rate limiting ──────────────────────────────────────────────────────────
    def _allow_global(self, now):
        """Spoof-proof global ceiling: forged source IPs can't each mint a fresh
        per-IP bucket past this. Bounds total work regardless of source spoofing."""
        self._gtok = min(RATE_GLOBAL_BURST, self._gtok + (now - self._glast) * RATE_GLOBAL_PER_S)
        self._glast = now
        if self._gtok < 1.0:
            return False
        self._gtok -= 1.0
        return True

    def _allow(self, addr):
        ip = addr[0]
        now = time.monotonic()
        tok, last = self.buckets.get(ip, None) or (RATE_BURST, now)
        # Don't let the bucket table grow without bound under a spoofed-source flood:
        # once full, refuse packets from as-yet-unseen IPs (the global ceiling already
        # caps total throughput, so honest live sessions — already in the table — keep
        # their buckets).
        if ip not in self.buckets and len(self.buckets) >= MAX_BUCKETS:
            return False
        tok = min(RATE_BURST, tok + (now - last) * RATE_REFILL_PER_S)
        if tok < 1.0:
            self.buckets[ip] = (tok, now)
            return False
        self.buckets[ip] = (tok - 1.0, now)
        return True

    # ── main loop ──────────────────────────────────────────────────────────────
    def serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(MAX_DATA)
            except socket.timeout:
                self._maybe_sweep()
                continue
            except OSError:
                continue
            if not data or not self._allow_global(time.monotonic()) or not self._allow(addr):
                continue
            self._maybe_sweep()
            if data[:2] in DATA_MAGICS:
                self._forward(data, addr)          # relayed encrypted payload
            elif len(data) <= MAX_LINE:
                self._control(data, addr)          # REG / RELAY / BYE
            # else: too big to be control, not v2 data -> drop

    # ── control messages ────────────────────────────────────────────────────────
    def _control(self, data, addr):
        try:
            parts = data.decode("utf-8", "ignore").split()
        except Exception:
            return
        if len(parts) < 1:
            return
        verb = parts[0].upper()
        now = time.monotonic()

        if verb in ("REG", "RELAY", "BYE"):
            if len(parts) < 3:
                return
            role = parts[1].upper()
            room = parts[2]
            if role not in ("H", "P") or not _valid_room(room):
                return

            if verb == "BYE":
                r = self.rooms.get(room)
                if r is not None:
                    if role == "H":
                        r.H = None
                    else:
                        r.P = None
                self.by_addr.pop(addr, None)
                return

            r = self.rooms.get(room)
            if r is None:
                if len(self.rooms) >= MAX_ROOMS and not self._evict_one(now):
                    return                          # table full of live paired rooms
                r = self.rooms[room] = Room()
            was_stale = not r.fresh(role, now)      # was this endpoint absent before?
            r.set(role, addr, now)
            self.by_addr[addr] = (room, role)

            if verb == "RELAY":
                r.relay = True
                _log(f"RELAY  room={room[:6]}.. {role} {addr[0]}:{addr[1]} -> relay mode (direct punch failed)")
                self._send(addr, b"RELAY OK")
                other = "P" if role == "H" else "H"
                if r.fresh(other, now):
                    self._send(r.slot(other)[0], b"RELAY OK")
                return

            # REG: reflect the sender's public endpoint (STUN-lite), then maybe
            # cross-introduce. Introduce on every phone (P) REG so its connect-time
            # retries survive a lost PEER, but only on a *new* laptop (H) arrival —
            # a laptop's periodic keepalive REG must not spray stray PEER lines at
            # an already-connected phone (they'd look like dropped poll replies).
            self._send(addr, f"SELF {addr[0]} {addr[1]}".encode())
            if was_stale:
                _log(f"REG    room={room[:6]}.. {role} {addr[0]}:{addr[1]}")
            other = "P" if role == "H" else "H"
            if r.fresh(other, now) and (role == "P" or was_stale):
                oaddr = r.slot(other)[0]
                self._send(addr, f"PEER {oaddr[0]} {oaddr[1]}".encode())
                self._send(oaddr, f"PEER {addr[0]} {addr[1]}".encode())
                _log(f"PEER   room={room[:6]}.. introduced {addr[0]}:{addr[1]} <-> "
                     f"{oaddr[0]}:{oaddr[1]} (direct-punch attempt)")

    # ── relay data ───────────────────────────────────────────────────────────────
    def _forward(self, data, addr):
        ent = self.by_addr.get(addr)
        if ent is None:
            return                                  # unknown sender — must REG first
        room, role = ent
        r = self.rooms.get(room)
        if r is None:
            return
        now = time.monotonic()
        r.set(role, addr, now)                      # data keeps the mapping warm
        other = "P" if role == "H" else "H"
        if r.fresh(other, now):
            self._send(r.slot(other)[0], data)

    def _send(self, addr, payload):
        try:
            self.sock.sendto(payload, addr)
        except OSError:
            pass

    def _evict_one(self, now):
        """Free a slot when the room table is full: drop the oldest UNPAIRED room
        (junk REGs that never paired), never a live paired session. Returns True if
        one was evicted. Stops a junk-room flood from locking out new sessions."""
        oldest, oldest_t = None, None
        for room, r in self.rooms.items():
            if r.H is not None and r.P is not None:
                continue                            # keep paired (live) rooms
            t = max((s[1] for s in (r.H, r.P) if s), default=0)
            if oldest_t is None or t < oldest_t:
                oldest, oldest_t = room, t
        if oldest is None:
            return False
        r = self.rooms.pop(oldest, None)
        if r is not None:
            for s in (r.H, r.P):
                if s:
                    self.by_addr.pop(s[0], None)
        return True

    # ── housekeeping ─────────────────────────────────────────────────────────────
    def _maybe_sweep(self):
        now = time.monotonic()
        if now - self._last_sweep < SWEEP_EVERY_S:
            return
        self._last_sweep = now
        dead_rooms = []
        for room, r in self.rooms.items():
            paired = r.H is not None and r.P is not None
            ttl = ENTRY_TTL_S if paired else UNPAIRED_TTL_S   # junk single-sided rooms die fast
            if r.H and (now - r.H[1]) > ttl:
                r.H = None
            if r.P and (now - r.P[1]) > ttl:
                r.P = None
            if r.H is None and r.P is None:
                dead_rooms.append(room)
        for room in dead_rooms:
            self.rooms.pop(room, None)
        # prune stale addr map + rate buckets
        live = set()
        for r in self.rooms.values():
            if r.H:
                live.add(r.H[0])
            if r.P:
                live.add(r.P[0])
        for a in [a for a in self.by_addr if a not in live]:
            self.by_addr.pop(a, None)
        for ip in [ip for ip, (_, last) in self.buckets.items()
                   if now - last > BUCKET_IDLE_S]:
            self.buckets.pop(ip, None)


def _valid_room(room):
    # 16 raw bytes -> 22 base64url chars, no padding. Reject anything else so the
    # table can't be poisoned with junk keys.
    if not (20 <= len(room) <= 24):
        return False
    try:
        raw = base64.urlsafe_b64decode(room + "=" * (-len(room) % 4))
        return len(raw) == 16
    except Exception:
        return False


def main():
    # Never let a non-ASCII log line crash the coordinator on a host whose console
    # encoding isn't UTF-8 (e.g. Windows cp1252). Best-effort; ignored if unsupported.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="LazeR rendezvous coordinator")
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP port")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(SWEEP_EVERY_S)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        print(f"[rdv] cannot bind {args.host}:{args.port} — {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[rdv] LazeR rendezvous listening on udp {args.host}:{args.port}")
    try:
        Rendezvous(sock).serve()
    except KeyboardInterrupt:
        print("\n[rdv] stopped.")


if __name__ == "__main__":
    main()
