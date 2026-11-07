#!/usr/bin/env python3
"""
LazeR — LAN remote-control server.

Opens a polished GUI window (tkinter + Pillow) showing the QR code, connection
info, live status, and an activity feed.  Falls back to terminal mode if
tkinter is unavailable or you pass --no-gui.

Run:
    python remote_server.py           # GUI mode (default)
    python remote_server.py --no-gui  # terminal/headless mode
"""

import argparse
import base64
import ipaddress
import os
import queue
import re
import secrets
import socket
import string
import sys
import threading
import time

from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController

# ── constants ─────────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 50505
# Keep in step with android/app/build.gradle.kts versionName — publish_release.ps1
# asserts the git tag matches THAT, and the update check below compares against
# whatever tag the newest GitHub release carries, so a stale value here would
# either nag forever or never nag at all.
APP_VERSION = "2.5.0"
RELEASES_API = "https://api.github.com/repos/B-ismark/lazeR/releases/latest"
RELEASES_PAGE = "https://github.com/B-ismark/lazeR/releases/latest"
UPDATE_TIMEOUT_S = 6      # a slow/blocked network must never delay the server
SINGLETON_PORT = 50506  # loopback-only: a 2nd launch uses it to surface the running window
TOKEN_LEN = 6
RESUME_GAP_S = 8        # recv-loop tick gap larger than this ⇒ the laptop slept; recover net
CLIENT_IDLE_S = 12      # no packet from the pinned phone this long ⇒ it left (phone polls 1.5–4s when idle)
# How long a phone that was pinned before we slept has to speak up after the wake
# before we report it gone. Far shorter than CLIENT_IDLE_S — which is what the wake
# used to grant, leaving the window claiming a phone was connected for 12s after
# every resume — but it has to clear the phone's worst-case idle gap or we would
# drop a phone that genuinely survived the sleep. That gap is not the 4s poll
# interval alone: an idle tick is queryVolume(400ms) + ping(500ms) + delay(4000ms),
# so ~4.9s before jitter or Android doze stretches it further.
POST_WAKE_GRACE_S = 8
# How often to re-check our own LAN address. Sleep/resume is NOT the only way it
# moves — roaming to another SSID, a DHCP lease change, or docking all change it
# with no tick gap at all, and the resume path was the only thing that ever
# re-read it. Without this the server keeps advertising an address nobody is at
# (mDNS record + the on-screen QR) until the app is restarted, which is exactly
# what "I had to close it and start again" looked like.
NET_WATCH_S = 5
# How often the Windows volume backend checks that it is still on the default
# output device (see _follow_default in make_volume).
AUDIO_DEVICE_CHECK_S = 1.0
# After a wake, the NIC is usually still coming up: lan_ip() called at that instant
# returns the loopback fallback, and re-announcing THAT poisons mDNS with 127.0.0.1
# for the rest of the process. Wait (bounded) for a real address before announcing.
NET_SETTLE_S = 45
# How long the QR stays fully rendered while UNPAIRED and idle before it is
# swapped for a click-to-show placeholder. The QR encodes the token AND the
# 256-bit key — whoever can photograph the screen owns the machine until
# Regenerate — so an unattended laptop must not advertise it indefinitely.
# Pairing flow is unchanged: one click restores it, and any repaint (new IP,
# regenerate, returning to the QR view) shows it again and restarts the clock.
QR_HIDE_AFTER_MS = 5 * 60 * 1000
SERVICE_TYPE = "_lazer._udp.local."
# Resource vs. writable paths differ when frozen into a PyInstaller onefile exe:
# bundled data lives in the temp _MEIPASS extraction dir (read-only), while the
# token must persist across launches.
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR = _BUNDLE_DIR


def _user_data_dir():
    """Per-user folder for the pairing secrets. Windows uses Local, not Roaming
    AppData: a roamed key would give every laptop on the account one identity."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "LazeR")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/LazeR")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "lazer")


# The secrets used to sit beside the binary (_APP_DIR), which littered Downloads
# with two dotfiles and gave the exe and a source run different identities. They
# now live in the user's data folder; migrate_legacy_secrets() moves an old pair.
_DATA_DIR = _user_data_dir()
TOKEN_FILE = os.path.join(_DATA_DIR, ".lazer_token")
KEY_FILE = os.path.join(_DATA_DIR, ".lazer_key")
ICON_FILE = os.path.join(_BUNDLE_DIR, "LazeR.ico")

# HELLO freshness: a secure HELLO is answered with a one-time random challenge the
# client must echo (encrypted) in an AUTH before it is pinned as controller. This
# stops a captured session (a HELLO + its control packets) being replayed later by
# anyone who lacks the key — an on-path observer on the LAN can capture ciphertext
# without being able to forge it, so possession of the key must be proven FRESH, not
# merely proven. The challenge is single-use and expires quickly.
CHAL_TTL_S = 6.0
CHAL_MAX = 256              # bound the pending-challenge table (anti-flood)
# ...and a per-source-IP quota. The global bound alone is not enough: a replayed
# HELLO needs no key, so anyone who captured one can mint challenges from hundreds
# of spoofed source PORTS, and any eviction policy that treats all entries alike
# then pushes out the real phone's outstanding nonce — stalling its handshake. A
# per-IP cap makes a flood evict only its own entries. One genuine phone needs a
# handful at most (handshake retries from one or two source ports).
CHAL_PER_IP = 8
# Client nonces remembered per challenge: the one that opened it, plus the latest
# others. A HELLO is replayable, so keeping only the latest let a replay slipped in
# between the phone's HELLO and its AUTH swap in an old nonce, and the OK then
# echoed that one, which the phone refuses.
CNONCE_KEEP = 4

# Verbs that move or press the pointer. Listed separately because Windows needs a
# nudge to make the pointer visible before one lands (see make_pointer_waker), and
# defined FIRST so CONTROL_VERBS can be built from it — two hand-maintained copies
# of the same eight strings would drift the moment a gesture verb was added to one
# and not the other, silently bringing the invisible-pointer bug back for it.
POINTER_VERBS = frozenset({
    "MOVE", "SCROLL", "ZOOM", "CLICK", "RCLICK", "MCLICK", "MDOWN", "MUP",
})

# Verbs that actually drive the machine. While the user has taken over locally
# (or after a panic), these are dropped; PING/VGET/HELLO/BYE still flow.
CONTROL_VERBS = POINTER_VERBS | {
    "COMBO", "ASW", "SYS", "VOL", "MEDIA", "KEY", "KEYSP",
    "BRIGHT",
}

mouse = MouseController()
keyboard = KeyboardController()
_stop = threading.Event()   # set by GUI close or KeyboardInterrupt

# Local-input takeover: when the user touches the laptop's own mouse/keyboard,
# remote control is paused so the physical device always wins. A panic hotkey
# latches the pause until the user explicitly resumes.
_remote_paused = threading.Event()   # set ⇒ ignore remote control verbs
_panic_latched = threading.Event()   # set ⇒ stay paused until user resumes
class _ConnectedEvent(threading.Event):
    """threading.Event that also tells listeners when it is set, so the input
    guard can put its mouse hook in place the moment a phone pairs rather than on
    its next timer tick. set() runs on the UDP thread, which must never raise
    (see CLAUDE.md), so a failing listener is ignored."""

    def __init__(self):
        super().__init__()
        self.listeners = []

    def set(self):
        super().set()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass


_client_connected = _ConnectedEvent()  # a phone is currently paired
_last_physical_ts = [0.0]            # monotonic time of last physical input
_input_guard = [None]                # the live LocalInputGuard, for serve_loop's wake handling
PHYSICAL_RESUME_GRACE_S = 2.0        # auto-resume this long after local input stops
RATE_WINDOW_S = 10                   # rejected-packet rate window
RATE_MAX_BAD = 80                    # >this many rejected packets/window ⇒ warn (brute/flood)
# When that warning fires, plaintext (manual-code) ACCEPTANCE is paused for this
# many seconds: Wire.parse drops v1 packets before any parsing or comparison.
# Token brute force only exists on the plaintext wire — the secure wire authenticates by
# GCM tag and never sees a token — so pausing exactly that path makes guessing
# futile while QR-paired phones are untouched. Deliberately GLOBAL, not per-source
# blocking: UDP source addresses are spoofable, so per-IP blocking would let a
# flooder frame the real phone's address and lock the genuine user out.
PLAINTEXT_SUSPEND_S = 60


def win_exe(*parts):
    """Absolute path to a Windows system program.

    Bare names ("netsh", "cmd.exe") are resolved by CreateProcess, which looks in
    the application's own folder and the current directory BEFORE System32 — so a
    netsh.exe or cmd.exe dropped next to LazeR.exe (Downloads, a shared folder)
    would be run instead, and for the firewall fix, run elevated."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", *parts)


POWERSHELL = ("WindowsPowerShell", "v1.0", "powershell.exe")


def resume_remote():
    """User chose to resume — clear both the soft pause and the panic latch."""
    _panic_latched.clear()
    _remote_paused.clear()


# ── volume ────────────────────────────────────────────────────────────────────
def _com_init():
    """COM is per-thread, and comtypes only initializes the thread that imports
    it — the main one. Audio reads run on the UDP receive thread, where COM calls
    otherwise fail every time with "CoInitialize has not been called" (measured).
    Idempotent, so it's free after the first call on a thread."""
    import comtypes
    try:
        comtypes.CoInitialize()
    except OSError:
        # RPC_E_CHANGED_MODE: this thread already has an apartment of the other
        # kind. That is fine — it has one, which is all we need.
        pass


def make_volume():
    """Return (get_volume, set_volume, label); fns may be None if unavailable.
    A getter that can also tell whether output is muted carries that as its
    `muted` attribute (returns True/False, or None when it can't tell)."""
    plat = sys.platform

    if plat.startswith("win"):
        # The audio endpoint is looked up ON DEMAND and re-acquired when it breaks,
        # instead of being captured once here and used forever.
        #
        # An IAudioEndpointVolume is bound to the default output device as it was at
        # that moment. Windows invalidates it whenever that device changes underneath
        # us — resuming from sleep, plugging in headphones, a Bluetooth speaker
        # connecting — and from then on EVERY call raises COMError
        # (AUDCLNT_E_DEVICE_INVALIDATED / RPC_E_DISCONNECTED). The old getter had no
        # error handling at all (unlike the macOS and Linux ones below, which both
        # swallow failures), and serve_loop answers the phone's VGET by calling it
        # directly — above the try/except that guards verb handlers. So the first
        # volume poll after a sleep raised straight out of the receive loop and killed
        # the only thread serving the phone. The window still said "server running",
        # the status dot stayed green, and nothing short of restarting the app brought
        # it back. VGET is the phone's own liveness probe, so this fired every time.
        try:
            from pycaw.pycaw import AudioUtilities, EDataFlow, ERole, IAudioEndpointVolume
        except Exception as e:
            print(f"[volume] pycaw unavailable ({e}); pip install pycaw")
            return None, None, None

        _ep = [None]
        _ep_id = [None]         # the device _ep belongs to
        _checked = [0.0]        # when the default device was last compared to it

        # Re-acquisition and the default-device check run on the UDP receive
        # thread; without this both would be silent no-ops (see _com_init).
        _com = _com_init

        def _device_id(dev):
            i = getattr(dev, "id", None)        # pycaw's AudioDevice
            return i if i is not None else dev.GetId()   # a bare IMMDevice

        def _endpoint():
            if _ep[0] is None:
                _com()
                devices = AudioUtilities.GetSpeakers()
                vol = getattr(devices, "EndpointVolume", None)
                if vol is None:
                    from ctypes import cast, POINTER
                    from comtypes import CLSCTX_ALL
                    iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    vol = cast(iface, POINTER(IAudioEndpointVolume))
                _ep[0] = vol
                try:
                    _ep_id[0] = _device_id(devices)
                except Exception:
                    _ep_id[0] = None
                _checked[0] = time.monotonic()
            return _ep[0]

        def _follow_default():
            """Drop the endpoint if Windows' default output has moved to another
            device. Switching to headphones, a monitor or a Bluetooth speaker
            doesn't invalidate the old device's endpoint — it keeps answering for
            the device it was bound to — so the stale-endpoint retry never fires,
            and the phone went on reading and setting the old device's volume
            (measured: phone at the monitor's 74 %, headphones at 40 %). Checked at
            most once a second: this lookup costs ~2.5 ms, GetSpeakers() ~65."""
            now = time.monotonic()
            if _ep[0] is None or now - _checked[0] < AUDIO_DEVICE_CHECK_S:
                return
            _checked[0] = now
            try:
                _com()
                en = AudioUtilities.GetDeviceEnumerator()
                cur = en.GetDefaultAudioEndpoint(EDataFlow.eRender.value,
                                                 ERole.eMultimedia.value).GetId()
            except Exception:
                return      # can't tell: keep the endpoint; its own errors still recover
            if cur != _ep_id[0]:
                _ep[0] = None

        def _on_endpoint(call):
            """Run [call] against the endpoint, re-acquiring once if it has gone
            stale or the default output device has changed. Returns None if even a
            fresh endpoint fails (no output device at all), so callers can degrade
            instead of raising."""
            _follow_default()
            for _ in (0, 1):            # try, then retry once on a fresh endpoint
                try:
                    return call(_endpoint())
                except Exception:
                    _ep[0] = None       # drop the dead pointer; the retry re-acquires
            return None

        def get_win():
            v = _on_endpoint(lambda e: e.GetMasterVolumeLevelScalar())
            return None if v is None else int(round(v * 100))

        def set_win(pct):
            def apply(e):
                e.SetMasterVolumeLevelScalar(pct / 100.0, None)
                # Setting the level leaves mute as it was (measured), so a muted
                # laptop stayed silent while the phone's slider moved. Windows' own
                # slider unmutes when dragged; this does the same.
                if pct > 0 and e.GetMute():
                    e.SetMute(0, None)
            _on_endpoint(apply)

        def muted_win():
            m = _on_endpoint(lambda e: e.GetMute())
            return None if m is None else bool(m)

        get_win.muted = muted_win

        try:
            _endpoint()          # fail fast at startup, exactly as before
        except Exception as e:
            # Deliberately not "pycaw unavailable": _endpoint() also imports
            # comtypes and then talks to the audio device, so blaming pycaw for
            # any of the three sent a missing comtypes chasing the wrong package.
            print(f"[volume] Windows volume unavailable ({e}); "
                  "pip install pycaw comtypes")
            return None, None, None

        return get_win, set_win, "pycaw"

    if plat == "darwin":
        import subprocess

        def get_mac():
            try:
                out = subprocess.check_output(
                    ["osascript", "-e", "output volume of (get volume settings)"])
                return int(out.decode().strip())
            except Exception:
                return 0

        def set_mac(pct):
            subprocess.run(["osascript", "-e", f"set volume output volume {int(pct)}"],
                           check=False)
        return get_mac, set_mac, "osascript"

    import re, shutil, subprocess

    if shutil.which("amixer"):
        def get_amixer():
            try:
                out = subprocess.check_output(["amixer", "get", "Master"]).decode()
                m = re.search(r"\[(\d{1,3})%\]", out)
                return int(m.group(1)) if m else 0
            except Exception:
                return 0

        def set_amixer(pct):
            subprocess.run(["amixer", "-q", "sset", "Master", f"{int(pct)}%"],
                           check=False)
        return get_amixer, set_amixer, "amixer"

    if shutil.which("pactl"):
        def get_pactl():
            try:
                out = subprocess.check_output(
                    ["pactl", "get-sink-volume", "@DEFAULT_SINK@"]).decode()
                m = re.search(r"(\d{1,3})%", out)
                return int(m.group(1)) if m else 0
            except Exception:
                return 0

        def set_pactl(pct):
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                            f"{int(pct)}%"], check=False)
        return get_pactl, set_pactl, "pactl"

    print("[volume] no supported backend found")
    return None, None, None


get_volume, set_volume, VOLUME_BACKEND = make_volume()


# ── microphone mute ───────────────────────────────────────────────────────────
def make_mic():
    """Return (get_mic_muted, toggle_mic_mute); both None when unsupported.

    Laptop-wide: it mutes the input device itself, so every app hears silence and
    the state read back is the truth (unlike a meeting app's own shortcut, which is
    a blind toggle). get returns True/False, or None when there is no mic to ask."""
    plat = sys.platform

    if plat.startswith("win"):
        try:
            from pycaw.pycaw import AudioUtilities, EDataFlow, ERole, IAudioEndpointVolume
        except Exception:
            return None, None

        # Windows keeps two default inputs: one for "everything" (console) and one
        # for calls (communications). Teams/Zoom/Meet often follow the second, and
        # they can be different mics (a headset vs. the built-in array). Mute both,
        # or the button would show "muted" while the call still hears you.
        roles = (ERole.eConsole.value, ERole.eCommunications.value)
        _eps = [None]           # [(device id, IAudioEndpointVolume)], None = re-acquire
        _checked = [0.0]

        def _default_ids():
            """The distinct default input device ids, console first. Cheap
            (~2.5 ms): no endpoint is activated."""
            _com_init()
            en = AudioUtilities.GetDeviceEnumerator()
            ids = []
            for role in roles:
                try:
                    did = en.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, role).GetId()
                except Exception:
                    continue                # no default input for this role
                if did not in ids:
                    ids.append(did)
            return ids

        def _acquire(ids):
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            en = AudioUtilities.GetDeviceEnumerator()
            eps = []
            for did in ids:
                iface = en.GetDevice(did).Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                eps.append((did, cast(iface, POINTER(IAudioEndpointVolume))))
            return eps

        def _endpoints():
            # Same reasoning as make_volume: a cached endpoint keeps answering for
            # the device it was bound to after the default moves (a headset plugged
            # in), and goes stale outright across a sleep. So compare the defaults
            # at most once a second, and re-activate only when they have changed;
            # a stale endpoint on an unchanged device fails its call instead, and
            # _run re-acquires.
            now = time.monotonic()
            if _eps[0] is None or now - _checked[0] >= AUDIO_DEVICE_CHECK_S:
                _checked[0] = now
                ids = _default_ids()
                if _eps[0] is None or ids != [d for d, _ in _eps[0]]:
                    _eps[0] = _acquire(ids)     # errors reach _run, which retries once
            return _eps[0]

        def _run(call):
            for _ in (0, 1):            # try, then once more on fresh endpoints
                try:
                    eps = _endpoints()
                    return call([e for _, e in eps]) if eps else None
                except Exception:
                    _eps[0] = None
            return None

        def get_win():
            # Muted only when every default input is: one live mic is a live mic.
            return _run(lambda eps: all(bool(e.GetMute()) for e in eps))

        def toggle_win():
            def flip(eps):
                target = 0 if all(bool(e.GetMute()) for e in eps) else 1
                for e in eps:
                    e.SetMute(target, None)
                return True
            _run(flip)

        try:
            if not _endpoints():
                print("[mic] no input device; mic mute hidden on the phone")
        except Exception as e:
            print(f"[mic] Windows mic mute unavailable ({e})")
            return None, None
        return get_win, toggle_win

    import shutil, subprocess

    if plat != "darwin" and shutil.which("pactl"):
        def get_pactl():
            try:
                # This runs on the UDP thread for every VGET: a hung PulseAudio
                # must cost the mic button, never the phone's connection.
                out = subprocess.check_output(
                    ["pactl", "get-source-mute", "@DEFAULT_SOURCE@"],
                    stderr=subprocess.DEVNULL, timeout=1.0).decode().lower()
            except Exception:
                return None
            return True if "yes" in out else False if "no" in out else None

        def toggle_pactl():
            try:
                subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "toggle"],
                               check=False, timeout=2.0)
            except Exception:
                pass
        return get_pactl, toggle_pactl

    # macOS has no input-mute switch to read back (only an input level), so the
    # phone simply doesn't show the button there.
    return None, None


get_mic_muted, toggle_mic_mute = make_mic()


# ── phone as microphone (via VB-CABLE) ────────────────────────────────────────
# Windows only lists microphones that a signed driver provides, and signing our
# own costs money every year. So we borrow one: VB-CABLE (free for end users,
# https://vb-audio.com/Cable/) is a signed virtual cable whose playback side,
# "CABLE Input", comes out of its recording side, "CABLE Output", which every app
# lists as a microphone. We play the phone's voice into the first; Zoom / Teams /
# Meet pick the second. The user installs VB-CABLE themselves — its licence does
# not let us bundle it — and LazeR never downloads anything for them.
#
# Wire: the phone sends "AUD <base64>" — 20 ms of 16 kHz, 16-bit mono PCM — only on
# the encrypted wire (a voice never crosses the LAN in the clear). The VGET reply
# carries the readiness as its fourth word (see volume_reply).
PHONE_MIC_RATE = 16000
PHONE_MIC_FRAME = 320                    # samples per 20 ms frame
PHONE_MIC_MAX_BUF = 16000 * 2 // 5       # bytes: 200 ms; older audio is dropped
PHONE_MIC_PREBUF = 16000 * 2 * 3 // 50   # bytes: 60 ms cushion against Wi-Fi jitter
PHONE_MIC_TARGET = 16000 * 2 // 10       # bytes: 100 ms; held above this → trim a frame
PHONE_MIC_IDLE_S = 1.5                   # no frames this long → release the device
PHONE_MIC_RESCAN_S = 5.0                 # rate limit for looking for the cable again
PHONE_MIC_MAX_B64 = 4096                 # a sane AUD is ~856 chars; refuse the absurd
VB_CABLE_PAGE = "https://vb-audio.com/Cable/"


def pick_cable_device(devices, hostapis):
    """Index of VB-CABLE's playback side ("CABLE Input"), or None. Prefers the
    WASAPI copy, which can convert our 16 kHz to whatever rate the cable runs at
    (MME lists the same device too, under a name cut to 31 characters)."""
    best = None
    for i, d in enumerate(devices):
        if d.get("max_output_channels", 0) < 1:
            continue
        if not str(d.get("name", "")).startswith("CABLE Input"):
            continue
        try:
            api = hostapis[d["hostapi"]]["name"]
        except Exception:
            api = ""
        if "WASAPI" in api:
            return i
        if best is None:
            best = i
    return best


class PhoneMicSink:
    """Plays the phone's voice into VB-CABLE.

    Threads: the UDP thread only appends to a small buffer (feed) and pokes an
    event — it never waits on audio. PortAudio's own thread pulls 20 ms at a time
    from that buffer (_callback). Everything else — loading PortAudio, looking
    for the cable, opening and closing the stream — happens on ONE long-lived
    audio thread (_run), because PortAudio's WASAPI layer (COM) is per-thread and
    wants its start-up and shut-down on the same one.

    Costs nothing while no phone streams: the audio thread sleeps on the event
    with no timeout, no device is open and nothing is scanned."""

    def __init__(self, backend=None, loader=None):
        self._sd = backend
        self._loader = loader
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._buf = bytearray()
        self._primed = False
        self._over_since = None     # when the cushion went above target
        self._stream = None         # set and cleared by the audio thread only
        self._opened = 0.0
        self._last = 0.0            # monotonic time of the phone's last frame
        self._cb_last = 0.0         # ...and of PortAudio's last callback
        self._device = None
        self._wasapi = False
        self._scanned = 0.0
        self._needs_scan = False    # the device index may be stale: look again
        self._rescan = False        # the GUI asked to look again
        self._fails = 0
        self._retry_at = 0.0        # no new open attempt before this
        self.error = None           # why VB-CABLE wouldn't open, for the GUI
        self.supported = backend is not None or loader is not None
        if backend is not None:
            self._scan(refresh=False)
        if self.supported:
            threading.Thread(target=self._run, daemon=True,
                             name="LazeR-phone-mic").start()

    @classmethod
    def create(cls):
        if not sys.platform.startswith("win"):
            return cls(None)

        def load():
            import sounddevice
            return sounddevice
        return cls(loader=load)

    # ── state ──
    def status(self):
        """unsupported | checking (still starting) | missing (no VB-CABLE) |
        error (installed but won't open; see .error) | ready | live."""
        if not self.supported:
            return "unsupported"
        if self._sd is None:
            return "checking"
        if self._stream is not None and time.monotonic() - self._last < PHONE_MIC_IDLE_S:
            return "live"
        if self.error is not None:
            return "error"
        if self._device is None:
            return "missing"
        return "ready"

    def phone_word(self):
        """The fourth word of the VGET reply: None (no such feature), "0" (not
        usable now: VB-CABLE missing or failing) or "1" (ready)."""
        st = self.status()
        if st in ("unsupported", "checking"):
            return None
        return "0" if st in ("missing", "error") else "1"

    def rescan_soon(self):
        """Look for the cable again and forget the last failure (the GUI's "try
        again"). Done on the audio thread, once no stream is open."""
        if not self.supported or self._sd is None:
            return
        self._rescan = True
        self._wake.set()

    # ── the UDP thread's side ──
    def feed(self, pcm):
        """One frame from the phone. Append and return; never waits."""
        if not self.supported or self._sd is None or not pcm:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        now = time.monotonic()
        with self._lock:
            if now - self._last >= PHONE_MIC_IDLE_S:
                self._buf.clear()           # a new burst: nothing old may play
            self._last = now
            self._buf += pcm
            # No stream yet (opening, or failing): keep only a cushion's worth,
            # so the voice starts current instead of 200 ms late.
            cap = PHONE_MIC_MAX_BUF if self._stream is not None else PHONE_MIC_PREBUF
            over = len(self._buf) - cap
            if over > 0:
                del self._buf[:over + (over & 1)]   # keep whole 16-bit samples
            poke = self._stream is None
        if poke:
            self._wake.set()

    # ── PortAudio's side ──
    def _callback(self, outdata, frames, time_info, status):
        n = frames * 2
        now = time.monotonic()
        with self._lock:
            self._cb_last = now
            if not self._primed and len(self._buf) >= PHONE_MIC_PREBUF:
                self._primed = True
                self._over_since = None
                excess = len(self._buf) - PHONE_MIC_PREBUF
                if excess > 0:                      # start from the cushion, not behind it
                    del self._buf[:excess + (excess & 1)]
            chunk = b""
            if self._primed:
                # Hold the delay near the target: a Wi-Fi burst, or the phone's
                # clock running a hair fast, would otherwise park us at the cap.
                # One dropped 20 ms frame a second is inaudible in speech.
                if len(self._buf) > PHONE_MIC_TARGET:
                    if self._over_since is None:
                        self._over_since = now
                    elif now - self._over_since >= 1.0:
                        del self._buf[:PHONE_MIC_FRAME * 2]
                        self._over_since = None
                else:
                    self._over_since = None
                chunk = bytes(self._buf[:n])
                del self._buf[:n]
                if len(chunk) < n:
                    self._primed = False    # ran dry: rebuild the cushion first
        outdata[:] = chunk + b"\x00" * (n - len(chunk))

    # ── the audio thread ──
    def _run(self):
        if self._sd is None:
            # Importing sounddevice starts PortAudio, which walks every audio
            # device on the machine — up to a second or two with some drivers.
            # Here, so LazeR's window never waits for it.
            try:
                sd = self._loader()
            except Exception as e:
                print(f"[phone mic] unavailable ({type(e).__name__}); pip install sounddevice")
                self.supported = False
                return
            self._scan(refresh=False, sd=sd)
            self._sd = sd
        while True:
            try:
                self._step()
            except Exception as e:      # never let the audio thread die
                print(f"[phone mic] {type(e).__name__}: {e}")
                self._close()
                time.sleep(1.0)

    def _step(self):
        self._wake.clear()              # before reading state: a later poke still wakes us
        now = time.monotonic()
        streaming = now - self._last < PHONE_MIC_IDLE_S
        if self._rescan and self._stream is None:
            self._rescan = False
            self._fails, self._retry_at, self.error = 0, 0.0, None
            self._scan(refresh=True)
        if self._stream is not None:
            if not streaming:
                self._close()           # the phone stopped: release the device
                self._fails = 0
            elif not self._stream_alive(now):
                # The device went away under us (cable disabled or removed, its
                # format changed, the audio service restarted across a sleep).
                # PortAudio stops calling back and says nothing, so we notice the
                # silence and reopen, looking for the cable afresh.
                print("[phone mic] VB-CABLE stopped playing; reopening")
                self._close()
                self._needs_scan = True
                self._backoff(now)
        elif streaming and now >= self._retry_at:
            if self._needs_scan or (self._device is None
                                    and now - self._scanned >= PHONE_MIC_RESCAN_S):
                self._scan(refresh=True)
            if self._device is not None:
                self._open(now)
        if self._stream is not None or streaming:
            self._wake.wait(0.25)
        else:
            self._wake.wait()           # idle: nothing until a frame or a click

    def _stream_alive(self, now):
        st = self._stream
        if not getattr(st, "active", True):
            return False
        # A fresh stream gets a moment to make its first callback.
        return now - max(self._cb_last, self._opened) < 1.0

    def _backoff(self, now):
        self._fails += 1
        self._retry_at = now + min(30.0, 0.5 * 2 ** self._fails)

    def _scan(self, refresh, sd=None):
        sd = sd or self._sd
        try:
            if refresh:
                # PortAudio lists devices once, when it starts; a cable installed
                # (or re-enabled) since is only visible after a restart of it.
                # Only ever done here, with no stream open.
                sd._terminate()
                sd._initialize()
            devs = list(sd.query_devices())
            apis = list(sd.query_hostapis())
            idx = pick_cable_device(devs, apis)
            self._wasapi = idx is not None and "WASAPI" in apis[devs[idx]["hostapi"]]["name"]
        except Exception:
            idx = None
        self._device = idx
        if idx is None:
            self.error = None           # not there at all: that's "missing", not "error"
        self._needs_scan = False
        self._scanned = time.monotonic()

    def _open(self, now):
        sd = self._sd
        extra = sd.WasapiSettings(auto_convert=True) if self._wasapi else None
        try:
            st = sd.RawOutputStream(
                samplerate=PHONE_MIC_RATE, channels=1, dtype="int16",
                device=self._device, blocksize=PHONE_MIC_FRAME, latency="low",
                callback=self._callback, extra_settings=extra)
            st.start()
        except Exception as e:
            # Installed but won't open: held in exclusive mode by another app,
            # disabled in Sound settings... Say why (the GUI shows it) and back
            # off, rather than tell the user to install what they already have.
            self.error = f"{type(e).__name__}: {e}"[:160]
            print(f"[phone mic] couldn't open VB-CABLE ({self.error})")
            self._needs_scan = True
            self._backoff(now)
            return
        with self._lock:
            self._primed = False
            self._over_since = None
            self._stream = st
        self._opened = now
        self.error = None

    def _close(self):
        with self._lock:
            st, self._stream = self._stream, None
            self._buf.clear()
            self._primed = False
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass


phone_mic = PhoneMicSink.create()


def decode_audio(rest):
    """PCM bytes from an AUD payload, or None if it isn't one."""
    rest = rest.strip()
    if not rest or len(rest) > PHONE_MIC_MAX_B64:
        return None
    try:
        return base64.b64decode(rest, validate=True)
    except Exception:
        return None


# ── brightness ──────────────────────────────────────────────────────────────
# ── in-process WMI (no PowerShell) ─────────────────────────────────────────────
# Brightness and the chassis type used to be read by starting `powershell.exe` —
# ~0.5–1 s of CPU and a PowerShell + conhost pair in Task Manager on every poll,
# plus one warm PowerShell kept alive for writes. WMI is a COM API, and comtypes
# (already here for pycaw) talks to it directly from our own process in a few
# milliseconds. PowerShell stays only as the fallback for a machine where the COM
# route doesn't work at all.
#
# COM is per-thread (see make_volume), so each thread gets its own initialized
# connection. Every property and method goes through the SWbemScripting type
# library (Properties_ / Methods_ / ExecMethod_) rather than through WMI's dynamic
# members: comtypes binds names against the type library, where a WMI class's own
# properties don't exist.
_wmi_local = threading.local()


def _wmi_service(namespace):
    cache = getattr(_wmi_local, "svc", None)
    if cache is None:
        cache = _wmi_local.svc = {}
    svc = cache.get(namespace)
    if svc is None:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        loc = comtypes.client.CreateObject("WbemScripting.SWbemLocator", dynamic=True)
        svc = loc.ConnectServer(".", namespace)
        cache[namespace] = svc
        made = getattr(_wmi_local, "made", None)
        if made is None:
            made = _wmi_local.made = {}
        made[namespace] = time.monotonic()
    return svc


_WMI_RECONNECT_S = 60.0


def _wmi_forget(namespace):
    """Drop this thread's connection, so the next call reconnects. A connection
    made before a sleep can go stale, like the audio endpoint.

    At most once a minute per namespace: on a desktop the brightness query fails
    on every poll (there is no panel), and a fresh locator + ConnectServer each
    time would be pure waste for an answer that won't change."""
    cache = getattr(_wmi_local, "svc", None)
    if cache is None or namespace not in cache:
        return
    stamps = getattr(_wmi_local, "made", None)
    if stamps is None:
        stamps = _wmi_local.made = {}
    if time.monotonic() - stamps.get(namespace, 0.0) >= _WMI_RECONNECT_S:
        cache.pop(namespace, None)


def _wmi_first(namespace, wql):
    """First object a WQL query returns, or None when it returns nothing.
    Raises on a COM/WMI failure."""
    for obj in _wmi_service(namespace).ExecQuery(wql):
        return obj
    return None


def _wmi_prop(obj, name):
    return obj.Properties_.Item(name).Value


def _wmi_com_works():
    """True when in-process WMI answers on this machine (Win32_OperatingSystem
    always exists), so a later brightness failure means "no panel", not "broken
    plumbing"."""
    try:
        return _wmi_first(r"root\cimv2",
                          "SELECT Caption FROM Win32_OperatingSystem") is not None
    except Exception:
        return False


def _wmi_brightness_get():
    """0..100 from the integrated panel, or None when there is none right now
    (desktop, lid closed, docked to an external screen only)."""
    try:
        obj = _wmi_first(r"root\WMI",
                         "SELECT CurrentBrightness FROM WmiMonitorBrightness")
        return None if obj is None else int(_wmi_prop(obj, "CurrentBrightness"))
    except Exception:
        _wmi_forget(r"root\WMI")
        return None


