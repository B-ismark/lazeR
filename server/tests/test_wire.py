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
import sys
import types
import unittest

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
    """Minimal mirror of the Kotlin SecureChannel's send side."""

    def __init__(self, key, sid=b"\x11\x22\x33\x44"):
        self.aes = AESGCM(key)
        self.sid = sid
        self.ctr = 0

    def seal(self, body, ctr=None, sid=None):
        """Build a v2 datagram. [ctr]/[sid] override for replay/forgery cases."""
        sid = sid if sid is not None else self.sid
        if ctr is None:
            self.ctr += 1
            ctr = self.ctr
        nonce = sid + ctr.to_bytes(8, "big")
        hdr = rs.MAGIC_V2 + nonce
        return hdr + self.aes.encrypt(nonce, body.encode("utf-8"), hdr), ctr


def unseal(key, data):
    """Decrypt a server reply the way the phone does. Returns the plaintext."""
    hdr, body = data[:14], data[14:]
    return AESGCM(key).decrypt(hdr[2:14], body, hdr).decode("utf-8")


def handshake(wire, sock, addr=CLIENT, client=None, now=100.0):
    """Drive a full HELLO→CHAL→AUTH→OK and return the pinned FakeClient."""
    fc = FakeClient(KEY)
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


# ── the cross-implementation anchor ──────────────────────────────────────────
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
        wire.cli_sid, wire.cli_ctr, wire.secure_client = bytes([1, 2, 3, 4]), 0, True
        self.assertEqual(wire.parse(self.pkt, CLIENT, CLIENT),
                         ("MOVE", "3 -4", True))


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
        pkt, _ = self.fc.seal("CLICK", sid=b"\xaa\xbb\xcc\xdd")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
