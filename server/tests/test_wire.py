#!/usr/bin/env python3
"""Wire-format and dispatch tests for the LazeR server.

Run from the repo root (needs `cryptography`; pynput is stubbed, so no display
and no input backend are required):

    python -m unittest discover -s server/tests -v

Why this file exists
--------------------
The v2 secure wire is implemented TWICE — here in Python and again in Kotlin
(`android/.../net/SecureChannel.kt`) — and the two must agree byte for byte or
pairing silently stops working. Nothing but a test can hold that line, so the
`GoldenVector` case below pins one exact packet that BOTH implementations assert
against (`SecureChannelTest.kt` opens the same bytes). Change the layout and
both sides fail loudly instead of drifting apart.

The rest of the file pins the security properties the wire is supposed to have
(freshness, replay rejection, source pinning, watermark integrity) and the
crash-safety of the code that runs on the single UDP thread.
"""

import base64
import os
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

# ── bootstrap ────────────────────────────────────────────────────────────────
# remote_server imports pynput at module scope and builds a MouseController /
# KeyboardController immediately, which needs a real input backend (an X11
# display on Linux). Tests only exercise the wire + pure dispatch logic, so stub
# the backend before the import. Attributes resolve to stable, printable
# sentinels ("<Key.ctrl>") so tests can assert on exact key identities.


class _KeyStub:
    """Stands in for pynput's Key enum: any attribute is a unique sentinel."""

    def __init__(self):
        self._seen = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._seen.setdefault(name, f"<Key.{name}>")


class _Recorder:
    """Stands in for a pynput Controller, recording every call in order."""

    def __init__(self):
        self.calls = []

    def _rec(self, name):
        def fn(*a, **kw):
            self.calls.append((name, *a))
        return fn

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._rec(name)


def _install_pynput_stub():
    mouse = types.ModuleType("pynput.mouse")
    keyboard = types.ModuleType("pynput.keyboard")
    mouse.Button = _KeyStub()
    mouse.Controller = _Recorder
    keyboard.Key = _KeyStub()
    keyboard.Controller = _Recorder
    sys.modules.setdefault("pynput", types.ModuleType("pynput"))
    sys.modules["pynput.mouse"] = mouse
    sys.modules["pynput.keyboard"] = keyboard


_install_pynput_stub()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import remote_server as rs  # noqa: E402

if not rs._HAVE_CRYPTO:                      # pragma: no cover
    raise unittest.SkipTest("cryptography missing — pip install cryptography")

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

# ── helpers ──────────────────────────────────────────────────────────────────
KEY = bytes(range(32))          # deterministic test key, never a real one
TOKEN = "A1B2C3"
CLIENT = ("192.168.1.50", 41234)
STRANGER = ("192.168.1.99", 55555)


class FakeSock:
    """Captures sendto() so replies can be inspected without a real socket."""

    def __init__(self):
        self.sent = []            # [(payload, addr), ...]

    def sendto(self, payload, addr):
        self.sent.append((bytes(payload), addr))

    def last(self):
        return self.sent[-1][0]


class FakeClient:
    """Minimal mirror of the Kotlin SecureChannel's send side, either dialect."""

    def __init__(self, key, magic=None, sid=None):
        self.aes = AESGCM(key)
        self.magic = magic or rs.MAGIC_V3
        self.sid_len, self.ctr_len = rs.WIRE_FORMATS[self.magic]
        self.sid = sid if sid is not None else bytes(range(0x11, 0x11 + self.sid_len))
        self.ctr = 0

    def seal(self, body, ctr=None, sid=None):
        """Build a datagram. [ctr]/[sid] override for replay/forgery cases."""
        sid = sid if sid is not None else self.sid
        if ctr is None:
            self.ctr += 1
            ctr = self.ctr
        nonce = sid + ctr.to_bytes(self.ctr_len, "big")
        hdr = self.magic + nonce
        return hdr + self.aes.encrypt(nonce, body.encode("utf-8"), hdr), ctr


def unseal(key, data):
    """Decrypt a server reply the way the phone does. Returns the plaintext."""
    hdr, body = data[:14], data[14:]
    return AESGCM(key).decrypt(hdr[2:14], body, hdr).decode("utf-8")


def handshake(wire, sock, addr=CLIENT, client=None, now=100.0, magic=None):
    """Drive a full HELLO→CHAL→AUTH→OK and return the pinned FakeClient."""
    fc = FakeClient(KEY, magic=magic)
    pkt, _ = fc.seal("HELLO")
    assert wire.parse(pkt, addr, client)[0] == "HELLO"
    wire.issue_challenge(sock, addr, now)
    nonce = unseal(KEY, sock.last()).split(" ", 1)[1]
    pkt, _ = fc.seal(f"AUTH {nonce}")
    verb, rest, secure = wire.parse(pkt, addr, client)
    assert verb == "AUTH" and secure
    assert wire.verify_challenge(addr, rest, now)
    wire.commit_hello(True)
    return fc