def _wmi_brightness_set(pct):
    """Apply [pct]. True when applied, None when there is no panel to apply it to,
    False on a COM/WMI failure (the caller may try another route)."""
    try:
        obj = _wmi_first(r"root\WMI", "SELECT * FROM WmiMonitorBrightnessMethods")
        if obj is None:
            return None
        args = obj.Methods_.Item("WmiSetBrightness").InParameters.SpawnInstance_()
        args.Properties_.Item("Timeout").Value = 1
        args.Properties_.Item("Brightness").Value = max(0, min(100, int(pct)))
        obj.ExecMethod_("WmiSetBrightness", args)
        return True
    except Exception:
        _wmi_forget(r"root\WMI")
        return False


_LAPTOP_CHASSIS = {8, 9, 10, 11, 12, 14, 30, 31, 32}


def _wmi_is_laptop():
    """Chassis says portable → True; says desktop → False; can't tell → None."""
    try:
        obj = _wmi_first(r"root\cimv2",
                         "SELECT ChassisTypes FROM Win32_SystemEnclosure")
        if obj is None:
            return None
        types = {int(t) for t in (_wmi_prop(obj, "ChassisTypes") or ())}
        return bool(types & _LAPTOP_CHASSIS)
    except Exception:
        return None


def _has_system_battery():
    """GetSystemPowerStatus: BatteryFlag 128 = no system battery, 255 = unknown."""
    try:
        import ctypes

        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]
        st = SPS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return False
        return st.BatteryFlag not in (128, 255)
    except Exception:
        return False


def make_brightness():
    """Return (get_brightness, set_brightness, label); fns may be None if absent.
    Same shape as make_volume — best effort per OS, integrated display only."""
    plat = sys.platform

    if plat.startswith("win"):
        # WMI's WmiMonitorBrightness(Methods) drive the integrated panel. Reached
        # via PowerShell so we need no extra dependency. CREATE_NO_WINDOW keeps the
        # console from flashing each call.
        import subprocess
        _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def _ps(cmd):
            # stdin=DEVNULL is essential under the --windowed PyInstaller exe: a
            # windowed process has no valid stdin handle, and without redirecting it
            # PowerShell fails to start — which silently made the brightness probe
            # (and reads) fail in the packaged build while working from source.
            return subprocess.run([win_exe(*POWERSHELL), "-NoProfile", "-Command", cmd],
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=4, creationflags=_flags)

        def get_win():
            # None means "no integrated panel right now" (empty stdout → IndexError,
            # or PowerShell/WMI error) — distinct from a real 0%. The service uses
            # that to flip availability, so a laptop booted docked shows the slider
            # once the panel reappears instead of hiding it for the whole session.
            try:
                out = _ps("(Get-CimInstance -Namespace root/WMI "
                          "-ClassName WmiMonitorBrightness).CurrentBrightness")
                return int(out.stdout.strip().splitlines()[0])
            except Exception:
                return None

        # A fresh `powershell -Command "..."` per write costs ~600ms of process
        # startup, which made the brightness slider feel laggy. Keep ONE warm
        # PowerShell reading commands from stdin and pipe each set into it — the
        # WmiSetBrightness then runs in tens of ms. Writes are fire-and-forget so
        # the caller never blocks. Must PIPE the instance into Invoke-CimMethod — a
        # CIM instance doesn't expose .WmiSetBrightness() as a callable.
        _setproc = {"p": None}
        _setlock = threading.Lock()

        def _ensure_setproc():
            p = _setproc["p"]
            if p is None or p.poll() is not None:
                p = subprocess.Popen([win_exe(*POWERSHELL), "-NoProfile", "-Command", "-"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, text=True,
                                     creationflags=_flags)
                _setproc["p"] = p
            return p

        def set_win(pct):
            try:
                with _setlock:
                    p = _ensure_setproc()
                    p.stdin.write(
                        "Get-CimInstance -Namespace root/WMI -ClassName "
                        "WmiMonitorBrightnessMethods | Invoke-CimMethod -MethodName "
                        f"WmiSetBrightness -Arguments @{{Timeout=1; Brightness={int(pct)}}}"
                        " | Out-Null\n")
                    p.stdin.flush()
            except Exception:
                _setproc["p"] = None     # force a respawn next time

        # Is this a machine that HAS an integrated panel at all? Decided from the
        # chassis type (Win32_SystemEnclosure) — a stable, power-INDEPENDENT signal,
        # unlike a brightness read which fails whenever the panel is merely off
        # (locked / asleep). We need this because the availability probe deliberately
        # doesn't gate on WmiMonitorBrightness anymore: without a chassis hint, a
        # laptop that launches the server while locked/screen-off looks identical to a
        # desktop, and the no-panel give-up would wrongly stop probing it. Laptop
        # chassis → panel known present → the service never gives up (keeps probing so
        # unlock reveals the slider). Desktop / unknown → give-up allowed.
        def _is_laptop():
            try:
                out = _ps("(Get-CimInstance -ClassName Win32_SystemEnclosure)"
                          ".ChassisTypes -join ','")
                types = {int(x) for x in out.stdout.strip().split(",") if x.strip().isdigit()}
                # 8 Portable · 9 Laptop · 10 Notebook · 11 Hand Held · 12 Docking
                # Station · 14 Sub Notebook · 30 Tablet · 31 Convertible · 32 Detachable
                return bool(types & {8, 9, 10, 11, 12, 14, 30, 31, 32})
            except Exception:
                return False   # unknown → treat as desktop (give-up allowed)

        # Return the read/write pair unconditionally — availability is decided at
        # RUNTIME by BrightnessService, not by a one-shot probe here. WmiMonitorBrightness
        # can be absent at launch (booted docked / lid-closed / external-only, or a WMI
        # service race at cold boot) yet appear minutes later; a probe-and-latch here
        # would hide the slider for the entire session in those cases (the recurring
        # "brightness slider keeps disappearing" bug). get_win returns None while the
        # class is absent, so the service simply reports unavailable until it shows up.
        if _wmi_com_works():
            # The normal path: no child processes at all. A failed COM write (a
            # driver that rejects WmiSetBrightness through this route) falls back
            # to the warm PowerShell for that write, so the slider keeps working.
            def set_com(pct):
                if _wmi_brightness_set(pct) is False:
                    set_win(pct)

            laptop = _wmi_is_laptop()
            if laptop is None:
                laptop = _has_system_battery()
            return _wmi_brightness_get, set_com, "wmi", laptop
        return get_win, set_win, "wmi", _is_laptop()

    if plat == "darwin":
        import shutil, subprocess
        if shutil.which("brightness"):           # the `brightness` CLI (brew)
            def get_mac():
                try:
                    out = subprocess.check_output(["brightness", "-l"]).decode()
                    import re
                    m = re.search(r"brightness\s+([0-9.]+)", out)
                    return int(round(float(m.group(1)) * 100)) if m else None
                except Exception:
                    return None

            def set_mac(pct):
                subprocess.run(["brightness", str(max(0, min(100, int(pct))) / 100.0)],
                               check=False)
            # `which brightness` matched → treat the panel as known present (no
            # WMI-style flakiness here), so the give-up never fires on macOS.
            return get_mac, set_mac, "brightness", True
        return None, None, None, False

    # Linux: sysfs backlight (read always; write needs a udev rule or root).
    import glob
    bls = glob.glob("/sys/class/backlight/*")
    if bls:
        bl = bls[0]

        def get_linux():
            try:
                with open(f"{bl}/brightness") as f:
                    cur = int(f.read().strip())
                with open(f"{bl}/max_brightness") as f:
                    mx = int(f.read().strip())
                return int(round(cur / mx * 100)) if mx else None
            except Exception:
                return None

        def set_linux(pct):
            try:
                with open(f"{bl}/max_brightness") as f:
                    mx = int(f.read().strip())
                with open(f"{bl}/brightness", "w") as f:
                    f.write(str(int(round(max(0, min(100, pct)) / 100 * mx))))
            except Exception:
                pass
        # /sys/class/backlight/* exists → panel is definitively present; never give up.
        return get_linux, set_linux, "sysfs", True

    return None, None, None, False


get_brightness, set_brightness, BRIGHTNESS_BACKEND, BRIGHTNESS_PANEL_KNOWN = make_brightness()


class BrightnessService:
    """Non-blocking brightness access for the UDP loop.

    Reading brightness (e.g. Windows WMI via PowerShell) costs ~0.5–1s, and the
    phone polls it ~every 1.5s. Doing that read inline would stall the single recv
    loop, delaying MOVE/PING/VGET enough that the phone thinks the link died and
    reconnects — the whole app flickers. So we serve BGET from a cached value kept
    fresh by a background thread, and apply BRIGHT writes on a worker thread.

    `available` is RUNTIME state, not a launch-time latch: the panel can be absent
    at boot (docked / lid-closed / WMI race) and appear later, or vanish on undock.
    It flips true on any successful read and false when reads stop working, so the
    phone's slider follows the actual hardware instead of being decided once."""

    def __init__(self, get_fn, set_fn, panel_known=False):
        self._get = get_fn
        self._set = set_fn
        self._has_backend = get_fn is not None   # platform *might* expose a panel
        # True when we KNOW a panel exists (laptop chassis / sysfs backlight / macOS
        # brightness CLI) independently of whether it's currently powered. Disables
        # the no-panel give-up so a server launched while the laptop is locked keeps
        # probing and reveals the slider on unlock.
        self._panel_known = panel_known
        self.available = False                    # decided at runtime by reads
        self._lock = threading.Lock()
        self._val = 0
        self._target = None                  # latest requested brightness, or None
        self._wake = threading.Event()        # signals the setter there's work
        if self._has_backend:
            v = self._read()
            if v is not None:
                self._val = v
                self.available = True
            # Start the poller even when the first read failed — that's the whole
            # point: it keeps looking so a panel that shows up later reveals the slider.
            threading.Thread(target=self._poll, daemon=True).start()
            if self._set is not None:
                threading.Thread(target=self._setter_loop, daemon=True).start()

    def _read(self):
        # Returns 0..100 on a real read, or None when there's no panel right now.
        try:
            v = self._get()
        except Exception:
            return None
        return None if v is None else max(0, min(100, int(v)))

    # A machine that hasn't shown a panel within this window of connected probing
    # is treated as a true no-panel desktop: stop probing so we don't spawn a
    # PowerShell read forever for a slider that will never appear. Only applies when
    # a panel is NOT known present (see _panel_known) AND none has been seen yet —
    # once a panel has been seen, undock / redock must keep working, so a lost panel
    # is re-probed indefinitely.
    _NO_PANEL_GIVEUP_S = 5 * 60

    def _poll(self):
        # Refresh the cache + availability while a phone is connected. Skip while a
        # write is pending so we don't read a value the panel is mid-change to; our
        # own writes already update the cache. Cadence is adaptive: poll briskly (4s)
        # while UNAVAILABLE so a boot race / undock is detected quickly and the slider
        # appears within seconds, then ease to 20s. Once available, 20s: brightness
        # rarely changes from under us and each read spawns PowerShell.
        misses = 0
        ever = self.available          # has a panel EVER been seen this session
        probing_since = time.monotonic()   # when connected probing (while never-seen) began
        while not _stop.is_set():
            if _client_connected.is_set() and self._target is None:
                v = self._read()
                if v is not None:
                    with self._lock:
                        self._val = v
                    self.available = True
                    misses = 0
                    ever = True
                else:
                    self.available = False
                    misses += 1
                    # Real desktop (panel not known present, none ever seen) and the
                    # give-up window has elapsed → stop the poller so no thread is left
                    # spinning PowerShell. A known-panel machine (laptop) never gives up.
                    if (not ever and not self._panel_known
                            and time.monotonic() - probing_since > self._NO_PANEL_GIVEUP_S):
                        return
            else:
                # Not actively probing (no phone paired): don't count idle time
                # against the give-up window — the clock only runs while we're
                # genuinely trying and failing to find a panel.
                probing_since = time.monotonic()
            fast = not self.available and misses < 8   # ~32s of brisk re-probing
            _stop.wait(4.0 if fast else 20.0)

    def get_cached(self):
        with self._lock:
            return self._val

    def set_async(self, pct):
        # Coalesce: store the latest target and wake one worker. A slider drag fires
        # many BRIGHT packets — applying each would spawn dozens of ~0.5s PowerShell
        # writes and starve the server. The worker only ever applies the newest value.
        pct = max(0, min(100, int(pct)))
        with self._lock:
            self._val = pct                  # optimistic: reflect immediately for BGET
            self._target = pct
        self._wake.set()

    def _setter_loop(self):
        while not _stop.is_set():
            if not self._wake.wait(0.5):
                continue
            self._wake.clear()
            with self._lock:
                tgt = self._target
                self._target = None
            if tgt is not None:
                try:
                    self._set(tgt)
                except Exception:
                    pass


brightness_svc = BrightnessService(get_brightness, set_brightness, BRIGHTNESS_PANEL_KNOWN)


MEDIA_KEYS = {
    "play_pause": Key.media_play_pause,
    "next":       Key.media_next,
    "prev":       Key.media_previous,
}

SPECIAL_KEYS = {
    "enter": Key.enter, "backspace": Key.backspace, "space": Key.space,
    "tab": Key.tab,     "esc": Key.esc,             "delete": Key.delete,
    "up": Key.up,       "down": Key.down,            "left": Key.left,
    "right": Key.right, "home": Key.home,            "end": Key.end,
    "pageup": Key.page_up, "pagedown": Key.page_down,
}
# function keys f1..f12
for _i in range(1, 13):
    SPECIAL_KEYS[f"f{_i}"] = getattr(Key, f"f{_i}")

MODIFIER_KEYS = {
    "ctrl": Key.ctrl, "control": Key.ctrl,
    "alt": Key.alt,
    "shift": Key.shift,
    "win": Key.cmd, "cmd": Key.cmd, "super": Key.cmd, "meta": Key.cmd,
}

def _resolve_key(name):
    """A combo's target key: a single literal char, a special, or a function key."""
    name = name.strip()
    if len(name) == 1:
        return name
    return SPECIAL_KEYS.get(name.lower())


def do_combo(rest):
    """e.g. 'ctrl c', 'ctrl shift t', 'alt tab' — hold modifiers, tap the key."""
    tokens = rest.split()
    if not tokens:
        return
    mods, key = [], None
    for tok in tokens:
        m = MODIFIER_KEYS.get(tok.lower())
        if m is not None and key is None:
            mods.append(m)
        else:
            key = tok           # last non-modifier wins
    target = _resolve_key(key) if key else None
    if target is None:
        return
    try:
        for m in mods:
            keyboard.press(m)
        keyboard.press(target)
        keyboard.release(target)
    finally:
        for m in reversed(mods):
            keyboard.release(m)


# App-switcher session: hold Alt across many packets so Tab cycles forward through
# every window (Windows three-finger swipe feel), instead of toggling two.
_alt_held = False


def do_appswitch(action):
    """next | prev | end. First next/prev presses & holds Alt; end releases it."""
    global _alt_held
    import time
    if action == "end":
        if _alt_held:
            time.sleep(0.05)        # let the highlighted window settle before committing
            keyboard.release(Key.alt)
            _alt_held = False
        return
    if action not in ("next", "prev"):
        return
    if not _alt_held:
        keyboard.press(Key.alt)
        _alt_held = True
        time.sleep(0.08)            # Windows needs Alt registered before Tab opens the switcher
    if action == "prev":
        keyboard.press(Key.shift)
        keyboard.press(Key.tab)
        keyboard.release(Key.tab)
        keyboard.release(Key.shift)
    else:
        keyboard.press(Key.tab)
        keyboard.release(Key.tab)
    time.sleep(0.04)                # spacing so rapid taps each register as a cycle step


def appswitch_reset():
    """Release a stuck Alt if the client leaves mid-gesture."""
    global _alt_held
    if _alt_held:
        try:
            keyboard.release(Key.alt)
        except Exception:
            pass
        _alt_held = False


# Mouse buttons the phone is holding down ("Hold drag"). MUP rides lossy UDP and is
# dropped while the remote is paused, so every path that ends the phone's control
# lets go of these too, or the laptop's own mouse keeps dragging.
_held_buttons = set()


def release_held_input():
    """Let go of everything the phone is holding: a drag-lock button and the Alt
    of an app-switch session. Called wherever the phone stops driving — it left,
    the loop restarted, or the user took over locally. Never raises."""
    for b in list(_held_buttons):
        try:
            mouse.release(b)
        except Exception:
            pass
    _held_buttons.clear()
    appswitch_reset()


def volume_reply(vol, muted, mic_muted=None, phone_mic=None):
    """The answer to VGET: "VOL n [m [c [p]]]". m is output mute, c the laptop
    mic's mute, p whether the phone can be the microphone ("1" ready, "0" needs
    VB-CABLE on the laptop). Each word is sent only when known; an unknown word
    that a later one needs holds its slot with "-". An older phone reads only the
    words it knows, so the extras cost it nothing."""
    def flag(v):
        return "-" if v is None else ("1" if v else "0")
    words = [f"VOL {vol}"]
    tail = [flag(muted), flag(mic_muted), phone_mic if phone_mic is not None else "-"]
    while tail and tail[-1] == "-":
        tail.pop()
    return " ".join(words + tail)


def do_system(action):
    """lock | sleep | mute | micmute — best effort per OS."""
    plat = sys.platform
    if action == "micmute":
        # Laptop-wide microphone mute toggle. The phone only offers the button
        # when VGET reported a mic state, so a missing backend is a no-op.
        if toggle_mic_mute is not None:
            toggle_mic_mute()
        return
    if action == "mute":
        # toggle mute via the media key (cross-platform-ish)
        k = getattr(Key, "media_volume_mute", None)
        if k is not None:
            keyboard.press(k)
            keyboard.release(k)
        elif set_volume is not None and get_volume is not None:
            cur = get_volume()          # None when the output device is unavailable
            if cur is not None:
                set_volume(0 if cur > 0 else 30)
        return
    if action == "lock":
        if plat.startswith("win"):
            import ctypes
            ctypes.windll.user32.LockWorkStation()
        elif plat == "darwin":
            os.system("pmset displaysleepnow")
        else:
            os.system("loginctl lock-session 2>/dev/null || "
                      "xdg-screensaver lock 2>/dev/null")
        return
    if action == "sleep":
        if plat.startswith("win"):
            import subprocess
            subprocess.Popen([win_exe("rundll32.exe"), "powrprof.dll,SetSuspendState",
                              "0,1,0"], creationflags=_no_window_flags())
        elif plat == "darwin":
            os.system("pmset sleepnow")
        else:
            os.system("systemctl suspend 2>/dev/null")
        return


# ── crypto: secure wire (L3) ──────────────────────────────────────────────────
#   v1 (legacy, PLAINTEXT):  "<TOKEN> <VERB> [args]"   — trusted LAN only.
#   SECURE:  b"L3" || sid(8) || counter(4 BE) || AES-256-GCM(ct+tag)
#     nonce = sid||counter (12 B) · AAD = b"L3"||sid||counter · plaintext = "VERB [args]"
# The 256-bit key is shipped in the QR (and auto-discovery never carries it). A valid
# GCM tag *is* the authentication (proves key possession) — no token on the wire —
# and the monotonic counter gives replay protection. Sniffing/forgery/replay all fail.
#
# Why sid(8)|counter(4): the key is PERSISTENT across launches while the sid is random
# per session, so a sid collision means GCM nonce reuse under the same key — which
# leaks the authentication key, not just a plaintext. The original L2 dialect split
# sid(4)|counter(8), putting reuse at the 2^32 birthday bound (~1.2% by 10k sessions,
# ~39% by 65k — reachable, since every reconnect mints a session and the watchdog
# reconnects on any drop). L3 moved four bytes to the sid for 2^64 at zero cost: a
# 4-byte counter still allows 4.29e9 packets per session, and exhausting it re-keys
# the sid rather than wrapping. L2 was accepted until 2.2.0 so un-updated phones
# kept working, and is REMOVED — an L2 packet is now just unknown
# magic and falls through like any other junk. Do not add a new dialect without also
# updating the golden vectors in server/tests/test_wire.py AND
# android/.../SecureChannelTest.kt — they are what stop the two implementations
# drifting apart.
MAGIC_V3 = b"L3"
WIRE_FORMATS = {MAGIC_V3: (8, 4)}   # magic -> (sid_len, ctr_len)


try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:
    AESGCM = None
    _HAVE_CRYPTO = False


# ── secret files ──────────────────────────────────────────────────────────────
# .lazer_token and .lazer_key are everything needed to drive this laptop. A plain
# open() inherits the folder's access list (next to an exe on D:\ that is
# "Authenticated Users: Modify") or the umask, so one writer makes them owner-only
# from the moment the file exists: a fresh temp file renamed over the target (a
# rename keeps the temp file's own access list), raising on failure rather than
# pretending.

# Filled when a secret could not be persisted at startup, for the UI to show: the
# pairing then lasts only until the app is closed.
SECRET_WRITE_ERRORS = []


def _win_owner_only_sddl():
    """SDDL granting full control to this user and SYSTEM only, protected from
    inheritance (the "P"), so nothing the folder grants reaches the file."""
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(wintypes.LPWSTR)]
    TOKEN_QUERY, TOKEN_USER = 0x0008, 1
    tok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                     ctypes.byref(tok)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(tok, TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(tok, TOKEN_USER, buf, size,
                                            ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        s = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(s)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = s.value
        finally:
            kernel32.LocalFree(s)
    finally:
        kernel32.CloseHandle(tok)
    return f"D:P(A;;FA;;;SY)(A;;FA;;;{sid})"


def _win_create_private(path):
    """Create [path] (which must not exist) with the owner-only access list set
    by CreateFileW itself, so there is no moment it is readable by anyone else.
    Returns a writable text file object."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG)]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    psd = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            _win_owner_only_sddl(), 1, ctypes.byref(psd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), psd, False)
        GENERIC_WRITE, CREATE_NEW, FILE_ATTRIBUTE_NORMAL = 0x40000000, 1, 0x80
        h = kernel32.CreateFileW(path, GENERIC_WRITE, 0, ctypes.byref(sa), CREATE_NEW,
                                 FILE_ATTRIBUTE_NORMAL, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(psd)
    fd = msvcrt.open_osfhandle(h, os.O_WRONLY)
    return os.fdopen(fd, "w", encoding="ascii")


def _secret_tmp_path(path):
    return f"{path}.tmp-{secrets.token_hex(4)}"


def _write_secret_tmp(path, text):
    """Write [text] to a fresh owner-only temp file beside [path]; return its path.
    Raises OSError on any failure (and leaves no temp file behind)."""
    tmp = _secret_tmp_path(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            f = _win_create_private(tmp)
        else:
            f = os.fdopen(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                          "w", encoding="ascii")
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        return tmp
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_secret(path, text):
    """Persist one secret owner-only and atomically. Raises OSError on failure."""
    tmp = _write_secret_tmp(path, text)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _secret_is_private(path):
    """False when a secret file is readable by more than its owner — an install
    from before write_secret existed. Unknown counts as private (no rewrite)."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32.GetNamedSecurityInfoW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p)]
            advapi32.GetSecurityDescriptorControl.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, SE_DACL_PROTECTED = 1, 4, 0x1000
            psd = ctypes.c_void_p()
            if advapi32.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                              None, None, None, None, ctypes.byref(psd)):
                return True
            try:
                ctl, rev = wintypes.WORD(), wintypes.DWORD()
                if not advapi32.GetSecurityDescriptorControl(psd, ctypes.byref(ctl),
                                                             ctypes.byref(rev)):
                    return True
                return bool(ctl.value & SE_DACL_PROTECTED)
            finally:
                kernel32.LocalFree(psd)
        return (os.stat(path).st_mode & 0o077) == 0
    except Exception:
        return True


def _harden_existing_secret(path, text):
    """Re-write an old, loosely-permissioned secret in place with the same
    contents. Best effort: a folder we can't write to keeps what it has."""
    if not _secret_is_private(path):
        try:
            write_secret(path, text)
        except OSError:
            pass


def migrate_legacy_secrets(legacy_dir=None):
    """Move a pair saved beside the binary (before 2.4) into the data folder, so an
    update keeps every phone paired. Never overwrites: a data folder that already
    holds either file wins. Best effort — a failure just leaves the old files."""
    legacy_dir = _APP_DIR if legacy_dir is None else legacy_dir
    moves = [(os.path.join(legacy_dir, os.path.basename(p)), p)
             for p in (TOKEN_FILE, KEY_FILE)]
    if any(os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new))
           or os.path.exists(new) for old, new in moves):
        return
    found = []
    for old, new in moves:
        try:
            with open(old, "r") as f:
                found.append((old, new, f.read().strip()))
        except OSError:
            pass
    written = []
    try:
        for _, new, text in found:
            write_secret(new, text)
            written.append(new)
    except OSError:
        # Half a pair in the data folder would stop the next launch retrying and
        # pair the moved token with a fresh key. Undo; the old pair still works.
        for new in written:
            try:
                os.remove(new)
            except OSError:
                pass
        return
    for old, _, _ in found:
        try:
            os.remove(old)
        except OSError:
            pass   # a read-only folder keeps a stale copy; the new one is used


def remove_data_dir():
    """Delete the saved pairing (and the folder, once empty). Returns the paths
    that couldn't be removed."""
    left = []
    for p in (TOKEN_FILE, KEY_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError:
            left.append(p)
    try:
        os.remove(STARTUP_DEFAULT_MARKER)  # not a secret: never reported as left
    except OSError:
        pass
    try:
        os.rmdir(_DATA_DIR)
    except OSError:
        pass                  # absent, or holds something that isn't ours
    return left


def _new_token():
    return "".join(secrets.choice(string.ascii_uppercase + string.digits)
                   for _ in range(TOKEN_LEN))


def load_or_create_key():
    """Persistent 32-byte key (base64url in KEY_FILE), reused across launches."""
    try:
        with open(KEY_FILE, "r") as f:
            text = f.read().strip()
        raw = base64.urlsafe_b64decode(text + "===")
        if len(raw) == 32:
            _harden_existing_secret(KEY_FILE, text)
            return raw
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        write_secret(KEY_FILE, key_b64(key))
    except OSError as e:
        SECRET_WRITE_ERRORS.append(f"{KEY_FILE}: {e}")
    return key


def key_b64(key):
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode()


def rotate_secrets(wire):
    """Generate a fresh token + key, persist them, and update the live Wire.
    Any currently-paired phone is invalidated (must rescan) — a one-click 'kick'.

    Raises OSError, leaving the live Wire and both files untouched, if the new
    secrets can't be saved: swapping them in anyway would kick the phone only until
    the next launch reloaded the old pair."""
    token = _new_token()
    key = secrets.token_bytes(32)
    # Both temp files first: if either can't be written (the usual failure — a
    # folder like Program Files that isn't ours), neither target has changed.
    tmp_tok = _write_secret_tmp(TOKEN_FILE, token)
    try:
        tmp_key = _write_secret_tmp(KEY_FILE, key_b64(key))
    except OSError:
        os.remove(tmp_tok)
        raise
    def _drop(*paths):
        for t in paths:
            try:
                os.remove(t)
            except OSError:
                pass

    try:
        os.replace(tmp_tok, TOKEN_FILE)
    except OSError:
        _drop(tmp_tok, tmp_key)
        raise
    try:
        os.replace(tmp_key, KEY_FILE)
    except OSError:
        # The token on disk is already the new one. Put the old one back, or the
        # next launch pairs the new token with the old key and every phone fails
        # with nothing to say why — while the GUI said nothing had changed.
        try:
            write_secret(TOKEN_FILE, wire.token)
        except OSError:
            pass
        _drop(tmp_key)
        raise
    with wire.lock:
        _swap_secrets(wire, token, key)
    return token, key


def _swap_secrets(wire, token, key):
    wire.token = token
    wire.key = key
    wire.aes = AESGCM(key) if (key and _HAVE_CRYPTO) else None
    wire.cli_magic, wire.cli_sid, wire.cli_ctr = None, None, -1
    wire.secure_client = False
    wire.pt_suspended_until = 0.0   # fresh credentials deserve a fresh chance
    # Fresh key ⇒ start the server's send sessions over. Not strictly required (a
    # repeated nonce under a DIFFERENT key is harmless), but it keeps the invariant
    # "one (key, dialect, sid) never reuses a counter" true without a caveat.
    wire._srv.clear()
    # serve_loop still holds the kicked phone's address as its client; this tells
    # it to drop that pin on its next tick, so the window stops saying "connected"
    # right away instead of after the 12s idle timeout.
    wire.rotated = True


# ── refusal hints ─────────────────────────────────────────────────────────────
# Silence is what a firewall, a sleeping laptop and a wrong IP produce too, so for
# the two refusals the phone can act on, the server answers one plaintext line,
# "ERR <reason>":
#   secure-required  a plaintext HELLO carrying the RIGHT token, while encryption is
#                    required: pair by QR, or turn Require encryption off.
#   bad-key          a secure-framed packet that failed authentication: this phone's
#                    key is not ours, almost always a pairing from before Regenerate.
# What it reveals: that a LazeR server is listening (mDNS already announces that),
# and for secure-required, that the sender's token is right — worth nothing while
# encryption is required, since the token alone opens nothing then. A wrong token
# on the plaintext wire still gets silence, so there is no token-guessing oracle.
# It is unauthenticated, so the phone treats it as a hint for its error message,
# never as a reason to stop trying. Older phones ignore it: they discard anything
# that isn't a sealed reply or a bare "OK".
REFUSE_SECURE_REQUIRED = "secure-required"
REFUSE_BAD_KEY = "bad-key"
REFUSAL_MIN_GAP_S = 2.0          # per address: one hint per handshake attempt is plenty
REFUSAL_MAX_PER_WINDOW = 32      # overall, per RATE_WINDOW_S — no use as a reflector
WSAEMSGSIZE = 10040              # Windows: datagram larger than the receive buffer
# A receive loop that ran this long before crashing was healthy; the next crash
# starts the restart backoff over instead of paying for crashes hours ago.
LOOP_HEALTHY_S = 60.0


class RefusalLimiter:
    """Rate limit for refusal hints: at most one per address per
    REFUSAL_MIN_GAP_S, and REFUSAL_MAX_PER_WINDOW in total until reset()."""

    def __init__(self):
        self._last = {}
        self._count = 0

    def allow(self, addr, now):
        if self._count >= REFUSAL_MAX_PER_WINDOW:
            return False
        last = self._last.get(addr)
        if last is not None and now - last < REFUSAL_MIN_GAP_S:
            return False
        self._last[addr] = now
        self._count += 1
        return True

    def reset(self):
        self._last.clear()
        self._count = 0


def send_refusal(sock, addr, reason):
    """Send one "ERR <reason>" hint. Fail-soft: it runs above the handler guard."""
    try:
        sock.sendto(f"ERR {reason}".encode("ascii"), addr)
    except OSError:
        pass


class Wire:
    """Per-server auth + encrypt/decrypt + replay state for one controller."""

    def __init__(self, token, key, require_secure):
        self.token = token
        self.key = key
        self.require_secure = require_secure
        # Taken by rotate_secrets (Tk thread) around the swap, and by serve_loop
        # around each parse and handshake step, so a packet is never judged half
        # against the old secrets and half against the new.
        self.lock = threading.RLock()
        self.rotated = False
        self.aes = AESGCM(key) if (key and _HAVE_CRYPTO) else None
        # Default reply dialect, used only until a client is pinned. It is NOT
        # mutated by incoming packets — see _seal_reply for why that mattered.
        self.wire_magic = MAGIC_V3
        # The server's own send session, PER DIALECT: magic -> [sid, counter].
        # Per-dialect rather than one pair, because the two dialects have different
        # sid widths, so a single pair would have to be re-drawn every time the
        # dialect changed — and re-drawing a sid while restarting the counter at 0
        # is exactly how a nonce gets repeated under a key that outlives the
        # session. Each dialect keeps its own monotonic counter for the life of the
        # key instead, so switching back and forth costs nothing and repeats nothing.
        self._srv = {}
        # pinned client's secure session (set when a HELLO is authenticated via AUTH)
        self.cli_magic = None
        self.cli_sid = None
        self.cli_ctr = -1
        self.secure_client = False
        # Set when a correctly-tokened plaintext packet was refused because
        # encryption is required; serve_loop turns it into a one-time explanation.
        self.plaintext_refused = False
        # Set when a secure-framed packet failed authentication — almost always a
        # phone paired to this laptop's PREVIOUS key. Same one-shot contract.
        self.key_refused = False
        # Set when a tag-valid packet was dropped as stale (wrong source/session,
        # replay, reorder); serve_loop leaves it out of the flood count.
        self.stale_rejected = False
        # The client nonces to echo in the OKs that answer a verified AUTH, newest
        # first ("" for a phone too old to send one). See issue_challenge.
        self.auth_echoes = ()
        # Monotonic deadline while plaintext v1 acceptance is paused (see
        # PLAINTEXT_SUSPEND_S). 0.0 ⇒ accepting.
        self.pt_suspended_until = 0.0
        self._pending = None            # (sid, ctr) of the in-flight HELLO/AUTH
        self._chal = {}                 # addr -> (nonce, expiry_monotonic): open challenges

    def parse(self, data, addr, client):
        """Authenticate one datagram. Returns (verb, rest, secure) or None if rejected.
        [client] is the currently pinned controller address (or None). HELLO/AUTH are
        handshake verbs (freshness is enforced by the challenge, not the counter); every
        other secure verb must come FROM the pinned client, carry the pinned sid, and a
        strictly-greater counter — and the counter watermark advances ONLY for such
        packets, so a tag-valid replay from a stranger can't desync the real client."""
        fmt = WIRE_FORMATS.get(data[:2])
        if fmt is not None and self.aes is not None and len(data) >= 14 + 16:
            magic = data[:2]
            sid_len = fmt[0]
            # The nonce is the 12 bytes after the magic either way; only the point at
            # which it stops being a session id and starts being a counter moves.
            sid = data[2:2 + sid_len]
            ctr = int.from_bytes(data[2 + sid_len:14], "big")
            try:
                pt = self.aes.decrypt(data[2:14], data[14:], data[0:14])
            except Exception:
                # Bad tag: forged, corrupt, or (usually) a key from before a re-pair.
                # Flag it for the bad-key hint.
                self.key_refused = True
                return None
            # Trailing NULs are padding: a phone that saw this server bind its
            # handshake (so knows it strips them) pads KEY/KEYSP to fixed buckets,
            # so the ciphertext length stops giving away how much was typed or
            # which special key it was. Nothing legitimate ends in a NUL.
            text = pt.decode("utf-8", "ignore").rstrip("\x00")
            verb, rest = _split_verb(text)
            if verb in ("HELLO", "AUTH"):
                # Record the dialect so the CHAL goes back in the one this client
                # used (it can't read the other one's framing offset reliably, and a
                # dead-ended handshake is the result). Deliberately does NOT set
                # self.wire_magic: a HELLO is unauthenticated in the sense that
                # matters here — a REPLAYED one is tag-valid — so letting it change
                # server-wide reply state let any keyless replayer re-draw the
                # server's session id out from under the pinned phone.
                self._pending = (magic, sid, ctr)  # commit_hello pins the AUTH session
                return verb, rest, True
            if (addr != client or magic != self.cli_magic
                    or sid != self.cli_sid or ctr <= self.cli_ctr):
                # Not the pinned client / replay / reorder. Tag-valid, so whoever
                # made it held the key: not a guess, and not what the flood alarm
                # is for. A phone streaming its voice (50 packets/s) into a session
                # the laptop already dropped (LazeR restarted, a re-pin) would trip
                # the alarm and pause typed-code pairing for nothing.
                self.stale_rejected = True
                return None
            self.cli_ctr = ctr
            return verb, rest, True
        # plaintext v1. Flood suspension first (see PLAINTEXT_SUSPEND_S): while
        # paused, drop before ANY decoding or comparison. The secure path above is
        # untouched — manual-code pairing is the only thing paused, because it is
        # the only path a token brute-force exists against.
        if time.monotonic() < self.pt_suspended_until:
            return None
        # Split and compare as BYTES: secrets.compare_digest raises TypeError on
        # non-ASCII str, and this runs above the handler guard.
        parts = data.rstrip(b"\r\n").split(b" ", 2)
        if len(parts) < 2 or not secrets.compare_digest(parts[0], self._token_b):
            return None
        if self.require_secure:
            # Nothing is accepted here — but tell apart "a real phone tried to pair
            # with the manual code" from random junk, so the UI can explain the
            # refusal. Otherwise flipping the default to secure-only turns manual
            # pairing into an unexplained timeout, which is a worse experience than
            # the insecure wire it replaced.
            self.plaintext_refused = True
            return None
        verb = parts[1].decode("utf-8", "ignore")
        rest = parts[2].decode("utf-8", "ignore") if len(parts) > 2 else ""
        return verb, rest, False

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        # Kept alongside its bytes form so parse never encodes per packet, and so
        # rotate_secrets (which assigns .token) can't leave the two out of step.
        self._token = value
        self._token_b = (value or "").encode("utf-8")

    def _clean_cnonce(self, rest):
        """The client nonce a HELLO carries (see issue_challenge), or "" if it has
        none — an older phone — or sends something that isn't one."""
        c = rest.strip()
        if 16 <= len(c) <= 64 and all(ch.isascii() and (ch.isalnum() or ch in "-_")
                                      for ch in c):
            return c
        return ""

    def issue_challenge(self, sock, addr, now, magic=None, cnonce=""):
        """Answer a secure HELLO with a one-time challenge (encrypted). The client
        must echo the nonce in an AUTH to be pinned. Does NOT pin — a replayed HELLO
        just draws a challenge the replayer can't answer.

        [magic] is the dialect of the HELLO being answered; it defaults to the one
        parse() just recorded, which is that same packet's. It is carried per
        challenge rather than held server-wide so a stranger's HELLO can never
        change how the PINNED client's replies are framed.

        The challenge is IDEMPOTENT per address: repeated HELLOs from the same addr
        (a handshake burst, or resends faster than a slow link's round trip) reuse
        the SAME outstanding nonce until it's answered or expires. Without
        this, each HELLO would mint a new nonce and overwrite the last, so the client's
        AUTH — echoing whichever CHAL it happened to receive first — would never match
        the server's latest, and the handshake could never complete on a slow link.

        [cnonce] is the client's own nonce from its HELLO. It is echoed in the CHAL
        and again in the final OK (one OK per nonce the challenge collected, see
        CNONCE_KEEP), which is what binds our replies to THIS handshake:
        the key outlives every session, so any sealed CHAL or OK captured earlier is
        still tag-valid, and a phone that took the first one it received could be
        handed a replay from a stranger and pin a session that isn't ours. A phone
        too old to send one gets the old unbound replies; it never parses the rest."""
        if self.aes is None:
            return
        if magic is None:
            magic = self._pending[0] if self._pending else self.wire_magic
        cnonce = self._clean_cnonce(cnonce)
        ent = self._chal.get(addr)
        if ent is not None and now <= ent[1]:
            nonce = ent[0]                      # reuse the live challenge for this addr
            # Keep the nonce (that idempotency is the point) but track the dialect
            # of the LATEST HELLO, and add its client nonce to the ones the OK echoes.
            # The first one is never evicted: it came with the HELLO that opened the
            # challenge, which is the phone's own (nobody knew its port before), and
            # without the pin a few replayed HELLOs would push it out.
            first, rest = ent[3][0], tuple(c for c in ent[3][1:] if c != cnonce)
            if cnonce != first:
                rest += (cnonce,)
            seen = (first,) + rest[-(CNONCE_KEEP - 1):]
            self._chal[addr] = (nonce, ent[1], magic, seen)
        else:
            # Make room without ever letting one source cost another its challenge.
            # This used to clear the WHOLE table at the cap, so a replayed-HELLO
            # flood (no key required) wiped the real phone's nonce and its AUTH
            # echoed something the server had already discarded. Evicting merely the
            # "oldest" is no better: a flood arriving inside the victim's TTL leaves
            # nothing expired and the victim IS the oldest.
            self.sweep_challenges(now)
            same_ip = [a for a in self._chal if a[0] == addr[0]]
            if len(same_ip) >= CHAL_PER_IP:
                # This source is already holding plenty: recycle ITS oldest.
                self._chal.pop(min(same_ip, key=lambda a: self._chal[a][1]), None)
            elif len(self._chal) >= CHAL_MAX:
                # Global cap reached across many sources: take from whichever IP
                # holds the most entries — the flooder — never from a quiet one.
                counts = {}
                for a in self._chal:
                    counts[a[0]] = counts.get(a[0], 0) + 1
                worst = max(counts, key=counts.get)
                victims = [a for a in self._chal if a[0] == worst]
                self._chal.pop(min(victims, key=lambda a: self._chal[a][1]), None)
            nonce = secrets.token_bytes(16)
            self._chal[addr] = (nonce, now + CHAL_TTL_S, magic, (cnonce,))
        text = "CHAL " + base64.urlsafe_b64encode(nonce).rstrip(b"=").decode()
        if cnonce:
            text += " " + cnonce
        self._seal_reply(sock, addr, text, magic=magic)

    def verify_challenge(self, addr, rest, now):
        """True iff [rest] echoes the fresh, unexpired challenge issued to [addr].

        Single-use: a MATCH consumes it, so a replay of the genuine AUTH fails. A
        mismatch does not, so a tag-valid AUTH replayed from an older session can't
        burn the phone's challenge mid-handshake. On success the
        client nonces to echo are left in [auth_echoes]."""
        self.auth_echoes = ()
        ent = self._chal.get(addr)
        if ent is None:
            return False
        nonce, exp = ent[0], ent[1]
        if now > exp:
            self._chal.pop(addr, None)
            return False
        try:
            r = rest.strip()
            got = base64.urlsafe_b64decode(r + "=" * (-len(r) % 4))
        except Exception:
            return False
        if not secrets.compare_digest(got, nonce):
            return False
        self._chal.pop(addr, None)
        self.auth_echoes = tuple(reversed(ent[3]))
        return True

    def ok_texts(self):
        """The OKs answering a just-verified AUTH, newest HELLO first: each echoes
        one client nonce the challenge collected, or is a bare "OK" for a phone too
        old to send one (see issue_challenge). The phone takes the one it sent."""
        return ["OK " + c if c else "OK" for c in self.auth_echoes]

    def sweep_challenges(self, now):
        """Drop expired challenges so a flood of unanswered HELLOs can't accumulate."""
        for a in [a for a, ent in self._chal.items() if now > ent[1]]:
            self._chal.pop(a, None)

    def commit_hello(self, secure):
        """Pin the just-authenticated session (the AUTH packet's dialect/sid/ctr)."""
        self.secure_client = secure
        if secure and self._pending is not None:
            self.cli_magic, self.cli_sid, self.cli_ctr = self._pending
            self.wire_magic = self.cli_magic
        else:
            self.cli_magic, self.cli_sid, self.cli_ctr = None, None, -1

    def unpin_client(self):
        """Forget the pinned phone's secure session and any half-finished handshake.

        serve_loop's `client = None` only stopped ACCEPTING that address; the sid /
        counter watermark and secure_client stayed behind, so replies could still be
        sealed for a session that no longer exists and a stale `_pending` could be
        committed by a later AUTH. Called wherever the phone is genuinely gone (idle
        timeout, BYE, resume) so the next handshake starts from a clean slate.

        Deliberately leaves the challenge table alone. Clearing it looks tidy and
        is a bug: `_chal` is keyed by address and the departing client is not the
        only one in it. The reconnecting phone comes back on a NEW source port, so
        the idle drop for its OLD address routinely fires while its fresh HELLO is
        already outstanding — wiping the table there would delete the nonce it is
        about to echo, dead-ending the handshake until its next retry. Unanswered
        challenges expire on their own via sweep_challenges."""
        self.cli_magic, self.cli_sid, self.cli_ctr = None, None, -1
        self.secure_client = False
        self._pending = None

    def _srv_session(self, magic):
        """This dialect's [sid, counter], created on first use. Never re-drawn on a
        dialect change — only when the counter is genuinely spent."""
        ent = self._srv.get(magic)
        if ent is None:
            ent = [secrets.token_bytes(WIRE_FORMATS[magic][0]), 0]
            self._srv[magic] = ent
        return ent

    def reply_magic(self, magic=None):
        """Which dialect to answer in: the one being answered if the caller knows
        it, else the pinned client's, else the default. Never a value an incoming
        packet was able to set."""
        return magic or self.cli_magic or self.wire_magic

    def _seal_reply(self, sock, addr, text, magic=None):
        """Encrypt+send a reply under this dialect's own sid/counter."""
        magic = self.reply_magic(magic)
        sid_len, ctr_len = WIRE_FORMATS[magic]
        ent = self._srv_session(magic)
        # Only a spent counter re-keys. Wrapping would repeat a nonce under a key
        # that outlives the session — the failure this whole dialect change exists
        # to prevent — and a fresh sid restarts the counter safely.
        if ent[1] >= (1 << (8 * ctr_len)) - 1:
            ent[0], ent[1] = secrets.token_bytes(sid_len), 0
        ent[1] += 1
        nonce = ent[0] + ent[1].to_bytes(ctr_len, "big")
        hdr = magic + nonce
        ct = self.aes.encrypt(nonce, text.encode("utf-8"), hdr)
        try:
            sock.sendto(hdr + ct, addr)
        except OSError:
            # Same fail-soft contract as reply() below, and for the same reason —
            # but this path is the one that answers a HELLO with its challenge, and
            # it runs OUTSIDE serve_loop's handler guard. A send to a phone that has
            # just moved networks raises ENETUNREACH/EHOSTUNREACH (and WSAECONNRESET
            # on Windows), so letting it propagate took the whole receive thread down
            # while the window still read "server running" — the laptop went deaf
            # until it was restarted. A dropped reply is nothing: the phone resends.
            pass

    def reply(self, sock, addr, text):
        """Send a reply, encrypted iff the client is on the secure wire."""
        try:
            if self.secure_client and self.aes is not None:
                self._seal_reply(sock, addr, text)
            else:
                sock.sendto(text.encode("utf-8"), addr)
        except OSError:
            pass


def _split_verb(text):
    parts = text.split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


# ── local-input takeover guard (Windows) ─────────────────────────────────────
def panic_chord_held(get_async_key_state):
    """True iff the panic modifiers are physically down at this instant.

    Asked of the OS, not tracked from the hook: the hook reports side-specific codes
    (see LocalInputGuard.PANIC_KEY), and misses key-UPs across a lock screen / UAC
    prompt / session switch. Three register reads, cheap enough for a hook callback."""
    return all(get_async_key_state(vk) & 0x8000
               for vk in LocalInputGuard.PANIC_MODIFIERS)


def want_wake_hold(client, last_remote, mono):
    """Should the machine be held awake? Only while a phone is paired AND it drove
    the machine within REMOTE_AWAKE_S. [last_remote] is None until the first
    control verb: time.monotonic() counts from boot, so a 0.0 sentinel read as
    "just now" for the first REMOTE_AWAKE_S of uptime and held a freshly booted
    laptop awake for nothing."""
    return (client is not None and last_remote is not None
            and (mono - last_remote) < REMOTE_AWAKE_S)


def swap_hooks(install, unhook, current):
    """Replace the hook pair [current] with a freshly installed one; return the
    pair now in force. Install FIRST and drop the old pair only once the new one
    is real: SetWindowsHookExW can fail mid session/desktop transition — exactly
    when a rearm runs — and unhooking first would throw away two working hooks
    for nothing. A partial install is undone and [current] kept."""
    new_kb, new_ms = install()
    if new_kb and new_ms:
        for h in current:
            unhook(h)
        return new_kb, new_ms
    for h in (new_kb, new_ms):
        if h:
            unhook(h)
    return current


class LocalInputGuard:
    """Low-level Windows hooks that fire ONLY on physical (non-injected) input.
    Our own pynput injections carry the INJECTED flag and are ignored, so the
    user's real mouse/keyboard always wins: touching it pauses the remote, and a
    panic chord (Ctrl+Alt+Shift+L) latches the pause until they resume."""

    # The panic chord is Ctrl+Alt+Shift+L. Only the L is matched from the hook's
    # own report; the three modifiers are read back from the OS in chord_held().
    #
    # That split is not a style choice. A WH_KEYBOARD_LL hook reports SIDE-SPECIFIC
    # virtual keys — VK_LSHIFT (0xA0), VK_LCONTROL (0xA2), VK_LMENU (0xA4) — never
    # the generic VK_SHIFT/CONTROL/MENU (0x10/0x11/0x12). This used to test a set of
    # the generic codes against what the hook had recorded, which could not match
    # for any keypress on any keyboard, so the panic hotkey had never once fired
    # despite being documented in START_HERE.md and shown in the GUI. Asking
    # GetAsyncKeyState instead fixes that AND is side-agnostic: the generic codes
    # are exactly what it answers for, whichever Shift you press.
    PANIC_KEY = 0x4C                                 # L
    PANIC_MODIFIERS = (0x10, 0x11, 0x12)             # shift, ctrl, alt (generic)

    WM_REARM = 0x8000 + 1        # WM_APP+1: our private "re-install the hooks" ping
    WM_SYNC = 0x8000 + 2         # WM_APP+2: "a phone just connected — hook the mouse"
    MOUSE_SYNC_MS = 250          # backstop: how often the mouse hook follows the connection

    def __init__(self, on_physical, on_panic):
        self._on_physical = on_physical
        self._on_panic = on_panic
        self._thread = None
        self._tid = None

    def start(self):
        _input_guard[0] = self        # so serve_loop can re-arm us after a wake
        if not sys.platform.startswith("win"):
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _client_connected.listeners.append(self._sync_now)
        return True

    def _sync_now(self):
        """Ask the hook thread to install the mouse hook now (a phone connected).
        Without it, physical mouse input in the first timer period after pairing
        went unnoticed and didn't pause the phone."""
        tid = self._tid
        if tid is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(tid, self.WM_SYNC, 0, 0)
        except Exception:
            pass

    def rearm(self):
        """Re-install the hooks after a wake or a lock/unlock.

        Windows silently drops a low-level hook whose callback ever overran
        LowLevelHooksTimeout, and a session switch (lock screen, UAC secure
        desktop) can take them out too. Nothing reports it — the guard simply
        stops noticing physical input, so the remote no longer yields to the
        laptop's own mouse and the panic key goes dead, on a session that looks
        entirely healthy.

        Handled on the hook thread, since that is the thread the hooks belong to;
        see _run for why the new pair is installed before the old one is dropped."""
        tid = self._tid
        if tid is None or not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(tid, self.WM_REARM, 0, 0)
        except Exception:
            pass                          # best effort; the next wake tries again

    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
        WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
        WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
        WM_TIMER = 0x0113
        LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01

        LRESULT = ctypes.c_ssize_t
        # WINFUNCTYPE (stdcall) — a CFUNCTYPE callback is rejected by SetWindowsHookExW.
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                             wintypes.HINSTANCE, wintypes.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        # Declared explicitly: the API returns SHORT, and the "is it down" answer is
        # the high bit. Left at ctypes' default c_int it happens to work, but only by
        # accident of how the value is sign-extended.
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        class MSLL(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        def chord_held():
            return panic_chord_held(user32.GetAsyncKeyState)

        def kb_proc(nCode, wParam, lParam):
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBD)).contents
                if not (kb.flags & LLKHF_INJECTED):       # physical only
                    vk = kb.vkCode
                    if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if vk == self.PANIC_KEY and chord_held():
                            self._on_panic()
                        else:
                            self._on_physical()
            return user32.CallNextHookEx(0, nCode, wParam, lParam)

        def ms_proc(nCode, wParam, lParam):
            if nCode == 0:
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLL)).contents
                if not (ms.flags & LLMHF_INJECTED):       # physical only
                    self._on_physical()
            return user32.CallNextHookEx(0, nCode, wParam, lParam)

        self._kb_cb = HOOKPROC(kb_proc)   # keep refs alive
        self._ms_cb = HOOKPROC(ms_proc)
        hmod = kernel32.GetModuleHandleW(None)

        def install():
            return (user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, hmod, 0),
                    user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb, hmod, 0))

        def install_kb():
            return user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, hmod, 0)

        def install_ms():
            return user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb, hmod, 0)

        # The MOUSE hook is installed only while a phone is connected. A low-level
        # mouse hook sits in front of every physical mouse event on the machine:
        # Windows waits for our Python callback (and so for the GIL) before the
        # pointer moves. With no phone attached the callback had nothing to do
        # (_physical_event returns at once) but the laptop's own mouse still paid
        # that round trip on every move — up to 1000 times a second on a gaming
        # mouse, and a visible hitch whenever another of our threads held the GIL.
        # A 250 ms thread timer syncs the hook with the connection, so serve_loop
        # needs no knowledge of us. The keyboard hook stays: keys are rare and the
        # panic chord must always work.
        kb_hook = install_kb()
        ms_hook = install_ms() if _client_connected.is_set() else None
        user32.SetTimer(None, 0, self.MOUSE_SYNC_MS, None)
        # Set LAST: rearm() posts to this id, and a caller that read it before the
        # hooks existed would ping a thread with nothing to re-install.
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        while not _stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            if (msg.message == WM_TIMER and not msg.hwnd) or msg.message == self.WM_SYNC:
                want = _client_connected.is_set()
                if want and not ms_hook:
                    ms_hook = install_ms() or None
                elif not want and ms_hook:
                    user32.UnhookWindowsHookEx(ms_hook)
                    ms_hook = None
                continue
            if msg.message == self.WM_REARM and not ms_hook:
                # Mouse hook is off (no phone): re-install just the keyboard one,
                # new before old for the same reason as below.
                new_kb = install_kb()
                if new_kb:
                    user32.UnhookWindowsHookEx(kb_hook)
                    kb_hook = new_kb
                continue
            if msg.message == self.WM_REARM:
                # A thread-message has no window, so it is never dispatched — handle
                # it here.
                #
                # Install FIRST, and only drop the old handles once the new pair is
                # real. Unhooking up front is the obvious order and the wrong one:
                # SetWindowsHookExW can fail during a session or desktop transition,
                # which is exactly the wake-and-unlock moment this runs at, and that
                # would have thrown away two working hooks for nothing. The guard
                # would go quiet with no error anywhere — physical input would stop
                # pausing the remote, and the panic key would stop working, for the
                # rest of the session. A duplicate LL hook from one thread is legal,
                # so the brief overlap costs nothing.
                kb_hook, ms_hook = swap_hooks(install, user32.UnhookWindowsHookEx,
                                              (kb_hook, ms_hook))
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(kb_hook)
        if ms_hook:
            user32.UnhookWindowsHookEx(ms_hook)


# ── packet handler ────────────────────────────────────────────────────────────
# Bounds on motion arguments. A real gesture is a few hundred px at most, so these
# sit far above anything the client sends — they exist because the values go
# straight into ctypes/OS calls: an out-of-range magnitude (a bigint from a buggy
# or hostile client) raises out of pynput, and before serve_loop guarded the
# dispatch that killed the one receive thread outright. Clamping keeps the OS layer
# in its comfort zone; the guard is the backstop.
MOVE_MAX_PX = 20_000
SCROLL_MAX_STEPS = 1_000
ZOOM_MAX_STEPS = 100


def _clamp_int(tok, limit):
    """Parse a signed int argument and clamp it to ±[limit]. Raises ValueError on
    junk so the caller can drop the packet."""
    return max(-limit, min(limit, int(tok)))


def make_pointer_waker():
    """Return a callable that un-hides the mouse pointer, or None off Windows.

    Windows only draws the pointer while it believes the most recent input came
    from a mouse. pynput moves the cursor with SetCursorPos, and SetCursorPos does
    NOT register as input — measured on Windows 11: it never advances
    GetLastInputInfo, while a SendInput mouse event does, every time.

    So remote movement drove an invisible pointer. Hover states lit up and clicks
    landed exactly where they should; the user simply couldn't see where "there"
    was, and only touching the physical mouse brought it back. It showed up after
    a resume and on a fresh connect — any moment the system's last input wasn't a
    real mouse.

    The fix is a zero-delta SendInput: a genuine mouse event that moves nothing,
    so the pointer reappears without being displaced and without Windows' pointer
    ballistics touching our carefully-tuned deltas (which is why the movement
    itself stays on SetCursorPos rather than switching wholesale to SendInput).

    Only fired when the pointer is actually hidden, so the normal path costs one
    cheap GetCursorInfo and nothing else. CURSOR_SUPPRESSED matters as much as a
    clear CURSOR_SHOWING: that's the specific state Windows 8+ puts the pointer in
    after touch input, which is how a touchscreen laptop lands here mid-session."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hCursor", wintypes.HANDLE),
                        ("ptScreenPos", wintypes.POINT)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

        INPUT_MOUSE = 0
        MOUSEEVENTF_MOVE = 0x0001
        CURSOR_SHOWING, CURSOR_SUPPRESSED = 0x0001, 0x0002

        nudge = INPUT(type=INPUT_MOUSE,
                      mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                    dwFlags=MOUSEEVENTF_MOVE, time=0,
                                    dwExtraInfo=None))
        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        size = ctypes.sizeof(INPUT)

        def wake():
            # Never let this break a MOVE. It is a cosmetic assist, and it runs on
            # the single receive thread, where the cost of a raise is the whole
            # session.
            try:
                if not user32.GetCursorInfo(ctypes.byref(info)):
                    return
                if (info.flags & CURSOR_SHOWING) and not (info.flags & CURSOR_SUPPRESSED):
                    return                      # already visible; nothing to do
                user32.SendInput(1, ctypes.byref(nudge), size)
            except Exception:
                pass

        return wake
    except Exception:
        return None


wake_pointer = make_pointer_waker()

# Monotonic time of the last remote control verb, and how long after one we keep
# telling Windows the machine is in use. Sized to outlast a pause for thought
# mid-task without pinning the display awake once the phone is merely idling.
#
# None, not 0.0: time.monotonic() counts from boot, so a 0.0 sentinel reads as
# "90 seconds ago" for the first 90 seconds of uptime. With the server on autostart
# and a phone auto-reconnecting at launch, that meant asserting the wake hold at
# boot with no remote activity at all.
_last_remote_ts = [None]
REMOTE_AWAKE_S = 90


def make_idle_suppressor():
    """Return a callable(bool) that holds the machine awake, or None off Windows.

    Remote control did not count as activity, for the same root reason the pointer
    stayed invisible: SetCursorPos never advances the system's last-input time. So
    a session driven entirely from the phone let the idle timer run to completion
    underneath it — the display would blank, and a lock-on-wake policy would lock
    the machine, while the user was actively using it.

    The tempting fix is to make every move inject a real mouse event. That works,
    but it works by lying: it fabricates input to move a clock, which also means
    the phone's idle polling would have to be carefully excluded forever or the
    laptop could never sleep again. SetThreadExecutionState is the API that exists
    for exactly this, is what media players use, and says what it means.

    Per-THREAD state, so every call must come from the one that serves the phone —
    which is why serve_loop owns the transitions rather than the packet handler."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002

        def hold(active):
            try:
                # ES_CONTINUOUS alone RESETS to the machine's normal policy; it
                # does not "keep it awake with no display". Dropping the other two
                # flags is how the hold is released.
                kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                    if active else ES_CONTINUOUS)
            except Exception:
                pass

        return hold
    except Exception:
        return None


hold_awake = make_idle_suppressor()


def handle_packet(verb, rest):
    """Drive the machine for one authenticated verb.

    Handlers NEVER reply. serve_loop owns every response and sends it through
    wire.reply(), which encrypts when the client is on the secure wire. This used
    to take (sock, addr) and contained its own PING/VGET answers that wrote
    PLAINTEXT straight to the socket; they were dead code (serve_loop intercepts
    both first) but would have leaked in clear the moment dispatch order changed.
    Removing the parameters means a future handler can't reintroduce that."""
    if verb in CONTROL_VERBS:
        # Any verb that drives the machine is the user being present, whether or
        # not it moves the pointer — a phone used purely as a presenter clicker
        # counts. serve_loop turns this into the actual wake hold.
        _last_remote_ts[0] = time.monotonic()

    if wake_pointer is not None and verb in POINTER_VERBS:
        wake_pointer()

    if verb == "MOVE":
        try:
            a, b = rest.split()
            dx, dy = _clamp_int(a, MOVE_MAX_PX), _clamp_int(b, MOVE_MAX_PX)
        except ValueError:        # wrong arity or non-numeric
            return
        mouse.move(*_dpi_move(dx, dy))

    elif verb == "SCROLL":
        try:
            a, b = rest.split()
            dx, dy = _clamp_int(a, SCROLL_MAX_STEPS), _clamp_int(b, SCROLL_MAX_STEPS)
        except ValueError:
            return
        mouse.scroll(dx, dy)

    elif verb == "ZOOM":          # pinch → ctrl+wheel (zoom in/out in most apps)
        try:
            steps = _clamp_int(rest.split()[0], ZOOM_MAX_STEPS)
        except (IndexError, ValueError):
            return
        keyboard.press(Key.ctrl)
        try:
            mouse.scroll(0, steps)
        finally:
            keyboard.release(Key.ctrl)

    elif verb == "CLICK":
        mouse.click(Button.left, 1)

    elif verb == "RCLICK":
        mouse.click(Button.right, 1)

    elif verb == "MCLICK":
        mouse.click(Button.middle, 1)

    elif verb == "MDOWN":        # drag-lock: hold the left button down
        mouse.press(Button.left)
        _held_buttons.add(Button.left)

    elif verb == "MUP":          # drag-lock: release
        # Unconditional, not "only if tracked": a loop restart forgets the set, and
        # the phone resends MUP, so the OS may still hold a button we don't know of.
        _held_buttons.discard(Button.left)
        mouse.release(Button.left)

    elif verb == "COMBO":
        do_combo(rest)

    elif verb == "ASW":          # app-switch session: next | prev | end
        do_appswitch(rest.strip().lower())

    elif verb == "SYS":
        do_system(rest.strip().lower())

    elif verb == "VOL":
        if set_volume is None:
            return
        try:
            pct = max(0, min(100, int(rest.split()[0])))
        except (IndexError, ValueError):
            return
        set_volume(pct)

    elif verb == "BRIGHT":
        if not brightness_svc.available:
            return
        try:
            pct = max(0, min(100, int(rest.split()[0])))
        except (IndexError, ValueError):
            return
        brightness_svc.set_async(pct)   # offload the slow WMI write off the loop

    elif verb == "MEDIA":
        key = MEDIA_KEYS.get(rest.strip())
        if key is not None:
            keyboard.press(key)
            keyboard.release(key)

    elif verb == "KEY":
        if rest:
            keyboard.type(rest)

    elif verb == "KEYSP":
        key = SPECIAL_KEYS.get(rest.strip().lower())
        if key is not None:
            keyboard.press(key)
            keyboard.release(key)


# ── helpers ───────────────────────────────────────────────────────────────────
# Interface-name fragments that mark an adapter a phone can NEVER reach us on:
# hypervisor/container bridges, tunnels and VPN adapters. A dev laptop routinely
# has several, and any of them can end up owning the default route.
_VIRTUAL_IF_NEEDLES = (
    "vethernet", "hyper-v", "vmware", "virtualbox", "vbox", "docker", "wsl",
    "loopback", "teredo", "isatap", "tailscale", "zerotier", "utun", "tap-",
    "tun", "wintun", "vpn", "forti", "wireguard", "openvpn", "zscaler",
    "globalprotect", "anyconnect", "nordlynx", "expressvpn", "pangp", "bridge",
)
# ...and fragments suggesting the Wi-Fi NIC, where a phone most likely shares our
# subnet. Only used to rank, never to exclude.
_WIFI_IF_NEEDLES = ("wi-fi", "wifi", "wlan", "wlp", "wlo", "en0", "airport")


def _default_route_ip():
    """The address owning the default route — what we have always advertised."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))     # no packet is sent; this just picks a route
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def candidate_ips():
    """Private IPv4 addresses a phone could plausibly reach us on, best first.

    Virtual/tunnel adapters are EXCLUDED, not merely deprioritised — the whole
    point is to answer "is this address one a phone could use?", and an address
    that only sorts last still answers yes. Wi-Fi ranks above wired because that
    is where a phone most often shares our subnet.

    Needs psutil. An empty list (no psutil, or nothing but virtual adapters) just
    means the caller falls back to the default-route probe — i.e. the old
    behaviour, never anything worse."""
    try:
        import psutil
        addrs, stats = psutil.net_if_addrs(), psutil.net_if_stats()
    except Exception:
        return []
    out = []
    for name, entries in addrs.items():
        st = stats.get(name)
        if st is not None and not getattr(st, "isup", False):
            continue                    # down / unplugged
        low = name.lower()
        if any(n in low for n in _VIRTUAL_IF_NEEDLES):
            continue                    # a phone can never reach us here
        rank_wifi = not any(n in low for n in _WIFI_IF_NEEDLES)
        for e in entries:
            if getattr(e, "family", None) != socket.AF_INET:
                continue
            try:
                a = ipaddress.ip_address(e.address)
            except ValueError:
                continue
            # Private LAN space only: a public or link-local address is never the
            # one a phone on the same Wi-Fi is talking to.
            if not a.is_private or a.is_loopback or a.is_link_local:
                continue
            out.append(((rank_wifi, e.address), e.address, name))
    out.sort(key=lambda t: t[0])
    return [(ip, name) for _, ip, name in out]


def lan_ip():
    """The address to put in the QR and the mDNS advertisement.

    The default-route probe alone is wrong more often than it looks: WSL2,
    Hyper-V, Docker, VirtualBox and VPN adapters all install virtual interfaces
    that can own the default route, and when one does we advertise an address no
    phone can reach — while the server still looks perfectly healthy, because
    loopback and the GUI do not care which NIC it is. The phone just times out.

    So the probe stays authoritative WHENEVER it names a real LAN interface (no
    behaviour change on an ordinary machine); we override it only when it points
    somewhere a phone provably cannot follow."""
    probe = _default_route_ip()
    cands = candidate_ips()
    if probe and any(ip == probe for ip, _ in cands):
        return probe
    if cands:
        return cands[0][0]
    return probe or "127.0.0.1"


# ── update check ──────────────────────────────────────────────────────────────
# The ONLY outbound internet request LazeR ever makes. Everything else is LAN-only
# by design (v2.0 removed the off-LAN path entirely), so this is deliberately:
#   * opt-out           (--no-update-check, and the GUI reflects when it's off)
#   * notify-only       (never downloads or installs — it links to the release page)
#   * unauthenticated   (public API, no token; we send no identifying data)
#   * non-blocking      (runs on a daemon thread; a hung request can't stall serving)
# It asks GitHub for the newest release tag and compares it with APP_VERSION.
def parse_version(text):
    """'v2.1.0' / '2.1' → (2, 1, 0). None if it isn't a numeric dotted version.

    Compared as a TUPLE OF INTS, never as strings: lexically "2.0.10" sorts below
    "2.0.9", which would silently stop offering updates after the ninth patch.
    Short versions are padded so 2.1 == 2.1.0 rather than comparing unequal."""
    if not text:
        return None
    s = str(text).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    # Drop any pre-release/build suffix ("2.1.0-rc1", "2.1.0+win") before parsing.
    for sep in ("-", "+", " "):
        s = s.split(sep, 1)[0]
    parts = s.split(".")
    if not parts or len(parts) > 4:
        return None
    out = []
    for p in parts:
        # ASCII digits only. str.isdigit() also accepts "²" and other Unicode
        # digits that int() then rejects — raising on the update thread instead of
        # returning None for a tag we can't read.
        if not (p.isascii() and p.isdigit()):
            return None
        out.append(int(p))
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def is_newer_version(latest, current):
    """True iff [latest] is a strictly newer release than [current]. Unparseable
    input answers False — a garbled tag must never be reported as an update."""
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


RELEASE_READ_CAP = 256_000   # bytes of the releases API response we will read


def fetch_latest_release(url=RELEASES_API, timeout=UPDATE_TIMEOUT_S):
    """The newest release's tag name, or None on any failure.

    Never raises: no network, DNS down, rate limited, GitHub 5xx and malformed JSON
    all mean the same thing here — we simply don't know, so say nothing. A failed
    update check must be invisible, not an error the user has to think about."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                # GitHub rejects requests with no User-Agent outright (403).
                "User-Agent": f"LazeR/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            # Cap the read: we only need one short field, and an unbounded read from
            # a host we don't control is how a hung transfer becomes a memory bug.
            raw = r.read(RELEASE_READ_CAP).decode("utf-8", "ignore")
    except Exception:
        return None
    try:
        data = json.loads(raw)
        tag = data.get("tag_name") if isinstance(data, dict) else None
    except ValueError:
        # A release with long notes or many assets can outgrow the cap, and the
        # cut leaves invalid JSON — which would read as "couldn't check" forever
        # for that release. tag_name sits near the top of GitHub's response, so
        # take it from the prefix we did read.
        import re
        m = re.search(r'"tag_name"\s*:\s*"([^"\\]{1,64})"', raw)
        tag = m.group(1) if m else None
    return tag if isinstance(tag, str) and tag.strip() else None