# ── the cross-implementation anchors ─────────────────────────────────────────
class GoldenVector(unittest.TestCase):
    """One frozen packet both implementations must agree on.

    key = bytes(0..31), sid = 01020304, counter = 1, plaintext = "MOVE 3 -4".
    AES-GCM is deterministic for a fixed (key, nonce, aad, plaintext), so these
    bytes are stable forever. SecureChannelTest.kt opens the SAME hex string —
    if either side changes the header layout, nonce derivation, or AAD, exactly
    one of the two tests goes red and the skew is caught before it ships."""

    HEX = ("4c32010203040000000000000001"
           "9cbe917b3b744aee3cc2838803178e6aa93726c156c2f73bb4")
    PLAINTEXT = "MOVE 3 -4"

    def setUp(self):
        self.pkt = bytes.fromhex(self.HEX)

    def test_layout_is_frozen(self):
        # Field offsets are part of the contract, not an implementation detail:
        # magic(2) | sid(4) | counter(8 BE) | ciphertext+tag.
        self.assertEqual(len(self.pkt), 14 + len(self.PLAINTEXT) + 16)
        self.assertEqual(self.pkt[:2], b"L2")
        self.assertEqual(self.pkt[2:6], bytes([1, 2, 3, 4]))
        self.assertEqual(int.from_bytes(self.pkt[6:14], "big"), 1)

    def test_decrypts_with_nonce_and_aad_as_specified(self):
        # nonce = sid|counter (12B) and AAD = the packet's first 14 bytes.
        nonce, aad = self.pkt[2:14], self.pkt[0:14]
        self.assertEqual(
            AESGCM(KEY).decrypt(nonce, self.pkt[14:], aad).decode(),
            self.PLAINTEXT)

    def test_wire_accepts_it_from_the_pinned_client(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        wire.cli_magic = rs.MAGIC_V2
        wire.cli_sid, wire.cli_ctr, wire.secure_client = bytes([1, 2, 3, 4]), 0, True
        self.assertEqual(wire.parse(self.pkt, CLIENT, CLIENT),
                         ("MOVE", "3 -4", True))


class GoldenVectorL3(unittest.TestCase):
    """The CURRENT dialect's frozen packet — the one that matters going forward.

    key = bytes(0..31), sid = 0102030405060708, counter = 1, "MOVE 3 -4".
    SecureChannelTest.kt asserts these same bytes. L3 moves four bytes from the
    counter to the session id: same 14-byte header, same 12-byte nonce, but a 2^64
    session space instead of 2^32, because the key outlives the session and a sid
    collision would mean GCM nonce reuse under it."""

    HEX = ("4c3301020304050607080000000128"
           "c4b7f683f190dd940a5b824850a6437fe2cda8b9655271b8")
    PLAINTEXT = "MOVE 3 -4"

    def setUp(self):
        self.pkt = bytes.fromhex(self.HEX)

    def test_layout_is_frozen(self):
        self.assertEqual(len(self.pkt), 14 + len(self.PLAINTEXT) + 16)
        self.assertEqual(self.pkt[:2], b"L3")
        self.assertEqual(self.pkt[2:10], bytes([1, 2, 3, 4, 5, 6, 7, 8]))
        self.assertEqual(int.from_bytes(self.pkt[10:14], "big"), 1)

    def test_header_and_nonce_widths_match_l2(self):
        # The point of splitting differently rather than resizing: framing is
        # untouched, so nothing but the sid/counter boundary had to change.
        l2 = bytes.fromhex(GoldenVector.HEX)
        self.assertEqual(len(self.pkt), len(l2))
        self.assertEqual(len(self.pkt[2:14]), 12)      # GCM nonce width

    def test_decrypts_with_nonce_and_aad_as_specified(self):
        nonce, aad = self.pkt[2:14], self.pkt[0:14]
        self.assertEqual(
            AESGCM(KEY).decrypt(nonce, self.pkt[14:], aad).decode(),
            self.PLAINTEXT)

    def test_wire_accepts_it_from_the_pinned_client(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        wire.cli_magic = rs.MAGIC_V3
        wire.cli_sid = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        wire.cli_ctr, wire.secure_client = 0, True
        self.assertEqual(wire.parse(self.pkt, CLIENT, CLIENT),
                         ("MOVE", "3 -4", True))


class Dialects(unittest.TestCase):
    """L2 stays accepted for one release so an un-updated phone keeps working, and
    the server must answer in whichever dialect the client opened with — otherwise
    the phone can't read its own CHAL and the handshake dead-ends."""

    def setUp(self):
        self.wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.sock = FakeSock()

    def test_both_dialects_complete_a_handshake(self):
        for magic in (rs.MAGIC_V2, rs.MAGIC_V3):
            with self.subTest(magic=magic):
                wire = rs.Wire(TOKEN, KEY, require_secure=True)
                fc = handshake(wire, FakeSock(), magic=magic)
                self.assertEqual(wire.cli_magic, magic)
                self.assertEqual(wire.cli_sid, fc.sid)
                pkt, _ = fc.seal("CLICK")
                self.assertEqual(wire.parse(pkt, CLIENT, CLIENT),
                                 ("CLICK", "", True))

    def test_replies_use_the_clients_dialect(self):
        for magic in (rs.MAGIC_V2, rs.MAGIC_V3):
            with self.subTest(magic=magic):
                wire = rs.Wire(TOKEN, KEY, require_secure=True)
                sock = FakeSock()
                handshake(wire, sock, magic=magic)
                wire.reply(sock, CLIENT, "PONG")
                self.assertEqual(sock.last()[:2], magic)
                self.assertEqual(unseal(KEY, sock.last()), "PONG")

    def test_challenge_is_answered_in_the_clients_dialect(self):
        # The very first reply happens BEFORE pinning, so the dialect has to be
        # picked up from the HELLO itself.
        for magic in (rs.MAGIC_V2, rs.MAGIC_V3):
            with self.subTest(magic=magic):
                wire = rs.Wire(TOKEN, KEY, require_secure=True)
                sock = FakeSock()
                fc = FakeClient(KEY, magic=magic)
                pkt, _ = fc.seal("HELLO")
                wire.parse(pkt, CLIENT, None)
                wire.issue_challenge(sock, CLIENT, 100.0)
                self.assertEqual(sock.last()[:2], magic)
                self.assertTrue(unseal(KEY, sock.last()).startswith("CHAL "))

    def test_switching_dialects_never_repeats_a_reply_nonce(self):
        """The whole point of L3 is that a nonce is never reused under the
        persistent key. Supporting two dialects nearly gave that away again: the
        server used ONE (sid, counter) pair, so every dialect change had to re-draw
        the sid — and re-drawing a sid while restarting the counter at 0 repeats a
        nonce as soon as two draws collide. With a 4-byte L2 sid that is the 2^32
        birthday bound, reachable at packet rate, and a keyless attacker can drive
        it by replaying one captured HELLO of each dialect.

        A repeated (magic, nonce) leaks the GHASH subkey, i.e. forgery for the rest
        of the key's life.

        Asserted as "sid stable per dialect, counter strictly increasing" rather
        than "no duplicate nonce appeared": a duplicate only shows up once two
        random 4-byte draws collide, which needs ~77k flips to be even odds, so a
        uniqueness check over a few hundred would pass with the bug present. This
        pins the mechanism instead, and fails on the first flip.
        """
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        sock = FakeSock()
        per_dialect = {}
        seen = set()
        for i in range(60):
            magic = rs.MAGIC_V2 if i % 2 else rs.MAGIC_V3
            fc = FakeClient(KEY, magic=magic)
            pkt, _ = fc.seal("HELLO")
            wire.parse(pkt, CLIENT, None)
            wire.issue_challenge(sock, CLIENT, 100.0 + i * 0.001)
            out = sock.last()
            self.assertEqual(out[:2], magic)
            sid_len = rs.WIRE_FORMATS[magic][0]
            sid = out[2:2 + sid_len]
            ctr = int.from_bytes(out[2 + sid_len:14], "big")
            prev = per_dialect.get(magic)
            if prev is not None:
                self.assertEqual(sid, prev[0],
                                 f"{magic} sid re-drawn on flip {i} — a restarted "
                                 f"counter under a new sid is how nonces repeat")
                self.assertGreater(ctr, prev[1], f"{magic} counter did not advance")
            per_dialect[magic] = (sid, ctr)
            nonce = out[:14]
            self.assertNotIn(nonce, seen, f"nonce repeated outright on flip {i}")
            seen.add(nonce)

    def test_each_dialect_keeps_its_own_monotonic_counter(self):
        # Per-dialect sessions: flipping away and back must RESUME that dialect's
        # counter, not restart it under a fresh sid.
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        sock = FakeSock()
        first = wire._srv_session(rs.MAGIC_V2)[0]
        wire._seal_reply(sock, CLIENT, "a", magic=rs.MAGIC_V2)
        wire._seal_reply(sock, CLIENT, "b", magic=rs.MAGIC_V3)
        wire._seal_reply(sock, CLIENT, "c", magic=rs.MAGIC_V2)
        sess = wire._srv_session(rs.MAGIC_V2)
        self.assertEqual(sess[0], first, "L2 sid was re-drawn by the round trip")
        self.assertEqual(sess[1], 2, "L2 counter restarted instead of resuming")

    def test_a_strangers_hello_cannot_move_the_pinned_clients_session(self):
        # A replayed HELLO is tag-valid, so it must not be able to touch reply state
        # the pinned phone depends on. The client pins the server's sid on the first
        # reply and rejects a changed one, so re-drawing it here would silently break
        # the live session (every PONG dropped -> watchdog reconnect loop).
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        sock = FakeSock()
        handshake(wire, sock, magic=rs.MAGIC_V3)
        wire.reply(sock, CLIENT, "PONG")
        before = sock.last()[:10]              # magic + 8-byte sid
        # Stranger replays a captured L2 HELLO from a different port.
        other = FakeClient(KEY, magic=rs.MAGIC_V2)
        pkt, _ = other.seal("HELLO")
        wire.parse(pkt, STRANGER, CLIENT)
        wire.issue_challenge(sock, STRANGER, 100.0)
        wire.reply(sock, CLIENT, "PONG")
        self.assertEqual(sock.last()[:10], before,
                         "a stranger's HELLO changed the pinned client's reply session")

    def test_a_cross_dialect_packet_is_rejected_mid_session(self):
        # Same key, same address, valid tag — but a different dialect is a different
        # session and must not be accepted against the pinned one.
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        handshake(wire, FakeSock(), magic=rs.MAGIC_V3)
        other = FakeClient(KEY, magic=rs.MAGIC_V2)
        pkt, _ = other.seal("CLICK", ctr=10_000)
        self.assertIsNone(wire.parse(pkt, CLIENT, CLIENT))

    def test_dialect_sid_widths_stay_distinct(self):
        # Honest note on the check above: parse() compares (dialect, sid), but the
        # DIALECT half is currently belt-and-braces — it is unreachable as the sole
        # reason for a rejection, because the two sid widths differ and so a
        # cross-dialect sid can never compare equal. Mutating the magic comparison
        # away therefore breaks no test.
        #
        # That reasoning is only valid while the widths differ. If a future dialect
        # reuses one, the magic comparison becomes load-bearing and its absence would
        # silently accept a foreign session. This asserts the premise so that day
        # fails here instead of in the field.
        widths = [fmt[0] for fmt in rs.WIRE_FORMATS.values()]
        self.assertEqual(len(set(widths)), len(widths),
                         "two dialects share a sid width — the magic check in "
                         "Wire.parse is now load-bearing; test it directly")

    def test_only_known_magics_are_secure_dialects(self):
        # parse() dispatches on WIRE_FORMATS membership, so an unknown magic must fall
        # through to the plaintext path rather than being mis-sliced as v2/v3.
        for magic in (b"L1", b"L4", b"XX", b"\x00\x00"):
            self.assertNotIn(magic, rs.WIRE_FORMATS)
        self.assertIn(rs.MAGIC_V2, rs.WIRE_FORMATS)
        self.assertIn(rs.MAGIC_V3, rs.WIRE_FORMATS)
        # ...and an unknown magic under secure-only is dropped, not parsed.
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.assertIsNone(wire.parse(b"L9" + b"\x00" * 40, CLIENT, CLIENT))

    def test_the_last_usable_counter_value_is_still_used(self):
        # 0xFFFFFFFF fits in four bytes, so it must be spent, not skipped. Pins the
        # boundary from below so the re-key can't creep early.
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        sock = FakeSock()
        handshake(wire, sock, magic=rs.MAGIC_V3)
        sess = wire._srv_session(rs.MAGIC_V3)
        sess[1] = (1 << 32) - 2
        sid_before = sess[0]
        wire.reply(sock, CLIENT, "PONG")
        self.assertEqual(sess[0], sid_before)
        self.assertEqual(sess[1], (1 << 32) - 1)
        self.assertEqual(int.from_bytes(sock.last()[10:14], "big"), (1 << 32) - 1)

    def test_exhausting_the_counter_rekeys_instead_of_wrapping(self):
        # Wrapping would repeat a nonce under a key that outlives the session — the
        # exact failure L3 exists to prevent. A fresh sid restarts the counter safely.
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        sock = FakeSock()
        handshake(wire, sock, magic=rs.MAGIC_V3)
        sess = wire._srv_session(rs.MAGIC_V3)
        sess[1] = (1 << 32) - 1               # no headroom left
        sid_before = sess[0]
        wire.reply(sock, CLIENT, "PONG")
        sess = wire._srv_session(rs.MAGIC_V3)
        self.assertNotEqual(sess[0], sid_before)
        self.assertEqual(len(sess[0]), 8)
        self.assertEqual(sess[1], 1)
        self.assertEqual(unseal(KEY, sock.last()), "PONG")

    def test_rotate_secrets_clears_the_pinned_dialect(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        handshake(wire, FakeSock(), magic=rs.MAGIC_V3)
        # rotate_secrets PERSISTS the new token+key, and TOKEN_FILE/KEY_FILE point at the
        # real install (server/.lazer_*). Unpatched, merely RUNNING this suite rotated the
        # developer's live credentials and silently un-paired every phone — which surfaces
        # as "my phone won't connect any more, not even by scanning the QR", pointing
        # nowhere near the tests. Redirect the writes to a throwaway dir.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(rs, "TOKEN_FILE", os.path.join(tmp, ".lazer_token")), \
                 mock.patch.object(rs, "KEY_FILE", os.path.join(tmp, ".lazer_key")):
                rs.rotate_secrets(wire)
        self.assertIsNone(wire.cli_magic)
        self.assertIsNone(wire.cli_sid)
        self.assertFalse(wire.secure_client)

    def test_rotate_secrets_writes_only_where_it_is_pointed(self):
        """Guard the guard: prove the redirect above actually catches the writes, so a
        future change can't quietly go back to clobbering a real pairing."""
        with tempfile.TemporaryDirectory() as tmp:
            token_path = os.path.join(tmp, ".lazer_token")
            key_path = os.path.join(tmp, ".lazer_key")
            wire = rs.Wire(TOKEN, KEY, require_secure=True)
            with mock.patch.object(rs, "TOKEN_FILE", token_path), \
                 mock.patch.object(rs, "KEY_FILE", key_path):
                rs.rotate_secrets(wire)
            self.assertTrue(os.path.exists(token_path), "no token written to the temp dir")
            self.assertTrue(os.path.exists(key_path), "no key written to the temp dir")
            # and the rotation must actually have changed the live values
            self.assertNotEqual(wire.token, TOKEN)
            self.assertNotEqual(wire.key, KEY)


# ── handshake: freshness via challenge-response ──────────────────────────────
class Handshake(unittest.TestCase):

    def setUp(self):
        self.wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.sock = FakeSock()

    def test_full_handshake_pins_the_auth_session(self):
        fc = handshake(self.wire, self.sock)
        self.assertEqual(self.wire.cli_sid, fc.sid)
        self.assertEqual(self.wire.cli_ctr, fc.ctr)   # baseline = the AUTH packet
        self.assertTrue(self.wire.secure_client)

    def test_hello_alone_never_pins(self):
        # A HELLO is replayable, so it must only draw a challenge — pinning waits
        # for an AUTH that proves possession of the key AND freshness.
        fc = FakeClient(KEY)
        pkt, _ = fc.seal("HELLO")
        self.wire.parse(pkt, CLIENT, None)
        self.assertIsNone(self.wire.cli_sid)
        self.assertFalse(self.wire.secure_client)

    def test_challenge_is_idempotent_per_address(self):
        # A punch burst or a slow relay makes the phone resend HELLO before the
        # first CHAL lands. Minting a new nonce each time would mean the phone's
        # AUTH echoes a nonce the server has already discarded, and the handshake
        # could never complete. Same address ⇒ same live nonce.
        fc = FakeClient(KEY)
        for _ in range(3):
            pkt, _ = fc.seal("HELLO")
            self.wire.parse(pkt, CLIENT, None)
            self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        nonces = {unseal(KEY, p).split(" ", 1)[1] for p, _ in self.sock.sent}
        self.assertEqual(len(nonces), 1)

    def test_challenge_is_single_use(self):
        self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        nonce = unseal(KEY, self.sock.last()).split(" ", 1)[1]
        self.assertTrue(self.wire.verify_challenge(CLIENT, nonce, 100.0))
        # A replayed AUTH carries a nonce the server has already consumed.
        self.assertFalse(self.wire.verify_challenge(CLIENT, nonce, 100.0))

    def test_expired_challenge_is_rejected(self):
        self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        nonce = unseal(KEY, self.sock.last()).split(" ", 1)[1]
        self.assertFalse(
            self.wire.verify_challenge(CLIENT, nonce, 100.0 + rs.CHAL_TTL_S + 0.1))

    def test_wrong_nonce_is_rejected(self):
        self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        bogus = base64.urlsafe_b64encode(b"\x00" * 16).rstrip(b"=").decode()
        self.assertFalse(self.wire.verify_challenge(CLIENT, bogus, 100.0))

    def test_malformed_nonce_does_not_raise(self):
        self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        # Not base64 at all — must be a clean False, never an exception on the
        # UDP thread.
        self.assertFalse(self.wire.verify_challenge(CLIENT, "!!!not base64!!!", 100.0))

    def test_challenge_table_stays_bounded(self):
        # An unanswered-HELLO flood must not grow the table without limit.
        for i in range(rs.CHAL_MAX + 50):
            self.wire.issue_challenge(self.sock, ("10.0.0.1", 1000 + i), 100.0)
        self.assertLessEqual(len(self.wire._chal), rs.CHAL_MAX)

    def test_sweep_drops_only_expired_challenges(self):
        self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        self.wire.issue_challenge(self.sock, STRANGER, 100.0 + rs.CHAL_TTL_S)
        self.wire.sweep_challenges(100.0 + rs.CHAL_TTL_S + 0.01)
        self.assertNotIn(CLIENT, self.wire._chal)
        self.assertIn(STRANGER, self.wire._chal)


# ── replay / forgery / source pinning ────────────────────────────────────────
class ReplayAndForgery(unittest.TestCase):

    def setUp(self):
        self.wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.sock = FakeSock()
        self.fc = handshake(self.wire, self.sock)

    def test_monotonic_counter_accepted(self):
        pkt, _ = self.fc.seal("CLICK")
        self.assertEqual(self.wire.parse(pkt, CLIENT, CLIENT), ("CLICK", "", True))

    def test_replayed_packet_rejected(self):
        pkt, ctr = self.fc.seal("CLICK")
        self.assertIsNotNone(self.wire.parse(pkt, CLIENT, CLIENT))
        self.assertIsNone(self.wire.parse(pkt, CLIENT, CLIENT))   # same counter

    def test_equal_counter_rejected(self):
        pkt, _ = self.fc.seal("CLICK")
        self.wire.parse(pkt, CLIENT, CLIENT)
        again, _ = self.fc.seal("CLICK", ctr=self.wire.cli_ctr)
        self.assertIsNone(self.wire.parse(again, CLIENT, CLIENT))

    def test_wrong_sid_rejected(self):
        # Same width as the pinned sid, so this tests authorisation and not framing.
        pkt, _ = self.fc.seal("CLICK", sid=b"\xaa" * self.fc.sid_len)
        self.assertIsNone(self.wire.parse(pkt, CLIENT, CLIENT))

    def test_right_session_from_wrong_source_rejected(self):
        # Tag-valid and counter-fresh, but not from the pinned address.
        pkt, _ = self.fc.seal("CLICK")
        self.assertIsNone(self.wire.parse(pkt, STRANGER, CLIENT))

    def test_stranger_cannot_desync_the_watermark(self):
        # THE property that keeps a captured-packet replay from locking the real
        # phone out: the counter watermark may only advance for packets that came
        # from the pinned client. A stranger replaying with a huge counter must
        # not move it, or every subsequent genuine packet would look stale.
        before = self.wire.cli_ctr
        far, _ = self.fc.seal("CLICK", ctr=before + 10_000)
        self.assertIsNone(self.wire.parse(far, STRANGER, CLIENT))
        self.assertEqual(self.wire.cli_ctr, before)
        nxt, _ = self.fc.seal("CLICK", ctr=before + 1)
        self.assertIsNotNone(self.wire.parse(nxt, CLIENT, CLIENT))

    def test_tampered_tag_rejected(self):
        pkt, _ = self.fc.seal("CLICK")
        bad = bytearray(pkt)
        bad[-1] ^= 0x01
        self.assertIsNone(self.wire.parse(bytes(bad), CLIENT, CLIENT))

    def test_tampered_aad_rejected(self):
        # Flipping a header bit changes both the nonce and the AAD ⇒ tag fails.
        pkt, _ = self.fc.seal("CLICK")
        bad = bytearray(pkt)
        bad[3] ^= 0x01
        self.assertIsNone(self.wire.parse(bytes(bad), CLIENT, CLIENT))

    def test_truncated_packet_rejected(self):
        pkt, _ = self.fc.seal("CLICK")
        for n in (0, 1, 2, 13, 14, 29):
            self.assertIsNone(self.wire.parse(pkt[:n], CLIENT, CLIENT),
                              f"accepted a {n}-byte packet")

    def test_replies_are_sealed_under_the_servers_own_session(self):
        sock = FakeSock()
        self.wire.reply(sock, CLIENT, "PONG")
        self.wire.reply(sock, CLIENT, "PONG")
        a, b = sock.sent[0][0], sock.sent[1][0]
        self.assertEqual(unseal(KEY, a), "PONG")
        # Distinct counters ⇒ distinct nonces ⇒ distinct ciphertext, so the phone
        # can enforce freshness on replies too.
        self.assertNotEqual(a, b)
        self.assertEqual(a[2:6], b[2:6])                      # same server sid
        self.assertLess(int.from_bytes(a[6:14], "big"),
                        int.from_bytes(b[6:14], "big"))


# ── v1 plaintext + secure-only ───────────────────────────────────────────────
class PlaintextV1(unittest.TestCase):

    def test_token_match_accepted(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=False)
        self.assertEqual(wire.parse(f"{TOKEN} KEY hello world".encode(), CLIENT, CLIENT),
                         ("KEY", "hello world", False))

    def test_rest_keeps_spaces(self):
        # The server splits at most 3 fields so typed text survives intact.
        wire = rs.Wire(TOKEN, KEY, require_secure=False)
        _, rest, _ = wire.parse(f"{TOKEN} KEY a  b   c".encode(), CLIENT, CLIENT)
        self.assertEqual(rest, "a  b   c")

    def test_wrong_token_rejected(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=False)
        self.assertIsNone(wire.parse(b"WRONG1 CLICK", CLIENT, CLIENT))

    def test_missing_verb_rejected(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=False)
        self.assertIsNone(wire.parse(TOKEN.encode(), CLIENT, CLIENT))

    def test_secure_only_drops_valid_plaintext(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.assertIsNone(wire.parse(f"{TOKEN} CLICK".encode(), CLIENT, CLIENT))

    def test_v2_magic_falls_through_to_v1_when_no_key(self):
        # No key ⇒ aes is None ⇒ a v2-looking packet is just junk on the v1 path.
        wire = rs.Wire(TOKEN, None, require_secure=False)
        self.assertIsNone(wire.parse(b"L2" + b"\x00" * 40, CLIENT, CLIENT))


# ── crash-safety of code that runs on the single UDP thread ──────────────────
class ActionLabels(unittest.TestCase):
    """Every label lambda is evaluated inline on the serve thread, so one that
    raises takes the whole receive loop down and the server goes deaf with the
    window still looking healthy. No input may make them throw."""

    HOSTILE = ["", " ", "abc", "-", "--", "0", "-5", "999999999999999999999",
               "1 2 3", "nan", "inf", "  12  ", "\x00", "🙂", "a" * 500,
               "-0", "+3", "3.5", "next extra junk"]

    def test_no_label_raises_on_hostile_input(self):
        for verb, fn in rs._ACTION_LABELS.items():
            for rest in self.HOSTILE:
                with self.subTest(verb=verb, rest=rest[:16]):
                    try:
                        fn(rest)
                    except Exception as e:            # pragma: no cover
                        self.fail(f"{verb} label raised on {rest[:16]!r}: {e}")

    def test_every_control_verb_is_known_to_the_dispatcher(self):
        # Guards against a verb being added to CONTROL_VERBS (so it gets gated by
        # the local-takeover pause) but never handled, or vice versa.
        self.assertIn("MOVE", rs.CONTROL_VERBS)
        self.assertNotIn("PING", rs.CONTROL_VERBS)   # liveness must never be gated
        self.assertNotIn("BGET", rs.CONTROL_VERBS)
        self.assertNotIn("VGET", rs.CONTROL_VERBS)


class Dispatch(unittest.TestCase):
    """Pure argument-parsing helpers — no OS input involved."""

    def setUp(self):
        self._kb = rs.keyboard
        rs.keyboard = _Recorder()
        self.addCleanup(lambda: setattr(rs, "keyboard", self._kb))

    def test_split_verb(self):
        self.assertEqual(rs._split_verb("MOVE 1 2"), ("MOVE", "1 2"))
        self.assertEqual(rs._split_verb("CLICK"), ("CLICK", ""))
        self.assertEqual(rs._split_verb(""), ("", ""))

    def test_resolve_key(self):
        self.assertEqual(rs._resolve_key("c"), "c")
        self.assertIsNotNone(rs._resolve_key("enter"))
        self.assertIsNotNone(rs._resolve_key("f7"))
        self.assertIsNone(rs._resolve_key("nonsense"))

    def test_combo_holds_modifiers_then_taps(self):
        rs.do_combo("ctrl c")
        self.assertEqual(rs.keyboard.calls, [
            ("press", "<Key.ctrl>"), ("press", "c"),
            ("release", "c"), ("release", "<Key.ctrl>"),
        ])

    def test_combo_multiple_modifiers_release_in_reverse(self):
        rs.do_combo("ctrl shift t")
        names = [c[1] for c in rs.keyboard.calls]
        self.assertEqual(names, ["<Key.ctrl>", "<Key.shift>", "t", "t",
                                 "<Key.shift>", "<Key.ctrl>"])

    def test_combo_special_target(self):
        rs.do_combo("shift enter")           # the KeyboardPanel's "new line" key
        self.assertEqual(rs.keyboard.calls[1], ("press", "<Key.enter>"))

    def test_combo_with_no_target_is_a_noop(self):
        for spec in ("", "   ", "ctrl", "ctrl alt", "ctrl nonsense"):
            rs.keyboard.calls.clear()
            rs.do_combo(spec)
            self.assertEqual(rs.keyboard.calls, [], f"{spec!r} pressed something")

    def test_appswitch_session_holds_and_releases_alt(self):
        rs.appswitch_reset()
        rs.do_appswitch("next")
        self.assertTrue(rs._alt_held)
        self.assertIn(("press", "<Key.alt>"), rs.keyboard.calls)
        rs.do_appswitch("end")
        self.assertFalse(rs._alt_held)
        self.assertIn(("release", "<Key.alt>"), rs.keyboard.calls)

    def test_appswitch_reset_releases_a_stuck_alt(self):
        rs.appswitch_reset()
        rs.do_appswitch("next")
        rs.appswitch_reset()                 # simulates BYE / a dropped link
        self.assertFalse(rs._alt_held)

    def test_appswitch_ignores_unknown_actions(self):
        rs.appswitch_reset()
        rs.keyboard.calls.clear()
        rs.do_appswitch("sideways")
        self.assertEqual(rs.keyboard.calls, [])
        self.assertFalse(rs._alt_held)


class MotionArguments(unittest.TestCase):
    """MOVE/SCROLL/ZOOM arguments go straight into ctypes/OS calls, so magnitude
    and shape both matter. A bigint used to raise out of pynput and — before the
    serve-loop guard — kill the one receive thread, leaving the server deaf with
    the window still showing "running"."""

    def setUp(self):
        self._mouse, self._kb = rs.mouse, rs.keyboard
        rs.mouse, rs.keyboard = _Recorder(), _Recorder()
        self.addCleanup(lambda: (setattr(rs, "mouse", self._mouse),
                                 setattr(rs, "keyboard", self._kb)))

    def test_move_passes_normal_deltas_through(self):
        rs.handle_packet("MOVE", "3 -4")
        self.assertEqual(rs.mouse.calls, [("move", 3, -4)])

    def test_move_clamps_absurd_magnitudes(self):
        rs.handle_packet("MOVE", "99999999999999999999 -10000000")
        self.assertEqual(rs.mouse.calls,
                         [("move", rs.MOVE_MAX_PX, -rs.MOVE_MAX_PX)])

    def test_scroll_and_zoom_clamp(self):
        rs.handle_packet("SCROLL", "0 -999999")
        self.assertEqual(rs.mouse.calls, [("scroll", 0, -rs.SCROLL_MAX_STEPS)])
        rs.mouse.calls.clear()
        rs.handle_packet("ZOOM", "500000")
        self.assertEqual(rs.mouse.calls, [("scroll", 0, rs.ZOOM_MAX_STEPS)])

    def test_zoom_always_releases_ctrl(self):
        # The press/release straddles a mouse.scroll; if that ever throws, a stuck
        # Ctrl would make the laptop unusable. It's in a finally — prove it.
        rs.handle_packet("ZOOM", "1")
        self.assertIn(("release", "<Key.ctrl>"), rs.keyboard.calls)

        def boom(*a, **kw):
            raise RuntimeError("scroll failed")
        rs.mouse.scroll = boom
        rs.keyboard.calls.clear()
        with self.assertRaises(RuntimeError):
            rs.handle_packet("ZOOM", "1")
        self.assertIn(("release", "<Key.ctrl>"), rs.keyboard.calls)

    def test_malformed_motion_args_are_dropped_silently(self):
        for verb, rest in [("MOVE", ""), ("MOVE", "1"), ("MOVE", "1 2 3"),
                           ("MOVE", "a b"), ("MOVE", "1 x"), ("MOVE", "  "),
                           ("SCROLL", ""), ("SCROLL", "up down"),
                           ("ZOOM", ""), ("ZOOM", "lots")]:
            rs.mouse.calls.clear()
            with self.subTest(verb=verb, rest=rest):
                rs.handle_packet(verb, rest)   # must not raise
                self.assertEqual(rs.mouse.calls, [])

    def test_unknown_verb_is_a_noop(self):
        rs.handle_packet("NONSENSE", "whatever")
        self.assertEqual(rs.mouse.calls, [])
        self.assertEqual(rs.keyboard.calls, [])


class AdvertisedAddress(unittest.TestCase):
    """Which IP goes in the QR. Getting this wrong is silent: the server looks
    healthy (loopback and the GUI don't care) while every phone times out."""

    def _fake_psutil(self, ifaces, down=()):
        """ifaces: {name: [ip, ...]}. Names in [down] report isup=False."""
        import socket as _s
        snic = lambda ip: types.SimpleNamespace(family=_s.AF_INET, address=ip)
        mod = types.ModuleType("psutil")
        mod.net_if_addrs = lambda: {n: [snic(i) for i in ips]
                                    for n, ips in ifaces.items()}
        mod.net_if_stats = lambda: {n: types.SimpleNamespace(isup=n not in down)
                                    for n in ifaces}
        self._saved = sys.modules.get("psutil")
        sys.modules["psutil"] = mod
        self.addCleanup(lambda: sys.modules.pop("psutil", None)
                        if self._saved is None
                        else sys.modules.__setitem__("psutil", self._saved))

    def test_virtual_adapters_are_excluded_not_just_deprioritised(self):
        # Excluded, because the caller asks "could a phone use this address?" and a
        # merely low-ranked entry still answers yes. (This was a real bug: ranking
        # alone let the WSL address satisfy the default-route check and win.)
        self._fake_psutil({
            "vEthernet (WSL)": ["172.28.0.1"],
            "Docker Bridge": ["172.17.0.1"],
            "Wi-Fi": ["192.168.1.20"],
        })
        self.assertEqual([ip for ip, _ in rs.candidate_ips()], ["192.168.1.20"])

    def test_down_interfaces_are_skipped(self):
        self._fake_psutil({"Wi-Fi": ["192.168.1.20"], "Ethernet": ["192.168.1.5"]},
                          down={"Ethernet"})
        self.assertEqual([ip for ip, _ in rs.candidate_ips()], ["192.168.1.20"])

    def test_wifi_ranks_above_ethernet(self):
        self._fake_psutil({"Ethernet": ["192.168.1.5"], "Wi-Fi": ["192.168.1.20"]})
        self.assertEqual([ip for ip, _ in rs.candidate_ips()],
                         ["192.168.1.20", "192.168.1.5"])

    def test_public_loopback_and_linklocal_are_excluded(self):
        self._fake_psutil({"Wi-Fi": ["192.168.1.20"], "lo": ["127.0.0.1"],
                           "ppp0": ["8.8.4.4"], "eth9": ["169.254.3.4"]})
        self.assertEqual([ip for ip, _ in rs.candidate_ips()], ["192.168.1.20"])

    def test_probe_is_kept_when_it_names_a_real_nic(self):
        # No behaviour change on an ordinary machine: if the default route already
        # points at a genuine LAN interface, that's what we advertise.
        self._fake_psutil({"Wi-Fi": ["192.168.1.20"], "Ethernet": ["192.168.1.5"]})
        saved = rs._default_route_ip
        rs._default_route_ip = lambda: "192.168.1.5"     # wired route
        self.addCleanup(lambda: setattr(rs, "_default_route_ip", saved))
        self.assertEqual(rs.lan_ip(), "192.168.1.5")

    def test_probe_is_overridden_when_it_names_a_virtual_adapter(self):
        # The WSL2/Hyper-V/Docker/VPN case: the route works for the internet but is
        # unreachable from a phone, so advertising it guarantees a timeout.
        self._fake_psutil({"vEthernet (WSL)": ["172.28.0.1"],
                           "Wi-Fi": ["192.168.1.20"]})
        saved = rs._default_route_ip
        rs._default_route_ip = lambda: "172.28.0.1"
        self.addCleanup(lambda: setattr(rs, "_default_route_ip", saved))
        self.assertEqual(rs.lan_ip(), "192.168.1.20")

    def test_falls_back_to_the_probe_when_psutil_is_unusable(self):
        self._saved = sys.modules.get("psutil")
        sys.modules["psutil"] = None      # import psutil -> raises
        self.addCleanup(lambda: sys.modules.pop("psutil", None)
                        if self._saved is None
                        else sys.modules.__setitem__("psutil", self._saved))
        self.assertEqual(rs.candidate_ips(), [])
        saved = rs._default_route_ip
        rs._default_route_ip = lambda: "10.0.0.7"
        self.addCleanup(lambda: setattr(rs, "_default_route_ip", saved))
        self.assertEqual(rs.lan_ip(), "10.0.0.7")


class ActivityFeedPrivacy(unittest.TestCase):
    """The activity feed is rendered on the laptop screen. Echoing keystrokes there
    defeats the point of encrypting them on the wire — a shoulder-surfer or a screen
    share reads them straight off the window."""

    SECRETS = ["hunter2", "correct horse battery staple",
               "https://example.com/reset?token=abc123", "sk-live-0123456789"]

    def test_typed_text_is_never_echoed(self):
        label = rs._ACTION_LABELS["KEY"]
        for s in self.SECRETS:
            text = label(s)[0]
            self.assertNotIn(s, text)
            # not even a prefix: the old code logged the first 24 characters
            self.assertNotIn(s[:8], text)
            self.assertIn(str(len(s)), text)      # shape is still reported

    def test_empty_payloads_log_nothing(self):
        self.assertIsNone(rs._ACTION_LABELS["KEY"](""))

    # Verbs whose payload is arbitrary text the USER typed or pasted. These are the
    # only ones where echoing the payload leaks something the wire encryption was
    # protecting. Every other verb carries a bounded enum (MEDIA play_pause, SYS
    # lock, KEYSP enter) or a number, which is safe to show and useful to see — the
    # feed would be worthless if it hid those too.
    #
    # ADD TO THIS SET when a new verb carries free text. CLIP used to be here and
    # was the second offender found; it has since been removed along with PRES.
    FREE_TEXT_VERBS = {"KEY"}

    def test_free_text_verbs_never_echo_their_payload(self):
        for verb in self.FREE_TEXT_VERBS:
            label = rs._ACTION_LABELS[verb]
            for s in self.SECRETS:
                text = label(s)[0]
                self.assertNotIn(s, text, f"{verb} echoed a full payload")
                self.assertNotIn(s[:8], text, f"{verb} echoed a payload prefix")

    def test_the_free_text_set_still_matches_the_verbs_that_take_text(self):
        # Guards the SET above, not the labels: if a verb starts carrying user text
        # and isn't listed, the test above silently stops covering it. KEY is the
        # only free-text verb left, so anything new in _ACTION_LABELS is a prompt to
        # classify it deliberately rather than by omission.
        known = {"CLICK", "RCLICK", "MCLICK", "MDOWN", "MUP", "ZOOM", "MEDIA",
                 "KEY", "KEYSP", "COMBO", "ASW", "SYS", "VOL", "BRIGHT"}
        self.assertEqual(set(rs._ACTION_LABELS), known,
                         "a verb was added/removed in _ACTION_LABELS — decide "
                         "whether its payload is free text and update "
                         "FREE_TEXT_VERBS accordingly")

    def test_removed_verbs_are_gone_from_every_table(self):
        # PRES (slides) and CLIP (paste-text-to-laptop) were removed with their UI.
        # Half a removal is how the "complete but unreachable" PRES verb survived
        # from the first commit: server-side support with no caller. If either comes
        # back it must come back everywhere, including a UI entry point.
        for verb in ("PRES", "CLIP"):
            self.assertNotIn(verb, rs.CONTROL_VERBS, f"{verb} still gated as control")
            self.assertNotIn(verb, rs._ACTION_LABELS, f"{verb} still has a feed label")
        self.assertFalse(hasattr(rs, "do_presentation"))
        self.assertFalse(hasattr(rs, "PRES_KEYS"))
        self.assertFalse(hasattr(rs, "do_clip"))
        self.assertFalse(hasattr(rs, "set_clipboard"))

    def test_a_removed_verb_is_ignored_without_killing_the_loop(self):
        # An un-updated phone will keep sending these. The dispatch guard must treat
        # them as unknown-and-harmless, not raise on the one receive thread.
        for verb in ("PRES", "CLIP"):
            rs.handle_packet(verb, "next")     # must not raise

    def test_singular_plural_reads_naturally(self):
        self.assertEqual(rs._chars(1), "1 char")
        self.assertEqual(rs._chars(2), "2 chars")
        self.assertEqual(rs._chars(0), "0 chars")


class SecureByDefault(unittest.TestCase):
    """Plaintext v1 is now opt-in. Flipping that default must not turn manual
    pairing into a silent timeout, so a correctly-tokened refusal is explainable."""

    def test_refusing_a_valid_token_is_flagged_for_the_ui(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.assertFalse(wire.plaintext_refused)
        self.assertIsNone(wire.parse(f"{TOKEN} HELLO".encode(), CLIENT, None))
        self.assertTrue(wire.plaintext_refused,
                        "a real phone's manual-code attempt must be distinguishable")

    def test_junk_does_not_raise_the_hint(self):
        # Only a MATCHING token means a genuine phone; random traffic must not
        # produce a misleading "scan the QR" message.
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        for junk in [b"WRONG1 HELLO", b"garbage", b"", b"\xff\xfe\x00",
                     b"L2short", TOKEN.encode()]:
            wire.plaintext_refused = False
            wire.parse(junk, CLIENT, None)
            self.assertFalse(wire.plaintext_refused, f"{junk!r} raised the hint")

    def test_nothing_is_accepted_while_refusing(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.assertIsNone(wire.parse(f"{TOKEN} CLICK".encode(), CLIENT, CLIENT))

    def test_allowing_plaintext_does_not_set_the_flag(self):
        wire = rs.Wire(TOKEN, KEY, require_secure=False)
        self.assertIsNotNone(wire.parse(f"{TOKEN} CLICK".encode(), CLIENT, CLIENT))
        self.assertFalse(wire.plaintext_refused)


class ChallengeEviction(unittest.TestCase):
    """The pending-challenge table is bounded, but HOW it sheds load matters: it
    used to clear wholesale, so a replayed-HELLO flood (which needs no key) could
    repeatedly wipe the real client's outstanding nonce and stall its handshake."""

    def setUp(self):
        self.wire = rs.Wire(TOKEN, KEY, require_secure=True)
        self.sock = FakeSock()

    def test_a_live_challenge_survives_a_port_varying_flood(self):
        self.wire.issue_challenge(self.sock, CLIENT, 100.0)
        mine = unseal(KEY, self.sock.last()).split(" ", 1)[1]

        # The cheap attack: replay one captured HELLO from hundreds of source ports,
        # all inside the victim's TTL so nothing is expired and the victim is the
        # oldest entry. Neither a wholesale clear nor evict-oldest survives this.
        for i in range(rs.CHAL_MAX + 100):
            self.wire.issue_challenge(self.sock, ("10.0.0.9", 1000 + i), 100.0)

        self.assertLessEqual(len(self.wire._chal), rs.CHAL_MAX)
        self.assertTrue(self.wire.verify_challenge(CLIENT, mine, 100.5),
                        "the flood evicted the legitimate client's challenge")

    def test_one_source_cannot_hoard_the_table(self):
        for i in range(200):
            self.wire.issue_challenge(self.sock, ("10.0.0.9", 1000 + i), 100.0)
        held = [a for a in self.wire._chal if a[0] == "10.0.0.9"]
        self.assertLessEqual(len(held), rs.CHAL_PER_IP)

    def test_a_genuine_handshake_burst_still_fits(self):
        # A real phone retries HELLO from ONE port (challenges are idempotent per
        # address) and may re-punch from a few more. The quota must not clip that.
        for port in range(41230, 41234):
            self.wire.issue_challenge(self.sock, ("192.168.1.50", port), 100.0)
        held = [a for a in self.wire._chal if a[0] == "192.168.1.50"]
        self.assertEqual(len(held), 4)

    def test_expired_entries_are_reclaimed_before_live_ones(self):
        for i in range(rs.CHAL_MAX):
            self.wire.issue_challenge(self.sock, ("10.0.0.1", 2000 + i), 100.0)
        # Everything above is now expired; a new challenge should reclaim that space
        # rather than evict itself.
        later = 100.0 + rs.CHAL_TTL_S + 1
        self.wire.issue_challenge(self.sock, CLIENT, later)
        nonce = unseal(KEY, self.sock.last()).split(" ", 1)[1]
        self.assertTrue(self.wire.verify_challenge(CLIENT, nonce, later))
        self.assertLess(len(self.wire._chal), rs.CHAL_MAX)


class ServeLoopResilience(unittest.TestCase):
    """Integration tests over a real loopback socket.

    serve_loop is the ONLY thread serving every phone. An unhandled raise inside a
    verb handler used to end it for good: the process stayed up, the window kept
    showing "server running", and every subsequent packet was ignored until a
    manual restart. These drive an actual handshake and then break things on
    purpose."""

    def setUp(self):
        # Bind on an ephemeral port so a real server on 50505 can't collide.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self._saved = (rs.PORT, rs.HOST, rs.mouse, rs.keyboard)
        rs.PORT, rs.HOST = port, "127.0.0.1"
        rs.mouse, rs.keyboard = _Recorder(), _Recorder()
        self.srv = ("127.0.0.1", port)
        self.events = []
        self.thread = None
        rs._stop.clear()
        self.cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cli.settimeout(2.0)
        self.addCleanup(self._teardown)

    def _teardown(self):
        rs._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.assertFalse(self.thread.is_alive(), "serve_loop did not shut down")
        self.cli.close()
        rs.PORT, rs.HOST, rs.mouse, rs.keyboard = self._saved
        rs._stop.clear()
        _client_connected_clear()

    def _start(self, require_secure=True, net=None):
        self.wire = rs.Wire(TOKEN, KEY, require_secure)
        self.thread = threading.Thread(
            target=rs.serve_loop,
            args=(self.wire, lambda kind, *a: self.events.append((kind,) + a),
                  net, "testhost"),
            daemon=True)
        self.thread.start()
        return self.wire

    def _recv_secure(self):
        data, _ = self.cli.recvfrom(2048)
        return unseal(KEY, data)

    def _pair_secure(self):
        """Complete a real HELLO→CHAL→AUTH→OK over the socket. Retries because the
        loop may not have finished binding on the first send."""
        fc = FakeClient(KEY)
        for _ in range(25):
            pkt, _ = fc.seal("HELLO")
            self.cli.sendto(pkt, self.srv)
            try:
                reply = self._recv_secure()
            except (socket.timeout, Exception):
                continue
            if reply.startswith("CHAL "):
                pkt, _ = fc.seal("AUTH " + reply.split(" ", 1)[1])
                self.cli.sendto(pkt, self.srv)
                self.assertEqual(self._recv_secure(), "OK")
                return fc
        self.fail("secure handshake never completed")

    def _await_event(self, pred, what, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(pred(e) for e in self.events):
                return
            time.sleep(0.05)
        self.fail(f"never saw {what}; events={self.events}")

    def test_handshake_over_a_real_socket(self):
        self._start()
        self._pair_secure()
        self._await_event(lambda e: e[0] == "connected", "a connected event")

    def test_loop_survives_a_raising_handler_and_keeps_serving(self):
        self._start()
        fc = self._pair_secure()

        def boom(*a, **kw):
            raise RuntimeError("injected handler failure")
        rs.mouse.move = boom

        pkt, _ = fc.seal("MOVE 1 1")
        self.cli.sendto(pkt, self.srv)
        self._await_event(lambda e: e[0] == "warn" and "MOVE" in str(e[1]),
                          "a warning about the failed MOVE")
        self.assertTrue(self.thread.is_alive(), "the raise killed the loop")

        # The real proof: the very next packet is still served.
        pkt, _ = fc.seal("PING")
        self.cli.sendto(pkt, self.srv)
        self.assertEqual(self._recv_secure(), "PONG")

    def test_repeated_failures_do_not_flood_the_feed(self):
        self._start()
        fc = self._pair_secure()

        def boom(*a, **kw):
            raise RuntimeError("injected handler failure")
        rs.mouse.move = boom

        for i in range(6):
            pkt, _ = fc.seal(f"MOVE {i} {i}")
            self.cli.sendto(pkt, self.srv)
        self._await_event(lambda e: e[0] == "warn", "the first warning")
        # Give any extra warnings time to show up before counting.
        time.sleep(0.4)
        warns = [e for e in self.events if e[0] == "warn"]
        self.assertEqual(len(warns), 1, f"expected one warning per window, got {warns}")

    def test_bigint_motion_no_longer_reaches_the_os_layer(self):
        # The concrete crash that motivated the guard: a magnitude that raises out
        # of ctypes. It's now clamped before it gets there, so no warning at all.
        self._start()
        fc = self._pair_secure()
        pkt, _ = fc.seal("MOVE 99999999999999999999 0")
        self.cli.sendto(pkt, self.srv)
        pkt, _ = fc.seal("PING")
        self.cli.sendto(pkt, self.srv)
        self.assertEqual(self._recv_secure(), "PONG")
        self.assertEqual([e for e in self.events if e[0] == "warn"], [])
        self.assertIn(("move", rs.MOVE_MAX_PX, 0), rs.mouse.calls)

    def test_a_broken_volume_backend_cannot_kill_the_loop(self):
        # The bug behind "after the laptop sleeps I have to restart the app".
        # VGET is answered by calling get_volume() inline, ABOVE the handler guard.
        # On Windows that reaches an IAudioEndpointVolume captured once at import,
        # which Windows invalidates on resume (or when the output device changes) —
        # every later call raises. The raise escaped serve_loop and killed the only
        # receive thread, while the window still showed a healthy green server. VGET
        # is the phone's own liveness probe, so this fired on the first poll after
        # every sleep.
        self._start()
        fc = self._pair_secure()

        def boom():
            raise OSError("audio endpoint invalidated")

        with mock.patch.object(rs, "get_volume", boom):
            pkt, _ = fc.seal("VGET")
            self.cli.sendto(pkt, self.srv)
            # No VOL comes back — that's fine, one poll is cheap. What must survive
            # is the loop itself.
            pkt, _ = fc.seal("PING")
            self.cli.sendto(pkt, self.srv)
            self.assertEqual(self._recv_secure(), "PONG")
        self.assertTrue(self.thread.is_alive(), "a volume failure killed the loop")

    def test_an_absent_output_device_is_answered_with_silence_not_a_crash(self):
        # The re-acquiring Windows getter returns None when there is no output
        # device at all. "VOL None" would reach the phone as an unparseable reply,
        # so the branch must send nothing and leave the session intact.
        self._start()
        fc = self._pair_secure()
        with mock.patch.object(rs, "get_volume", lambda: None):
            pkt, _ = fc.seal("VGET")
            self.cli.sendto(pkt, self.srv)
            pkt, _ = fc.seal("PING")
            self.cli.sendto(pkt, self.srv)
            self.assertEqual(self._recv_secure(), "PONG")

    def test_a_phone_that_rebuilt_its_session_can_re_pair_from_a_new_port(self):
        # What actually happens across a laptop sleep: the phone's watchdog gives up
        # after ~2.5s of silence and rebuilds from scratch — new socket (so a new
        # source port), new session id, counter back to 0 — while the server may
        # still be holding the OLD address as its pinned client. The handshake has
        # to land anyway, or the phone can never get back in on its own.
        self._start()
        self._pair_secure()
        self._await_event(lambda e: e[0] == "connected", "the first pairing")

        fresh = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fresh.settimeout(2.0)
        self.addCleanup(fresh.close)
        fc = FakeClient(KEY, sid=b"\x99" * 8)
        for _ in range(25):
            pkt, _ = fc.seal("HELLO")
            fresh.sendto(pkt, self.srv)
            try:
                reply = unseal(KEY, fresh.recvfrom(2048)[0])
            except Exception:
                continue
            if reply.startswith("CHAL "):
                pkt, _ = fc.seal("AUTH " + reply.split(" ", 1)[1])
                fresh.sendto(pkt, self.srv)
                self.assertEqual(unseal(KEY, fresh.recvfrom(2048)[0]), "OK")
                break
        else:
            self.fail("the rebuilt session never re-paired")

        # And it is really in control, not merely acknowledged.
        pkt, _ = fc.seal("PING")
        fresh.sendto(pkt, self.srv)
        self.assertEqual(unseal(KEY, fresh.recvfrom(2048)[0]), "PONG")

    def test_moving_to_another_network_re_announces_without_a_restart(self):
        # Roaming to another SSID, or a DHCP lease change, moves our address with no
        # tick gap at all — so the sleep/resume branch never fires. lan_ip() was read
        # only at startup and inside that branch, so the mDNS record and the on-screen
        # QR kept pointing at the old address for the rest of the process, and the
        # phone had nothing reachable to dial. Closing and reopening the app was the
        # only cure. The watch re-reads it on its own cadence instead.
        registered = []
        net = {"ip": "192.168.1.9", "zc": "zc0", "info": "info0"}
        address = ["192.168.1.9"]

        def fake_mdns(ip, hostname):
            registered.append(ip)
            return f"zc-{ip}", f"info-{ip}"

        with mock.patch.object(rs, "start_mdns", fake_mdns), \
                mock.patch.object(rs, "lan_ip", lambda: address[0]), \
                mock.patch.object(rs, "NET_WATCH_S", 0.2):
            self._start(net=net)
            time.sleep(0.6)                     # a few watch passes on the old address
            self.assertEqual(registered, [], "re-announced an address that hadn't moved")

            address[0] = "10.0.0.77"            # the laptop lands on another network
            self._await_event(lambda e: e[0] == "netchange" and e[1] == "10.0.0.77",
                              "a netchange for the new address")

        self.assertEqual(net["ip"], "10.0.0.77")
        self.assertIn("10.0.0.77", registered)

    def test_a_missing_address_is_never_published(self):
        # Between networks lan_ip() falls back to 127.0.0.1. Publishing that is what
        # poisoned discovery after a wake; the watch must sit the outage out and then
        # announce the real address once there is one.
        registered = []
        net = {"ip": "192.168.1.9", "zc": "zc0", "info": "info0"}
        address = ["127.0.0.1"]

        def fake_mdns(ip, hostname):
            registered.append(ip)
            return f"zc-{ip}", f"info-{ip}"

        with mock.patch.object(rs, "start_mdns", fake_mdns), \
                mock.patch.object(rs, "lan_ip", lambda: address[0]), \
                mock.patch.object(rs, "NET_WATCH_S", 0.2):
            self._start(net=net)
            time.sleep(0.8)
            self.assertEqual(registered, [], f"published a useless address: {registered}")

            address[0] = "10.0.0.77"            # Wi-Fi finally associates
            self._await_event(lambda e: e[0] == "netchange" and e[1] == "10.0.0.77",
                              "a netchange once a real address existed")
        self.assertEqual(registered, ["10.0.0.77"])

    def test_a_wake_rebinds_the_socket_and_keeps_serving(self):
        # Squeeze RESUME_GAP_S below the loop's own 1s recv timeout so an idle tick
        # looks like a wake: the resume branch then runs for real — closing and
        # rebinding the socket, re-arming the guard, re-announcing — over and over.
        # If any of that is wrong the phone stops being served, which is the whole
        # failure this change exists to prevent.
        net = {"ip": "192.168.1.9", "zc": "zc0", "info": "info0"}
        with mock.patch.object(rs, "start_mdns", lambda ip, h: (f"zc-{ip}", f"info-{ip}")), \
                mock.patch.object(rs, "lan_ip", lambda: "192.168.1.9"), \
                mock.patch.object(rs, "RESUME_GAP_S", 0.5):
            self._start(net=net)
            self._await_event(lambda e: e[0] == "log" and "Woke" in str(e[1]),
                              "a simulated wake")
            # Pair AFTER the loop has started cycling through wakes, and keep
            # retrying — a rebind in flight can eat a datagram, exactly as a real
            # one can, and the phone's own handshake resends through it.
            fc = self._pair_secure()
            for _ in range(20):
                pkt, _ = fc.seal("PING")
                self.cli.sendto(pkt, self.srv)
                try:
                    if self._recv_secure() == "PONG":
                        break
                except Exception:
                    continue
            else:
                self.fail("the phone stopped being served across socket rebinds")
        self.assertTrue(self.thread.is_alive(), "the resume path killed the loop")

    def test_second_plaintext_phone_is_turned_away_and_reported(self):
        # v1 is gated only by the token, so a second phone that knows the code
        # reaches the pinned-source check. The GUI and terminal have always had a
        # handler for this; nothing ever emitted it, so takeovers were invisible.
        self._start(require_secure=False)
        self.cli.sendto(f"{TOKEN} HELLO".encode(), self.srv)
        for _ in range(25):
            try:
                if self.cli.recvfrom(2048)[0] == b"OK":
                    break
            except socket.timeout:
                self.cli.sendto(f"{TOKEN} HELLO".encode(), self.srv)
        else:
            self.fail("plaintext handshake never completed")

        intruder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(intruder.close)
        intruder.sendto(f"{TOKEN} CLICK".encode(), self.srv)
        self._await_event(lambda e: e[0] == "blocked", "a blocked-takeover event")
        # ...and the intruder's click must not have been executed.
        self.assertNotIn(("click", "<Key.left>", 1), rs.mouse.calls)


class _FakeEndpoint:
    """Stands in for an IAudioEndpointVolume. [dead] mimics the invalidation
    Windows performs on resume or an output-device change: every call raises."""

    def __init__(self, level=0.56, dead=False):
        self.level = level
        self.dead = dead

    def GetMasterVolumeLevelScalar(self):
        if self.dead:
            raise OSError("AUDCLNT_E_DEVICE_INVALIDATED")
        return self.level

    def SetMasterVolumeLevelScalar(self, scalar, _guid):
        if self.dead:
            raise OSError("AUDCLNT_E_DEVICE_INVALIDATED")
        self.level = scalar


class WindowsVolumeBackend(unittest.TestCase):
    """The Windows getter must survive its endpoint going stale.

    This is the bug that made a laptop sleep unrecoverable: the endpoint was
    captured once at import, the getter had no error handling, and serve_loop
    calls it inline to answer VGET — above the guard — so the first volume poll
    after a wake killed the only receive thread. Portable: sys.platform and the
    pycaw/comtypes imports are stubbed, so it runs on any OS."""

    def _make(self, endpoints):
        """Build the Windows backend over a queue of endpoints, newest last.
        Returns (get, set, label, speaker_calls)."""
        calls = []

        class FakeAudioUtilities:
            @staticmethod
            def GetSpeakers():
                calls.append(1)
                # Later re-acquisitions get the next endpoint, mimicking Windows
                # handing back a working one after the device settled.
                ep = endpoints[min(len(calls), len(endpoints)) - 1]
                return types.SimpleNamespace(EndpointVolume=ep)

        pycaw_pkg = types.ModuleType("pycaw")
        pycaw_mod = types.ModuleType("pycaw.pycaw")
        pycaw_mod.AudioUtilities = FakeAudioUtilities
        pycaw_mod.IAudioEndpointVolume = object
        pycaw_pkg.pycaw = pycaw_mod

        with mock.patch.dict(sys.modules, {"pycaw": pycaw_pkg,
                                           "pycaw.pycaw": pycaw_mod}), \
                mock.patch.object(sys, "platform", "win32"):
            get_fn, set_fn, label = rs.make_volume()
        return get_fn, set_fn, label, calls

    def test_a_healthy_endpoint_is_acquired_once_and_reused(self):
        get_fn, _set, label, calls = self._make([_FakeEndpoint(0.42)])
        self.assertEqual(label, "pycaw")
        self.assertEqual([get_fn() for _ in range(4)], [42, 42, 42, 42])
        # One lookup at startup (the fail-fast probe); the cached pointer serves
        # the rest. Re-resolving per call would be a COM round trip per poll.
        self.assertEqual(len(calls), 1, f"endpoint re-resolved needlessly: {calls}")

    def test_a_stale_endpoint_is_re_acquired_instead_of_raising(self):
        stale, fresh = _FakeEndpoint(0.42), _FakeEndpoint(0.77)
        get_fn, _set, _label, calls = self._make([stale, fresh])
        self.assertEqual(get_fn(), 42)

        stale.dead = True               # the wake / device change happens here
        before = len(calls)
        self.assertEqual(get_fn(), 77, "did not recover onto a fresh endpoint")
        self.assertGreater(len(calls), before, "never re-resolved the endpoint")
        # And it stays recovered — the new pointer is now the cached one.
        self.assertEqual(get_fn(), 77)

    def test_a_permanently_dead_backend_degrades_to_none(self):
        dead_a, dead_b = _FakeEndpoint(dead=True), _FakeEndpoint(dead=True)
        # The startup probe resolves an endpoint but never calls it, so a backend
        # whose device dies immediately still constructs — and then answers None.
        get_fn, set_fn, _label, _calls = self._make([dead_a, dead_b])
        self.assertIsNone(get_fn(), "a dead device must yield None, not raise")
        set_fn(50)                      # must also swallow, not raise

    def test_the_setter_recovers_the_same_way(self):
        stale, fresh = _FakeEndpoint(0.42), _FakeEndpoint(0.10)
        _get, set_fn, _label, _calls = self._make([stale, fresh])
        stale.dead = True
        set_fn(90)
        self.assertAlmostEqual(fresh.level, 0.90, places=6)


class InputGuardRearm(unittest.TestCase):
    """Key-UPs are not delivered to low-level hooks across a lock screen, so the
    held-key set keeps whatever was down when the screen locked. With ctrl, alt
    and shift stuck in it, the next literal 'L' matched the panic chord and
    latched remote input OFF on a session that still looked connected."""

    def test_rearm_forgets_keys_the_hook_never_saw_released(self):
        guard = rs.LocalInputGuard(on_physical=lambda: None, on_panic=lambda: None)
        guard._down.update(rs.LocalInputGuard.PANIC_VKS)
        self.assertTrue(rs.LocalInputGuard.PANIC_VKS.issubset(guard._down))
        guard.rearm()                   # no thread started: the clear still runs
        self.assertEqual(guard._down, set())

    def test_rearm_is_safe_before_the_hook_thread_exists(self):
        # serve_loop calls this on every wake, including one that lands before the
        # guard thread has published its id. It must not raise there.
        guard = rs.LocalInputGuard(on_physical=lambda: None, on_panic=lambda: None)
        self.assertIsNone(guard._tid)
        guard.rearm()


class PointerVisibility(unittest.TestCase):
    """Windows only draws the pointer while it thinks the last input was a mouse.

    pynput moves it with SetCursorPos, which does not register as input — measured
    on Windows 11: SetCursorPos never advances GetLastInputInfo, SendInput always
    does. So remote movement drove an INVISIBLE pointer: hover states lit up and
    clicks landed, but the user could not see where, and only touching the real
    mouse brought it back. A zero-delta SendInput fixes it without moving the
    pointer or putting Windows' pointer ballistics on our deltas."""

    def setUp(self):
        self._saved = (rs.mouse, rs.keyboard)
        rs.mouse, rs.keyboard = _Recorder(), _Recorder()
        self.addCleanup(lambda: setattr(rs, "mouse", self._saved[0]))
        self.addCleanup(lambda: setattr(rs, "keyboard", self._saved[1]))

    def test_pointer_verbs_wake_the_cursor(self):
        woke = []
        with mock.patch.object(rs, "wake_pointer", lambda: woke.append(1)):
            rs.handle_packet("MOVE", "3 -4")
        self.assertEqual(len(woke), 1, "a MOVE left the pointer hidden")
        self.assertIn(("move", 3, -4), rs.mouse.calls, "the move itself was lost")

    def test_every_pointer_verb_is_covered(self):
        for verb, rest in [("MOVE", "1 1"), ("SCROLL", "0 1"), ("ZOOM", "1"),
                           ("CLICK", ""), ("RCLICK", ""), ("MCLICK", ""),
                           ("MDOWN", ""), ("MUP", "")]:
            with self.subTest(verb=verb):
                woke = []
                with mock.patch.object(rs, "wake_pointer", lambda: woke.append(1)):
                    rs.handle_packet(verb, rest)
                self.assertEqual(len(woke), 1, f"{verb} did not wake the pointer")

    def test_typing_does_not_touch_the_pointer(self):
        # Keyboard verbs have no business un-hiding a pointer the user is not
        # driving — Windows hides it while typing on purpose.
        woke = []
        with mock.patch.object(rs, "wake_pointer", lambda: woke.append(1)):
            rs.handle_packet("KEYSP", "enter")
            rs.handle_packet("COMBO", "ctrl c")
        self.assertEqual(woke, [])

    def test_idle_polling_is_not_a_pointer_verb(self):
        # The phone polls PING/VGET/BGET every 1.5-4s forever. Waking the pointer
        # on those would register input around the clock and keep the laptop out
        # of idle sleep for as long as the app was open.
        self.assertFalse({"PING", "VGET", "BGET", "BYE"} & rs.POINTER_VERBS)

    def test_a_missing_waker_is_not_fatal(self):
        # Off Windows make_pointer_waker returns None and the call is skipped.
        with mock.patch.object(rs, "wake_pointer", None):
            rs.handle_packet("MOVE", "2 2")
        self.assertIn(("move", 2, 2), rs.mouse.calls)

    def test_no_waker_is_built_off_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(rs.make_pointer_waker())


class IdleSuppression(unittest.TestCase):
    """Remote control never counted as activity, for the same root cause as the
    invisible pointer: SetCursorPos does not advance the system idle timer. So a
    session driven entirely from the phone let the display blank and, under a
    lock-on-wake policy, lock the laptop out from under the user."""

    def setUp(self):
        self._saved = (rs.mouse, rs.keyboard, rs._last_remote_ts[0])
        rs.mouse, rs.keyboard = _Recorder(), _Recorder()

        def restore():
            rs.mouse, rs.keyboard = self._saved[0], self._saved[1]
            rs._last_remote_ts[0] = self._saved[2]
        self.addCleanup(restore)

    def test_control_verbs_mark_the_user_present(self):
        rs._last_remote_ts[0] = 0.0
        rs.handle_packet("MOVE", "1 1")
        self.assertGreater(rs._last_remote_ts[0], 0.0)

    def test_a_clicker_press_counts_even_though_it_moves_nothing(self):
        # A phone used as a presenter remote sends no pointer verbs at all. It is
        # still someone standing there using the machine.
        rs._last_remote_ts[0] = 0.0
        rs.handle_packet("KEYSP", "right")
        self.assertGreater(rs._last_remote_ts[0], 0.0,
                           "keyboard-only use would let the display blank")

    def test_the_hold_is_released_when_nothing_holds_it(self):
        # ES_CONTINUOUS on its own resets to the machine's normal power policy.
        # Getting this backwards would pin the display awake forever.
        calls = []
        with mock.patch.object(sys, "platform", "win32"):
            fake = types.SimpleNamespace(
                windll=types.SimpleNamespace(
                    kernel32=types.SimpleNamespace(
                        SetThreadExecutionState=lambda f: calls.append(f))))
            with mock.patch.dict(sys.modules, {"ctypes": fake}):
                hold = rs.make_idle_suppressor()
        self.assertIsNotNone(hold)
        hold(True)
        hold(False)
        ES_CONTINUOUS, ES_SYSTEM, ES_DISPLAY = 0x80000000, 0x1, 0x2
        self.assertEqual(calls[0], ES_CONTINUOUS | ES_SYSTEM | ES_DISPLAY)
        self.assertEqual(calls[1], ES_CONTINUOUS)

    def test_no_suppressor_off_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(rs.make_idle_suppressor())

    def test_a_broken_suppressor_never_reaches_the_loop(self):
        # It runs on the single receive thread; a raise here is the whole session.
        with mock.patch.object(sys, "platform", "win32"):
            def boom(_flags):
                raise OSError("SetThreadExecutionState failed")
            fake = types.SimpleNamespace(
                windll=types.SimpleNamespace(
                    kernel32=types.SimpleNamespace(SetThreadExecutionState=boom)))
            with mock.patch.dict(sys.modules, {"ctypes": fake}):
                hold = rs.make_idle_suppressor()
        hold(True)      # must not raise
        hold(False)


class LoopSupervision(unittest.TestCase):
    """serve_forever is the backstop for everything not yet found.

    The receive loop is the only thread that talks to the phone, and on the GUI
    path it was started bare — anything escaping it ended the session for good
    while the window still read "server running"."""

    def setUp(self):
        rs._stop.clear()
        self.addCleanup(rs._stop.clear)

    def test_a_crashed_loop_is_restarted_and_reported(self):
        calls, events = [], []

        def flaky(wire, emit, net, hostname):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("injected loop failure")
            rs._stop.set()          # second run exits cleanly, ending the supervisor

        with mock.patch.object(rs, "serve_loop", flaky):
            rs.serve_forever(None, lambda kind, *a: events.append((kind,) + a),
                             None, "testhost")

        self.assertEqual(len(calls), 2, "the crashed loop was not restarted")
        self.assertTrue(
            any(e[0] == "warn" and "restarted" in str(e[1]) for e in events),
            f"the restart was not reported; events={events}")

    def test_a_clean_exit_does_not_restart(self):
        calls = []

        def clean(wire, emit, net, hostname):
            calls.append(1)
            rs._stop.set()

        with mock.patch.object(rs, "serve_loop", clean):
            rs.serve_forever(None, lambda *a: None, None, "testhost")
        self.assertEqual(len(calls), 1, "a normal shutdown was treated as a crash")


class PublishableAddress(unittest.TestCase):
    """lan_ip() falls back to 127.0.0.1 so the GUI always has something to draw.

    That fallback must never be PUBLISHED. The resume path used to read the address
    the instant it detected the wake — before the socket rebind, and so usually
    before Wi-Fi had re-associated — then re-register mDNS and rebuild the QR with
    whatever came back. A phone resolving the laptop to 127.0.0.1 dials itself, and
    the poisoned record outlived the outage: only restarting the app cleared it."""

    def test_loopback_is_not_publishable(self):
        with mock.patch.object(rs, "lan_ip", lambda: "127.0.0.1"):
            self.assertIsNone(rs.usable_lan_ip())

    def test_unspecified_is_not_publishable(self):
        with mock.patch.object(rs, "lan_ip", lambda: "0.0.0.0"):
            self.assertIsNone(rs.usable_lan_ip())

    def test_a_real_lan_address_is_publishable(self):
        with mock.patch.object(rs, "lan_ip", lambda: "192.168.1.42"):
            self.assertEqual(rs.usable_lan_ip(), "192.168.1.42")

    def test_announce_reports_whether_the_record_is_live(self):
        # A failed registration must be reported as failure so the caller retries.
        # Storing the None and never looking again silently lost discovery for the
        # rest of the process.
        wire = rs.Wire(TOKEN, KEY, True)
        net = {"ip": "192.168.1.9", "zc": None, "info": None}
        events = []
        emit = lambda kind, *a: events.append((kind,) + a)

        with mock.patch.object(rs, "start_mdns", lambda ip, h: (None, None)):
            self.assertFalse(rs.announce_network(net, "host", wire, emit, "192.168.1.50"))
        with mock.patch.object(rs, "start_mdns", lambda ip, h: ("zc", "info")):
            self.assertTrue(rs.announce_network(net, "host", wire, emit, "192.168.1.50"))
        self.assertEqual(net["ip"], "192.168.1.50")
        # The address changed, so the UI was told to redraw the QR — once.
        changes = [e for e in events if e[0] == "netchange"]
        self.assertEqual(len(changes), 1, f"expected one netchange, got {changes}")
        self.assertEqual(changes[0][1], "192.168.1.50")

    def test_announce_is_quiet_when_the_address_did_not_change(self):
        # A wake re-registers even on an unchanged address (zeroconf's sockets are
        # bound to interfaces that just cycled), but "rescan the QR" would be a lie.
        wire = rs.Wire(TOKEN, KEY, True)
        net = {"ip": "192.168.1.9", "zc": None, "info": None}
        events = []
        with mock.patch.object(rs, "start_mdns", lambda ip, h: ("zc", "info")):
            rs.announce_network(net, "host", wire,
                                lambda kind, *a: events.append((kind,) + a),
                                "192.168.1.9")
        self.assertEqual([e for e in events if e[0] == "netchange"], [])


class SessionTeardown(unittest.TestCase):
    """A phone that is gone must leave nothing pinned behind it."""

    def test_unpin_clears_the_whole_client_session(self):
        wire = rs.Wire(TOKEN, KEY, True)
        sock = FakeSock()
        handshake(wire, sock)
        self.assertIsNotNone(wire.cli_sid)
        self.assertTrue(wire.secure_client)

        wire.unpin_client()
        self.assertIsNone(wire.cli_sid)
        self.assertIsNone(wire.cli_magic)
        self.assertEqual(wire.cli_ctr, -1)
        self.assertFalse(wire.secure_client)
        self.assertEqual(wire._chal, {})
        self.assertIsNone(wire._pending)

    def test_a_new_phone_can_pair_after_an_unpin(self):
        # The point of clearing it: the next handshake starts from a clean slate
        # rather than talking its way past a dead session's watermark.
        wire = rs.Wire(TOKEN, KEY, True)
        sock = FakeSock()
        handshake(wire, sock)
        wire.unpin_client()

        other = ("127.0.0.1", 55555)
        fc = handshake(wire, sock, addr=other, client=None)
        pkt, _ = fc.seal("PING")
        self.assertEqual(wire.parse(pkt, other, other)[0], "PING")


def _client_connected_clear():
    rs._client_connected.clear()


class UriAndToken(unittest.TestCase):

    def test_uri_carries_key_only_when_present(self):
        with_key = rs.build_uri("192.168.1.20", TOKEN, "laptop", KEY)
        self.assertIn("&k=", with_key)
        self.assertNotIn("&k=", rs.build_uri("192.168.1.20", TOKEN, "laptop"))

    def test_key_b64_is_unpadded_urlsafe_and_round_trips(self):
        b64 = rs.key_b64(KEY)
        self.assertNotIn("=", b64)
        self.assertNotIn("+", b64)
        self.assertNotIn("/", b64)
        self.assertEqual(base64.urlsafe_b64decode(b64 + "==="), KEY)


# ── update check ─────────────────────────────────────────────────────────────
class UpdateCheck(unittest.TestCase):
    """The only outbound internet request in the product. Nothing here touches the
    network: fetch_latest_release is driven through a stubbed urlopen, so the suite
    stays hermetic and runs the same offline and in CI."""

    def test_version_parsing_handles_the_shapes_a_tag_can_take(self):
        for text, want in (
            ("v2.0.0", (2, 0, 0)),
            ("2.0.0", (2, 0, 0)),
            ("V2.1", (2, 1, 0)),          # short form pads, so 2.1 == 2.1.0
            ("2", (2, 0, 0)),
            ("2.1.0-rc1", (2, 1, 0)),     # pre-release suffix dropped
            ("2.1.0+win", (2, 1, 0)),
            ("1.2.3.4", (1, 2, 3, 4)),
        ):
            with self.subTest(text=text):
                self.assertEqual(rs.parse_version(text), want)

    def test_unparseable_versions_are_rejected_not_guessed(self):
        for text in ("", None, "latest", "v", "2.x", "2..0", "abc", "1.2.3.4.5", "-1"):
            with self.subTest(text=text):
                self.assertIsNone(rs.parse_version(text))

    def test_versions_compare_numerically_not_lexically(self):
        # The bug this pins: as strings "2.0.10" < "2.0.9", so a lexical compare
        # would stop offering updates after the ninth patch of any minor.
        self.assertTrue(rs.is_newer_version("2.0.10", "2.0.9"))
        self.assertFalse(rs.is_newer_version("2.0.9", "2.0.10"))
        self.assertTrue(rs.is_newer_version("v2.1.0", "2.0.99"))
        self.assertTrue(rs.is_newer_version("3.0.0", "2.9.9"))

    def test_same_version_is_not_an_update(self):
        self.assertFalse(rs.is_newer_version("2.0.0", "2.0.0"))
        self.assertFalse(rs.is_newer_version("v2.0.0", "2.0.0"))
        self.assertFalse(rs.is_newer_version("2.0", "2.0.0"))     # padded equal

    def test_older_release_is_never_reported_as_an_update(self):
        # Guards against a rollback/mis-tag on the repo nagging every user forever.
        self.assertFalse(rs.is_newer_version("1.5.1", "2.0.0"))

    def test_garbled_tag_is_never_reported_as_an_update(self):
        for tag in ("latest", "", None, "release-two"):
            with self.subTest(tag=tag):
                self.assertFalse(rs.is_newer_version(tag, "2.0.0"))

    # --- fetch, with urlopen stubbed -----------------------------------------
    def _stub_urlopen(self, *, body=None, status=200, raises=None):
        """Replace urllib.request.urlopen for one test. Returns a list that records
        the Request objects passed in, so header/URL assertions are possible."""
        import urllib.request
        seen = []

        class _Resp:
            status = None

            def __init__(self, payload):
                self._payload = payload
                self.status = status

            def read(self, n=None):
                return self._payload if n is None else self._payload[:n]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            seen.append(req)
            if raises is not None:
                raise raises
            return _Resp(body if isinstance(body, bytes) else str(body).encode())

        real = urllib.request.urlopen
        urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", real))
        return seen

    def test_fetch_returns_the_tag_name(self):
        self._stub_urlopen(body=b'{"tag_name": "v2.3.0", "name": "LazeR v2.3"}')
        self.assertEqual(rs.fetch_latest_release(), "v2.3.0")

    def test_fetch_sends_a_user_agent_because_github_403s_without_one(self):
        seen = self._stub_urlopen(body=b'{"tag_name": "v2.0.0"}')
        rs.fetch_latest_release()
        self.assertEqual(len(seen), 1)
        ua = seen[0].get_header("User-agent")
        self.assertTrue(ua and ua.startswith("LazeR/"), f"missing User-Agent: {ua!r}")

    def test_fetch_sends_no_credentials_or_identifying_data(self):
        # The request must stay anonymous: no token, no cookie, no device id.
        seen = self._stub_urlopen(body=b'{"tag_name": "v2.0.0"}')
        rs.fetch_latest_release()
        headers = {k.lower() for k in seen[0].headers}
        for banned in ("authorization", "cookie", "x-api-key"):
            self.assertNotIn(banned, headers)
        self.assertIsNone(seen[0].data, "update check must be a GET, not a POST")

    def test_every_failure_mode_is_silent_and_returns_none(self):
        # Offline, DNS failure, rate limit, 5xx, HTML error page, truncated JSON —
        # all mean "we don't know", which must never surface as an error.
        for label, kw in (
            ("connection error", {"raises": OSError("unreachable")}),
            ("timeout", {"raises": TimeoutError()}),
            ("non-200", {"body": b'{"tag_name": "v9.9.9"}', "status": 403}),
            ("html body", {"body": b"<html>rate limited</html>"}),
            ("truncated json", {"body": b'{"tag_name": '}),
            ("json but not an object", {"body": b'["v2.0.0"]'}),
            ("object without tag_name", {"body": b'{"name": "LazeR v2.3"}'}),
            ("tag_name not a string", {"body": b'{"tag_name": 23}'}),
            ("blank tag_name", {"body": b'{"tag_name": "   "}'}),
        ):
            with self.subTest(label):
                self._stub_urlopen(**kw)
                self.assertIsNone(rs.fetch_latest_release())

    def test_read_is_capped_so_a_hostile_body_cannot_exhaust_memory(self):
        # We only need one short field from a host we don't control.
        huge = b'{"tag_name": "v2.1.0", "body": "' + b"A" * 500_000 + b'"}'
        self._stub_urlopen(body=huge)
        # Truncated at 64k mid-string ⇒ invalid JSON ⇒ None, rather than a 500kB read.
        self.assertIsNone(rs.fetch_latest_release())

    def test_check_for_update_pairs_the_tag_with_the_verdict(self):
        self._stub_urlopen(body=b'{"tag_name": "v99.0.0"}')
        self.assertEqual(rs.check_for_update(), ("v99.0.0", True))
        self._stub_urlopen(body=f'{{"tag_name": "v{rs.APP_VERSION}"}}'.encode())
        self.assertEqual(rs.check_for_update(), (f"v{rs.APP_VERSION}", False))
        self._stub_urlopen(raises=OSError("offline"))
        self.assertEqual(rs.check_for_update(), (None, False))

    def test_app_version_is_parseable_and_matches_the_apk(self):
        # A stale APP_VERSION would either nag forever or never nag. The APK's
        # versionName is the value publish_release.ps1 asserts the git tag against,
        # so the two must agree or the server compares against the wrong baseline.
        self.assertIsNotNone(rs.parse_version(rs.APP_VERSION))
        gradle = os.path.join(os.path.dirname(__file__), "..", "..",
                             "android", "app", "build.gradle.kts")
        if not os.path.exists(gradle):
            self.skipTest("android module not present")
        with open(gradle, "r", encoding="utf-8") as f:
            m = re.search(r'versionName\s*=\s*"([^"]+)"', f.read())
        self.assertIsNotNone(m, "couldn't read versionName from build.gradle.kts")
        self.assertEqual(rs.APP_VERSION, m.group(1),
                         "server APP_VERSION and APK versionName have drifted")

    def test_the_releases_url_is_the_official_repo_over_https(self):
        # A typo'd or http:// endpoint would send the request somewhere else.
        for url in (rs.RELEASES_API, rs.RELEASES_PAGE):
            self.assertTrue(url.startswith("https://"), url)
        self.assertIn("B-ismark/lazeR", rs.RELEASES_API)
        self.assertIn("B-ismark/lazeR", rs.RELEASES_PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