def check_for_update(url=RELEASES_API, timeout=UPDATE_TIMEOUT_S):
    """(tag, is_newer) for the newest release, or (None, False) if unknown."""
    tag = fetch_latest_release(url, timeout)
    if tag is None:
        return None, False
    return tag, is_newer_version(tag, APP_VERSION)


# Not in the socket module, whose ioctl() takes only three codes: asking it for this
# one raised AttributeError, which was caught, so the setting never took effect.
SIO_UDP_CONNRESET = 0x9800000C


def udp_resets_off(sock):
    """Windows raises WSAECONNRESET (ConnectionResetError) on a UDP socket's *next*
    recv after a send to an endpoint with no listener, which happens routinely when
    we reply to a phone that has just vanished (app killed, Wi-Fi dropped). Turn that
    off for [sock]. True if it took effect; never raises. serve_loop still catches
    the error, in case this couldn't be applied."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        ioctl = ctypes.WinDLL("ws2_32").WSAIoctl
        ioctl.argtypes = [ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p,
                          wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                          ctypes.c_void_p]
        ioctl.restype = ctypes.c_int
        off, got = wintypes.BOOL(False), wintypes.DWORD(0)
        return ioctl(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(off),
                     ctypes.sizeof(off), None, 0, ctypes.byref(got), None, None) == 0
    except Exception:
        return False


def open_socket():
    """Fresh bound UDP socket. Recreated after resume — a socket bound before the
    laptop slept can stop receiving once the NIC cycles, so we rebind to recover."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Exclusive, so a second LazeR can't bind the same port. Not SO_REUSEADDR: on
    # Windows that lets ANY later socket bind 50505 too: two servers then split
    # the phone's packets between them, which looks exactly like a flaky network
    # (constant drops and reconnects on a perfect link). UDP has no TIME_WAIT, so
    # the wake-up rebind — which closes the old socket first — never needed reuse.
    if sys.platform.startswith("win"):
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            sock.setsockopt(socket.SOL_SOCKET, excl, 1)
    udp_resets_off(sock)
    sock.settimeout(1.0)   # idle wake-ups: 1/s is plenty for the resume/idle/rate checks
    try:
        sock.bind((HOST, PORT))
    except OSError:
        sock.close()
        raise
    return sock


# What an owner answers a SHOW with, so a second launch knows whether a window
# actually came forward. A headless (terminal) owner has none to show, and the
# second launch — often a double-clicked windowed exe with no console to print to —
# used to exit silently, looking like the click did nothing.
SHOW_OK = b"SHOWN"
SHOW_HEADLESS = b"HEADLESS"
# Answers to RESUME, so `--resume` can say whether it worked.
RESUME_OK = b"RESUMED"
RESUME_DENIED = b"DENIED"


def singleton_acquire(poke=True):
    """Single-instance guard over a loopback control port.

    Returns ("existing", reply) if another instance is already running (when
    [poke], it was asked to surface its window and [reply] is its answer: SHOW_OK,
    SHOW_HEADLESS, or b"" from an older version that doesn't answer),
    ("owner", lsock) if we are the first instance (lsock is the control listener
    to serve), or ("solo", None) if the guard couldn't be set up.

    [poke]=False (terminal mode) still detects an existing owner but does NOT
    send SHOW — a second headless launch must not pop anyone's window; it only
    wants to claim the port so a later `--resume` can reach us."""
    # Already running? Connect to its control port — and, unless asked to be
    # quiet, ask it to come forward.
    probe = None
    try:
        probe = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=0.5)
    except OSError:
        probe = None
    if probe is not None:
        reply = b"" if poke else None
        try:
            if poke:
                probe.sendall(b"SHOW")
                probe.settimeout(1.0)
                reply = probe.recv(16)
        except OSError:
            pass
        finally:
            try:
                probe.close()
            except OSError:
                pass
        return "existing", reply
    # No one answered — claim the port. No SO_REUSEADDR: on Windows it would let
    # a second instance steal the port and defeat the guard; we want bind to fail.
    try:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.bind(("127.0.0.1", SINGLETON_PORT))
        lsock.listen(1)
        lsock.settimeout(0.5)
        return "owner", lsock
    except OSError:
        return "solo", None


def _reclaim_singleton_port(dead):
    """Re-bind the single-instance port after its socket died. Retries until it
    succeeds or we are stopping (returns None then)."""
    try:
        dead.close()
    except OSError:
        pass
    while not _stop.is_set():
        lsock = None
        try:
            lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            lsock.bind(("127.0.0.1", SINGLETON_PORT))
            lsock.listen(1)
            lsock.settimeout(0.5)
            return lsock
        except OSError:
            if lsock is not None:
                lsock.close()
            _stop.wait(0.5)
    return None


def singleton_serve(lsock, eq, token_fn=None, headless=False):
    """Accept loopback pokes from later launches: SHOW surfaces the window;
    RESUME clears a panic latch (the terminal has no Resume button — --resume).

    RESUME must carry the pairing token ([token_fn] returns the live one). The
    port is loopback, but loopback is shared by every session on the machine —
    fast user switching, RDP — so a bare RESUME let any local account undo the
    panic latch of the user sitting at the laptop. The token file is readable by
    this user only, which is what `--resume` reads it from. The answer
    (RESUME_OK / RESUME_DENIED) goes back before the connection closes."""
    while not _stop.is_set():
        try:
            conn, _ = lsock.accept()
        except socket.timeout:
            continue
        except (ConnectionResetError, ConnectionAbortedError):
            # One queued caller hung up before we accepted it (Windows reports a
            # reset there). The listener is fine; reclaiming it would free the port
            # for a moment, long enough for a launch to slip in as a second owner.
            continue
        except OSError:
            # The listening socket died under us. This used to `break`, closing
            # the port for good, and the next launch found it free, became the
            # owner, and opened a second server next to this one. Seen on a copy
            # that had been up for two days. Take the port back instead.
            lsock = _reclaim_singleton_port(lsock)
            if lsock is None:
                return
            # If a fresh socket fails the same way, retry at this pace rather
            # than spinning a core on close/rebind.
            _stop.wait(0.5)
            continue
        cmd = b""
        resumed = None
        bare = False
        try:
            conn.settimeout(1.0)
            cmd = conn.recv(64)
            if cmd.startswith(b"SHOW"):
                conn.sendall(SHOW_HEADLESS if headless else SHOW_OK)
            elif cmd.startswith(b"RESUME"):
                given = cmd[len(b"RESUME"):].strip()
                bare = not given
                want = (token_fn() if token_fn else "").encode("utf-8")
                resumed = bool(want) and secrets.compare_digest(given, want)
                if resumed:
                    # Safe from this thread: resume_remote only clears threading.Events.
                    resume_remote()
                conn.sendall(RESUME_OK if resumed else RESUME_DENIED)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
        if resumed is True:
            eq.put(("resumed",))
        elif resumed is False and bare:
            # What an older LazeR's --resume sends, and it reports success anyway.
            eq.put(("warn", "Ignored a resume request with no pairing code, as an "
                            "older LazeR's --resume sends. Use this version's "
                            "--resume, or click Resume."))
        elif resumed is False:
            eq.put(("warn", "Ignored a resume request that didn't carry this "
                            "laptop's pairing code."))
        elif cmd.startswith(b"SHOW"):
            eq.put(("show",))
    try:
        lsock.close()
    except OSError:
        pass


def build_uri(ip, token, hostname, key=None):
    base = f"lazer://{ip}:{PORT}/?token={token}&name={hostname}"
    if key is not None:
        # The 256-bit key rides the QR only (shown on the laptop screen), enabling
        # the secure encrypted wire. It is never broadcast over mDNS.
        base += f"&k={key_b64(key)}"
    return base


def load_or_create_token():
    try:
        with open(TOKEN_FILE, "r") as f:
            tok = f.read().strip()
        if tok:
            _harden_existing_secret(TOKEN_FILE, tok)
            return tok
    except OSError:
        pass
    tok = _new_token()
    try:
        write_secret(TOKEN_FILE, tok)
    except OSError as e:
        SECRET_WRITE_ERRORS.append(f"{TOKEN_FILE}: {e}")
    return tok


_MDNS_UNAVAILABLE = [False]   # zeroconf isn't installed — never worth retrying


def start_mdns(ip, hostname):
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except ImportError:
        # Latched so the network watch can tell "this machine will never do mDNS"
        # apart from "that registration failed, try again" — only the latter is
        # worth retrying, and retrying the former reprints this every few seconds.
        _MDNS_UNAVAILABLE[0] = True
        print("[discovery] zeroconf not installed — auto-discovery off. QR + manual still work.")
        return None, None
    try:
        safe = "".join(c for c in hostname if c.isalnum() or c in "-_") or "laptop"
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{safe}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=PORT,
            properties={"name": hostname},
            server=f"{safe}.local.",
        )
        zc = Zeroconf()
        try:
            zc.register_service(info)
        except Exception:
            # Close the engine we just built before giving up. It owns an event
            # loop thread and multicast sockets, and the network watch now retries
            # this on a timer — so leaking one per failed attempt would pile up
            # threads and sockets for as long as the failure persisted.
            try:
                zc.close()
            except Exception:
                pass
            raise
        return zc, info
    except Exception as e:
        print(f"[discovery] mDNS failed ({e}); QR + manual still work.")
        return None, None


def usable_lan_ip():
    """Our LAN address, or None if we don't have one *right now*.

    lan_ip() never fails — it falls back to 127.0.0.1 so the GUI always has
    something to render. That fallback must never reach an mDNS record or a QR:
    a phone that resolves the laptop to 127.0.0.1 dials itself and times out,
    and the bogus record outlives the outage. So callers that PUBLISH an address
    ask here instead, and simply wait when the answer is None."""
    ip = lan_ip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    # Link-local (169.254/16) matters as much as loopback here, and is the likelier
    # of the two right after a wake: Wi-Fi associates, DHCP has not answered yet,
    # and Windows self-assigns an APIPA address. candidate_ips() filters those out,
    # so lan_ip() falls through to the default-route probe and hands one back —
    # publishing it would put an address no phone on the real subnet can reach into
    # mDNS and the QR. The Android side already rejects 169.254 when picking a host
    # to dial, so accepting it here would have the two halves disagreeing.
    if addr.is_loopback or addr.is_unspecified or addr.is_link_local:
        return None
    return ip


def announce_network(net, hostname, wire, emit, ip):
    """(Re)publish [ip]: swap the mDNS record and tell the UI to refresh the QR.

    Tears the old registration down first — zeroconf binds sockets to the
    interfaces it saw at construction, so a Zeroconf built before the NIC cycled
    keeps answering for an address that no longer exists. Returns True if the
    record is live afterwards, so the caller can retry a failed registration
    instead of losing discovery for the rest of the process (the old code stored
    the None and never looked again)."""
    if net is None:
        return False
    old_ip = net.get("ip")
    if net.get("zc"):
        try:
            net["zc"].unregister_service(net["info"])
            net["zc"].close()
        except Exception:
            pass
        net["zc"], net["info"] = None, None
    net["zc"], net["info"] = start_mdns(ip, hostname)
    net["ip"] = ip
    if ip != old_ip:
        emit("netchange", ip, build_uri(ip, wire.token, hostname, wire.key))
    return net.get("zc") is not None


# ── Windows "start with Windows" (HKCU Run key) ───────────────────────────────
# The canonical per-user autostart location. Task Manager → Startup apps and
# Settings → Apps → Startup both list Run-key entries by name, and writing it
# needs no admin, no PowerShell/COM subprocess (which silently failed from the
# windowed exe before) — just winreg. We migrate off any old Startup-folder
# shortcut on first touch.
SCRIPT_PATH = os.path.abspath(__file__)
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "LazeR"


def startup_lnk_path():
    """Path of the legacy LazeR shortcut in the per-user Startup folder."""
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", "LazeR.lnk")


def _self_invocation():
    """How the user would re-run THIS build, for printable hints. The frozen exe
    and a source run need different words, and telling an .exe user to type
    `python remote_server.py` just sends them down a dead end."""
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable)
    return f"python {os.path.basename(SCRIPT_PATH)}"


def _startup_command(minimized=True):
    """Command string to register under Run — handles frozen .exe and .py.

    --minimized starts in the tray: at login nobody is looking yet, and the full
    window put the QR (token AND key) on a possibly unattended screen for up to
    QR_HIDE_AFTER_MS."""
    flag = " --minimized" if minimized else ""
    if getattr(sys, "frozen", False):              # PyInstaller bundle
        return f'"{sys.executable}"{flag}'
    # Source run: prefer pythonw so no console window pops at login.
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand
    return f'"{pyw}" "{SCRIPT_PATH}"{flag}'


def _startup_target_missing(cmd):
    """True when a Run command's quoted program or script no longer exists — the
    exe was moved or re-downloaded elsewhere. Windows then launches nothing at
    login while the toggle still reads On."""
    import re
    paths = re.findall(r'"([^"]+)"', cmd)
    return bool(paths) and any(not os.path.exists(p) for p in paths)


def migrate_startup_command():
    """Repair the Run entry for THIS binary: add --minimized to one an older
    release registered, and re-point one whose program has gone. An entry pointing
    at a different binary that still exists is the user's choice — left alone."""
    if not sys.platform.startswith("win"):
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            cur, _ = winreg.QueryValueEx(k, _RUN_VALUE)
            if cur == _startup_command(minimized=False) or _startup_target_missing(cur):
                winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
    except OSError:
        pass


def startup_enabled():
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _RUN_VALUE)
        return True
    except OSError:
        return False


def _remove_legacy_lnk():
    try:
        lnk = startup_lnk_path()
        if os.path.exists(lnk):
            os.remove(lnk)
    except OSError:
        pass


def set_startup(enabled):
    """Add/remove the HKCU Run entry. Returns True on success (Windows only)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        _remove_legacy_lnk()   # migrate away from the old Startup-folder shortcut
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(k, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        # Confirm the write took, so a failure can't masquerade as success.
        ok = startup_enabled() if enabled else not startup_enabled()
        if not ok:
            set_startup.last_error = "registry write did not persist"
        return ok
    except Exception as e:
        set_startup.last_error = str(e)
        return False


set_startup.last_error = ""


# There's no installer to switch Start with Windows on, and the toggle sits in
# Details, so almost nobody found it and LazeR quietly didn't come back after a
# restart. The exe now turns it on once, on its first run; this marker records
# that it did, so turning it off afterwards sticks.
STARTUP_DEFAULT_MARKER = os.path.join(_DATA_DIR, ".startup_default_done")


def apply_startup_default():
    """Turn Start with Windows on the first time the packaged exe runs for this
    user. Returns True only when it was switched on just now. A source run is a
    developer's, so it's left alone. The marker is written first: a launch that
    can't record the choice must not go on re-enabling it every time."""
    if not (sys.platform.startswith("win") and getattr(sys, "frozen", False)):
        return False
    if os.path.exists(STARTUP_DEFAULT_MARKER):
        return False
    try:
        os.makedirs(os.path.dirname(STARTUP_DEFAULT_MARKER), exist_ok=True)
        with open(STARTUP_DEFAULT_MARKER, "w") as f:
            f.write("1\n")
    except OSError:
        return False
    return not startup_enabled() and set_startup(True)


# ── Windows Firewall: auto-allow inbound UDP so phones can reach us ───────────
# The phone's packets are inbound UDP on PORT. Windows Defender Firewall drops
# them unless an allow rule exists — and loopback bypasses the firewall, so the
# server looks healthy locally while every phone times out. We add the rule for
# the user (one UAC click) instead of making them run netsh by hand. Third-party
# firewalls and VPN LAN-blocking are out of our reach; we detect + warn for those.
#
# profile=any, NOT private,domain. Windows classifies most Wi-Fi as *Public* —
# corporate SSIDs and plenty of home routers included — and a private,domain
# rule is inert there: the port stays shut, every check still reports the rule
# as present, and phones time out with nothing to go on. Opening it is not the
# exposure it looks like, because the port is not the security boundary — every
# datagram is gated by the pairing token or key, over AES-256-GCM with a
# replay-proof challenge-response handshake, so a reachable port without the QR
# is useless.
#
# We deliberately don't add remoteip=localsubnet on top. It would be a tighter
# rule, but it breaks the routed multi-subnet LANs some routers and mesh kits
# hand out (phone and PC on different subnets), trading a silent failure we
# just fixed for a rarer one. The auth gate already carries this.
#
# The name carries a version suffix on purpose: installs from before this fix
# left a private,domain rule under the old name, and matching that name would
# report a healthy firewall that in fact drops every packet. The new name can't
# match those, so the check fails honestly and the fix path replaces them.
FW_RULE_NAME = "LazeR inbound UDP 50505 v2"
_FW_LEGACY_RULE_NAMES = ("LazeR UDP 50505",)
_FW_ADD_ARGS = [
    "advfirewall", "firewall", "add", "rule", f"name={FW_RULE_NAME}",
    "dir=in", "action=allow", "protocol=UDP", f"localport={PORT}",
    "profile=any",
]


def _no_window_flags():
    import subprocess
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_admin():
    if not sys.platform.startswith("win"):
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def firewall_rule_exists():
    """True if our inbound allow rule is present. Read-only — needs no admin.

    Matches on name alone: netsh prints its field labels *and* its profile
    values in the system language, so parsing them to confirm profile=any
    breaks on a non-English Windows. The versioned name carries that guarantee
    instead — only we create it, and only ever with profile=any."""
    if not sys.platform.startswith("win"):
        return True   # we only manage Windows Firewall; assume OK elsewhere
    return _fw_rule_named(FW_RULE_NAME)


# The firewall reads go through the firewall's own COM API (HNetCfg.FwPolicy2)
# in-process. They used to start netsh.exe and powershell.exe — at launch, every
# FW_RECHECK_MS, after every network change and every wake — which is what put
# netsh, PowerShell and their conhost windows under LazeR in Task Manager, and
# PowerShell's start-up alone burns a second of CPU. The subprocess versions stay
# as fallbacks for a machine where the COM route fails outright.


def _fw_policy():
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()        # COM is per-thread; callers run on workers
    except Exception:
        pass
    return comtypes.client.CreateObject("HNetCfg.FwPolicy2", dynamic=True)


def _fw_rule_named_com(name):
    """True/False from the firewall API, or None when it can't be asked."""
    try:
        rules = _fw_policy().Rules
    except Exception:
        return None
    try:
        rules.Item(name)
        return True
    except Exception:
        pass
    # Item() on a missing name doesn't raise ERROR_FILE_NOT_FOUND through late
    # binding: it arrives as a bare DISP_E_EXCEPTION with the real code dropped,
    # so the error can't tell "absent" from "broken". Walking the names can — a
    # few hundred rules take ~10 ms — and keeps netsh out of the steady state.
    try:
        for r in rules:
            try:
                if str(r.Name) == name:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return None


# Group Policy can tell Windows to ignore every locally-created firewall rule on
# a given profile (the "Apply local firewall rules: No" setting). Managed laptops
# commonly ship that for Public. When it bites, our rule is created, `netsh show
# rule` lists it, and Get-NetFirewallRule reports it Enabled / Allow /
# Profile=Any in the ActiveStore — and Windows still drops every packet, because
# on that profile only GPO-delivered rules count. Nothing about the rule itself
# looks wrong, which is exactly why this cost a long afternoon to find: the phone
# times out, the server is healthy over loopback (which bypasses the firewall),
# and mDNS is dead too, so discovery can't even correct a stale saved IP.
#
# Read from the registry, not netsh: it's a plain DWORD with no localized text
# (netsh prints both labels and values in the system language), and it needs no
# elevation. A missing value means the default — local rules are honoured.
#
# TWO roots, because the setting arrives by two different routes and the machine
# this was found on used the second one: classic Group Policy writes under
# SOFTWARE\Policies, while Intune/MDM writes under the firewall service's own
# `Mdm` subkey. Checking only the GPO path found nothing on a laptop that was
# demonstrably dropping the packets — PersistentStore read "NotConfigured" while
# ActiveStore read False, and the RSOP store was empty. Both roots use the same
# profile subkey names (StandardProfile is Private).
_FW_POLICY_ROOTS = (
    r"SOFTWARE\Policies\Microsoft\WindowsFirewall",
    r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\Mdm",
)
_FW_PROFILE_BITS = (("DomainProfile", 0x1), ("StandardProfile", 0x2),
                    ("PublicProfile", 0x4))
_FW_PROFILE_LABELS = {0x1: "Domain", 0x2: "Private", 0x4: "Public"}


def _fw_profiles_ignoring_local_rules():
    """[(bit, label)] for profiles where policy discards local firewall rules."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import winreg
    except Exception:
        return []
    out = []
    for sub, bit in _FW_PROFILE_BITS:
        blocked = False
        for root in _FW_POLICY_ROOTS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    root + "\\" + sub) as k:
                    val, _ = winreg.QueryValueEx(k, "AllowLocalPolicyMerge")
                if int(val) == 0:
                    blocked = True
                    break     # either root saying "no merge" is enough
            except (OSError, TypeError, ValueError):
                continue      # absent ⇒ default ⇒ local rules honoured
            except Exception:
                continue
        if blocked:
            out.append((bit, _FW_PROFILE_LABELS[bit]))
    return out


def _fw_policy_rules_present():
    """True when Group Policy or MDM delivers firewall rules to this PC (either
    root's FirewallRules key holds any). A registry read: no subprocess."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
    except Exception:
        return False
    for root in _FW_POLICY_ROOTS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                root + "\\FirewallRules") as k:
                if winreg.QueryInfoKey(k)[1] > 0:      # value count
                    return True
        except OSError:
            continue
        except Exception:
            continue
    return False


def _fw_current_profile_types():
    """Bitmask of the firewall profiles in force right now (1 Domain / 2 Private
    / 4 Public), or None if we can't tell.

    COM is per-thread — comtypes only initialises the importing thread — so
    CoInitialize() first or this fails every time with "CoInitialize has not been
    called". Same trap the volume endpoint already paid for."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import comtypes
        import comtypes.client
    except Exception:
        return None
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        return int(_fw_policy().CurrentProfileTypes)
    except Exception:
        return None


def firewall_rule_is_inert():
    """(labels, active) when policy stops our rule from taking effect, else None.

    [labels] names the affected profiles; [active] is True when one of them is
    the profile currently in force, i.e. the rule is dead *right now* rather than
    only on network types we aren't attached to. We still report the inactive
    case, because the user's next Wi-Fi may well be one of them."""
    blocked = _fw_profiles_ignoring_local_rules()
    if not blocked:
        return None
    cur = _fw_current_profile_types()
    if cur is not None:
        live = [lbl for bit, lbl in blocked if cur & bit]
        if live:
            return (", ".join(live), True)
    return (", ".join(lbl for _, lbl in blocked), False)


def firewall_advice():
    """One-line explanation when inbound UDP is (or may be) blocked despite the
    rule, or None when there's nothing to say. Probes; call it off the UI thread."""
    return firewall_advice_for(firewall_rule_is_inert())


def firewall_advice_for(inert):
    """The same text from an ALREADY-PROBED firewall_rule_is_inert() result.

    Split out so a caller that already has the value doesn't probe again — the
    GUI computes it on a worker thread, and re-deriving the text on the Tk thread
    would repeat a registry walk and a COM CreateObject there for nothing."""
    if inert is None:
        return None
    labels, active = inert
    if active:
        return (f"Your network is classified {labels}, and this PC's policy "
                f"ignores locally-added firewall rules there — so LazeR's rule "
                f"exists but does nothing. Set the Wi-Fi to Private in "
                f"Settings › Network & internet, or ask IT to allow inbound "
                f"UDP {PORT} by policy.")
    return (f"Note: this PC's policy ignores locally-added firewall rules on "
            f"the {labels} profile. LazeR's rule won't work on a network "
            f"Windows classifies that way — mark such networks Private.")


# Clicking Cancel on Windows' "allow this app?" prompt doesn't just decline: it
# adds an enabled inbound BLOCK rule for the exe, and a block rule beats our
# port allow rule. Everything else then reads healthy while phones time out.
def _self_program():
    return os.path.abspath(sys.executable)


def _cmd_safe(text):
    """True when [text] can sit inside the cmd.exe chain ensure_firewall_rule
    builds: a path holding cmd metacharacters (a folder named "R&D") would split it."""
    return not any(c in text for c in '&|<>^%"')


def _fw_block_rules_com():
    """Names of enabled inbound block rules for this program, or None when the
    firewall API can't be asked. Checks the program first: it's the one test that
    rules out nearly every rule, so most cost a single COM call."""
    prog = os.path.normcase(_self_program())
    try:
        rules = _fw_policy().Rules
        out = []
        for r in rules:
            try:
                app = r.ApplicationName
                if not app or os.path.normcase(os.path.expandvars(app)) != prog:
                    continue
                if r.Direction == 1 and r.Action == 0 and r.Enabled:  # in / block
                    out.append(str(r.Name))
            except Exception:
                continue
        return out
    except Exception:
        return None


def _fw_policy_block_rules():
    """Names of enabled inbound block rules for this program among the rules
    Group Policy or MDM delivered, read from the registry; None when a policy
    rule store exists but can't be read.

    Each value is one rule in the firewall's own text form, e.g.
    "v2.31|Action=Block|Active=TRUE|Dir=In|App=%ProgramFiles%\\x.exe|Name=...|".
    The keys are plain ASCII field names, never localized."""
    try:
        import winreg
    except Exception:
        return []
    prog = os.path.normcase(_self_program())
    out = []
    for root in _FW_POLICY_ROOTS:
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + "\\FirewallRules")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        with k:
            i = 0
            while True:
                try:
                    _, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if not isinstance(val, str):
                    continue
                f = {}
                for part in val.split("|"):
                    key, sep, v = part.partition("=")
                    if sep and key not in f:
                        f[key] = v
                if (f.get("Action", "").lower() != "block"
                        or f.get("Dir", "").lower() != "in"
                        or f.get("Active", "TRUE").upper() != "TRUE"):
                    continue
                app = f.get("App")
                if not app or os.path.normcase(os.path.expandvars(app)) != prog:
                    continue
                out.append(f.get("Name") or "Policy rule")
    return out


def firewall_block_rules():
    """Display names of enabled inbound rules that block this program. Read-only,
    no admin. Empty when there are none or we can't tell."""
    if not sys.platform.startswith("win"):
        return []
    via_com = _fw_block_rules_com()
    # The firewall API lists the machine's own rules; rules that Group Policy or
    # Intune deliver may not be among them, so those are read from the policy
    # store in the registry. Both are in-process; PowerShell is only for a
    # machine where one of them can't be read at all.
    if via_com is not None:
        via_policy = (_fw_policy_block_rules()
                      if _fw_policy_rules_present() else [])
        if via_policy is not None:
            return via_com + [n for n in via_policy if n not in via_com]
    # Fallback. Enum values (Inbound/Block/True) are not localized, unlike netsh's.
    import subprocess
    prog = _self_program().replace("'", "''")
    cmd = ("Get-NetFirewallApplicationFilter -PolicyStore ActiveStore "
           f"-Program '{prog}' -ErrorAction SilentlyContinue | "
           "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
           "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' "
           "-and $_.Enabled -eq 'True' } | ForEach-Object { $_.DisplayName }")
    try:
        r = subprocess.run([win_exe(*POWERSHELL), "-NoProfile", "-NonInteractive",
                            "-Command", cmd],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=15, creationflags=_no_window_flags())
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def _firewall_unblock_args():
    """netsh args dropping every inbound rule tied to this program. Our own rule is
    port-only, so it never matches; an allow rule Windows made for the exe goes too,
    which costs nothing while the port rule stands."""
    return ["advfirewall", "firewall", "delete", "rule", "name=all", "dir=in",
            f"program={_self_program()}"]


def _firewall_purge_legacy_direct():
    """Drop the pre-fix private,domain rules (needs elevation; best effort)."""
    import subprocess
    for name in _FW_LEGACY_RULE_NAMES:
        try:
            subprocess.run(
                [win_exe("netsh.exe"), "advfirewall", "firewall", "delete", "rule",
                 f"name={name}"],
                capture_output=True, text=True, timeout=8,
                creationflags=_no_window_flags())
        except Exception:
            pass


def _firewall_add_direct(blocked=()):
    """Add the rule in-process (succeeds only if already elevated). Also clears
    the old private,domain rule so a dead one isn't left sitting behind, and the
    [blocked] rules when they're ours to delete."""
    import subprocess
    _firewall_purge_legacy_direct()
    unblock = blocked and _can_remove_program_rules()
    for args in ([_firewall_unblock_args()] if unblock else []) + \
            ([] if firewall_rule_exists() else [_FW_ADD_ARGS]):
        try:
            subprocess.run(
                [win_exe("netsh.exe")] + args,
                capture_output=True, text=True, timeout=8,
                creationflags=_no_window_flags())
        except Exception:
            pass
    return firewall_rule_exists() and not firewall_block_rules()


def _run_elevated_and_wait(exe, params, timeout_ms=30000):
    """Launch `exe params` elevated via UAC ('runas'); wait for it to finish.
    Returns False if the user declined the prompt or it couldn't launch."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = SW_HIDE
    try:
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            return False          # ERROR_CANCELLED (1223) ⇒ user said No
        if sei.hProcess:
            ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_ms)
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return True
    except Exception:
        return False


def ensure_firewall_rule(allow_elevate=False):
    """Make sure the inbound rule exists. Returns True if present afterward.
    [allow_elevate] lets us pop a single UAC prompt when we aren't already admin."""
    if not sys.platform.startswith("win"):
        return True
    blocked = firewall_block_rules()
    have = firewall_rule_exists()
    if have and not blocked:
        return True
    if have and not _can_remove_program_rules():
        return False        # a block we can't lift: an admin prompt would change nothing
    if is_admin():
        return _firewall_add_direct(blocked)
    if allow_elevate:
        # Purge + add has to run in one elevated shell, or the user eats a UAC
        # prompt per netsh call. cmd.exe chains them; the rule names are quoted
        # and hold no cmd metacharacters, so `&` only ever splits our commands.
        netsh = f'"{win_exe("netsh.exe")}"'
        parts = [
            f'{netsh} advfirewall firewall delete rule name="{name}" >nul 2>nul'
            for name in _FW_LEGACY_RULE_NAMES
        ]
        if blocked and _can_remove_program_rules():
            parts.append(f'{netsh} advfirewall firewall delete rule name=all dir=in '
                         f'program="{_self_program()}" >nul 2>nul')
        if not have:                     # netsh add happily makes a duplicate
            parts.append(netsh + " " + " ".join(
                f'name="{FW_RULE_NAME}"' if a.startswith("name=") else a
                for a in _FW_ADD_ARGS))
        # cmd.exe /c strips one pair of outer quotes when the line starts with one,
        # so wrap the whole chain in an extra pair to keep netsh's path quoted.
        _run_elevated_and_wait(win_exe("cmd.exe"), '/c "' + " & ".join(parts) + '"')
        return firewall_rule_exists() and not firewall_block_rules()
    return False


def _firewall_remove_ours():
    """Delete LazeR's rules (ours, the legacy ones, any Windows made for the exe) in
    one elevated shell. Returns True when none of ours is left."""
    import subprocess
    netsh = f'"{win_exe("netsh.exe")}"'
    # The legacy rules count too: an install from before the v2 name left one.
    # One cheap read, so a clean machine never sees a UAC prompt for nothing.
    present = [n for n in (FW_RULE_NAME,) + _FW_LEGACY_RULE_NAMES if _fw_rule_named(n)]
    ours = _can_remove_program_rules() and bool(firewall_block_rules())
    if not present and not ours:
        return True
    parts = [f'{netsh} advfirewall firewall delete rule name="{name}" >nul 2>nul'
             for name in present]
    if ours:
        parts.append(f'{netsh} advfirewall firewall delete rule name=all dir=in '
                     f'program="{_self_program()}" >nul 2>nul')
    chain = '/c "' + " & ".join(parts) + '"'
    if is_admin():
        # One string, not a list: list2cmdline would turn the inner quotes into \"
        # which cmd.exe doesn't understand.
        try:
            subprocess.run(f'"{win_exe("cmd.exe")}" {chain}', capture_output=True,
                           timeout=20, creationflags=_no_window_flags())
        except Exception:
            pass
    else:
        _run_elevated_and_wait(win_exe("cmd.exe"), chain)
    # Re-read everything we meant to delete: the current rule alone would report
    # success for a declined prompt on a machine holding only a legacy rule.
    return (not any(_fw_rule_named(n) for n in present)
            and not (ours and firewall_block_rules()))


def _fw_rule_named(name):
    if not sys.platform.startswith("win"):
        return False
    via_com = _fw_rule_named_com(name)
    if via_com is not None:
        return via_com
    import subprocess
    try:
        r = subprocess.run(
            [win_exe("netsh.exe"), "advfirewall", "firewall", "show", "rule",
             f"name={name}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=6,
            creationflags=_no_window_flags())
        return r.returncode == 0 and "No rules match" not in r.stdout
    except Exception:
        return False


def _can_remove_program_rules():
    """True when rules tied to this program are ours to delete: only a frozen
    LazeR.exe. From source the program is python.exe, and deleting its rules would
    strip every other Python app's firewall exceptions. No query, so it's safe on
    the UI thread — callers already hold firewall_block_rules()' answer."""
    return getattr(sys, "frozen", False) and _cmd_safe(_self_program())


def uninstall():
    """Undo everything LazeR set up for this user, for --uninstall. Returns the
    report; sets uninstall.failed when something was left behind."""
    uninstall.failed = False
    try:
        socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=0.5).close()
        uninstall.failed = True
        return ("[uninstall] LazeR is still running. Quit it from its tray icon "
                "(right-click → Quit), then run --uninstall again.")
    except OSError:
        pass                             # nothing running: carry on
    done, left = [], []
    if sys.platform.startswith("win"):
        _remove_legacy_lnk()
        if not startup_enabled() or set_startup(False):
            done.append("the Start with Windows entry")
        else:
            left.append("the Start with Windows entry (Task Manager → Startup apps)")
        if _firewall_remove_ours():
            done.append("the firewall rule")
        else:
            left.append(f"the firewall rule “{FW_RULE_NAME}” (the admin prompt was "
                        "declined — run --uninstall again and choose Yes)")
    stale = remove_data_dir()
    for name in (".lazer_token", ".lazer_key"):   # a pair from before 2.4
        try:
            os.remove(os.path.join(_APP_DIR, name))
        except OSError:
            pass
    if stale:
        left.append("the saved pairing in " + _DATA_DIR)
    else:
        done.append("the saved pairing")
    uninstall.failed = bool(left)
    msg = "[uninstall] removed " + ", ".join(done) + "." if done else "[uninstall]"
    if left:
        msg += "\n[uninstall] couldn't remove " + "; ".join(left) + "."
    else:
        msg += " You can delete the LazeR program now."
    return msg


uninstall.failed = False


_VPN_NEEDLES = ("vpn", "forti", "wireguard", "openvpn", "tap-", "tun",
                "zscaler", "globalprotect", "anyconnect", "cisco", "nordlynx",
                "expressvpn", "pangp", "wintun", "sonicwall", "netextender",
                "sophos", "netskope", "checkpoint", "ipsec", "softether")


def _win_adapter_descriptions():
    """{connection name: driver description} for Windows NICs, from the registry.

    psutil only knows the *connection* name, and Windows names VPN adapters just
    like ordinary ones — an active Fortinet SSL VPN shows up as plain
    "Ethernet 4". The vendor string exists only in the driver description, so
    matching names alone missed a live tunnel entirely, which made detect_vpn()
    effectively dead on Windows and left the firewall hint silent about a VPN
    that was right there. Registry read: no admin, no extra dependency."""
    out = {}
    if not sys.platform.startswith("win"):
        return out
    try:
        import winreg
    except Exception:
        return out
    net_class = r"{4D36E972-E325-11CE-BFC1-08002BE10318}"
    class_key = r"SYSTEM\CurrentControlSet\Control\Class" "\\" + net_class
    net_key = r"SYSTEM\CurrentControlSet\Control\Network" "\\" + net_class
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_key) as cls:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break                      # no more subkeys
                i += 1
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        desc = winreg.QueryValueEx(k, "DriverDesc")[0]
                        guid = winreg.QueryValueEx(k, "NetCfgInstanceId")[0]
                    with winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE,
                            net_key + "\\" + guid + r"\Connection") as c:
                        name = winreg.QueryValueEx(c, "Name")[0]
                except OSError:
                    continue                   # non-adapter subkey, or no name
                out[str(name)] = str(desc)
    except Exception:
        return out
    return out


def detect_vpn():
    """Best-effort: label of an active VPN-ish interface, or None. A VPN that
    full-tunnels or blocks LAN can stop phones reaching us even past the firewall;
    we can't override that, but we can tell the user where to look.

    Matches the driver description as well as the connection name, and returns
    whichever one matched so the message names something the user can recognise
    ("Fortinet SSL VPN Virtual Ethernet Adapter", not "Ethernet 4")."""
    try:
        import psutil
        stats = psutil.net_if_stats()
    except Exception:
        return None
    descs = _win_adapter_descriptions()
    for name, st in stats.items():
        if not getattr(st, "isup", False):
            continue
        if any(n in name.lower() for n in _VPN_NEEDLES):
            return name
        desc = descs.get(name, "")
        if desc and any(n in desc.lower() for n in _VPN_NEEDLES):
            return desc
    return None


def show_qr(uri):
    try:
        import qrcode
    except ImportError:
        print("[qr] pip install qrcode — scan disabled.")
        print(f"     URI: {uri}")
        return
    try:
        import io
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        text = buf.getvalue()
        try:
            sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.buffer.flush()
        except (AttributeError, ValueError):
            sys.stdout.write(text)
    except Exception:
        print(f"     URI (QR failed): {uri}")


# ── server thread (used in GUI mode) ─────────────────────────────────────────
def _chars(n):
    return f"{n} char" if n == 1 else f"{n} chars"


_ACTION_LABELS = {
    "CLICK":  lambda r: ("Left click", "act"),
    "RCLICK": lambda r: ("Right click", "act"),
    "MCLICK": lambda r: ("Middle click", "act"),
    "MDOWN":  lambda r: ("Drag start", "act"),
    "MUP":    lambda r: ("Drag end", "act"),
    "ZOOM":   lambda r: (f"Zoom {'in' if r.strip().lstrip('-').isdigit() and int(r) > 0 else 'out'}", "act"),
    "MEDIA":  lambda r: (f"Media · {r.strip()}", "act"),
    # KEY carries the user's actual keystrokes, which used to be echoed into the
    # activity feed (first 24 chars, in quotes) — on the laptop screen, which is
    # exactly what an over-the-shoulder viewer or a screen share can see. That was
    # printing the very thing AES-GCM is there to protect. Log the shape, never the
    # content. (CLIP carried the same risk for pasted text; the verb is gone now.)
    "KEY":    lambda r: (f"Type · {_chars(len(r))}", "act") if r else None,
    "KEYSP":  lambda r: (f"Key · {r.strip()}", "act"),
    "COMBO":  lambda r: (f"Shortcut · {r.strip()}", "act"),
    "ASW":    lambda r: (f"Switch app · {r.strip()}", "act"),
    "SYS":    lambda r: (f"System · {r.strip()}", "act"),
    "VOL":    lambda r: (f"Volume → {r.strip()}%", "act"),
    "BRIGHT": lambda r: (f"Brightness → {r.strip()}%", "act"),
}


def serve_loop(wire, emit, net, hostname):
    """The one UDP loop, used by both GUI and terminal modes.

    [wire] handles auth/encrypt/replay. [emit](kind, *args) reports events
    (GUI → queue, terminal → print). [net] is a shared {"ip","zc","info"} dict
    for mDNS re-announce after resume."""
    sock = open_socket()
    emit("serving")
    client = None
    last_tick = time.time()
    last_pkt = last_tick     # wall-clock of the last accepted packet from the pinned phone
    bad = 0
    bad_win = last_tick
    warned = False
    # Per-window "already reported" sets, so a repeating condition warns once
    # instead of flooding the activity feed. Both clear with the rate window.
    handler_errors = set()   # verbs whose handler raised
    blocked_seen = set()     # addresses turned away while a phone is already paired
    plaintext_hinted = False  # explained a secure-only refusal this window
    key_hinted = False        # explained a wrong-key refusal this window
    refusals = RefusalLimiter()
    _client_connected.clear()
    # Normalize the wake hold. This is per-thread state and serve_forever restarts
    # us on the SAME thread, so a loop that died mid-session would otherwise leave
    # the display pinned awake, with awake_held=False here and nothing to release it.
    if hold_awake is not None:
        hold_awake(False)

    awake_held = False        # are we currently telling Windows the machine is in use
    # zeroconf's register/unregister are synchronous and take on the order of a
    # second (probe + repeated announcements). Running them inline would stall the
    # ONE thread serving the phone for that long, and the watch now fires them on
    # every wake and every roam — precisely when the phone is mid-handshake. Five
    # missed replies at 500ms is all its watchdog needs to declare the link dead,
    # so a stall here would manufacture the very reconnect this change prevents.
    # Hand the work to a thread and let the loop keep answering.
    announce_busy = threading.Event()

    def announce_async(ip):
        if announce_busy.is_set():
            return                      # one in flight; the watch will re-check
        announce_busy.set()

        def work():
            try:
                if announce_network(net, hostname, wire, emit, ip):
                    emit("log", f"Reachable at {ip}")
            except Exception as e:
                emit("log", f"Discovery announce failed: {type(e).__name__}: {e}")
                emit("warn", "Phones can't find this laptop on the network right now. "
                             "Scanning the QR still works, and LazeR keeps retrying.")
            finally:
                announce_busy.clear()

        threading.Thread(target=work, daemon=True).start()

    net_checked = last_tick   # wall-clock of the last own-address check
    net_pending = False       # our published address is known-stale; keep trying
    net_pending_since = 0.0   # when that became true, to bound the post-wake wait

    def drop_client():
        nonlocal client
        if client is not None:
            release_held_input()
            client = None
            _client_connected.clear()
            emit("disconnected")
        # Whether or not an address was pinned, forget the crypto session that went
        # with it. Leaving it behind meant a phone that vanished kept a sid/counter
        # watermark alive on our side, and the next session had to talk its way past
        # the leftovers instead of starting clean.
        wire.unpin_client()

    try:
        while not _stop.is_set():
            now = time.time()
            mono = time.monotonic()

            # Regenerate ran (see _swap_secrets): drop the pinned phone now.
            if wire.rotated:
                wire.rotated = False
                drop_client()

            # Resume detection: a tick gap far longer than the 1s recv timeout means
            # the process was frozen (laptop slept). Rebind + re-announce so a phone
            # can reach us again without a restart.
            if now - last_tick > RESUME_GAP_S:
                try:
                    sock.close()
                except OSError:
                    pass
                # Retry the rebind until it succeeds: on wake the old port may not be released
                # yet, or the NIC may still be coming up. Giving up here would leave `sock`
                # closed and the loop permanently deaf.
                sock = None
                rebind_fail = 0
                while not _stop.is_set():
                    try:
                        sock = open_socket()
                        break
                    except OSError:
                        rebind_fail += 1
                        if rebind_fail == 1:
                            emit("log", "Waiting to rebind the socket after sleep…")
                        time.sleep(1.0)
                if sock is None:      # _stop was set while we were retrying — shutting down
                    break
                # The retry may have taken several seconds; refresh `now` so the stale
                # value doesn't spuriously re-trigger the resume path or idle-drop below.
                now = time.time()
                # Keep the phone pinned: HELLO/AUTH are accepted from any source, so a phone
                # that rebuilt its session re-pairs anyway, and one whose socket survived (both
                # devices slept) resumes seamlessly. But give it only a short grace, not a full
                # CLIENT_IDLE_S, so a phone that left is reported gone promptly.
                release_held_input()
                last_pkt = time.time() - max(0, CLIENT_IDLE_S - POST_WAKE_GRACE_S)
                # Hooks are commonly revoked across a wake or a lock, and key-ups during
                # one are never delivered — so re-install and forget any held keys.
                if _input_guard[0] is not None:
                    _input_guard[0].rearm()
                # A wake often lands with the panic/pause latch set from whatever the user
                # touched on the way in (or before sleeping). Clearing the SOFT pause is
                # safe — it re-arms on the next physical event 2s later. The panic latch is
                # deliberate, so it stays.
                _remote_paused.clear()
                # Re-announce, but not with lan_ip() now: a wake usually beats the Wi-Fi
                # association (see usable_lan_ip). Mark the address stale; the watch below
                # publishes it once there's a real one.
                net_pending, net_pending_since, net_checked = True, now, 0.0
                emit("log", "Woke from sleep — restoring the network…")
                emit("woke")
            last_tick = now

            # Own-address watch. Runs on every tick, not only after a sleep gap: roaming
            # to another SSID or a DHCP change moves us with no gap at all, and the mDNS
            # record and QR would otherwise keep pointing at the old address for the rest
            # of the process. [net_pending] also drives the post-wake retry — we may need
            # several passes before the NIC hands us a usable address.
            if (net is not None and not announce_busy.is_set()
                    and (net_pending or now - net_checked > NET_WATCH_S)):
                net_checked = now
                ip_now = usable_lan_ip()
                if ip_now is None:
                    # No LAN address yet (still associating, or offline): publish nothing,
                    # look again next tick.
                    if net_pending and now - net_pending_since > NET_SETTLE_S:
                        # Stop the every-tick retry and say so once. The periodic watch
                        # keeps running at its normal cadence, so plugging the network
                        # back in later still re-announces without an app restart.
                        net_pending = False
                        emit("warn", "Still no network address after waking — "
                                     "reconnect Wi-Fi and LazeR will re-announce itself.")
                elif (ip_now != net.get("ip") or net_pending
                        or (net.get("zc") is None and not _MDNS_UNAVAILABLE[0])):
                    # Re-register on a changed address, after a wake (the zeroconf
                    # sockets are bound to interfaces that just cycled), or when a
                    # previous attempt failed. A machine
                    # with no zeroconf installed is a different thing entirely and must
                    # NOT be retried: it would reprint the notice every few seconds.
                    # Stop the fast retry and fall back to the normal NET_WATCH_S
                    # cadence — the clause above re-tries a failed registration on the
                    # next pass without spinning on this one.
                    net_pending = False
                    announce_async(ip_now)

            # The phone pings ~every 1.5s; prolonged silence means it left without a
            # BYE (app killed, Wi-Fi dropped). Reflect that instead of showing it
            # "connected" forever — so the status is always truthful.
            if client is not None and now - last_pkt > CLIENT_IDLE_S:
                drop_client()

            if now - bad_win > RATE_WINDOW_S:
                bad, bad_win, warned = 0, now, False
                handler_errors.clear()
                blocked_seen.clear()
                plaintext_hinted = False
                key_hinted = False
                refusals.reset()
                wire.sweep_challenges(mono)

            # Local takeover auto-resume: once physical input has been quiet for the
            # grace period (and no panic latch), let the remote drive again.
            if (_remote_paused.is_set() and not _panic_latched.is_set()
                    and (mono - _last_physical_ts[0]) > PHYSICAL_RESUME_GRACE_S):
                _remote_paused.clear()
                emit("resumed")

            # Hold the machine awake while the phone is actively driving it (see
            # make_idle_suppressor). Only transitions are pushed, and releasing on quiet
            # lets an idle laptop sleep normally.
            if hold_awake is not None:
                want_awake = want_wake_hold(client, _last_remote_ts[0], mono)
                if want_awake != awake_held:
                    hold_awake(want_awake)
                    awake_held = want_awake

            sock.settimeout(1.0)   # 1/s idle wake-ups drive the resume/idle/rate checks

            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows: a prior send hit an endpoint with no listener (a phone that
                # left). Not fatal — the socket is fine; keep serving.
                continue
            except OSError as e:
                if getattr(e, "winerror", None) == WSAEMSGSIZE:
                    # Windows reports an oversized datagram as an error (the excess is
                    # discarded). It is one bad packet, not a sick socket, so it must not
                    # take the 100ms pause below: that made ~10 junk packets a second
                    # enough to keep the loop mostly asleep.
                    bad += 1
                    continue
                # A transient network error (e.g. host/net-unreachable surfacing on the
                # next recv after a send to a departed phone) must NOT tear the loop down
                # for good. Pause briefly and keep serving; a real dead socket is handled
                # by the sleep/resume rebind above.
                time.sleep(0.1)
                continue
            if not data:
                continue

            with wire.lock:
                res = wire.parse(data, addr, client)
            if res is None:
                if wire.stale_rejected:
                    wire.stale_rejected = False
                    continue
                bad += 1
                reason = None
                if wire.plaintext_refused:
                    wire.plaintext_refused = False
                    reason = REFUSE_SECURE_REQUIRED
                    if not plaintext_hinted:
                        plaintext_hinted = True
                        emit("warn", "A phone tried to pair with the typed code, but "
                                     "Require encryption is on. Scan the QR instead, or "
                                     "turn off Require encryption under Show details.")
                elif wire.key_refused:
                    wire.key_refused = False
                    reason = REFUSE_BAD_KEY
                    if not key_hinted:
                        key_hinted = True
                        emit("warn", f"{addr[0]} is using an old pairing (or sent a "
                                     "corrupt packet). If it's your phone, scan the QR "
                                     "again.")
                if reason is not None and refusals.allow(addr, mono):
                    # Tell the phone WHY, so it can say "scan the new QR" instead of
                    # blaming the firewall. See send_refusal for what it reveals.
                    send_refusal(sock, addr, reason)
                if bad > RATE_MAX_BAD and not warned:
                    warned = True
                    if wire.aes is not None:
                        # Suspend plaintext ACCEPTANCE for a window (PLAINTEXT_SUSPEND_S):
                        # brute force only exists on the plaintext wire, so pausing exactly that
                        # path makes guessing futile while QR-paired phones are untouched.
                        # Global, not per-source: UDP source addresses are spoofable, and
                        # per-IP blocking would let a flooder frame the real phone.
                        wire.pt_suspended_until = mono + PLAINTEXT_SUSPEND_S
                        emit("warn",
                             "High rate of rejected packets — possible brute-force / flood; "
                             f"manual-code pairing paused {int(PLAINTEXT_SUSPEND_S)}s "
                             "(QR pairing unaffected)")
                    else:
                        # No key (or no `cryptography`) ⇒ the plaintext wire is the ONLY
                        # way in, so pausing it would lock the user out entirely rather
                        # than push them to the QR. Warn and keep serving: a deliberate
                        # availability-over-throttling trade, bounded by a 36^6 (~2.2e9)
                        # token space that is only reachable from the LAN.
                        emit("warn", "High rate of rejected packets — possible brute-force / flood")
                continue
            verb, rest, secure = res

            if verb == "HELLO":
                if secure:
                    # Don't pin on HELLO — it's replayable. Answer with a one-time
                    # challenge; only an AUTH echoing it (which needs the key to seal)
                    # pins control. Defeats captured-session replay by a keyless attacker.
                    # [rest] is the phone's own nonce, echoed so it can tell our reply
                    # from a replayed one.
                    wire.issue_challenge(sock, addr, mono, cnonce=rest)
                else:
                    # v1 plaintext HELLO (legacy, trusted-LAN only; never reachable when
                    # remote access forces secure-only). Token match already gated it in
                    # parse; pin directly. A plaintext re-pin is logged as a warning.
                    repin = client is not None and addr != client
                    release_held_input()
                    client = addr
                    last_pkt = now
                    wire.commit_hello(False)
                    _client_connected.set()
                    emit("connected", f"{addr[0]}:{addr[1]}", False)
                    if repin:
                        emit("warn", f"Control moved to {addr[0]}:{addr[1]} over PLAINTEXT "
                                     "— turn on Require encryption to prevent takeovers")
                    wire.reply(sock, addr, "OK")
                continue

            if verb == "AUTH":
                # Second handshake leg: pins control iff it echoes the fresh challenge
                # we just issued to this address (proves key possession AND freshness).
                with wire.lock:
                    authed = secure and wire.verify_challenge(addr, rest, mono)
                    if authed:
                        wire.commit_hello(True)     # baseline = this AUTH's sid/counter
                if authed:
                    release_held_input()
                    client = addr
                    last_pkt = now
                    _client_connected.set()
                    emit("connected", f"{addr[0]}:{addr[1]}", True)
                    for text in wire.ok_texts():
                        wire.reply(sock, addr, text)
                continue

            if verb == "BYE":
                if addr == client:
                    drop_client()
                continue

            if addr != client:
                # Authenticated but not from the pinned phone. Reachable on the v1
                # plaintext wire, where a token match is the only gate — so this is a
                # second phone (or someone who learned the code) trying to take over a
                # live session. On the secure (L3) wire the same attempt is rejected
                # inside wire.parse (wrong
                # source for the pinned sid) and lands in the `bad` counter instead,
                # which is what raises the brute-force/flood warning.
                if client is not None and addr not in blocked_seen:
                    blocked_seen.add(addr)
                    emit("blocked", f"{addr[0]}:{addr[1]}")
                continue
            last_pkt = now   # pinned phone is alive — keep the idle timer fed

            # PING/VGET answer over the same (encrypted) wire the client used.
            if verb == "PING":
                wire.reply(sock, addr, "PONG")
                continue
            if verb == "VGET":
                if get_volume is not None:
                    # Belt and braces around the backend: make_volume's Windows path now
                    # re-acquires a stale endpoint by itself, but this branch runs ABOVE
                    # the handler guard below, so anything that still escapes here would
                    # take the whole receive loop with it. A missing answer costs the
                    # phone one poll — it re-probes with PING and stays connected.
                    try:
                        vol = get_volume()
                    except Exception:
                        vol = None
                    if vol is not None:
                        # Its own guard: a failed mute read costs the mute field,
                        # not the volume answer.
                        try:
                            muted = getattr(get_volume, "muted", lambda: None)()
                        except Exception:
                            muted = None
                        # Same for the mic: a failed read only hides its button.
                        try:
                            mic = get_mic_muted() if get_mic_muted is not None else None
                        except Exception:
                            mic = None
                        wire.reply(sock, addr,
                                   volume_reply(vol, muted, mic, phone_mic.phone_word()))
                continue
            if verb == "AUD":
                # The phone's voice, 50 packets a second: no activity-log line, no
                # handler dispatch, and its own guard (this is above the handler
                # guard). Encrypted wire only. Still flows while the user has
                # taken over the laptop's mouse — talking while typing is the
                # point — but stops on a panic, which means "stop the phone".
                if secure and not _panic_latched.is_set():
                    try:
                        pcm = decode_audio(rest)
                        if pcm:
                            phone_mic.feed(pcm)
                    except Exception:
                        pass
                continue
            if verb == "BGET":
                if brightness_svc.available:
                    wire.reply(sock, addr, f"BRI {brightness_svc.get_cached()}")   # cached: never blocks the loop
                continue

            # Local input wins: while the user has taken over (or after a panic),
            # every machine-driving verb is dropped on the floor.
            if verb in CONTROL_VERBS and (_remote_paused.is_set() or _panic_latched.is_set()):
                continue

            # A verb handler — or an activity-log label, which the GUI emitter
            # evaluates inline on THIS thread — must never take the receive loop down
            # with it. This is the single thread serving every phone: an unhandled
            # raise here left the server permanently deaf, window still green and
            # "server running" still lit, every packet ignored until a manual restart.
            # The GUI's event poll already guards itself for exactly this reason.
            try:
                emit("action", verb, rest)
                handle_packet(verb, rest)
            except Exception as e:
                if verb not in handler_errors:
                    handler_errors.add(verb)
                    what = _ACTION_LABELS.get(verb)
                    try:
                        what = (what(rest) or (verb,))[0] if what else verb
                    except Exception:
                        what = verb
                    emit("log", f"{verb} failed: {type(e).__name__}: {e}")
                    emit("warn", f"“{what}” doesn't work on this laptop, so LazeR "
                                 "skipped it. Everything else still works.")
    finally:
        # Runs on a clean shutdown AND on a crash serve_forever will restart us from:
        # release a held button, clear the connected state and the wake hold, and close
        # the socket now — with an exclusive bind, one left for the GC would block the
        # restart's rebind.
        release_held_input()
        if client is not None:
            _client_connected.clear()
            try:
                wire.unpin_client()
                emit("disconnected")
            except Exception:
                pass
        if hold_awake is not None and awake_held:
            try:
                hold_awake(False)
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def serve_forever(wire, emit, net, hostname):
    """Run serve_loop, and put it back on its feet if it ever falls over.

    serve_loop is the ONLY thread that talks to the phone, and it was started
    bare — on the GUI path, as a daemon thread with nothing above it. Any escaping
    exception therefore ended the session permanently while every visible sign said
    the server was fine: window open, status dot green, "server running" lit. The
    user's only recourse was to close the app and pair again.

    The specific escapes found have been fixed at source (a stale audio endpoint on
    VGET, a send to a departed phone while answering a HELLO), but "the receive
    thread cannot die unnoticed" should not depend on having found them all. So:
    log it, wait a moment, and rebuild. serve_loop opens a fresh socket each time it
    starts, so a restart is also the correct recovery for a socket that has gone bad.
    Session state is not carried over — the phone re-handshakes within seconds,
    which is a blink next to needing a human to restart the app."""
    failures = 0
    while not _stop.is_set():
        started = time.monotonic()
        try:
            serve_loop(wire, emit, net, hostname)
            return                      # clean exit — _stop was set
        except Exception as e:
            # Count only crashes in a row, or a stranger's packets could keep every later
            # restart at the full 5s backoff.
            if time.monotonic() - started > LOOP_HEALTHY_S:
                failures = 0
            failures += 1
            emit("loopdown")
            emit("log", f"Receive loop crashed: {type(e).__name__}: {e}")
            emit("warn", "LazeR's connection hit a problem and restarted itself. "
                         "The phone reconnects on its own.")
            # Back off a little so a hard-failing loop (e.g. the port genuinely
            # taken) doesn't spin the CPU, but stay responsive enough that the
            # phone's own retry finds us again within a few seconds.
            _stop.wait(min(1.0 * failures, 5.0))


# ── GUI ───────────────────────────────────────────────────────────────────────
# Material 3 tonal roles from a periwinkle seed (the brand colour), in a dark and
# a light scheme. The window follows Windows' app theme (Settings › Personalization
# › Colors) at launch. Status colours sit outside M3's roles: "ok" (green) and
# "warn" (amber) each come with a container tone for chips and banners.
_DARK = {
    "surface":      "#121318", "surface_low":   "#1A1B21",
    "container":    "#1E1F25", "container_hi":  "#292A2F",
    "container_top": "#34343A",
    "on_surface":   "#E3E1E9", "on_variant":    "#C6C5D0",
    "outline":      "#90909A", "outline_var":   "#46464F",
    "primary":      "#B9C3FF", "on_primary":    "#212C61",
    "primary_ctr":  "#394379", "on_primary_ctr": "#DDE1FF",
    "secondary_ctr": "#424659", "on_secondary_ctr": "#DFE1F9",
    "ok":           "#8AD7A8", "ok_ctr":        "#1C4B30", "on_ok_ctr": "#A6F3C2",
    "warn":         "#FFB77C", "warn_ctr":      "#4D2B0F", "on_warn_ctr": "#FFDCC2",
    "qr_dark":      "#121318",
}
_LIGHT = {
    "surface":      "#FBF8FF", "surface_low":   "#F5F2FA",
    "container":    "#EFEDF4", "container_hi":  "#E9E7EF",
    "container_top": "#E3E1E9",
    "on_surface":   "#1B1B21", "on_variant":    "#46464F",
    "outline":      "#767680", "outline_var":   "#C6C5D0",
    "primary":      "#515B92", "on_primary":    "#FFFFFF",
    "primary_ctr":  "#DDE1FF", "on_primary_ctr": "#394379",
    "secondary_ctr": "#E0E1F9", "on_secondary_ctr": "#424659",
    "ok":           "#2E6A44", "ok_ctr":        "#B1F1C3", "on_ok_ctr": "#0F5130",
    "warn":         "#8A5100", "warn_ctr":      "#FFDCC2", "on_warn_ctr": "#6A3C00",
    "qr_dark":      "#121318",
}


def _scheme(light):
    """The palette for a theme, plus the short names older code paths use."""
    c = dict(_LIGHT if light else _DARK)
    c.update(bg=c["surface"], card=c["container"], card2=c["container_hi"],
             border=c["outline_var"], fg=c["on_surface"], dim=c["on_variant"],
             faint=c["outline"], accent=c["primary"], accent2=c["primary_ctr"])
    return c


_C = _scheme(False)


def _windows_prefers_light():
    """Windows' "Choose your app mode" (AppsUseLightTheme). Dark when unknown."""
    if not sys.platform.startswith("win"):
        return os.environ.get("LAZER_THEME") == "light"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes"
                            r"\Personalize") as k:
            return int(winreg.QueryValueEx(k, "AppsUseLightTheme")[0]) == 1
    except Exception:
        return False


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _blend(base, over, amount):
    """[over] laid on [base] at [amount] opacity — M3's state layers (8% hover,
    12% press) as a flat colour, since Tk has no alpha on widget backgrounds."""
    b, o = _hex_rgb(base), _hex_rgb(over)
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * amount) for x, y in zip(b, o))


def _style_title_bar(root, light):
    """Windows 11 chrome: a title bar painted in the window's own surface colour
    (so it reads as one sheet, like Settings), dark caption buttons in dark mode,
    and rounded corners. Each call is a no-op where Windows doesn't know it."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwm = ctypes.windll.dwmapi

        def put(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        put(20, 0 if light else 1)            # DWMWA_USE_IMMERSIVE_DARK_MODE
        put(33, 2)                            # DWMWA_WINDOW_CORNER_PREFERENCE: round
        r, g, b = _hex_rgb(_C["surface"])
        put(35, (b << 16) | (g << 8) | r)     # DWMWA_CAPTION_COLOR (COLORREF)
        r, g, b = _hex_rgb(_C["outline_var"])
        put(34, (b << 16) | (g << 8) | r)     # DWMWA_BORDER_COLOR
    except Exception:
        pass


# How often the window re-checks the firewall on its own, on top of the checks
# after a network change and a wake. Moving to a Public or policy-locked network
# is the most common real-world failure, and it can happen with no address change.
FW_RECHECK_MS = 5 * 60 * 1000


# Screen pixels per layout pixel. The window's paddings and drawings are written
# for 96 DPI; _px() scales them so a DPI-aware window keeps its proportions.
_UI_SCALE = [1.0]


def _px(n):
    return int(round(n * _UI_SCALE[0]))


# DPI awareness is process-wide, so it reaches SetCursorPos too: an unaware process
# moves in 96-DPI units (a 10 px delta = 15 real pixels at 150%), an aware one in
# real pixels. Scale the phone's deltas back up, or the GUI's cursor is a third
# slower than --no-gui's (which never turns awareness on, so its scale stays 1).
# The remainder carries over, so slow drags don't lose their fraction every packet.
# Only the receive loop calls this.
_move_residual = [0.0, 0.0]


def _dpi_move(dx, dy):
    s = _UI_SCALE[0]
    if s == 1.0:
        return dx, dy
    fx, fy = dx * s + _move_residual[0], dy * s + _move_residual[1]
    ix, iy = int(round(fx)), int(round(fy))
    _move_residual[0], _move_residual[1] = fx - ix, fy - iy
    return ix, iy


def _win_dpi_scale():
    """Make this process system-DPI-aware and return the display scale (1.5 at
    150%). Without it Windows bitmap-stretches the window and every glyph and the
    QR are blurry. System-aware, not per-monitor: Tk 8.6 doesn't re-layout on
    WM_DPICHANGED, so per-monitor would draw at the wrong size on a second screen.
    Must run before the first Tk window exists."""
    if not sys.platform.startswith("win"):
        return 1.0
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # Windows 7/8
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()     # Windows 10 1607+
        except Exception:
            hdc = ctypes.windll.user32.GetDC(None)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(None, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def _win_app_id():
    """Give the process its own taskbar identity, so the taskbar shows LazeR's icon
    and groups/pins it as LazeR rather than as python/pythonw."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LazeR")
    except Exception:
        pass


def qr_box_size(screen_h):
    """Pixels per QR module for a screen [screen_h] pixels tall. At 7 the window
    is taller than a 768-pixel screen can show, and the window's height cap then
    cut off "Show details" — the only way to the pairing settings and the log.
    5 still scans easily from arm's length."""
    return 7 if screen_h >= 900 else 5


class LazeRWindow:
    # Layout constants, in 96-DPI layout pixels (scaled by _px).
    CARD_R = 24          # M3 "extra large" card corner
    BANNER_R = 20
    PAGE_W = 440         # content column width
    GUTTER = 20
    IMG_CACHE_MAX = 256

    def __init__(self, ip, port, wire, event_q, require_secure, update_check=True,
                 net=None, notices=(), minimized=False):
        """[ip] is the published address, or None while there is none yet (the
        QR then waits for the first netchange instead of showing 127.0.0.1)."""
        import tkinter as tk
        from tkinter import font as tkf

        self._tk = tk
        self._eq = event_q
        self._wire = wire
        self._net = net
        self._token = wire.token
        self._require_secure = require_secure
        self._update_check = update_check
        self._update_tag = None      # newest release tag once known
        self._hostname = socket.gethostname()
        self._warnings = 0           # unread warnings, badged on Show details
        self._close_noticed = False  # told the user once that close != quit
        self._fw_dismissed = False
        self._vpn_logged = None
        self._img_cache = {}         # rendered shapes, keyed by what they look like
        self._cards = []             # (canvas, inner, pad) for _sync_cards
        uri = build_uri(ip, wire.token, self._hostname, wire.key) if ip else None
        token = wire.token

        self._light = _windows_prefers_light()
        _C.clear()
        _C.update(_scheme(self._light))

        _UI_SCALE[0] = _win_dpi_scale()
        _win_app_id()
        root = tk.Tk()
        self._root = root
        self._placed = False         # centred once; after that the user's position wins
        root.title("LazeR")
        root.configure(bg=_C["surface"])
        try:
            # The title bar and taskbar would otherwise show Tk's feather.
            root.iconbitmap(default=ICON_FILE)
        except Exception:
            pass
        # Resizable, so large text scaling can't push controls off a fixed window.
        root.resizable(True, True)
        try:
            root.tk.call("tk", "scaling", 1.25 * _UI_SCALE[0])
        except Exception:
            pass

        # Type scale after M3 (title large / title medium / body / label), set in
        # Segoe UI Variable where Windows 11 has it, plain Segoe UI before that.
        fams = set()
        try:
            fams = {f.lower() for f in tkf.families(root)}
        except Exception:
            pass

        def pick(*names):
            for n in names:
                if n.lower() in fams:
                    return n
            return names[-1]

        display = pick("Segoe UI Variable Display", "Segoe UI", "Inter Display", "Inter",
                       "Segoe UI")
        text = pick("Segoe UI Variable Text", "Segoe UI", "Inter", "Segoe UI")
        semi = pick("Segoe UI Variable Display Semib", "Segoe UI Variable Display Semibold",
                    "Segoe UI Semibold", "Inter Display SemiBold", "Inter SemiBold", "")
        semi_text = pick("Segoe UI Variable Text Semibold", "Segoe UI Variable Text Semib",
                         "Segoe UI Semibold", "Inter SemiBold", "")
        mono = pick("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Consolas")

        def mkfont(fam, size, weight="normal"):
            try:
                return tkf.Font(root=root, family=fam, size=size, weight=weight)
            except Exception:
                return None

        def heavy(size, fam, fallback):
            # A named Semibold face where one exists; synthetic bold otherwise.
            return mkfont(fam, size) if fam else mkfont(fallback, size, "bold")

        self.f_brand = heavy(16, semi, display)
        self.f_hero = heavy(17, semi, display)          # title large
        self.f_title = heavy(12, semi_text, text)        # title medium
        self.f_btn = heavy(10, semi_text, text)          # label large
        self.f_lbl = heavy(9, semi_text, text)           # label medium
        self.f_sm = mkfont(text, 9)                      # body small
        self.f_md = mkfont(text, 10)                     # body medium
        self.f_val = mkfont(mono, 10)
        self.f_tok = mkfont(mono, 19, "bold")
        self.f_log = mkfont(mono, 9)

        self._ip, self._port = ip, port
        self._setup_ttk()

        # The whole body scrolls. On a 768-pixel screen the window is taller than
        # the screen as soon as a banner shows or details open, and the height cap
        # in _center then cut the bottom off — the details panel and the typed
        # code included — with no way to reach it.
        self._canvas = tk.Canvas(root, bg=_C["surface"], highlightthickness=0, bd=0)
        self._vbar = self._ttk.Scrollbar(root, orient="vertical",
                                         command=self._canvas.yview,
                                         style="Lazer.Vertical.TScrollbar")
        self._canvas.configure(yscrollcommand=self._vbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(self._canvas, bg=_C["surface"], padx=_px(self.GUTTER),
                         pady=_px(18), width=_px(self.PAGE_W))
        self._outer = outer
        self._outer_win = self._canvas.create_window(0, 0, window=outer, anchor="nw")
        outer.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._outer_win, width=e.width))
        root.bind_all("<MouseWheel>", self._on_wheel)
        # A strut fixes the column width, so text wraps the same way everywhere and
        # cards don't resize as their contents change.
        tk.Frame(outer, bg=_C["surface"], width=_px(self.PAGE_W), height=0).pack()

        self._build_header(outer)
        self._build_hero(outer)
        self._build_notice_banner(outer)
        self._build_pause_banner(outer)
        self._build_firewall_banner(outer)

        # swap area: QR card (not connected) <-> connected panel
        self._swap = tk.Frame(outer, bg=_C["surface"])
        self._swap.pack(fill="x", pady=(_px(12), 0))
        self._build_connect_card(self._swap, uri, token)
        self._build_connected_panel(self._swap)
        self._show_qr_view()   # first start: show the QR

        # everything technical lives behind this toggle, hidden by default
        self._build_details_toggle(outer)
        self._details = tk.Frame(outer, bg=_C["surface"])
        self._details_open = False
        self._build_details(self._details, ip, port, token)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        _style_title_bar(root, self._light)
        self._tray = None
        self._tray_thread = None
        self._setup_tray()
        self._log("Server started", "ok")
        for tag, text_ in notices:
            self._warn(text_) if tag == "warn" else self._log(text_, "info")
        if uri is None:
            self._hide_qr(waiting=True)
        self._poll()
        self._check_firewall()   # surface the Allow banner if inbound is blocked
        self._root.after(FW_RECHECK_MS, self._periodic_firewall)
        self._check_update()     # no-op unless enabled; never blocks the UI thread
        self._poll_discovery_pill()
        self._fit()
        if minimized and self._tray is not None:
            # Start with Windows: nobody is looking at login, so don't put the QR
            # (token AND key) up on the screen. The tray icon is the way in.
            self._close_noticed = True
            root.withdraw()

    def _setup_ttk(self):
        """Slim, arrow-less scrollbars in the theme's colours — the classic Tk
        scrollbar is the single most dated-looking widget on Windows."""
        from tkinter import ttk
        self._ttk = ttk
        st = ttk.Style(self._root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.layout("Lazer.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        for name, trough in (("Lazer.Vertical.TScrollbar", _C["surface"]),
                             ("Card.Vertical.TScrollbar", _C["container"])):
            if name != "Lazer.Vertical.TScrollbar":
                st.layout(name, st.layout("Lazer.Vertical.TScrollbar"))
            st.configure(name, troughcolor=trough, background=_C["outline_var"],
                         bordercolor=trough, lightcolor=_C["outline_var"],
                         darkcolor=_C["outline_var"], gripcount=0,
                         arrowsize=_px(8), relief="flat", borderwidth=0)
            st.map(name, background=[("active", _C["outline"])],
                   lightcolor=[("active", _C["outline"])],
                   darkcolor=[("active", _C["outline"])])

    # ── rendered shapes ───────────────────────────────────────────────────────
    # Tk draws no anti-aliased curves, so rounded surfaces and icons are drawn with
    # Pillow at 4× and scaled down, then cached. Without Pillow the window still
    # works — the shapes fall back to flat rectangles.
    def _image(self, key, draw, w, h):
        img = self._img_cache.get(key)
        if img is not None:
            return img
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception:
            return None
        s = 4
        big = Image.new("RGBA", (max(1, w) * s, max(1, h) * s), (0, 0, 0, 0))
        draw(ImageDraw.Draw(big), s)
        img = ImageTk.PhotoImage(big.resize((max(1, w), max(1, h)), Image.LANCZOS),
                                 master=self._root)
        if len(self._img_cache) >= self.IMG_CACHE_MAX:
            # Window resizes mint one-off sizes, so trim the oldest half. Safe:
            # whatever is on screen is also held by its widget (see _put), so
            # dropping it here never blanks it.
            for k in list(self._img_cache)[:self.IMG_CACHE_MAX // 2]:
                del self._img_cache[k]
        self._img_cache[key] = img
        return img

    @staticmethod
    def _put(widget, img, item=None):
        """Show [img] on a label (item None) or a canvas item, and keep it alive.
        Tk holds an image only by name: once the last Python reference goes, the
        image is deleted and whatever showed it goes blank. The widget therefore
        holds a reference per slot, so the shape cache is free to evict."""
        held = widget.__dict__.setdefault("_lazer_held", {})
        held[item] = img
        if img is None:
            return
        if item is None:
            widget.config(image=img)
        else:
            widget.itemconfigure(item, image=img)

    def _rrect(self, w, h, r, fill, outline=None, width=1):
        def draw(d, s):
            d.rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=r * s, fill=fill,
                                outline=outline, width=(width * s) if outline else 0)
        return self._image(("rr", w, h, r, fill, outline, width), draw, w, h)

    def _dot_img(self, color, d=None, ring=None, ring_w=0):
        d = d or _px(8)

        def draw(dr, s):
            if ring:
                dr.ellipse([0, 0, d * s - 1, d * s - 1], fill=ring)
                k = ring_w * s
                dr.ellipse([k, k, d * s - 1 - k, d * s - 1 - k], fill=color)
            else:
                dr.ellipse([0, 0, d * s - 1, d * s - 1], fill=color)
        return self._image(("dot", color, d, ring, ring_w), draw, d, d)

    def _icon(self, name, size, color, bg=None):
        """Small line icons in the Material Symbols spirit: 2-px rounded strokes on
        a 24-unit grid, optionally inside a filled circle."""
        def draw(d, s):
            u = size * s / 24.0
            if bg:
                d.ellipse([0, 0, size * s - 1, size * s - 1], fill=bg)
                inset = 0.22
            else:
                inset = 0.0
            k = 24 * inset

            def p(x, y):
                f = (1 - 2 * inset)
                return (k * u + x * u * f, k * u + y * u * f)

            lw = max(1, int(round(2 * u * (1 - 2 * inset) + 0.5)))

            def line(*pts):
                d.line([p(*q) for q in pts], fill=color, width=lw, joint="curve")
                for q in (pts[0], pts[-1]):
                    x, y = p(*q)
                    d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color)

            if name == "laptop":
                x0, y0 = p(4, 5)
                x1, y1 = p(20, 16)
                d.rounded_rectangle([x0, y0, x1, y1], radius=2 * u, outline=color, width=lw)
                line((2, 19.5), (22, 19.5))
            elif name == "check":
                line((5, 12.5), (10, 17.5), (19.5, 7))
            elif name == "warn":
                line((12, 6.5), (12, 13.5))
                x, y = p(12, 17.5)
                r = lw * 0.75
                d.ellipse([x - r, y - r, x + r, y + r], fill=color)
            elif name == "info":
                line((12, 11), (12, 17.5))
                x, y = p(12, 7)
                r = lw * 0.75
                d.ellipse([x - r, y - r, x + r, y + r], fill=color)
            elif name == "pause":
                line((9, 6.5), (9, 17.5))
                line((15, 6.5), (15, 17.5))
            elif name == "shield":
                line((12, 3.5), (19.5, 6.5), (19.5, 12), (12, 20.5), (4.5, 12),
                     (4.5, 6.5), (12, 3.5))
            elif name == "chev_down":
                line((6.5, 9.5), (12, 15), (17.5, 9.5))
            elif name == "chev_up":
                line((6.5, 14.5), (12, 9), (17.5, 14.5))
        return self._image(("ic", name, size, color, bg), draw, size, size)

    # ── surfaces ──────────────────────────────────────────────────────────────
    def _card(self, parent, color=None, radius=None, pad=None, outline=None, **pack):
        """A rounded surface. Returns the inner frame to fill; its .master is the
        canvas that draws the shape, which is what callers pack and unpack."""
        tk = self._tk
        color = color or _C["container"]
        r = _px(radius if radius is not None else self.CARD_R)
        p = _px(pad if pad is not None else 18)
        try:
            pbg = parent.cget("bg")
        except Exception:
            pbg = _C["surface"]
        cv = tk.Canvas(parent, bg=pbg, highlightthickness=0, bd=0)
        img_id = cv.create_image(0, 0, anchor="nw")
        inner = tk.Frame(cv, bg=color)
        win = cv.create_window(p, p, window=inner, anchor="nw")
        drawn = [None]

        def redraw():
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 2 or h < 2 or drawn[0] == (w, h):
                return
            drawn[0] = (w, h)
            img = self._rrect(w, h, r, color, outline)
            if img is not None:
                self._put(cv, img, img_id)
            else:
                cv.configure(bg=color)

        def on_inner(_e=None):
            cv.configure(width=inner.winfo_reqwidth() + 2 * p,
                         height=inner.winfo_reqheight() + 2 * p)

        def on_canvas(e):
            cv.itemconfigure(win, width=max(1, e.width - 2 * p))
            redraw()

        inner.bind("<Configure>", on_inner)
        cv.bind("<Configure>", on_canvas)
        self._cards.append((cv, inner, p))
        if pack:
            cv.pack(**pack)
        return inner

    def _sync_cards(self):
        """Re-measure every card: a label whose text grew changes its frame's
        requested size without any <Configure> to tell the card."""
        for cv, inner, p in self._cards:
            try:
                cv.configure(width=inner.winfo_reqwidth() + 2 * p,
                             height=inner.winfo_reqheight() + 2 * p)
            except Exception:
                pass

    def _section_label(self, parent, text):
        self._tk.Label(parent, text=text, bg=parent.cget("bg"), fg=_C["primary"],
                       font=self.f_lbl).pack(anchor="w")

    # ── controls ──────────────────────────────────────────────────────────────
    def _button(self, parent, text, command, kind="filled", small=False, ink=None):
        """An M3 common button: filled, tonal, outlined or text. A canvas, so it
        can be a real pill with hover/press state layers and a focus ring; Tab,
        Enter and Space work like any button."""
        tk = self._tk
        pbg = parent.cget("bg")
        h = _px(32 if small else 40)
        padx = _px(16 if small else 24) if kind != "text" else _px(12)
        cv = tk.Canvas(parent, bg=pbg, height=h, highlightthickness=0, bd=0,
                       cursor="hand2", takefocus=1)
        state = {"text": text, "hover": False, "press": False, "focus": False,
                 "kind": kind}
        img_id = cv.create_image(0, 0, anchor="nw")
        txt_id = cv.create_text(0, 0, text=text, font=self.f_btn)

        def colours():
            k = state["kind"]
            if k == "filled":
                return _C["primary"], _C["on_primary"], None
            if k == "tonal":
                return _C["secondary_ctr"], _C["on_secondary_ctr"], None
            if k == "warn":
                return _C["warn"], _C["warn_ctr"], None
            if k == "outlined":
                return pbg, ink or _C["primary"], _C["outline"]
            return pbg, ink or _C["primary"], None   # text

        def paint():
            fill, fg, outline = colours()
            layer = 0.12 if state["press"] else 0.08 if state["hover"] else 0
            if layer:
                fill = _blend(fill, fg, layer)
            if state["focus"]:
                outline = _C["on_surface"]
            tw = self.f_btn.measure(state["text"]) if self.f_btn else 8 * len(state["text"])
            w = tw + 2 * padx
            cv.configure(width=w)
            img = self._rrect(w, h, h // 2, fill, outline, _px(2) if state["focus"] else 1)
            if img is not None:
                self._put(cv, img, img_id)
            cv.coords(txt_id, w / 2, h / 2)
            cv.itemconfigure(txt_id, text=state["text"], fill=fg)

        def set_hover(v):
            state["hover"] = v
            if not v:
                state["press"] = False
            paint()

        def set_press(v):
            state["press"] = v
            paint()

        def release(e):
            inside = 0 <= e.x < cv.winfo_width() and 0 <= e.y < cv.winfo_height()
            state["press"] = False
            paint()
            if inside:
                command()

        cv.bind("<Enter>", lambda e: set_hover(True))
        cv.bind("<Leave>", lambda e: set_hover(False))
        cv.bind("<ButtonPress-1>", lambda e: set_press(True))
        cv.bind("<ButtonRelease-1>", release)
        cv.bind("<Return>", lambda e: command())
        cv.bind("<space>", lambda e: command())
        cv.bind("<FocusIn>", lambda e: (state.update(focus=True), paint(),
                                        self._scroll_into_view(cv)))
        cv.bind("<FocusOut>", lambda e: (state.update(focus=False), paint()))

        def set_text(t):
            state["text"] = t
            paint()

        def set_kind(k):
            state["kind"] = k
            paint()
        cv.set_text = set_text
        cv.set_kind = set_kind
        paint()
        return cv

    def _switch(self, parent, on, command, enabled=True):
        """An M3 switch: 52×32 track; the thumb grows from 16 to 24 and slides
        across when it turns on."""
        tk = self._tk
        pbg = parent.cget("bg")
        W, H = _px(52), _px(32)
        cv = tk.Canvas(parent, bg=pbg, width=W, height=H, highlightthickness=0, bd=0,
                       cursor="hand2" if enabled else "", takefocus=1 if enabled else 0)
        track = cv.create_image(0, 0, anchor="nw")
        thumb = cv.create_image(0, 0, anchor="center")
        st = {"on": bool(on), "pos": 1.0 if on else 0.0, "hover": False,
              "focus": False, "anim": None, "enabled": enabled, "press": False}

        def paint():
            pos = st["pos"]
            on_ = st["on"]
            if not st["enabled"]:
                tr = self._rrect(W, H, H // 2, _blend(pbg, _C["on_surface"], 0.12),
                                 _blend(pbg, _C["on_surface"], 0.12), _px(2))
                th_col, d = _blend(pbg, _C["on_surface"], 0.38), _px(16)
            elif on_:
                tr = self._rrect(W, H, H // 2, _C["primary"],
                                 _C["on_surface"] if st["focus"] else None, _px(2))
                th_col = _C["on_primary"] if not st["hover"] else _C["primary_ctr"]
                d = _px(16 + 8 * pos)
            else:
                tr = self._rrect(W, H, H // 2, _C["container_top"],
                                 _C["on_surface"] if st["focus"] else _C["outline"], _px(2))
                th_col = _C["outline"] if not st["hover"] else _C["on_variant"]
                d = _px(16 + 8 * pos)
            if tr is not None:
                self._put(cv, tr, track)
            x = _px(16) + (W - _px(32)) * pos
            img = self._dot_img(th_col, d)
            if img is not None:
                self._put(cv, img, thumb)
            cv.coords(thumb, x, H / 2)

        def animate():
            target = 1.0 if st["on"] else 0.0
            step = 0.25 if target > st["pos"] else -0.25
            st["pos"] = max(0.0, min(1.0, st["pos"] + step))
            paint()
            if st["pos"] != target:
                st["anim"] = cv.after(16, animate)
            else:
                st["anim"] = None

        def set_on(v, animated=True):
            st["on"] = bool(v)
            if st["anim"] is not None:
                cv.after_cancel(st["anim"])
                st["anim"] = None
            if animated:
                animate()
            else:
                st["pos"] = 1.0 if v else 0.0
                paint()

        def click(_e=None):
            if st["enabled"]:
                command()

        def press(_e):
            st["press"] = True

        def release(e):
            # Like _button: only a press that began here and ends here toggles,
            # so dragging across the switch and letting go on it changes nothing.
            pressed, st["press"] = st["press"], False
            if pressed and 0 <= e.x < cv.winfo_width() and 0 <= e.y < cv.winfo_height():
                click()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<ButtonRelease-1>", release)
        cv.bind("<Return>", click)
        cv.bind("<space>", click)
        cv.bind("<Enter>", lambda e: (st.update(hover=True), paint()))
        cv.bind("<Leave>", lambda e: (st.update(hover=False), paint()))
        cv.bind("<FocusIn>", lambda e: (st.update(focus=True), paint(),
                                        self._scroll_into_view(cv)))
        cv.bind("<FocusOut>", lambda e: (st.update(focus=False), paint()))

        def set_enabled(v):
            st["enabled"] = v
            cv.configure(cursor="hand2" if v else "", takefocus=1 if v else 0)
            paint()
        cv.set_on = set_on
        cv.set_enabled = set_enabled
        paint()
        return cv

    def _chip(self, parent, text, tone="neutral"):
        """A status chip: dot + label on a tonal pill. .set(text, tone) restyles."""
        tk = self._tk
        pbg = parent.cget("bg")
        h = _px(28)
        cv = tk.Canvas(parent, bg=pbg, height=h, highlightthickness=0, bd=0)
        img_id = cv.create_image(0, 0, anchor="nw")
        dot_id = cv.create_image(0, h / 2, anchor="w")
        txt_id = cv.create_text(0, h / 2, anchor="w", font=self.f_lbl)

        def set_(t, tn="neutral"):
            fill, fg, dot = {
                "ok": (_C["ok_ctr"], _C["on_ok_ctr"], _C["ok"]),
                "warn": (_C["warn_ctr"], _C["on_warn_ctr"], _C["warn"]),
                "accent": (_C["primary_ctr"], _C["on_primary_ctr"], _C["primary"]),
            }.get(tn, (_C["container_hi"], _C["on_variant"], _C["outline"]))
            tw = self.f_lbl.measure(t) if self.f_lbl else 7 * len(t)
            w = _px(12) + _px(8) + _px(8) + tw + _px(12)
            cv.configure(width=w)
            img = self._rrect(w, h, h // 2, fill)
            if img is not None:
                self._put(cv, img, img_id)
            di = self._dot_img(dot, _px(8))
            if di is not None:
                self._put(cv, di, dot_id)
            cv.coords(dot_id, _px(12), h / 2)
            cv.coords(txt_id, _px(28), h / 2)
            cv.itemconfigure(txt_id, text=t, fill=fg)
        cv.set = set_
        set_(text, tone)
        return cv

    # ── system tray ───────────────────────────────────────────────────────────
    def _setup_tray(self):
        """Closing the window hides it to the tray; quit only from the tray menu."""
        try:
            import pystray
            from PIL import Image
        except Exception:
            return  # no pystray -> close quits (handled in _on_close)
        try:
            img = Image.open(ICON_FILE)
        except Exception:
            img = None
        menu = pystray.Menu(
            pystray.MenuItem("Show LazeR", self._tray_show, default=True),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon("LazeR", img, "LazeR — LAN remote", menu)
        self._tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        self._tray_thread.start()

    def _tray_show(self, icon=None, item=None):
        self._root.after(0, self._restore)

    def _restore(self):
        # Bring the window forward — from the tray (withdrawn), minimized, or just
        # buried. A background process (we're poked over loopback by a 2nd launch)
        # can't steal the foreground with lift()/focus_force() alone under Windows'
        # foreground lock; briefly forcing -topmost then dropping it is the standard
        # way to actually raise to front.
        r = self._root
        try:
            r.deiconify()
            r.state("normal")   # un-minimize if iconified
        except Exception:
            pass
        try:
            r.attributes("-topmost", True)
            r.lift()
            r.focus_force()
            r.after(300, self._drop_topmost)
        except Exception:
            pass

    def _drop_topmost(self):
        try:
            self._root.attributes("-topmost", False)
        except Exception:
            pass

    def _tray_quit(self, icon=None, item=None):
        self._root.after(0, self._real_quit)

    def _real_quit(self):
        _stop.set()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        try:
            self._root.destroy()
        except Exception:
            pass

    # ── clickable labels, made keyboard-usable ─────────────────────────────────
    def _clickable(self, widget, command):
        """Bind the mouse AND make a label a Tab stop that Enter/Space activate,
        with a visible focus ring — so the keyboard reaches it too."""
        try:
            bg = widget.master.cget("bg")
        except Exception:
            bg = _C["container"]
        widget.config(takefocus=1, highlightthickness=1, highlightbackground=bg,
                      highlightcolor=_C["primary"])
        widget.bind("<Button-1>", lambda e: command())
        widget.bind("<Return>", lambda e: command())
        widget.bind("<space>", lambda e: command())
        widget.bind("<FocusIn>", lambda e: self._scroll_into_view(widget))
        return widget

    # ── scrolling body ──────────────────────────────────────────────────────
    def _fit(self):
        """Size the window to its content, capped to the screen; past the cap
        the body scrolls (scrollbar shown only then)."""
        root = self._root
        self._sync_cards()
        root.update_idletasks()
        w, h = self._outer.winfo_reqwidth(), self._outer.winfo_reqheight()
        avail = root.winfo_screenheight() - _px(80)
        if h > avail:
            self._vbar.pack(side="right", fill="y", before=self._canvas)
        else:
            self._vbar.pack_forget()
            self._canvas.yview_moveto(0)
        self._canvas.configure(width=w, height=min(h, avail))
        root.geometry("")
        root.after(10, self._center)

    def _scrolls(self):
        return self._outer.winfo_reqheight() > self._canvas.winfo_height() + 1

    def _on_wheel(self, e):
        # The activity log scrolls itself; don't scroll the page under it too.
        if isinstance(e.widget, self._tk.Text) or not self._scrolls():
            return
        self._canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1),
                                  "units")

    def _scroll_into_view(self, widget):
        """Tabbing to a control below the fold scrolls it up into sight."""
        if not self._scrolls():
            return
        try:
            top = widget.winfo_rooty() - self._outer.winfo_rooty()
            bottom = top + widget.winfo_height()
            total = max(1, self._outer.winfo_reqheight())
            view_top = self._canvas.canvasy(0)
            view_h = self._canvas.winfo_height()
            if top < view_top:
                self._canvas.yview_moveto(max(0, top - 8) / total)
            elif bottom > view_top + view_h:
                self._canvas.yview_moveto(max(0, bottom + 8 - view_h) / total)
        except Exception:
            pass

    # ── header ────────────────────────────────────────────────────────────────
    def _build_header(self, parent):
        tk = self._tk
        bar = tk.Frame(parent, bg=_C["surface"])
        bar.pack(fill="x")

        mark = tk.Label(bar, bg=_C["surface"], bd=0)
        self._brand_img = None
        try:
            from PIL import Image, ImageTk
            n = _px(32)
            im = Image.open(ICON_FILE)
            try:
                im.size = max(im.info.get("sizes", [(256, 256)]))
            except Exception:
                pass
            im = im.convert("RGBA").resize((n, n), Image.LANCZOS)
            self._brand_img = ImageTk.PhotoImage(im, master=self._root)
            mark.config(image=self._brand_img)
        except Exception:
            self._put(mark, self._icon("shield", _px(32), _C["on_primary_ctr"],
                                       _C["primary_ctr"]))
        mark.pack(side="left")

        names = tk.Frame(bar, bg=_C["surface"])
        names.pack(side="left", padx=(_px(10), 0))
        tk.Label(names, text="LazeR", bg=_C["surface"], fg=_C["on_surface"],
                 font=self.f_brand).pack(anchor="w")

        quit_btn = self._button(bar, "Quit", self._real_quit, kind="text", small=True)
        quit_btn.pack(side="right", padx=(_px(4), 0))
        # Driven by the receive loop's own events, so it goes dark with a dead loop.
        self._run_chip = self._chip(bar, "Starting…", "neutral")
        self._run_chip.pack(side="right")

    def _set_serving(self, ok):
        self._run_chip.set("Ready" if ok else "Restarting…", "ok" if ok else "warn")

    # ── hero status card ────────────────────────────────────────────────────
    def _build_hero(self, parent):
        tk = self._tk
        card = self._card(parent, fill="x", pady=(_px(16), 0), pad=16)
        bg = card.cget("bg")

        n = _px(52)
        self._avatar = tk.Canvas(card, width=n, height=n, bg=bg,
                                 highlightthickness=0, bd=0)
        self._avatar.pack(side="left")
        ic = self._icon("laptop", n, _C["on_primary_ctr"], _C["primary_ctr"])
        if ic is not None:
            self._put(self._avatar, ic, self._avatar.create_image(0, 0, anchor="nw"))
        self._dotid = self._avatar.create_image(n, n, anchor="se")
        self._paint_avatar_dot(False)

        col = tk.Frame(card, bg=bg)
        col.pack(side="left", padx=(_px(14), 0), anchor="w", fill="x", expand=True)
        tk.Label(col, text=socket.gethostname(), bg=bg, fg=_C["on_surface"],
                 font=self.f_hero, anchor="w").pack(anchor="w")
        self._status = tk.Label(col, text="Waiting for your phone…",
                                bg=bg, fg=_C["on_variant"], font=self.f_md, anchor="w")
        self._status.pack(anchor="w", pady=(_px(1), 0))

    def _paint_avatar_dot(self, connected):
        img = self._dot_img(_C["ok"] if connected else _C["outline"], _px(16),
                            ring=_C["container"], ring_w=_px(3))
        if img is not None:
            self._put(self._avatar, img, self._dotid)

    # ── banners ──────────────────────────────────────────────────────────────
    def _banner(self, parent, icon):
        """An attention surface: amber container, leading icon, then a column the
        caller fills (title, body, actions). Returns (wrap, column)."""
        tk = self._tk
        wrap = tk.Frame(parent, bg=_C["surface"])          # packed on demand
        card = self._card(wrap, color=_C["warn_ctr"], radius=self.BANNER_R, pad=16,
                          fill="x", pady=(_px(12), 0))
        ic = self._icon(icon, _px(24), _C["on_warn_ctr"])
        lead = tk.Label(card, bg=_C["warn_ctr"], bd=0)
        if ic is not None:
            self._put(lead, ic)
        lead.pack(side="left", anchor="n", padx=(0, _px(12)))
        col = tk.Frame(card, bg=_C["warn_ctr"])
        col.pack(side="left", fill="x", expand=True)
        return wrap, col

    def _build_notice_banner(self, parent):
        """The newest warning, shown until dismissed (in the log, per-click lines scroll
        it away); Show details is badged with the count."""
        tk = self._tk
        self._notice_wrap, col = self._banner(parent, "info")
        self._notice_txt = tk.Label(col, text="", bg=_C["warn_ctr"], fg=_C["on_warn_ctr"],
                                    font=self.f_md, justify="left", anchor="w",
                                    wraplength=_px(self.PAGE_W - 120))
        self._notice_txt.pack(anchor="w", fill="x")
        btns = tk.Frame(col, bg=_C["warn_ctr"])
        btns.pack(anchor="e", pady=(_px(8), 0))
        self._button(btns, "Dismiss", self._clear_notice, kind="text",
                     small=True, ink=_C["on_warn_ctr"]).pack(side="right")

    def _warn(self, msg):
        """Log a warning AND surface it: banner + details badge."""
        self._log(msg, "warn")
        self._notice_txt.config(text=msg)
        if not self._notice_wrap.winfo_ismapped():
            self._notice_wrap.pack(fill="x", before=self._swap)
        if not self._details_open:
            self._warnings += 1
            self._refresh_details_toggle()
        self._resize()

    def _clear_notice(self):
        try:
            self._notice_wrap.pack_forget()
        except Exception:
            pass
        self._resize()

    # ── local-takeover banner: shown when physical input has paused the remote ──
    def _build_pause_banner(self, parent):
        tk = self._tk
        self._banner_wrap, col = self._banner(parent, "pause")
        self._banner_title = tk.Label(col, text="", bg=_C["warn_ctr"],
                                      fg=_C["on_warn_ctr"], font=self.f_title,
                                      anchor="w")
        self._banner_title.pack(anchor="w")
        self._banner_sub = tk.Label(col, text="", bg=_C["warn_ctr"], fg=_C["on_warn_ctr"],
                                    font=self.f_md, justify="left", anchor="w",
                                    wraplength=_px(self.PAGE_W - 120))
        self._banner_sub.pack(anchor="w", fill="x", pady=(_px(2), _px(10)))
        btns = tk.Frame(col, bg=_C["warn_ctr"])
        btns.pack(anchor="w")
        self._button(btns, "Resume remote", self._do_resume, kind="warn",
                     small=True).pack(side="left")
        self._button(btns, "Quit LazeR", self._real_quit, kind="text",
                     small=True, ink=_C["on_warn_ctr"]).pack(side="left", padx=(_px(4), 0))

    def _set_paused(self, latched):
        title = ("Remote stopped — you took over"
                 if latched else "Paused while you use this computer")
        sub = ("The panic hotkey turned remote control off. Your mouse and keyboard "
               "are yours until you click Resume."
               if latched else
               "The phone picks up again a couple of seconds after you stop.")
        self._banner_title.config(text=title)
        self._banner_sub.config(text=sub)
        self._banner_wrap.pack(fill="x", before=self._swap)
        self._resize()

    def _clear_paused(self):
        try:
            self._banner_wrap.pack_forget()
        except Exception:
            pass
        self._resize()

    def _do_resume(self):
        resume_remote()
        self._clear_paused()
        self._log("Remote resumed by user", "ok")

    # ── firewall banner: shown when inbound UDP is blocked (Windows) ────────────
    def _build_firewall_banner(self, parent):
        tk = self._tk
        self._fw_ok = None
        self._fw_wrap, col = self._banner(parent, "shield")
        tk.Label(col, text="Phones can't reach this laptop", bg=_C["warn_ctr"],
                 fg=_C["on_warn_ctr"], font=self.f_title, anchor="w").pack(anchor="w")
        self._fw_sub = tk.Label(col, text="", bg=_C["warn_ctr"], fg=_C["on_warn_ctr"],
                                font=self.f_md, justify="left", anchor="w",
                                wraplength=_px(self.PAGE_W - 120))
        self._fw_sub.pack(anchor="w", fill="x", pady=(_px(2), _px(10)))
        btns = tk.Frame(col, bg=_C["warn_ctr"])
        btns.pack(anchor="w")
        self._fw_btn = self._button(btns, "Allow through firewall", self._fix_firewall,
                                    kind="warn", small=True)
        self._fw_btn.pack(side="left")
        self._fw_dismiss = self._button(btns, "Dismiss", self._dismiss_firewall_banner,
                                        kind="text", small=True, ink=_C["on_warn_ctr"])
        self._fw_dismiss.pack(side="left", padx=(_px(4), 0))
        self._fw_inert = False

    def _periodic_firewall(self):
        self._check_firewall()
        self._root.after(FW_RECHECK_MS, self._periodic_firewall)

    def _check_firewall(self):
        """Probe the rule off the UI thread, then show the banner if it's missing.
        Runs at launch, after every network change and wake, and every
        FW_RECHECK_MS, so joining a Public or policy-locked network mid-session
        still warns."""
        if not sys.platform.startswith("win"):
            return

        def work():
            ok = firewall_rule_exists()
            vpn = detect_vpn()
            inert = firewall_rule_is_inert()
            blocks = firewall_block_rules()
            self._root.after(0, lambda: self._on_firewall_status(ok, vpn, inert, blocks))
        threading.Thread(target=work, daemon=True).start()

    def _on_firewall_status(self, ok, vpn, inert=None, blocks=()):
        # A rule that EXISTS is not the same as a rule that WORKS. When policy
        # discards local rules on the profile we're on, the rule reads back as
        # Enabled/Allow/Profile=Any and Windows drops the packets anyway — so a
        # green pill here is a lie that costs hours. Count it as blocked.
        blocked_now = bool(inert and inert[1])
        was_ok = self._fw_ok
        self._fw_ok = ok and not blocked_now and not blocks
        self._fw_inert = blocked_now
        if self._fw_ok is False and was_ok is not False:
            self._fw_dismissed = False       # a NEW problem deserves the banner again
        self._update_fw_pill()
        advice = firewall_advice_for(inert)   # no re-probe on the Tk thread
        if vpn and vpn != self._vpn_logged:
            # Say it whenever one is present — README promises this — not only when
            # the rule is already known to be bad.
            self._vpn_logged = vpn
            self._log(f"VPN “{vpn}” is active. If phones can't connect, turn on "
                      "“allow local network” (LAN access) in the VPN.", "warn")
        if self._fw_ok:
            self._clear_firewall_banner()
            if advice and was_ok is None:
                self._log(advice, "warn")
            return
        if self._fw_dismissed:
            return
        if blocked_now:
            # Adding the rule again cannot help, so don't offer a button that
            # would just spend a UAC prompt to change nothing.
            sub = advice
            self._fw_btn.pack_forget()
        elif blocks and _can_remove_program_rules():
            sub = ("Windows Firewall has a rule blocking LazeR — usually from clicking "
                   "Cancel when Windows asked about it. One click removes it and "
                   "allows phones in (asks for admin once).")
            self._fw_btn.pack(side="left", before=self._fw_dismiss)
        elif blocks:
            # From source (the rule names python.exe) or a path cmd.exe can't carry,
            # the button can't remove it — don't spend a UAC prompt on failing.
            sub = ("Windows Firewall has a rule blocking “" + blocks[0] + "”. Remove it "
                   "in Windows Defender Firewall › Advanced settings › Inbound Rules.")
            self._fw_btn.pack_forget()
        else:
            sub = (f"Windows Firewall is dropping inbound traffic on UDP {PORT}, so phones "
                   "time out. One click adds the allow rule (asks for admin once).")
            if advice:
                sub += "\n" + advice
            self._fw_btn.pack(side="left", before=self._fw_dismiss)
        if vpn:
            sub += (f"\nNote: VPN “{vpn}” is active — if it blocks local network "
                    "traffic, also enable “allow local LAN” in the VPN.")
        self._fw_sub.config(text=sub)
        self._fw_wrap.pack(fill="x", before=self._swap)
        self._resize()

    def _clear_firewall_banner(self):
        try:
            self._fw_wrap.pack_forget()
        except Exception:
            pass
        self._resize()

    def _dismiss_firewall_banner(self):
        self._fw_dismissed = True
        self._clear_firewall_banner()
        self._update_fw_pill()

    def _reopen_firewall_banner(self):
        if self._fw_ok is False:
            self._fw_dismissed = False
            self._check_firewall()

    def _fix_firewall(self):
        self._fw_btn.set_text("Requesting admin…")

        def work():
            ok = ensure_firewall_rule(allow_elevate=True)
            self._root.after(0, lambda: self._after_fix_firewall(ok))
        threading.Thread(target=work, daemon=True).start()

    def _after_fix_firewall(self, ok):
        self._fw_btn.set_text("Allow through firewall")
        self._fw_ok = ok
        self._update_fw_pill()
        if ok:
            self._clear_firewall_banner()
            self._log(f"Firewall allowed — inbound UDP {PORT} open for phones", "ok")
            self._check_firewall()      # the rule can exist and still be inert
        else:
            self._warn("The firewall rule wasn't added: the admin prompt was declined "
                       "or failed. Click Allow through firewall again and choose Yes.")

    # ── update check ──────────────────────────────────────────────────────────
    def _check_update(self):
        """Ask GitHub for the newest release tag, off the UI thread.

        Runs once per launch. The server is normally left running for days, but a
        laptop that stays up that long is also one nobody is looking at, so a
        periodic re-check would spend requests to update a window no one reads —
        the next launch catches it."""
        if not self._update_check:
            self._set_update_pill("off — started with --no-update-check", "faint")
            return

        def work():
            tag, newer = check_for_update()
            self._root.after(0, lambda: self._on_update_result(tag, newer))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, tag, newer):
        if tag is None:
            # Offline, rate limited, or GitHub had a bad day. Not an error worth
            # anyone's attention — just say we don't know.
            self._set_update_pill("couldn't check — no internet?", "faint")
            return
        if not newer:
            self._set_update_pill(f"up to date (v{APP_VERSION})", "ok")
            return
        self._update_tag = tag
        self._set_update_pill(f"{tag} available — click to open", "warn", link=True)
        self._log(f"Update available: {tag} (running v{APP_VERSION})", "info")

    def _set_update_pill(self, text, colour, link=False):
        pill = getattr(self, "_upd_pill", None)
        if pill is None:
            return
        dot, val = pill
        dot.config(fg=_C[colour])
        val.config(text=text)
        if link:
            # Only clickable once there IS something to open, so a dead pointer
            # cursor never invites a click that does nothing.
            val.config(fg=_C["primary"], cursor="hand2")
            self._clickable(val, self._open_releases)
        else:
            val.config(fg=_C["on_variant"], cursor="")
            val.unbind("<Button-1>")

    # Phone microphone: what the row says in each state. Reading the state is a few
    # attribute reads, so polling it once a second costs nothing.
    _PMIC_TEXT = {
        "missing": ("Needs VB-CABLE, a free virtual mic — click to get it. "
                    "Install it, restart the PC, then reopen LazeR.", "warn"),
        "ready":   ("Ready — in Zoom, Teams or Meet, choose \u201cCABLE Output\u201d "
                    "as the microphone, then pick Phone mic on the phone. Use "
                    "headphones on the laptop, or its speakers will echo through "
                    "the phone.", "ok"),
        "live":    ("Live — your phone is the microphone (\u201cCABLE Output\u201d).",
                    "primary"),
        "checking": ("checking…", "outline"),
        "error":   ("VB-CABLE is installed but won't open — another app may hold it "
                    "exclusively, or it's disabled in Sound settings. Click to try "
                    "again.", "warn"),
    }

    def _poll_phone_mic_pill(self):
        st = phone_mic.status()
        now = time.monotonic()
        # The log notes a session, not every mute: muting the phone stops its
        # frames, which releases the device, so "off" waits for a real pause.
        if st == "live":
            self._pmic_quiet_since = None
            if not self._pmic_logged_on:
                self._pmic_logged_on = True
                self._log("Phone microphone on", "act")
        elif self._pmic_logged_on:
            if self._pmic_quiet_since is None:
                self._pmic_quiet_since = now
            elif now - self._pmic_quiet_since >= 10:
                self._pmic_logged_on = False
                self._log("Phone microphone off", "info")
        key = (st, phone_mic.error if st == "error" else None)
        if key != self._pmic_state:
            if st == "error":
                self._log(f"Phone mic: VB-CABLE is installed but won't open "
                          f"({phone_mic.error})", "warn")
            self._pmic_state = key
            dot, val = self._pmic_pill
            text, colour = self._PMIC_TEXT.get(st, ("unavailable", "outline"))
            dot.config(fg=_C[colour])
            val.config(text=text)
            for ev in ("<Button-1>", "<Return>", "<space>"):
                val.unbind(ev)
            if st in ("missing", "error"):
                val.config(fg=_C["primary"], cursor="hand2")
                self._clickable(val, self._get_vb_cable if st == "missing"
                                else phone_mic.rescan_soon)
            else:
                val.config(fg=_C["on_variant"], cursor="", takefocus=0,
                           highlightthickness=0)
        self._root.after(1000, self._poll_phone_mic_pill)

    def _get_vb_cable(self):
        """Open VB-Audio's own page (the user installs it; we never download it),
        and look for the cable again in case it is already there."""
        import webbrowser
        try:
            webbrowser.open(VB_CABLE_PAGE)
        except Exception as e:
            self._log(f"Couldn't open {VB_CABLE_PAGE} ({type(e).__name__})", "warn")
        self._log("Phone mic setup: install VB-CABLE from vb-audio.com, restart the "
                  "PC, then choose \u201cCABLE Output\u201d as the microphone in your "
                  "meeting app.", "info")
        phone_mic.rescan_soon()

    def _open_releases(self):
        import webbrowser
        try:
            webbrowser.open(RELEASES_PAGE)
        except Exception as e:
            self._log(f"Couldn't open the releases page ({type(e).__name__})", "warn")

    def _update_fw_pill(self):
        pill = getattr(self, "_fw_pill", None)
        if pill is None:
            return
        dot, txt = pill
        txt.unbind("<Button-1>")
        txt.config(cursor="", fg=_C["on_variant"])
        if self._fw_ok is None:
            dot.config(fg=_C["outline"]); txt.config(text="checking…")
        elif self._fw_ok:
            dot.config(fg=_C["ok"]); txt.config(text="inbound allowed")
        else:
            dot.config(fg=_C["warn"])
            # Say what is actually wrong, and never point at a button that isn't
            # there: policy lock hides Allow, and Dismiss hides the whole banner.
            what = ("blocked by this network's policy" if self._fw_inert
                    else "blocked — inbound UDP rule missing")
            if self._fw_dismissed or self._fw_inert:
                txt.config(text=what + " · click for details", cursor="hand2",
                           fg=_C["primary"])
                txt.bind("<Button-1>", lambda e: self._reopen_firewall_banner())
            else:
                txt.config(text=what + " · see the banner above")

    # ── friendly connect card: just the QR + pairing code ────────────────────
    def _build_connect_card(self, parent, uri, token):
        tk = self._tk
        pad = self._card(parent, pad=24)
        self._connect_border = pad.master   # the card canvas, for show/hide
        bg = pad.cget("bg")

        tk.Label(pad, text="Connect your phone", bg=bg, fg=_C["on_surface"],
                 font=self.f_hero).pack()
        tk.Label(pad, text="Open LazeR on your phone and scan this code",
                 bg=bg, fg=_C["on_variant"], font=self.f_md).pack(pady=(_px(4), 0))

        self._qr_img_label = None
        self._make_qr_photo = None
        self._photo = None
        self._qr_hidden = False
        self._qr_waiting = False
        self._qr_after = None
        try:
            import qrcode
            from PIL import ImageTk  # noqa: F401
            self._make_qr_photo = self._build_qr_maker(qrcode, ImageTk)
            # No address yet: render a throwaway so the slot has its size; it is
            # hidden behind the "waiting" placeholder until netchange repaints it.
            self._photo = self._make_qr_photo(uri or "lazer://")
            # The QR and its placeholder swap inside a slot packed ONCE, rather
            # than being packed into `pad` directly: Tk appends a re-packed widget
            # at the END of its parent's pack order, so a direct swap dropped the
            # code below the pairing chip and the plaintext note after the first
            # idle hide, and left it there.
            slot = tk.Frame(pad, bg=bg)
            slot.pack(pady=(_px(18), _px(16)))
            self._qr_slot = slot
            holder = tk.Frame(slot, bg=bg)
            holder.pack()
            self._qr_img_label = tk.Label(holder, image=self._photo, bg=bg, bd=0)
            self._qr_img_label.pack()
            self._qr_holder = holder
            # Click-to-show placeholder swapped in by the idle hide
            # (QR_HIDE_AFTER_MS): an unattended laptop must not advertise its
            # token+key to every passer-by indefinitely.
            ph = tk.Canvas(slot, bg=bg, highlightthickness=0, bd=0, takefocus=1)
            self._ph_shape = ph.create_image(0, 0, anchor="nw")
            self._ph_text = ph.create_text(0, 0, text="", justify="center",
                                           font=self.f_md, fill=_C["on_variant"])
            ph.bind("<Button-1>", lambda e: self._show_qr())
            ph.bind("<Return>", lambda e: self._show_qr())
            ph.bind("<space>", lambda e: self._show_qr())
            self._qr_placeholder = ph
            self._qr_ph_label = ph
            self._fit_placeholder()
        except ImportError:
            tk.Label(pad, text="Install Pillow for the QR image:\npip install pillow",
                     bg=bg, fg=_C["on_variant"], font=self.f_sm,
                     width=24, height=10, justify="center").pack(pady=_px(16))

        # Typed-code pairing. Encryption is required by default, and the typed code
        # is the plaintext wire, so it is shown only while it can work.
        self._code_box = tk.Frame(pad, bg=bg)
        self._code_box.pack()
        tk.Label(self._code_box, text="No camera? Type this pairing code in the app",
                 bg=bg, fg=_C["on_variant"], font=self.f_sm).pack()
        chip = self._card(self._code_box, color=_C["container_hi"], radius=16, pad=8,
                          pady=(_px(8), 0))
        self._token_chip = tk.Label(chip, text=token, bg=_C["container_hi"],
                                    fg=_C["primary"], font=self.f_tok, padx=_px(10))
        self._token_chip.pack(side="left")
        self._copy_btn = self._button(chip, "Copy", lambda: self._copy(self._wire.token),
                                      kind="tonal", small=True)
        self._copy_btn.pack(side="left", padx=(_px(6), 0))
        tk.Label(self._code_box, text="Typed codes aren't encrypted — scan the QR "
                 "when you can", bg=bg, fg=_C["warn"],
                 font=self.f_sm).pack(pady=(_px(8), 0))
        self._code_off = tk.Label(
            pad, text="Pairing is QR-only and encrypted. To allow typed codes, turn off "
                      "Require encryption in Details.",
            bg=bg, fg=_C["on_variant"], font=self.f_sm, wraplength=_px(340),
            justify="center")
        self._refresh_code_box()
        self._arm_qr_hide()

    def _refresh_code_box(self):
        if self._require_secure and _HAVE_CRYPTO:
            self._code_box.pack_forget()
            self._code_off.pack()
        else:
            self._code_off.pack_forget()
            self._code_box.pack()

    # ── connected panel: shown instead of the QR once a phone is paired ───────
    def _build_connected_panel(self, parent):
        tk = self._tk
        pad = self._card(parent, pad=28)
        self._connected_border = pad.master
        bg = pad.cget("bg")

        n = _px(64)
        ic = self._icon("check", n, _C["on_ok_ctr"], _C["ok_ctr"])
        check = tk.Label(pad, bg=bg, bd=0)
        if ic is not None:
            self._put(check, ic)
        check.pack()

        tk.Label(pad, text="Phone connected", bg=bg, fg=_C["on_surface"],
                 font=self.f_hero).pack(pady=(_px(14), 0))
        self._connected_sub = tk.Label(
            pad, text="Your phone is controlling this computer",
            bg=bg, fg=_C["on_variant"], font=self.f_md)
        self._connected_sub.pack(pady=(_px(4), 0))

        btns = tk.Frame(pad, bg=bg)
        btns.pack(pady=(_px(20), 0))
        self._button(btns, "Show QR code", self._show_qr_view, kind="tonal").pack(
            side="left")
        self._button(btns, "New code…", self._confirm_regenerate, kind="text").pack(
            side="left", padx=(_px(8), 0))
        tk.Label(pad, text="After a drop or a sleep, the phone reconnects by itself.",
                 bg=bg, fg=_C["outline"], font=self.f_sm).pack(pady=(_px(14), 0))

    def _build_qr_maker(self, qrcode, ImageTk):
        """Return a fn uri -> PhotoImage, so the QR can be rebuilt after an IP change."""
        # Sized for the screen in layout pixels, then drawn in real ones.
        s = _UI_SCALE[0]
        box = max(1, round(qr_box_size(self._root.winfo_screenheight() / s) * s))
        radius = _px(20)

        def make(uri):
            from PIL import Image, ImageDraw
            qr = qrcode.QRCode(border=2, box_size=box,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(uri)
            qr.make(fit=True)
            pil = qr.make_image(fill_color=_C["qr_dark"],
                                back_color="#FFFFFF").convert("RGBA")
            # A white tile with rounded corners. The quiet zone is kept white;
            # only the outermost corners go, well clear of the finder patterns.
            w, h = pil.size
            mask = Image.new("L", (w * 4, h * 4), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * 4 - 1, h * 4 - 1],
                                                   radius=radius * 4, fill=255)
            pil.putalpha(mask.resize((w, h), Image.LANCZOS))
            return ImageTk.PhotoImage(pil, master=self._root)
        return make

    def refresh_network(self, new_ip, new_uri):
        """Laptop resumed on a new IP — repaint the QR and IP so the phone reconnects."""
        self._ip = new_ip
        try:
            self._ip_value.config(text=new_ip)
        except Exception:
            pass
        self._qr_waiting = False
        self._repaint_qr(new_uri)
        self._refresh_discovery_pill()

    def _fit_placeholder(self):
        """Size the hidden-QR tile to the QR it stands in for. A new address can
        change the QR's version and so its size; a tile left at the old size
        made the card jump on every idle hide."""
        ph, photo = getattr(self, "_qr_placeholder", None), getattr(self, "_photo", None)
        if ph is None or photo is None:
            return
        qw, qh = photo.width(), photo.height()
        ph.configure(width=qw, height=qh)
        self._put(ph, self._rrect(qw, qh, _px(20), _C["container_hi"]), self._ph_shape)
        ph.coords(self._ph_text, qw / 2, qh / 2)
        ph.itemconfigure(self._ph_text, width=max(1, qw - _px(40)))

    def _repaint_qr(self, uri):
        if self._make_qr_photo and self._qr_img_label is not None:
            try:
                self._photo = self._make_qr_photo(uri)
                self._qr_img_label.config(image=self._photo)
                self._fit_placeholder()
                # A freshly painted QR exists to be scanned — show it and restart
                # the idle-hide clock.
                self._show_qr()
            except Exception:
                pass

    # ── QR idle-hide ──────────────────────────────────────────────────────────
    def _arm_qr_hide(self):
        """(Re)start the timer that hides the QR while unpaired and idle. Every
        view change or repaint re-arms, so the clock measures idle time, not
        absolute time since launch."""
        if self._qr_after is not None:
            try:
                self._root.after_cancel(self._qr_after)
            except Exception:
                pass
            self._qr_after = None
        self._qr_after = self._root.after(QR_HIDE_AFTER_MS, self._hide_qr)

    def _hide_qr(self, waiting=False):
        """Swap the QR for its placeholder: after the idle timeout, or — [waiting]
        — while there is no network address to put in it yet."""
        self._qr_after = None
        if waiting:
            self._qr_waiting = True
        if self._qr_img_label is None:
            return                      # no Pillow, or nothing rendered
        self._qr_hidden = True
        try:
            ph = self._qr_placeholder
            ph.itemconfigure(self._ph_text,
                             text=("Waiting for a network connection…"
                                   if self._qr_waiting
                                   else "QR code hidden for privacy\nClick to show"))
            ph.configure(cursor="" if self._qr_waiting else "hand2")
            self._qr_holder.pack_forget()
            ph.pack()
        except Exception:
            pass
        self._resize()      # let the card shrink to the placeholder

    def _show_qr(self):
        if getattr(self, "_qr_waiting", False):
            return                      # nothing valid to show until netchange
        if self._qr_hidden:
            self._qr_hidden = False
            try:
                self._qr_placeholder.pack_forget()
                self._qr_holder.pack()
            except Exception:
                pass
            self._resize()
        self._arm_qr_hide()

    def _regenerate(self):
        try:
            rotate_secrets(self._wire)
        except OSError as e:
            # Nothing was swapped: the current pairing still works (see rotate_secrets).
            self._log(f"Saving the new pairing failed: {e}", "warn")
            self._warn("Couldn't save a new pairing code. Nothing changed — the "
                       "current code still works.")
            return
        self._token = self._wire.token
        if self._ip:
            uri = build_uri(self._ip, self._wire.token, self._hostname,
                            self._wire.key)
            self._repaint_qr(uri)
        try:
            self._token_chip.config(text=self._wire.token)
        except Exception:
            pass
        # serve_loop drops the pinned phone on its next tick (wire.rotated); show
        # it now rather than waiting for that round trip.
        self._set_status(False)
        self._log("New pairing code generated — every phone must scan the new QR", "ok")

    def _confirm_regenerate(self):
        """Rotation kicks every paired phone and invalidates saved pairings —
        destructive enough that one misclick must not do it."""
        try:
            import tkinter.messagebox as mb
            ok = mb.askyesno(
                "New pairing code",
                "Make a new pairing code? The connected phone disconnects now, and "
                "every paired phone must scan the new QR.", parent=self._root)
        except Exception:
            ok = True       # no dialog available — behave as before
        if ok:
            self._regenerate()

    def _toggle_secure(self):
        self._require_secure = not self._require_secure
        self._wire.require_secure = self._require_secure
        self._refresh_secure_btn()
        self._refresh_code_box()
        self._refresh_encryption_pill()
        # Not remembered: every launch starts with encryption required again, so a
        # toggle left off by mistake can't quietly outlive the session.
        self._log("Require encryption: " + ("on" if self._require_secure else
                  "off until LazeR restarts — typed-code (plaintext) pairing allowed"),
                  "ok" if self._require_secure else "warn")
        self._resize()

    def _refresh_secure_btn(self):
        self._secure_btn.set_on(self._require_secure)

    # ── swap QR <-> connected ─────────────────────────────────────────────────
    def _show_qr_view(self):
        try:
            self._connected_border.pack_forget()
        except Exception:
            pass
        self._connect_border.pack(fill="x")
        self._resize()
        if not self._qr_waiting:
            self._show_qr()     # unhide if it had timed out, and restart the clock

    def _show_connected_view(self):
        try:
            self._connect_border.pack_forget()
        except Exception:
            pass
        self._connected_border.pack(fill="x")
        self._resize()
        # Paired: stop the idle-hide clock and leave the QR renderable, so a
        # later disconnect lands on a visible code rather than the placeholder.
        if self._qr_after is not None:
            try:
                self._root.after_cancel(self._qr_after)
            except Exception:
                pass
            self._qr_after = None
        if self._qr_hidden and not self._qr_waiting:
            self._qr_hidden = False
            try:
                self._qr_placeholder.pack_forget()
                self._qr_holder.pack()
            except Exception:
                pass

    def _resize(self):
        self._fit()

    # ── details toggle (hides all the technical stuff) ────────────────────────
    def _build_details_toggle(self, parent):
        tk = self._tk
        bar = tk.Frame(parent, bg=_C["surface"])
        bar.pack(fill="x", pady=(_px(10), 0))
        self._toggle = tk.Label(bar, text="", bg=_C["surface"], fg=_C["primary"],
                                font=self.f_btn, cursor="hand2", compound="right",
                                padx=_px(12), pady=_px(6))
        self._toggle.pack()
        self._clickable(self._toggle, self._toggle_details)
        self._details_open = False
        hover = _blend(_C["surface"], _C["primary"], 0.08)
        self._toggle.bind("<Enter>", lambda e: self._toggle.config(bg=hover))
        self._toggle.bind("<Leave>", lambda e: self._toggle.config(bg=_C["surface"]))
        self._refresh_details_toggle()

    # ── details panel: network info, services, activity ───────────────────────
    def _build_details(self, parent, ip, port, token):
        tk = self._tk
        # Status
        pad = self._card(parent, fill="x", pady=(_px(8), 0), pad=20)
        bg = pad.cget("bg")
        self._section_label(pad, "Connection")

        grid = tk.Frame(pad, bg=bg)
        grid.pack(fill="x", pady=(_px(6), 0))

        def row(label, value):
            r = grid.grid_size()[1]
            tk.Label(grid, text=label, bg=bg, fg=_C["on_variant"], font=self.f_md,
                     anchor="w").grid(row=r, column=0, sticky="w", pady=_px(3))
            val = tk.Label(grid, text=value, bg=bg, fg=_C["on_surface"], font=self.f_val,
                           anchor="w")
            val.grid(row=r, column=1, sticky="w", padx=(_px(16), 0), pady=_px(3))
            return val

        self._ip_value = row("IP address", ip or "no network yet")
        row("Port", str(port))

        # More than one LAN address (Wi-Fi + Ethernet, or a virtual adapter) means
        # we had to CHOOSE which to advertise, and choosing wrong is the classic
        # invisible "phone times out but the server looks fine" failure. Show the
        # alternates so it is diagnosable instead of a mystery.
        others = [a for a, _ in candidate_ips() if a != ip]
        if others:
            row("Also at", ", ".join(others[:3]))

        self._divider(pad)
        self._section_label(pad, "Status")
        vol_ok = VOLUME_BACKEND is not None
        self._pill(pad, "Volume control",
                   VOLUME_BACKEND if vol_ok else "unavailable", vol_ok)
        if phone_mic.supported:
            self._pmic_pill = self._mutable_pill(pad, "Phone microphone", "")
            self._pmic_state = None
            self._pmic_logged_on = False
            self._pmic_quiet_since = None
            self._poll_phone_mic_pill()
        # Both driven by live state: a registered mDNS record (not zeroconf importing),
        # and the Require-encryption toggle.
        self._disc_pill = self._mutable_pill(pad, "Auto-discovery", "checking…")
        self._enc_pill = self._mutable_pill(pad, "Encryption", "")
        self._refresh_encryption_pill()
        if sys.platform.startswith("win"):
            self._fw_pill = self._mutable_pill(pad, "Firewall", "checking…")
        self._upd_pill = self._mutable_pill(
            pad, "Version", f"v{APP_VERSION} — checking for updates…")

        # Settings
        spad = self._card(parent, fill="x", pady=(_px(12), 0), pad=20)
        self._section_label(spad, "Settings")
        self._build_security_controls(spad)
        if sys.platform.startswith("win"):
            self._build_startup_toggle(spad)

        self._build_activity(parent)

    def _divider(self, parent):
        self._tk.Frame(parent, bg=_C["outline_var"], height=1).pack(
            fill="x", pady=(_px(14), _px(12)))

    def _setting_row(self, parent, title, sub):
        """A two-line list item; returns the frame its trailing control packs into."""
        tk = self._tk
        bg = parent.cget("bg")
        f = tk.Frame(parent, bg=bg)
        f.pack(fill="x", pady=(_px(10), 0))
        trail = tk.Frame(f, bg=bg)
        trail.pack(side="right", padx=(_px(12), 0))
        col = tk.Frame(f, bg=bg)
        col.pack(side="left", anchor="w", fill="x", expand=True)
        tk.Label(col, text=title, bg=bg, fg=_C["on_surface"], font=self.f_md,
                 anchor="w").pack(anchor="w")
        tk.Label(col, text=sub, bg=bg, fg=_C["on_variant"], font=self.f_sm, anchor="w",
                 justify="left", wraplength=_px(self.PAGE_W - 170)).pack(anchor="w")
        return trail

    def _refresh_discovery_pill(self):
        pill = getattr(self, "_disc_pill", None)
        if pill is None:
            return
        dot, val = pill
        net = self._net or {}
        if _MDNS_UNAVAILABLE[0]:
            dot.config(fg=_C["outline"]); val.config(text="off — zeroconf not installed")
        elif net.get("zc") is not None:
            dot.config(fg=_C["ok"]); val.config(text=f"announcing {net.get('ip')}")
        elif not net.get("ip"):
            dot.config(fg=_C["outline"]); val.config(text="waiting for a network")
        else:
            dot.config(fg=_C["warn"])
            val.config(text="not announcing (network blocks multicast?) — scan the QR")

    def _poll_discovery_pill(self):
        self._refresh_discovery_pill()
        self._root.after(5000, self._poll_discovery_pill)

    def _refresh_encryption_pill(self):
        pill = getattr(self, "_enc_pill", None)
        if pill is None:
            return
        dot, val = pill
        if not _HAVE_CRYPTO:
            dot.config(fg=_C["warn"])
            val.config(text="unavailable — pip install cryptography")
        elif self._require_secure:
            dot.config(fg=_C["ok"]); val.config(text="required · AES-256-GCM (QR pairing)")
        else:
            dot.config(fg=_C["warn"])
            val.config(text="optional · typed-code phones are NOT encrypted")

    def _build_security_controls(self, parent):
        """Require-encryption switch + new-code button + panic-key hint."""
        trail = self._setting_row(parent, "Require encryption",
                                  "Only phones paired by QR can connect")
        self._secure_btn = self._switch(trail, self._require_secure, self._toggle_secure,
                                        enabled=_HAVE_CRYPTO)
        self._secure_btn.pack()

        trail = self._setting_row(parent, "Pairing code",
                                  "Make a new code and QR; every phone must rescan")
        self._button(trail, "New code", self._confirm_regenerate, kind="tonal",
                     small=True).pack()

        if sys.platform.startswith("win"):
            hint = ("Your own mouse and keyboard always win: touching them pauses the "
                    "phone. Press Ctrl+Alt+Shift+L to stop the remote until you "
                    "click Resume.")
        else:
            # LocalInputGuard is Windows-only: here the laptop's own input does NOT
            # pause the phone, and there is no panic key. Say so rather than show a
            # promise this platform doesn't keep.
            hint = ("On this system the laptop's own mouse doesn't pause the phone and "
                    "there is no panic key. To stop the remote, quit LazeR or tap back "
                    "on the phone.")
        self._hint_box(parent, hint)

    def _hint_box(self, parent, text):
        tk = self._tk
        box = self._card(parent, color=_C["surface_low"], radius=16, pad=12,
                         fill="x", pady=(_px(14), 0))
        ic = self._icon("info", _px(18), _C["on_variant"])
        lead = tk.Label(box, bg=_C["surface_low"], bd=0)
        if ic is not None:
            self._put(lead, ic)
        lead.pack(side="left", anchor="n", padx=(0, _px(10)))
        tk.Label(box, text=text, bg=_C["surface_low"], fg=_C["on_variant"],
                 font=self.f_sm, justify="left", anchor="w",
                 wraplength=_px(self.PAGE_W - 130)).pack(side="left", fill="x")

    def _build_startup_toggle(self, parent):
        """Switch row: launch LazeR automatically when Windows starts."""
        trail = self._setting_row(parent, "Start with Windows",
                                  "Open in the tray when you sign in")
        self._startup_btn = self._switch(trail, startup_enabled(), self._toggle_startup)
        self._startup_btn.pack()

    def _refresh_startup_btn(self):
        self._startup_btn.set_on(startup_enabled())

    def _toggle_startup(self):
        if set_startup(not startup_enabled()):
            self._refresh_startup_btn()
            self._log("Start with Windows: "
                      + ("on" if startup_enabled() else "off"), "info")
        else:
            reason = getattr(set_startup, "last_error", "") or "unknown error"
            self._warn(f"Couldn't change Start with Windows ({reason}). Try again, or "
                       f"run  {_self_invocation()} --enable-startup  in a terminal.")

    def _pill(self, parent, name, detail, ok):
        dot, _val = self._mutable_pill(parent, name, detail)
        dot.config(fg=_C["ok"] if ok else _C["outline"])

    def _mutable_pill(self, parent, name, detail):
        """A two-line status row — name, then a coloured dot and the detail.
        Returns (dot, detail) labels so the status can change later."""
        tk = self._tk
        bg = parent.cget("bg")
        f = tk.Frame(parent, bg=bg)
        f.pack(fill="x", pady=(_px(8), 0))
        tk.Label(f, text=name, bg=bg, fg=_C["on_surface"], font=self.f_md,
                 anchor="w").pack(anchor="w")
        line = tk.Frame(f, bg=bg)
        line.pack(anchor="w", fill="x")
        dot = tk.Label(line, text="●", bg=bg, fg=_C["outline"], font=self.f_sm)
        dot.pack(side="left", anchor="n")
        val = tk.Label(line, text=detail, bg=bg, fg=_C["on_variant"], font=self.f_sm,
                       anchor="w", justify="left", wraplength=_px(self.PAGE_W - 90))
        val.pack(side="left", padx=(_px(6), 0), fill="x")
        return dot, val

    # ── activity feed ─────────────────────────────────────────────────────────
    def _build_activity(self, parent):
        tk = self._tk
        pad = self._card(parent, fill="both", expand=True, pady=(_px(12), 0), pad=20)
        bg = pad.cget("bg")
        self._section_label(pad, "Activity")

        wrap = tk.Frame(pad, bg=bg)
        wrap.pack(fill="both", expand=True, pady=(_px(8), 0))
        sb = self._ttk.Scrollbar(wrap, orient="vertical",
                                 style="Card.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self._log_txt = tk.Text(
            wrap, bg=bg, fg=_C["on_variant"], font=self.f_log,
            height=6, width=48, state="disabled", relief="flat", bd=0, wrap="word",
            cursor="arrow", highlightthickness=0, padx=0, pady=0,
            spacing1=2, spacing3=2, yscrollcommand=sb.set,
            selectbackground=_C["primary_ctr"], selectforeground=_C["on_primary_ctr"],
        )
        self._log_txt.pack(side="left", fill="both", expand=True)
        sb.config(command=self._log_txt.yview)
        self._log_txt.tag_config("ts", foreground=_C["outline"])
        self._log_txt.tag_config("ok", foreground=_C["ok"])
        self._log_txt.tag_config("act", foreground=_C["on_surface"])
        self._log_txt.tag_config("info", foreground=_C["on_variant"])
        self._log_txt.tag_config("warn", foreground=_C["warn"])
        self._log_txt.tag_config("body", lmargin2=self.f_log.measure("00:00:00  ")
                                 if self.f_log else 0)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _toggle_details(self):
        if self._details_open:
            self._details.pack_forget()
            self._details_open = False
        else:
            self._details.pack(fill="both", expand=True)
            self._details_open = True
            self._warnings = 0          # opening the log counts as reading them
        self._refresh_details_toggle()
        self._fit()
        if self._details_open:
            # On a short screen the panel opens below the fold: bring its top up.
            self._root.after(30, self._scroll_details_up)

    def _scroll_details_up(self):
        if not self._scrolls():
            return
        top = self._toggle.winfo_rooty() - self._outer.winfo_rooty()
        self._canvas.yview_moveto(max(0, top - 8) / max(1, self._outer.winfo_reqheight()))

    def _refresh_details_toggle(self):
        icon = "chev_up" if self._details_open else "chev_down"
        if self._details_open:
            text, fg = "Hide details", _C["primary"]
        elif self._warnings:
            n = self._warnings
            text, fg = f"Details · {n} warning{'s' * (n != 1)}", _C["warn"]
        else:
            text, fg = "Details", _C["primary"]
        img = self._icon(icon, _px(18), fg)
        self._toggle.config(text=text + "  ", fg=fg)
        if img is not None:
            self._put(self._toggle, img)
        else:
            self._toggle.config(image="")

    def _center(self):
        self._root.update_idletasks()
        w, h = self._root.winfo_width(), self._root.winfo_height()
        sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        # Never let the window grow past the screen (the details panel can make it
        # very tall). Cap the height — the activity log scrolls to absorb the rest —
        # and clamp the position so the whole window, especially the bottom controls,
        # stays on screen above the taskbar.
        avail = sh - _px(80)
        if h > avail:
            h = avail
            self._root.geometry(f"{w}x{h}")
            self._root.update_idletasks()
        if self._placed:
            # Every connect, banner and details toggle refits the window. Snapping it
            # back to the middle each time undid wherever the user had put it, even
            # on another monitor — so after the first show it stays put, and only
            # moves up when it has grown past the bottom of the screen.
            # sw/sh are the primary screen's, so leave a window on another monitor
            # alone rather than drag it onto the primary's coordinates.
            x, y = self._root.winfo_x(), self._root.winfo_y()
            if 0 <= x < sw and 0 <= y < sh and y + h > sh - _px(48):
                self._root.geometry(f"+{x}+{max(0, sh - h - _px(48))}")
            return
        self._placed = True
        x = max(0, (sw - w) // 2)
        y = max(0, min((sh - h) // 3, sh - h - _px(48)))
        self._root.geometry(f"+{x}+{y}")

    def _copy(self, text):
        self._root.clipboard_clear()
        self._root.clipboard_append(text)
        self._copy_btn.set_text("Copied")
        self._root.after(1100, lambda: self._copy_btn.set_text("Copy"))

    def _log(self, msg, tag="info"):
        import time
        ts = time.strftime("%H:%M:%S")
        t = self._log_txt
        t.configure(state="normal")
        t.insert("end", f"{ts}  ", ("ts", "body"))
        t.insert("end", f"{msg}\n", (tag, "body"))
        t.see("end")
        t.configure(state="disabled")
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 400:
            t.configure(state="normal")
            t.delete("1.0", f"{lines - 400}.0")
            t.configure(state="disabled")

    def _set_status(self, connected, who="", secure=True):
        if connected:
            self._paint_avatar_dot(True)
            txt = "Connected · encrypted" if secure else "Connected · NOT encrypted"
            if who:
                txt += f" · {who.split(':')[0]}"
            self._status.config(fg=_C["ok"] if secure else _C["warn"], text=txt)
            self._show_connected_view()
        else:
            self._paint_avatar_dot(False)
            self._status.config(fg=_C["on_variant"], text="Waiting for your phone…")
            self._show_qr_view()

    def _poll(self):
        try:
            while True:
                ev = self._eq.get_nowait()
                # One malformed/handler-thrown event must never kill the poll loop
                # (that would freeze the status on a stale value forever).
                try:
                    self._handle_event(ev)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._root.after(100, self._poll)

    def _handle_event(self, ev):
        kind = ev[0]
        if kind == "connected":
            secure = ev[2] if len(ev) > 2 else True
            self._set_status(True, ev[1], secure)
            self._log(f"Phone paired · {ev[1]} · "
                      + ("encrypted" if secure else "PLAINTEXT (insecure)"),
                      "ok" if secure else "warn")
        elif kind == "disconnected":
            self._set_status(False)
            self._log("Phone disconnected", "info")
        elif kind == "action":
            self._log(ev[1], ev[2] if len(ev) > 2 else "act")
        elif kind == "netchange":
            self.refresh_network(ev[1], ev[2])
            # The phone re-finds a moved laptop over mDNS and saves the new
            # address; a rescan is only needed where discovery is blocked.
            self._log(f"Now at {ev[1]} — paired phones find it automatically "
                      "(rescan the QR only if yours doesn't reconnect)", "ok")
            self._check_firewall()      # a new network can mean a new profile
        elif kind == "blocked":
            self._warn(f"Blocked a control attempt from {ev[1]} — another phone is "
                       "already connected. Turn Require encryption on to stop "
                       "typed-code takeovers.")
        elif kind == "warn":
            self._warn(ev[1])
        elif kind == "serving":
            self._set_serving(True)
        elif kind == "loopdown":
            self._set_serving(False)
        elif kind == "woke":
            self._check_firewall()
        elif kind == "paused":
            self._set_paused(latched=False)
            self._log("Local input detected — remote paused", "warn")
        elif kind == "resumed":
            self._clear_paused()
            self._log("Remote resumed", "info")
        elif kind == "panic":
            self._set_paused(latched=True)
            self._restore()   # bring window to front so the user sees it
            self._log("PANIC hotkey — remote latched OFF", "warn")
        elif kind == "log":
            self._log(ev[1], "info")
        elif kind == "show":
            self._restore()   # a 2nd launch asked us to come to the front

    def _on_close(self):
        # Hide to the tray if available; otherwise quit.
        if self._tray is not None:
            self._root.withdraw()
            if not self._close_noticed:
                # Closing a window normally ends the program; here it doesn't, and
                # the server keeps accepting the phone. Say so the first time.
                self._close_noticed = True
                try:
                    self._tray.notify("LazeR is still running in the tray. Right-click "
                                      "its icon and choose Quit to stop it.", "LazeR")
                except Exception:
                    pass
        else:
            self._real_quit()

    def run(self):
        self._root.mainloop()
        # Icon.stop() only asks the tray thread to remove the icon. The process is
        # ended outright after this, so wait for the removal, or a dead LazeR icon
        # stays in the notification area until the mouse passes over it.
        if self._tray_thread is not None:
            self._tray_thread.join(timeout=1)


# ── terminal mode ─────────────────────────────────────────────────────────────
def run_terminal(token, key, ip, require_secure, update_check=True):
    """[ip] is the address to publish, or None when there is none yet — then the
    address watch in serve_loop announces it and prints the QR when it appears."""
    hostname = socket.gethostname()
    # Refuse to be the second server before printing anything that suggests we
    # are serving.
    kind_i, lsock_i = singleton_acquire(poke=False)
    if kind_i == "existing":
        print("[error] another LazeR is already running on this laptop. Close it "
              "first — two servers split the phone's packets between them.")
        _exit_process(1)
    busy = _probe_udp_port(retry_s=2.0)
    if busy is not None:
        kind, text, _ = diagnose_port_busy(busy)
        print(f"[error] {text} ({busy})")
        if kind == "reserved":
            print(f"[error] Fix (admin terminal): net stop winnat, then netsh int ipv4 "
                  f"add excludedportrange protocol=udp startport={PORT} "
                  f"numberofports=1, then net start winnat.")
        _exit_process(1)
    uri = build_uri(ip, token, hostname, key) if ip else None
    zc, mdns_info = start_mdns(ip, hostname) if ip else (None, None)
    net = {"ip": ip, "zc": zc, "info": mdns_info}

    print("=" * 44)
    print("  LazeR - server running")
    print("=" * 44)
    print(f"  Laptop    : {hostname}")
    print(f"  Laptop IP : {ip or 'no network yet'}")
    print(f"  Port      : {PORT}")
    if phone_mic.supported:
        # Startup is looking for the cable off-thread; a moment is plenty.
        for _ in range(30):
            if phone_mic.status() != "checking":
                break
            time.sleep(0.1)
        st = phone_mic.status()
        if st in ("ready", "live"):
            print("  Phone mic : ready (choose \"CABLE Output\" in your meeting app)")
        elif st == "missing":
            print(f"  Phone mic : needs VB-CABLE (free): {VB_CABLE_PAGE}")
        elif st == "error":
            print(f"  Phone mic : VB-CABLE won't open ({phone_mic.error})")
    print(f"  Token     : {token}")
    sec = "ON (QR scan)" if _HAVE_CRYPTO else "unavailable (pip install cryptography)"
    print(f"  Encryption: {sec}" + ("  · plaintext blocked" if require_secure else ""))
    print("=" * 44)
    if uri:
        print("  Scan this QR in the LazeR app:")
        print()
        show_qr(uri)
        print()
    else:
        print("  No network address yet — the QR prints once this laptop joins one.")
    print("  Ctrl+C to quit.\n")

    # Update check: notify-only, and the one outbound internet request LazeR makes.
    #
    # On a daemon thread, NOT inline: serve_loop below is what binds the UDP socket,
    # so doing this first would leave phones unable to connect for as long as the
    # request takes — up to UPDATE_TIMEOUT_S against a slow or black-holed network.
    # Nothing about a version check is worth delaying the actual service, so it runs
    # alongside and prints whenever it lands. That can interleave with the first
    # activity lines, which is a fair trade for not stalling the server.
    if update_check:
        def _update_notice():
            tag, newer = check_for_update()
            if newer:
                print(f"\n  [update] {tag} is available (running v{APP_VERSION}):")
                print(f"           {RELEASES_PAGE}\n")
            elif tag is None:
                print("\n  [update] couldn't check for updates (offline?). "
                      "--no-update-check silences this.\n")
        threading.Thread(target=_update_notice, daemon=True).start()

    # Inbound UDP must be allowed or phones silently time out (loopback bypasses
    # the firewall, so the server looks fine here). If we're admin this adds the
    # rule outright; otherwise point at the one-shot self-elevating flag.
    if sys.platform.startswith("win"):
        if not ensure_firewall_rule():
            print(f"  [firewall] Inbound UDP {PORT} appears blocked — phones may not connect.")
            print("             Fix once (accepts a UAC prompt):")
            print(f"               {_self_invocation()} --setup-firewall")
            vpn = detect_vpn()
            if vpn:
                print(f"             VPN '{vpn}' is active — if it blocks LAN, also enable "
                      "'allow local network' in the VPN.")
            print()
        # Reported even when the rule IS present: policy can discard local rules
        # outright, and then a healthy-looking rule blocks every phone anyway.
        advice = firewall_advice()
        if advice:
            print(f"  [firewall] {advice}")
            print()

    def emit(kind, *a):
        if kind == "connected":
            print(f"[handshake] paired: {a[0]}  ({'secure' if a[1] else 'PLAINTEXT'})")
        elif kind == "disconnected":
            print("[handshake] phone left")
        elif kind == "blocked":
            print(f"[security] blocked control attempt from {a[0]} (a phone is already paired)")
        elif kind == "warn":
            print(f"[warn] {a[0]}")
        elif kind == "paused":
            print("[takeover] local input detected — remote paused")
        elif kind == "resumed":
            print("[takeover] remote resumed")
        elif kind == "panic":
            print("[takeover] PANIC hotkey — remote latched OFF. Resume from the "
                  f"GUI window, or run:  {_self_invocation()} --resume")
        elif kind == "netchange":
            print(f"\n[resume] new IP {a[0]} — rescan:\n")
            show_qr(a[1])
            print()
        elif kind == "log":
            print(f"[info] {a[0]}")

    wire = Wire(token, key, require_secure)
    # Serve the loopback control port (claimed above) so a later `--resume` can
    # clear a panic latch here too — the terminal has no Resume button.
    if kind_i == "owner" and lsock_i is not None:
        ctrl_q = queue.Queue()
        threading.Thread(target=singleton_serve,
                         args=(lsock_i, ctrl_q, lambda: wire.token, True),
                         daemon=True).start()

        def _drain_ctrl():
            while not _stop.is_set():
                try:
                    ev = ctrl_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if ev[0] == "resumed":
                    emit("resumed")
                elif ev[0] == "warn":
                    emit("warn", ev[1])
                # "show" has nothing to surface in a terminal; the caller was told
                # so (SHOW_HEADLESS).
        threading.Thread(target=_drain_ctrl, daemon=True).start()

    guard = LocalInputGuard(
        on_physical=lambda: _physical_event(emit),
        on_panic=lambda: _panic_event(emit),
    )
    guard.start()

    try:
        serve_forever(wire, emit, net, hostname)
    except KeyboardInterrupt:
        pass
    finally:
        _exit_watchdog()
        try:
            _stop.set()
            _stop_mdns(net)
        except KeyboardInterrupt:
            pass    # a second Ctrl+C during cleanup still ends in _exit_process
    print("\nServer stopped.")
    _exit_process(0)


def _stop_mdns(net):
    """Withdraw our mDNS announcement, on a time limit. A zeroconf call that
    blocks here must not keep a quitting process alive."""
    if not net["zc"]:
        return

    def work():
        try:
            net["zc"].unregister_service(net["info"])
            net["zc"].close()
        except Exception:
            pass
    w = threading.Thread(target=work, daemon=True)
    w.start()
    w.join(timeout=3)


def _exit_process(code=0):
    """End the process now, once shutdown cleanup has run.

    A normal return leaves exit to interpreter finalization, and a copy of the
    server was found still running two days after it had stopped serving: no
    window, neither port held, yet the process never ended. The cause wasn't
    reproduced (a plain quit, one with a phone's audio reads, and one with input
    flowing through the hooks all exited cleanly), so this closes off the
    outcome: after our own cleanup nothing is left worth waiting for."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


def _exit_watchdog(seconds=10.0):
    """Backstop for the one way out that skips _exit_process: an exception on
    its way up, left to propagate so its traceback (or the exe's error dialog)
    still appears. If the process is still alive [seconds] later, end it. Longer
    than the cleanup's own limits (2s serve join + 3s mDNS)."""
    t = threading.Timer(seconds, os._exit, args=(1,))
    t.daemon = True
    t.start()


# ── local-takeover event helpers (shared by GUI + terminal) ──────────────────
def _physical_event(emit):
    """A physical mouse/key event: pause the remote if a phone is driving."""
    if not _client_connected.is_set():
        return
    _last_physical_ts[0] = time.monotonic()
    if not _remote_paused.is_set():
        _remote_paused.set()
        # The phone's own MUP is dropped while paused, so let go of a held drag
        # button here or the user's mouse keeps dragging.
        release_held_input()
        emit("paused")


def _panic_event(emit):
    """Panic chord: latch the remote off until the user explicitly resumes."""
    _last_physical_ts[0] = time.monotonic()
    if not _panic_latched.is_set():
        _panic_latched.set()
        _remote_paused.set()
        release_held_input()
        emit("panic")


# ── main ──────────────────────────────────────────────────────────────────────
def _attach_parent_console():
    """Give a windowed exe somewhere to print when it was run from a terminal.

    The release exe is built --windowed, so it has no console of its own:
    `LazeR.exe --resume`, `--setup-firewall`, `--help` and every startup notice
    printed to nowhere, and the user saw nothing happen. Attaching to the parent's
    console (if there is one) makes a terminal invocation behave like one. A
    double-click has no parent console, and nothing changes."""
    if not (sys.platform.startswith("win") and getattr(sys, "frozen", False)):
        return False
    if sys.stdout is not None:
        return False                     # a console build: nothing to do
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
            return False
        out = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = out
        print()                          # the prompt was already drawn; start below it
        return True
    except Exception:
        return False


def _dialog_text(text):
    """A console line as dialog text: the "[startup] " style tags help in a
    terminal log, but in a message box they're just developer noise."""
    import re
    lines = [re.sub(r"^\[[a-z]+\] ", "", ln) for ln in text.split("\n")]
    return "\n".join(ln[:1].upper() + ln[1:] for ln in lines)


def _cli_say(text, title="LazeR", error=False, box=False):
    """Report the outcome of a one-shot CLI action. Prints when there is a
    console; otherwise (a shortcut or Run entry to the windowed exe) shows a
    message box, so the result is never silently lost. [box] forces the message
    box on Windows, for the GUI's own start-up errors."""
    if sys.stdout is not None and not (box and sys.platform.startswith("win")):
        print(text)
        return
    if sys.platform.startswith("win"):
        try:
            import ctypes
            icon = 0x30 if error else 0x40   # MB_ICONWARNING / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, _dialog_text(text), title, icon)
        except Exception:
            pass


def _resume_request():
    """The bytes `--resume` sends: RESUME plus this install's token (see
    singleton_serve). Read from the token file, never created here."""
    try:
        with open(TOKEN_FILE, "r") as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    return b"RESUME " + tok.encode("utf-8")


def _resume_exit_code(reply):
    """`--resume`'s exit status for the running copy's [reply]: 0 resumed, 1 refused,
    2 unconfirmed (a copy older than this one resumes without answering)."""
    if reply == RESUME_OK:
        return 0
    return 1 if reply == RESUME_DENIED else 2


def _resume_outcome(reply):
    """What `--resume` reports for the running copy's [reply]."""
    if reply == RESUME_OK:
        return "[resume] remote control resumed."
    if reply == RESUME_DENIED:
        return ("[resume] the running LazeR refused: its pairing code doesn't match "
                "this one's. It may be an older version, or running as another "
                "Windows user.")
    return ("[resume] sent, but the running LazeR didn't confirm (a version older "
            "than this one doesn't). Check its window.")


def _probe_udp_port(retry_s=0.0):
    """None if UDP PORT can be bound, else the OSError. Run before starting, so
    a port already held (another LazeR, or anything else) is reported once, in
    words, instead of as a receive loop that crashes and restarts forever.

    [retry_s] keeps trying for that long first: a LazeR that was just quit (or
    restarted by Start with Windows) can hold the port for a moment while it exits,
    and that is no reason to show anyone an error."""
    deadline = time.monotonic() + retry_s
    while True:
        try:
            s = open_socket()
        except OSError as e:
            if time.monotonic() >= deadline:
                return e
            time.sleep(0.25)
            continue
        s.close()
        return None


# Why the port couldn't be bound. A blanket "another LazeR is running" was wrong
# often enough to send people hunting through Task Manager for a copy that didn't
# exist. In particular WinError 10013 (WSAEACCES) with nobody holding the port is
# Windows' own port reservation: Hyper-V, WSL 2 and Docker reserve blocks of the
# dynamic range (49152–65535, 50505 included) at boot, at random, so it can come
# and go between reboots.
PORT_BUSY_TEXT = (f"UDP port {PORT} is already in use — most likely another copy of "
                  "LazeR (check the tray and Task Manager). Close it and start LazeR "
                  "again. Two servers on one port split the phone's packets, which "
                  "looks like a flaky connection.")


def _udp_port_owner(port):
    """(pid, process name) of whoever holds UDP [port] here, or None. psutil reads
    the owning PID straight from the UDP table — no admin, no subprocess."""
    try:
        import psutil
        for c in psutil.net_connections(kind="udp"):
            if c.laddr and c.laddr.port == port and c.pid and c.pid != os.getpid():
                try:
                    name = psutil.Process(c.pid).name()
                except Exception:
                    name = None
                return c.pid, name
    except Exception:
        pass
    return None


def _is_lazer_process(pid, name):
    """Is [pid] a LazeR server — the exe, or `python remote_server.py`?"""
    own = os.path.basename(sys.executable).lower()
    if name and ("lazer" in name.lower()
                 or (getattr(sys, "frozen", False) and name.lower() == own)):
        return True
    try:
        import psutil
        return any("remote_server" in str(a) for a in psutil.Process(pid).cmdline())
    except Exception:
        return False


def _windows_reserved_range(port):
    """(start, end) of a Windows port reservation covering UDP [port], or None.
    Reads `netsh int ipv4 show excludedportrange` — only ever on this error path.
    The numbers aren't localized; the labels around them are ignored."""
    if not sys.platform.startswith("win"):
        return None
    import subprocess
    try:
        r = subprocess.run(
            [win_exe("netsh.exe"), "int", "ipv4", "show", "excludedportrange",
             "protocol=udp"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=6,
            creationflags=_no_window_flags())
        return parse_reserved_range(r.stdout, port)
    except Exception:
        return None


def parse_reserved_range(text, port):
    for line in (text or "").splitlines():
        nums = re.findall(r"\d+", line)
        if len(nums) >= 2:
            lo, hi = int(nums[0]), int(nums[1])
            if lo <= port <= hi:
                return lo, hi
    return None


def diagnose_port_busy(err):
    """(kind, message, pid) for a failed bind. kind is "lazer" (another LazeR holds
    it — pid set), "other" (another program — pid set), "reserved" (Windows
    reserved it for Hyper-V/WSL/Docker) or "unknown"."""
    owner = _udp_port_owner(PORT)
    if owner is not None:
        pid, name = owner
        if _is_lazer_process(pid, name):
            return ("lazer",
                    f"Another copy of LazeR (process {pid}) is already using UDP port "
                    f"{PORT} but isn't answering — it may be stuck, or running for "
                    "another Windows user.", pid)
        label = name or f"process {pid}"
        return ("other",
                f"UDP port {PORT} is being used by {label} (process {pid}). LazeR needs "
                f"that port — close {label} and start LazeR again.", pid)
    if getattr(err, "winerror", None) == 10013 or getattr(err, "errno", None) == 13:
        rng = _windows_reserved_range(PORT)
        where = f" (ports {rng[0]}–{rng[1]})" if rng else ""
        return ("reserved",
                f"Windows has reserved UDP port {PORT} for Hyper-V, WSL or Docker"
                f"{where}, so LazeR isn't allowed to use it. This isn't another copy "
                "of LazeR. It happens at random after a restart.", None)
    return ("unknown", PORT_BUSY_TEXT, None)


def _end_process(pid, timeout=4.0):
    """Stop [pid]; True once it's gone. Fails (False) for another user's process."""
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout)
        except psutil.TimeoutExpired:
            p.kill()
            p.wait(timeout)
        return True
    except Exception:
        return False


def _reserve_port_for_lazer():
    """Ask Windows (one UAC prompt) to keep PORT out of the dynamic reservations.

    winnat holds the Hyper-V/WSL/Docker reservations, and an exclusion can't be
    added over a range it holds, so it is stopped for the moment the exclusion is
    added and started again — WSL/Docker networking blinks for about a second.
    The exclusion is persistent: later reboots reserve around it.

    winnat is stopped and started again only if it was running: starting a NAT
    service the user (or their setup) had left stopped would be a side effect
    nobody asked for. False when the user declined the UAC prompt."""
    if not sys.platform.startswith("win"):
        return False
    net = f'"{win_exe("net.exe")}"'
    netsh = f'"{win_exe("netsh.exe")}"'
    add = (f'{netsh} int ipv4 add excludedportrange protocol=udp startport={PORT} '
           f'numberofports=1 store=persistent >nul 2>nul')
    if _winnat_running() is False:
        chain = add
    else:       # running, or can't tell: the stop/start pair is what makes room
        chain = (f'{net} stop winnat >nul 2>nul & {add} & '
                 f'{net} start winnat >nul 2>nul')
    return _run_elevated_and_wait(win_exe("cmd.exe"), '/c "' + chain + '"', 60000)


def _winnat_running():
    """True/False for the winnat service, None when it can't be read. psutil asks
    the service manager directly — no `sc.exe`."""
    try:
        import psutil
        return psutil.win_service_get("winnat").status() == "running"
    except Exception:
        return None


def _ask_yes_no(text, title="LazeR", box=False):
    """A Yes/No question: a message box for the windowed app (or whenever [box]
    is set — the GUI must not wait on a terminal prompt nobody is looking at), a
    prompt in a terminal. False when nobody can answer."""
    if sys.platform.startswith("win") and (box or sys.stdout is None):
        try:
            import ctypes
            # MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND; IDYES = 6
            return ctypes.windll.user32.MessageBoxW(
                None, _dialog_text(text), title, 0x4 | 0x20 | 0x10000) == 6
        except Exception:
            return False
    try:
        return input(text + " [y/N] ").strip().lower() in ("y", "yes")
    except Exception:
        return False


def resolve_port_busy(err, box=False):
    """Explain a failed bind and offer the fix that fits. True when the port is
    free afterwards and starting can continue. [box]: ask and report in message
    boxes even when a console is attached (the GUI path)."""
    kind, text, pid = diagnose_port_busy(err)

    def say(msg):
        _cli_say(msg, error=True, box=box)

    if kind == "lazer":
        if _ask_yes_no(text + "\n\nEnd it and start this one?", box=box):
            if _end_process(pid) and _probe_udp_port(retry_s=3.0) is None:
                return True
            say("Couldn't end the other LazeR. Restart Windows, or end it in Task "
                "Manager → Details as the user who started it.")
        return False
    if kind == "reserved":
        if _ask_yes_no(text + f"\n\nLazeR can ask Windows to keep port {PORT} free "
                       "for it from now on. This needs admin once, and briefly "
                       "restarts Windows' NAT service (WSL and Docker networking "
                       "blink for a second).\n\nFix it now?", box=box):
            if not _reserve_port_for_lazer():
                say("Nothing was changed — Windows didn't get admin permission. "
                    "Start LazeR again to retry.")
                return False
            if _probe_udp_port(retry_s=3.0) is None:
                return True
            say("Windows still won't let LazeR use the port. Restart the laptop "
                "and start LazeR again.")
        return False
    say(text + f"\n\n({err})")
    return False


def main():
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1:] != ["--minimized"]:
        _attach_parent_console()

    ap = argparse.ArgumentParser(description="LazeR server")
    ap.add_argument("--no-gui", action="store_true", help="terminal/headless mode")
    ap.add_argument("--minimized", action="store_true",
                    help="start hidden in the system tray (used by Start with Windows)")
    ap.add_argument("--secure-only", action="store_true",
                    help="(deprecated: now the default) reject plaintext v1 clients; "
                         "kept so existing scripts and shortcuts keep working")
    ap.add_argument("--no-update-check", action="store_true",
                    help="never contact GitHub to see if a newer release exists. "
                         "This is the only outbound internet request LazeR makes; "
                         "everything else is LAN-only")
    ap.add_argument("--allow-plaintext", action="store_true",
                    help="permit manual-code (plaintext v1) pairing. Trusted LANs "
                         "only — v1 offers no confidentiality and is replayable")
    ap.add_argument("--resume", action="store_true",
                    help="tell an already-running LazeR to resume remote control "
                         "(clears a panic latch) and exit")
    ap.add_argument("--regenerate", action="store_true",
                    help="make a new pairing code and key (every paired phone must "
                         "scan the new QR) and exit. Restart a running LazeR after")
    ap.add_argument("--setup-firewall", action="store_true",
                    help="add the Windows Firewall inbound rule (self-elevates) and exit")
    ap.add_argument("--enable-startup", action="store_true",
                    help="launch LazeR at Windows login (HKCU Run key, starts in the "
                         "tray) and exit")
    ap.add_argument("--disable-startup", action="store_true",
                    help="remove the launch-at-login registration and exit")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove everything LazeR set up — the launch-at-login entry, "
                         "its firewall rule (one admin prompt) and the saved pairing "
                         "— and exit. Then delete the exe")
    args = ap.parse_args()
    if not args.uninstall:
        migrate_legacy_secrets()

    if args.enable_startup or args.disable_startup:
        if not sys.platform.startswith("win"):
            _cli_say("[startup] launch-at-login is Windows-only.", error=True)
            return
        want = args.enable_startup
        if set_startup(want):
            _cli_say("[startup] LazeR now starts in the tray when you sign in to "
                     "Windows. Turn it off in Task Manager → Startup apps or in "
                     "LazeR's Show details." if want else
                     "[startup] LazeR no longer starts when you sign in to Windows.")
        else:
            reason = getattr(set_startup, "last_error", "") or "unknown error"
            _cli_say(f"[startup] could not change launch-at-login: {reason}", error=True)
        return

    if args.uninstall:
        _cli_say(uninstall(), error=bool(getattr(uninstall, "failed", False)))
        return

    if args.resume:
        try:
            c = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=1.0)
        except OSError:
            _cli_say("[resume] no running LazeR found — start it first.", error=True)
            sys.exit(1)
        reply = b""
        try:
            c.sendall(_resume_request())
            c.settimeout(2.0)
            reply = c.recv(16)
        except OSError:
            pass
        finally:
            c.close()
        code = _resume_exit_code(reply)
        _cli_say(_resume_outcome(reply), error=code != 0)
        sys.exit(code)

    if args.regenerate:
        wire = Wire(load_or_create_token(), load_or_create_key(), True)
        try:
            token, _ = rotate_secrets(wire)
        except OSError as e:
            _cli_say(f"[pairing] could not save a new code ({e}). Nothing changed. "
                     f"Is {_DATA_DIR} writable?", error=True)
            return
        _cli_say(f"[pairing] new pairing code {token} and key saved. Every paired "
                 "phone must scan the new QR. A LazeR that is already running keeps "
                 "the old code until it is restarted.")
        return

    if args.setup_firewall:
        if not sys.platform.startswith("win"):
            _cli_say("[firewall] --setup-firewall is Windows-only; on macOS/Linux allow "
                     f"inbound UDP {PORT} in your firewall.", error=True)
            return
        ok = ensure_firewall_rule(allow_elevate=True)
        if ok:
            msg = "[firewall] Windows Firewall now lets phones reach LazeR."
        else:
            msg = ("[firewall] the rule was not added: the admin prompt was declined "
                   "or failed. Run this again and choose Yes.")
        # Say this even on success — the rule can be in place and still inert.
        advice = firewall_advice()
        if advice:
            msg += f"\n[firewall] {advice}"
        _cli_say(msg, error=not ok or bool(advice))
        return

    if args.no_gui and sys.stdout is None:
        # The windowed exe started with --no-gui and no terminal to attach to: that
        # would be a server with no window, no tray and no way to quit.
        _cli_say("--no-gui runs LazeR in a terminal. Start it from Command Prompt or "
                 "PowerShell, or leave the flag off to get the window.")
        return

    token = load_or_create_token()
    key = load_or_create_key()
    # Encryption is required unless explicitly waived. QR pairing is the primary
    # path and gives every user a key; manual-code entry is the rare fallback and is
    # plaintext, so defaulting it OFF means the safe wire is what you get by default
    # instead of what you have to know to ask for.
    # --secure-only is now the default and kept only so old shortcuts still run, but
    # if someone passes BOTH flags the explicit request for security has to win —
    # resolving a contradiction toward the weaker wire is how a "harmless" leftover
    # flag in a script silently turns encryption off.
    require_secure = not args.allow_plaintext or args.secure_only
    # Startup notices, (tag, text). Printed in a terminal; the window logs them and
    # raises the warnings, since the windowed exe has no terminal to print them to.
    notices = []
    if SECRET_WRITE_ERRORS:
        notices.append(("warn", "Couldn't save the pairing code, so phones will need to "
                                "scan the QR again after LazeR closes. Check that "
                                f"{_DATA_DIR} can be written to."))
    for err in SECRET_WRITE_ERRORS:
        notices.append(("info", f"Secret file not saved: {err}"))
    if args.allow_plaintext and args.secure_only:
        notices.append(("warn", "--secure-only and --allow-plaintext conflict; "
                                "honouring --secure-only (encryption required)."))
    elif args.allow_plaintext:
        notices.append(("warn", "Typed-code (plaintext) pairing is ALLOWED — trusted "
                                "LANs only. Omit --allow-plaintext to require encryption."))
    elif args.secure_only:
        notices.append(("info", "--secure-only is the default now; the flag can be "
                                "dropped."))
    # Publish only a usable address (see usable_lan_ip: autostart often runs before
    # Wi-Fi associates). serve_loop's address watch announces the real one once it
    # exists, and the window paints the QR then.
    pub_ip = usable_lan_ip()
    # Say so loudly when the default route is NOT the address we advertise: it means
    # a virtual/VPN adapter owns the route, which used to silently put an
    # unreachable IP in the QR.
    _probe = _default_route_ip()
    if pub_ip and _probe and _probe != pub_ip:
        notices.append(("warn", f"The default route is {_probe}, which phones cannot "
                                f"reach (virtual/VPN adapter) — advertising {pub_ip} "
                                "instead."))
    if pub_ip is None:
        notices.append(("info", "No network address yet — the QR appears once this "
                                "laptop joins a network."))
    hostname = socket.gethostname()

    if not args.no_gui:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print("[gui] tkinter unavailable — falling back to terminal mode")
        else:
            inst_kind, inst_ret = singleton_acquire()
            if inst_kind == "existing":
                if inst_ret == SHOW_HEADLESS:
                    _cli_say("LazeR is already running in a terminal window (no app "
                             "window to show). Close that one to use the app.")
                else:
                    print("LazeR is already running — opened the existing window.")
                return
            busy = _probe_udp_port(retry_s=2.0)
            if busy is not None and not resolve_port_busy(busy, box=True):
                return
            migrate_startup_command()
            if apply_startup_default():
                notices.append(("info", "LazeR will now start in the tray when you sign "
                                        "in to Windows. Turn this off with Start with "
                                        "Windows in Details, or in Task Manager → "
                                        "Startup apps."))

            eq = queue.Queue()
            wire = Wire(token, key, require_secure)
            if inst_ret is not None:
                threading.Thread(target=singleton_serve,
                                 args=(inst_ret, eq, lambda: wire.token),
                                 daemon=True).start()
            if pub_ip:
                zc, mdns_info = start_mdns(pub_ip, hostname)
            else:
                zc, mdns_info = None, None
            net = {"ip": pub_ip, "zc": zc, "info": mdns_info}

            def emit(kind, *a):
                if kind == "action":
                    verb, rest = a
                    fn = _ACTION_LABELS.get(verb)
                    if fn:
                        r = fn(rest)
                        if r:
                            eq.put(("action", r[0], r[1]))
                else:
                    eq.put((kind, *a))

            guard = LocalInputGuard(
                on_physical=lambda: _physical_event(emit),
                on_panic=lambda: _panic_event(emit),
            )
            guard.start()

            t = threading.Thread(target=serve_forever,
                                 args=(wire, emit, net, hostname), daemon=True)
            t.start()

            try:
                LazeRWindow(pub_ip, PORT, wire, eq, require_secure,
                            update_check=not args.no_update_check, net=net,
                            notices=notices, minimized=args.minimized).run()
            except KeyboardInterrupt:
                pass
            finally:
                _exit_watchdog()
                try:
                    _stop.set()
                    t.join(timeout=2)
                    _stop_mdns(net)
                    print("Server stopped.")
                except KeyboardInterrupt:
                    pass
            _exit_process(0)

    for tag, text in notices:
        print(f"[{'warn' if tag == 'warn' else 'info'}] {text}")
    run_terminal(token, key, pub_ip, require_secure,
                 update_check=not args.no_update_check)


if __name__ == "__main__":
    main()
