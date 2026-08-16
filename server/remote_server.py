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
APP_VERSION = "2.0.1"
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
# After a wake, the NIC is usually still coming up: lan_ip() called at that instant
# returns the loopback fallback, and re-announcing THAT poisons mDNS with 127.0.0.1
# for the rest of the process. Wait (bounded) for a real address before announcing.
NET_SETTLE_S = 45
SERVICE_TYPE = "_lazer._udp.local."
# Resource vs. writable paths differ when frozen into a PyInstaller onefile exe:
# bundled data lives in the temp _MEIPASS extraction dir (read-only), while the
# token must persist next to the exe across launches.
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR = _BUNDLE_DIR
TOKEN_FILE = os.path.join(_APP_DIR, ".lazer_token")
KEY_FILE = os.path.join(_APP_DIR, ".lazer_key")
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
_client_connected = threading.Event()  # a phone is currently paired
_last_physical_ts = [0.0]            # monotonic time of last physical input
_input_guard = [None]                # the live LocalInputGuard, for serve_loop's wake handling
PHYSICAL_RESUME_GRACE_S = 2.0        # auto-resume this long after local input stops
RATE_WINDOW_S = 10                   # rejected-packet rate window
RATE_MAX_BAD = 80                    # >this many rejected packets/window ⇒ warn (brute/flood)


def resume_remote():
    """User chose to resume — clear both the soft pause and the panic latch."""
    _panic_latched.clear()
    _remote_paused.clear()


# ── volume ────────────────────────────────────────────────────────────────────
def make_volume():
    """Return (get_volume, set_volume, label); fns may be None if unavailable."""
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
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except Exception as e:
            print(f"[volume] pycaw unavailable ({e}); pip install pycaw")
            return None, None, None

        _ep = [None]

        def _endpoint():
            if _ep[0] is None:
                # COM is per-thread, and comtypes only initializes the thread that
                # imports it — the main one. Re-acquisition happens on the UDP
                # receive thread, where GetSpeakers() otherwise fails every time
                # with "CoInitialize has not been called" (measured). That would
                # have made this whole recovery path a no-op: the first stale
                # endpoint would leave volume dead until a restart, which is the
                # symptom it exists to remove. Idempotent, so calling it on each
                # re-acquire is free after the first.
                import comtypes
                try:
                    comtypes.CoInitialize()
                except OSError:
                    # RPC_E_CHANGED_MODE: this thread already has an apartment of
                    # the other kind. That is fine — it has one, which is all we
                    # need. Anything else surfaces on the GetSpeakers call below.
                    pass
                devices = AudioUtilities.GetSpeakers()
                vol = getattr(devices, "EndpointVolume", None)
                if vol is None:
                    from ctypes import cast, POINTER
                    from comtypes import CLSCTX_ALL
                    iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    vol = cast(iface, POINTER(IAudioEndpointVolume))
                _ep[0] = vol
            return _ep[0]

        def _on_endpoint(call):
            """Run [call] against the endpoint, re-acquiring once if it has gone
            stale. Returns None if even a fresh endpoint fails (no output device at
            all), so callers can degrade instead of raising."""
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
            _on_endpoint(lambda e: e.SetMasterVolumeLevelScalar(pct / 100.0, None))

        try:
            _endpoint()          # fail fast at startup, exactly as before
        except Exception as e:
            print(f"[volume] pycaw unavailable ({e}); pip install pycaw")
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


# ── brightness ──────────────────────────────────────────────────────────────
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
            return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
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
                p = subprocess.Popen(["powershell", "-NoProfile", "-Command", "-"],
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


def do_system(action):
    """lock | sleep | mute — best effort per OS."""
    plat = sys.platform
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
            os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        elif plat == "darwin":
            os.system("pmset sleepnow")
        else:
            os.system("systemctl suspend 2>/dev/null")
        return


# ── crypto: secure wire (v2) ──────────────────────────────────────────────────
# Two wire formats coexist so existing flows keep working:
#   v1 (legacy, PLAINTEXT):  "<TOKEN> <VERB> [args]"   — trusted LAN only.
#   v2 (SECURE):  b"L2" || sid(4) || counter(8 BE) || AES-256-GCM(ct+tag)
#     nonce = sid||counter (12 B) · AAD = b"L2"||sid||counter · plaintext = "VERB [args]"
# The 256-bit key is shipped in the QR (and auto-discovery never carries it). A valid
# GCM tag *is* the authentication (proves key possession) — no token on the wire —
# and the monotonic counter gives replay protection. Sniffing/forgery/replay all fail.
# Two wire dialects, differing ONLY in how the 12-byte GCM nonce is split. The
# header is 14 bytes either way (magic 2 + nonce 12), so nothing else about framing
# changes:
#   L2 (legacy)  sid(4) | counter(8)   — 32-bit session space
#   L3 (current) sid(8) | counter(4)   — 64-bit session space
#
# Why: the key is PERSISTENT across launches while the sid is random per session, so
# a sid collision means GCM nonce reuse under the same key — which leaks the
# authentication key, not just a plaintext. A 4-byte sid puts that at the birthday
# bound of 2^32: ~1.2% odds by 10k sessions, ~39% by 65k. Every reconnect mints a
# session and the watchdog reconnects on any drop, so those numbers are reachable
# over a phone's lifetime. Moving four bytes from the counter to the sid buys 2^64
# at zero real cost: a 4-byte counter still allows 4.29e9 packets in one session.
#
# L2 is accepted for one release so a phone that hasn't been updated keeps working;
# it is scheduled for removal the release after v2.0. Do not add a third dialect
# without also updating the golden vectors in server/tests/test_wire.py AND
# android/.../SecureChannelTest.kt — they are what stop the two implementations
# drifting apart.
MAGIC_V2 = b"L2"
MAGIC_V3 = b"L3"
WIRE_FORMATS = {MAGIC_V2: (4, 8), MAGIC_V3: (8, 4)}   # magic -> (sid_len, ctr_len)


try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:
    AESGCM = None
    _HAVE_CRYPTO = False


def load_or_create_key():
    """Persistent 32-byte key (base64url in KEY_FILE), reused across launches."""
    import base64
    try:
        with open(KEY_FILE, "r") as f:
            raw = base64.urlsafe_b64decode(f.read().strip() + "===")
            if len(raw) == 32:
                return raw
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        with open(KEY_FILE, "w") as f:
            f.write(base64.urlsafe_b64encode(key).rstrip(b"=").decode())
        if not sys.platform.startswith("win"):
            os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def key_b64(key):
    import base64
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode()


def rotate_secrets(wire):
    """Generate a fresh token + key, persist them, and update the live Wire.
    Any currently-paired phone is invalidated (must rescan) — a one-click 'kick'."""
    import base64
    token = "".join(secrets.choice(string.ascii_uppercase + string.digits)
                    for _ in range(TOKEN_LEN))
    key = secrets.token_bytes(32)
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(token)
    except OSError:
        pass
    try:
        with open(KEY_FILE, "w") as f:
            f.write(base64.urlsafe_b64encode(key).rstrip(b"=").decode())
        if not sys.platform.startswith("win"):
            os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    wire.token = token
    wire.key = key
    wire.aes = AESGCM(key) if (key and _HAVE_CRYPTO) else None
    wire.cli_magic, wire.cli_sid, wire.cli_ctr = None, None, -1
    wire.secure_client = False
    # Fresh key ⇒ start the server's send sessions over. Not strictly required (a
    # repeated nonce under a DIFFERENT key is harmless), but it keeps the invariant
    # "one (key, dialect, sid) never reuses a counter" true without a caveat.
    wire._srv.clear()
    return token, key


class Wire:
    """Per-server auth + encrypt/decrypt + replay state for one controller."""

    def __init__(self, token, key, require_secure):
        self.token = token
        self.key = key
        self.require_secure = require_secure
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
                return None                     # bad tag ⇒ forged/corrupt ⇒ drop
            text = pt.decode("utf-8", "ignore")
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
                return None                     # not the pinned client / replay / reorder
            self.cli_ctr = ctr
            return verb, rest, True
        # plaintext v1
        if self.require_secure:
            # Nothing is accepted here — but tell apart "a real phone tried to pair
            # with the manual code" from random junk, so the UI can explain the
            # refusal. Otherwise flipping the default to secure-only turns manual
            # pairing into an unexplained timeout, which is a worse experience than
            # the insecure wire it replaced.
            try:
                head = data.decode("utf-8", "ignore").split(" ", 2)
                if len(head) >= 2 and secrets.compare_digest(head[0], self.token):
                    self.plaintext_refused = True
            except Exception:
                pass
            return None
        try:
            text = data.decode("utf-8", "ignore").rstrip("\r\n")
        except Exception:
            return None
        parts = text.split(" ", 2)
        if len(parts) < 2:
            return None
        if not secrets.compare_digest(parts[0], self.token):
            return None
        return parts[1], (parts[2] if len(parts) > 2 else ""), False

    def issue_challenge(self, sock, addr, now, magic=None):
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
        the server's latest, and the relay handshake could never complete."""
        if self.aes is None:
            return
        if magic is None:
            magic = self._pending[0] if self._pending else self.wire_magic
        ent = self._chal.get(addr)
        if ent is not None and now <= ent[1]:
            nonce = ent[0]                      # reuse the live challenge for this addr
            # Keep the nonce (that idempotency is the point) but track the dialect of
            # the LATEST HELLO, in case the client retried on the legacy fallback.
            self._chal[addr] = (nonce, ent[1], magic)
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
            self._chal[addr] = (nonce, now + CHAL_TTL_S, magic)
        self._seal_reply(sock, addr,
                         "CHAL " + base64.urlsafe_b64encode(nonce).rstrip(b"=").decode(),
                         magic=magic)

    def verify_challenge(self, addr, rest, now):
        """True iff [rest] echoes the fresh, unexpired challenge issued to [addr].
        Single-use: the challenge is consumed whether or not it matches."""
        ent = self._chal.pop(addr, None)
        if ent is None:
            return False
        nonce, exp = ent[0], ent[1]
        if now > exp:
            return False
        try:
            got = base64.urlsafe_b64decode(rest.strip() + "=" * (-len(rest.strip()) % 4))
        except Exception:
            return False
        return secrets.compare_digest(got, nonce)

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
        return True

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
            """True iff the panic modifiers are physically down at this instant.

            Asked of the OS rather than read from `self._down`, for two reasons.
            The set holds side-specific codes the generic constants never match
            (see PANIC_KEY), and it is not trustworthy anyway: hooks miss key-UP
            events across a lock screen / UAC prompt / session switch, so a
            modifier can linger in it long after the user let go. GetAsyncKeyState
            answers for the real keyboard, and for either Shift. Three register
            reads, cheap enough for a hook callback."""
            return all(user32.GetAsyncKeyState(vk) & 0x8000
                       for vk in self.PANIC_MODIFIERS)

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

        kb_hook, ms_hook = install()
        # Set LAST: rearm() posts to this id, and a caller that read it before the
        # hooks existed would ping a thread with nothing to re-install.
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        while not _stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
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
                new_kb, new_ms = install()
                if new_kb and new_ms:
                    user32.UnhookWindowsHookEx(kb_hook)
                    user32.UnhookWindowsHookEx(ms_hook)
                    kb_hook, ms_hook = new_kb, new_ms
                else:                       # keep what still works
                    if new_kb:
                        user32.UnhookWindowsHookEx(new_kb)
                    if new_ms:
                        user32.UnhookWindowsHookEx(new_ms)
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(kb_hook)
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
        mouse.move(dx, dy)

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

    elif verb == "MUP":          # drag-lock: release
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
        if not p.isdigit():
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
            data = json.loads(r.read(64_000).decode("utf-8", "ignore"))
    except Exception:
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag.strip() else None


def check_for_update(url=RELEASES_API, timeout=UPDATE_TIMEOUT_S):
    """(tag, is_newer) for the newest release, or (None, False) if unknown."""
    tag = fetch_latest_release(url, timeout)
    if tag is None:
        return None, False
    return tag, is_newer_version(tag, APP_VERSION)


def open_socket():
    """Fresh bound UDP socket. Recreated after resume — a socket bound before the
    laptop slept can stop receiving once the NIC cycles, so we rebind to recover."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Windows raises WSAECONNRESET (ConnectionResetError) on a UDP socket's *next*
    # recv after we send to an endpoint with no listener — which happens routinely
    # when we reply to a phone that has just vanished (app killed, Wi-Fi dropped).
    # SIO_UDP_CONNRESET off stops that spurious error tearing down the recv loop.
    if sys.platform.startswith("win"):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except (AttributeError, OSError):
            pass
    sock.settimeout(1.0)   # idle wake-ups: 1/s is plenty for the resume/idle/rate checks
    sock.bind((HOST, PORT))
    return sock


def singleton_acquire():
    """Single-instance guard over a loopback control port.

    Returns ("existing", None) if another instance is already running (and was
    signaled to surface its window), ("owner", lsock) if we are the first
    instance (lsock is the control listener to serve), or ("solo", None) if the
    guard couldn't be set up (proceed unguarded)."""
    # Already running? Connect to its control port and ask it to come forward.
    try:
        c = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=0.5)
        try:
            c.sendall(b"SHOW")
        except OSError:
            pass
        finally:
            c.close()
        return "existing", None
    except OSError:
        pass
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


def singleton_serve(lsock, eq):
    """Accept loopback 'SHOW' pokes from later launches; surface the window."""
    while not _stop.is_set():
        try:
            conn, _ = lsock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            conn.recv(16)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
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
                return tok
    except OSError:
        pass
    tok = "".join(secrets.choice(string.ascii_uppercase + string.digits)
                  for _ in range(TOKEN_LEN))
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(tok)
    except OSError:
        pass
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


def _startup_command():
    """Command string to register under Run — handles frozen .exe and .py."""
    if getattr(sys, "frozen", False):              # PyInstaller bundle
        return f'"{sys.executable}"'
    # Source run: prefer pythonw so no console window pops at login.
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand
    return f'"{pyw}" "{SCRIPT_PATH}"'


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


# ── Windows Firewall: auto-allow inbound UDP so phones can reach us ───────────
# The phone's packets are inbound UDP on PORT. Windows Defender Firewall drops
# them unless an allow rule exists — and loopback bypasses the firewall, so the
# server looks healthy locally while every phone times out. We add the rule for
# the user (one UAC click) instead of making them run netsh by hand. Third-party
# firewalls and VPN LAN-blocking are out of our reach; we detect + warn for those.
FW_RULE_NAME = "LazeR UDP 50505"
_FW_NETSH_ARGS = (
    f'advfirewall firewall add rule name="{FW_RULE_NAME}" dir=in action=allow '
    f'protocol=UDP localport={PORT} profile=private,domain'
)


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
    """True if our inbound allow rule is present. Read-only — needs no admin."""
    if not sys.platform.startswith("win"):
        return True   # we only manage Windows Firewall; assume OK elsewhere
    import subprocess
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FW_RULE_NAME}"],
            capture_output=True, text=True, timeout=6,
            creationflags=_no_window_flags())
        return r.returncode == 0 and "No rules match" not in r.stdout
    except Exception:
        return False


def _firewall_add_direct():
    """Add the rule in-process (succeeds only if already elevated)."""
    import subprocess
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={FW_RULE_NAME}", "dir=in", "action=allow",
             "protocol=UDP", f"localport={PORT}", "profile=private,domain"],
            capture_output=True, text=True, timeout=8,
            creationflags=_no_window_flags())
    except Exception:
        pass
    return firewall_rule_exists()


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
    if firewall_rule_exists():
        return True
    if is_admin():
        return _firewall_add_direct()
    if allow_elevate:
        _run_elevated_and_wait("netsh", _FW_NETSH_ARGS)
        return firewall_rule_exists()
    return False


def detect_vpn():
    """Best-effort: name of an active VPN-ish interface, or None. A VPN that
    full-tunnels or blocks LAN can stop phones reaching us even past the firewall;
    we can't override that, but we can tell the user where to look."""
    try:
        import psutil
        stats = psutil.net_if_stats()
    except Exception:
        return None
    needles = ("vpn", "forti", "wireguard", "openvpn", "tap-", "tun",
               "zscaler", "globalprotect", "anyconnect", "cisco", "nordlynx",
               "expressvpn", "pangp", "wintun")
    for name, st in stats.items():
        if getattr(st, "isup", False) and any(n in name.lower() for n in needles):
            return name
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


def _emit_action(event_q, verb, rest):
    fn = _ACTION_LABELS.get(verb)
    if fn is None:
        return
    res = fn(rest)
    if res:
        event_q.put(("action", res[0], res[1]))


def serve_loop(wire, emit, net, hostname):
    """The one UDP loop, used by both GUI and terminal modes.

    [wire] handles auth/encrypt/replay. [emit](kind, *args) reports events
    (GUI → queue, terminal → print). [net] is a shared {"ip","zc","info"} dict
    for mDNS re-announce after resume."""
    sock = open_socket()
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
                emit("warn", f"Could not re-announce on the network ({e})")
            finally:
                announce_busy.clear()

        threading.Thread(target=work, daemon=True).start()

    net_checked = last_tick   # wall-clock of the last own-address check
    net_pending = False       # our published address is known-stale; keep trying
    net_pending_since = 0.0   # when that became true, to bound the post-wake wait

    def drop_client():
        nonlocal client
        if client is not None:
            appswitch_reset()
            client = None
            _client_connected.clear()
            emit("disconnected")
        # Whether or not an address was pinned, forget the crypto session that went
        # with it. Leaving it behind meant a phone that vanished kept a sid/counter
        # watermark alive on our side, and the next session had to talk its way past
        # the leftovers instead of starting clean.
        wire.unpin_client()

    while not _stop.is_set():
        now = time.time()
        mono = time.monotonic()

        # Resume detection: a tick gap far longer than the 1s recv timeout means
        # the process was frozen (laptop slept). Rebind + re-announce so a phone
        # can reach us again without a restart.
        if now - last_tick > RESUME_GAP_S:
            try:
                sock.close()
            except OSError:
                pass
            # Retry the rebind until it succeeds. On wake the OS may not have released
            # the old port yet, or the NIC may still be coming up, so a single
            # open_socket() can raise OSError. The old code bailed here with
            # `last_tick = time.time(); continue`, which DISARMED the resume-gap check
            # (now - last_tick > RESUME_GAP_S could never fire again) and left `sock`
            # pointing at the already-closed socket — so the recv loop spun forever on
            # a dead socket and the server went permanently deaf after sleep until a
            # manual restart. Loop instead, so we always come out with a live socket.
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
            # Keep the phone pinned, but stop giving it a FULL idle window to prove
            # it's still there.
            #
            # The pin costs nothing to hold: HELLO/AUTH are accepted from any source,
            # so a phone that rebuilt its session (new port, new sid — what its
            # watchdog does after ~2.5s of silence, i.e. always, since the gap that
            # got us here is longer than that) re-pairs immediately regardless. And on
            # the rarer path where BOTH devices slept, the phone's socket really did
            # survive and holding the pin means it resumes with no interruption at all.
            #
            # What was wrong was the idle window. `last_pkt = now` handed the departed
            # phone a fresh CLIENT_IDLE_S, so the window sat there claiming a phone was
            # connected for 12s after a wake, every time. Give it a short grace instead:
            # a phone that is genuinely still there speaks well inside it, and one that
            # isn't is reported gone promptly.
            appswitch_reset()
            last_pkt = time.time() - max(0, CLIENT_IDLE_S - POST_WAKE_GRACE_S)
            # Hooks are commonly revoked across a wake or a lock, and key-ups during
            # one are never delivered — so re-install and forget any held keys.
            if _input_guard[0] is not None:
                _input_guard[0].rearm()
            # A wake often lands with the panic/pause latch set from whatever the user
            # touched on the way in (or before sleeping). Clearing the SOFT pause is
            # safe — it re-arms on the next physical event 2s later. The panic latch is
            # deliberate, so it stays; the false-trigger it used to suffer is fixed in
            # LocalInputGuard.rearm/chord_held.
            _remote_paused.clear()
            # Re-announce, but NOT with whatever lan_ip() says at this instant. This
            # ran before the rebind and published the answer unconditionally, so a
            # wake that beat the Wi-Fi association — the normal case — advertised the
            # 127.0.0.1 fallback over mDNS and painted it into the QR, and it stayed
            # that way until the app was restarted. Just mark the address stale; the
            # watch below publishes it once there's a real one to publish.
            net_pending, net_pending_since, net_checked = True, now, 0.0
            emit("log", "Woke from sleep — restoring the network…")
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
                # No LAN address yet (still associating, or genuinely offline).
                # Publishing the loopback fallback here is what poisoned discovery,
                # so publish nothing and look again on the next tick.
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
                # previous attempt failed — that last case used to store the failure
                # and never look again, silently losing discovery for good. A machine
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
            wire.sweep_challenges(mono)   # drop expired, unanswered HELLO challenges

        # Local takeover auto-resume: once physical input has been quiet for the
        # grace period (and no panic latch), let the remote drive again.
        if (_remote_paused.is_set() and not _panic_latched.is_set()
                and (mono - _last_physical_ts[0]) > PHYSICAL_RESUME_GRACE_S):
            _remote_paused.clear()
            emit("resumed")

        # Hold the machine awake while the phone is actively driving it, and let go
        # once it goes quiet. Remote control never counted as activity — the moves
        # go through SetCursorPos, which does not advance the system's idle timer —
        # so a session run entirely from the phone would blank the display and, with
        # a lock-on-wake policy, lock the laptop out from under the user. Only the
        # transitions are pushed, so a steady session costs one call, not one a
        # second; and releasing on quiet means an idle phone still lets the laptop
        # sleep normally. See make_idle_suppressor for why this is a wake hold
        # rather than fabricated mouse input.
        if hold_awake is not None:
            last = _last_remote_ts[0]
            want_awake = (client is not None and last is not None
                          and (mono - last) < REMOTE_AWAKE_S)
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
        except OSError:
            # A transient network error (e.g. host/net-unreachable surfacing on the
            # next recv after a send to a departed phone) must NOT tear the loop down
            # for good. Pause briefly and keep serving; a real dead socket is handled
            # by the sleep/resume rebind above.
            time.sleep(0.1)
            continue
        if not data:
            continue

        res = wire.parse(data, addr, client)
        if res is None:
            bad += 1
            if wire.plaintext_refused:
                wire.plaintext_refused = False
                if not plaintext_hinted:
                    plaintext_hinted = True
                    emit("warn", "A phone tried to pair with the typed code, but "
                                 "Require encryption is on — scan the QR instead "
                                 "(or restart with --allow-plaintext).")
            if bad > RATE_MAX_BAD and not warned:
                emit("warn", "High rate of rejected packets — possible brute-force / flood")
                warned = True
            continue
        verb, rest, secure = res

        if verb == "HELLO":
            if secure:
                # Don't pin on HELLO — it's replayable. Answer with a one-time
                # challenge; only an AUTH echoing it (which needs the key to seal)
                # pins control. Defeats captured-session replay by a keyless attacker.
                wire.issue_challenge(sock, addr, mono)
            else:
                # v1 plaintext HELLO (legacy, trusted-LAN only; never reachable when
                # remote access forces secure-only). Token match already gated it in
                # parse; pin directly. A plaintext re-pin is logged as a warning.
                repin = client is not None and addr != client
                appswitch_reset()
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
            if secure and wire.verify_challenge(addr, rest, mono):
                appswitch_reset()
                client = addr
                last_pkt = now
                wire.commit_hello(True)         # baseline = this AUTH's sid/counter
                _client_connected.set()
                emit("connected", f"{addr[0]}:{addr[1]}", True)
                wire.reply(sock, addr, "OK")
            continue

        if verb == "BYE":
            if addr == client:
                drop_client()
            continue

        if addr != client:
            # Authenticated but not from the pinned phone. Reachable on the v1
            # plaintext wire, where a token match is the only gate — so this is a
            # second phone (or someone who learned the code) trying to take over a
            # live session. The GUI and terminal have always had a handler for this
            # event, but nothing ever emitted it, so the attempt was invisible.
            # On the v2 wire the same attempt is rejected inside wire.parse (wrong
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
                    wire.reply(sock, addr, f"VOL {vol}")
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
                emit("warn", f"{verb} failed and was ignored "
                             f"({type(e).__name__}: {e})")

    # Drop the wake hold on the way out — shutdown, or a crash the supervisor is
    # about to restart us from. It is thread state, so a restarted loop starts on a
    # fresh thread with none of it; leaking it here would pin the display awake for
    # the life of the process with nothing left to release it.
    if hold_awake is not None and awake_held:
        hold_awake(False)
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
        try:
            serve_loop(wire, emit, net, hostname)
            return                      # clean exit — _stop was set
        except Exception as e:
            failures += 1
            emit("warn", f"Receive loop crashed and was restarted "
                         f"({type(e).__name__}: {e})")
            # Back off a little so a hard-failing loop (e.g. the port genuinely
            # taken) doesn't spin the CPU, but stay responsive enough that the
            # phone's own retry finds us again within a few seconds.
            _stop.wait(min(1.0 * failures, 5.0))


# ── GUI ───────────────────────────────────────────────────────────────────────
# Palette — deep neutral dark, periwinkle brand accent, calm status colors.
_C = {
    "bg":      "#0F0F14",   # window
    "card":    "#181820",   # card surface
    "card2":   "#202029",   # nested / chip
    "border":  "#2A2A36",   # 1px card outline
    "fg":      "#ECECF1",   # primary text
    "dim":     "#9A9AA7",   # secondary text
    "faint":   "#62626E",   # tertiary / timestamps
    "accent":  "#8C9BFF",   # brand periwinkle
    "accent2": "#3A3F66",   # accent-tinted fill
    "ok":      "#5BD6A0",   # connected green
    "warn":    "#FFB68A",   # attention
}


class LazeRWindow:
    def __init__(self, ip, port, wire, event_q, require_secure, update_check=True):
        import tkinter as tk
        from tkinter import font as tkf

        self._tk = tk
        self._eq = event_q
        self._wire = wire
        self._token = wire.token
        self._require_secure = require_secure
        self._update_check = update_check
        self._update_tag = None      # newest release tag once known
        self._hostname = socket.gethostname()
        uri = build_uri(ip, wire.token, self._hostname, wire.key)
        token = wire.token

        root = tk.Tk()
        self._root = root
        root.title("LazeR")
        root.configure(bg=_C["bg"])
        root.resizable(False, False)
        try:
            root.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass

        # fonts
        def mkfont(fam, size, weight="normal"):
            try:
                return tkf.Font(family=fam, size=size, weight=weight)
            except Exception:
                return None
        self.f_brand = mkfont("Segoe UI Semibold", 17, "bold")
        self.f_hero  = mkfont("Segoe UI Semibold", 15, "bold")
        self.f_lbl   = mkfont("Segoe UI", 8)
        self.f_sm    = mkfont("Segoe UI", 9)
        self.f_md    = mkfont("Segoe UI", 10)
        self.f_val   = mkfont("Consolas", 11)
        self.f_tok   = mkfont("Consolas", 15, "bold")
        self.f_log   = mkfont("Consolas", 9)

        self._ip, self._port = ip, port

        outer = tk.Frame(root, bg=_C["bg"], padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        self._build_header(outer)
        self._build_hero(outer)
        self._build_pause_banner(outer)
        self._build_firewall_banner(outer)

        # swap area: QR card (not connected) <-> connected panel
        self._swap = tk.Frame(outer, bg=_C["bg"])
        self._swap.pack(fill="x", pady=(14, 0))
        self._build_connect_card(self._swap, uri, token)
        self._build_connected_panel(self._swap)
        self._show_qr_view()   # first start: show the QR

        # everything technical lives behind this toggle, hidden by default
        self._build_details_toggle(outer)
        self._details = tk.Frame(outer, bg=_C["bg"])
        self._details_open = False
        self._build_details(self._details, ip, port, token)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tray = None
        self._setup_tray()
        self._log("Server started", "ok")
        self._poll()
        self._check_firewall()   # surface the Allow banner if inbound is blocked
        self._check_update()     # no-op unless enabled; never blocks the UI thread
        self._center()

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
        threading.Thread(target=self._tray.run, daemon=True).start()

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

    # ── card primitive ──────────────────────────────────────────────────────
    def _card(self, parent, **pack):
        """A bordered surface that reads as a rounded card."""
        tk = self._tk
        border = tk.Frame(parent, bg=_C["border"])
        inner = tk.Frame(border, bg=_C["card"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        border.pack(**pack)
        return inner

    def _section_label(self, parent, text):
        self._tk.Label(parent, text=text.upper(), bg=_C["card"], fg=_C["faint"],
                       font=self.f_lbl).pack(anchor="w")

    # ── header ────────────────────────────────────────────────────────────────
    def _build_header(self, parent):
        tk = self._tk
        bar = tk.Frame(parent, bg=_C["bg"])
        bar.pack(fill="x")

        badge = tk.Canvas(bar, width=30, height=30, bg=_C["bg"],
                          highlightthickness=0, bd=0)
        badge.pack(side="left")
        badge.create_oval(2, 2, 28, 28, fill=_C["accent2"], outline=_C["accent"])
        badge.create_oval(11, 11, 19, 19, fill=_C["accent"], outline="")
        badge.create_line(15, 4, 15, 9, fill=_C["accent"], width=2)
        badge.create_line(15, 21, 15, 26, fill=_C["accent"], width=2)
        badge.create_line(4, 15, 9, 15, fill=_C["accent"], width=2)
        badge.create_line(21, 15, 26, 15, fill=_C["accent"], width=2)

        tk.Label(bar, text="LazeR", bg=_C["bg"], fg=_C["fg"],
                 font=self.f_brand).pack(side="left", padx=(9, 0))
        tk.Label(bar, text="LAN Remote", bg=_C["bg"], fg=_C["faint"],
                 font=self.f_sm).pack(side="left", padx=(8, 0), pady=(4, 0))

        run = tk.Frame(bar, bg=_C["bg"])
        run.pack(side="right", pady=(4, 0))
        tk.Label(run, text="●", bg=_C["bg"], fg=_C["ok"],
                 font=self.f_sm).pack(side="left")
        tk.Label(run, text="server running", bg=_C["bg"], fg=_C["dim"],
                 font=self.f_sm).pack(side="left", padx=(5, 0))

    # ── hero status card ────────────────────────────────────────────────────
    def _build_hero(self, parent):
        tk = self._tk
        card = self._card(parent, fill="x", pady=(14, 0))
        pad = tk.Frame(card, bg=_C["card"], padx=16, pady=14)
        pad.pack(fill="x")

        # circular avatar with overlaid status dot
        self._avatar = tk.Canvas(pad, width=52, height=52, bg=_C["card"],
                                 highlightthickness=0, bd=0)
        self._avatar.pack(side="left")
        self._avatar.create_oval(2, 2, 50, 50, fill=_C["card2"],
                                 outline=_C["border"])
        self._avatar.create_text(26, 25, text="🖥", font=("Segoe UI Emoji", 17))
        self._dotid = self._avatar.create_oval(
            36, 36, 50, 50, fill=_C["faint"], outline=_C["card"], width=2)

        col = tk.Frame(pad, bg=_C["card"])
        col.pack(side="left", padx=(14, 0), anchor="w")
        tk.Label(col, text=socket.gethostname(), bg=_C["card"], fg=_C["fg"],
                 font=self.f_hero).pack(anchor="w")
        self._status = tk.Label(col, text="Waiting for phone to connect…",
                                bg=_C["card"], fg=_C["dim"], font=self.f_sm)
        self._status.pack(anchor="w", pady=(2, 0))

    # ── local-takeover banner: shown when physical input has paused the remote ──
    def _build_pause_banner(self, parent):
        tk = self._tk
        self._banner_wrap = tk.Frame(parent, bg=_C["bg"])
        # packed/unpacked dynamically by _set_paused
        border = tk.Frame(self._banner_wrap, bg=_C["warn"])
        border.pack(fill="x", pady=(12, 0))
        pad = tk.Frame(border, bg=_C["card"])
        pad.pack(fill="both", expand=True, padx=2, pady=2)
        inner = tk.Frame(pad, bg=_C["card"], padx=14, pady=12)
        inner.pack(fill="x")
        self._banner_title = tk.Label(inner, text="", bg=_C["card"], fg=_C["warn"],
                                      font=self.f_hero)
        self._banner_title.pack(anchor="w")
        self._banner_sub = tk.Label(inner, text="", bg=_C["card"], fg=_C["dim"],
                                    font=self.f_sm, justify="left")
        self._banner_sub.pack(anchor="w", pady=(2, 8))
        btns = tk.Frame(inner, bg=_C["card"])
        btns.pack(anchor="w")
        resume = tk.Label(btns, text="Resume remote", bg=_C["accent"], fg=_C["bg"],
                          font=self.f_sm, cursor="hand2", padx=14, pady=6)
        resume.pack(side="left")
        resume.bind("<Button-1>", lambda e: self._do_resume())
        quitb = tk.Label(btns, text="Quit LazeR", bg=_C["card2"], fg=_C["fg"],
                         font=self.f_sm, cursor="hand2", padx=14, pady=6)
        quitb.pack(side="left", padx=(8, 0))
        quitb.bind("<Button-1>", lambda e: self._real_quit())

    def _set_paused(self, latched):
        title = ("Remote stopped — you took over"
                 if latched else "Local input — remote paused")
        sub = ("Panic hotkey latched control OFF. Your mouse & keyboard are yours.\n"
               "Click Resume when you're ready to hand control back, or Quit."
               if latched else
               "You're using this computer — the phone is paused.\n"
               "It resumes automatically a couple of seconds after you stop.")
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
        self._fw_wrap = tk.Frame(parent, bg=_C["bg"])   # packed/unpacked dynamically
        border = tk.Frame(self._fw_wrap, bg=_C["warn"])
        border.pack(fill="x", pady=(12, 0))
        pad = tk.Frame(border, bg=_C["card"])
        pad.pack(fill="both", expand=True, padx=2, pady=2)
        inner = tk.Frame(pad, bg=_C["card"], padx=14, pady=12)
        inner.pack(fill="x")
        tk.Label(inner, text="Phones can't reach this laptop", bg=_C["card"],
                 fg=_C["warn"], font=self.f_hero).pack(anchor="w")
        self._fw_sub = tk.Label(inner, text="", bg=_C["card"], fg=_C["dim"],
                                font=self.f_sm, justify="left")
        self._fw_sub.pack(anchor="w", pady=(2, 8))
        btns = tk.Frame(inner, bg=_C["card"])
        btns.pack(anchor="w")
        self._fw_btn = tk.Label(btns, text="Allow through firewall", bg=_C["accent"],
                                fg=_C["bg"], font=self.f_sm, cursor="hand2",
                                padx=14, pady=6)
        self._fw_btn.pack(side="left")
        self._fw_btn.bind("<Button-1>", lambda e: self._fix_firewall())
        dismiss = tk.Label(btns, text="Dismiss", bg=_C["card2"], fg=_C["fg"],
                           font=self.f_sm, cursor="hand2", padx=14, pady=6)
        dismiss.pack(side="left", padx=(8, 0))
        dismiss.bind("<Button-1>", lambda e: self._clear_firewall_banner())

    def _check_firewall(self):
        """Probe the rule off the UI thread, then show the banner if it's missing."""
        if not sys.platform.startswith("win"):
            return

        def work():
            ok = firewall_rule_exists()
            vpn = detect_vpn()
            self._root.after(0, lambda: self._on_firewall_status(ok, vpn))
        threading.Thread(target=work, daemon=True).start()

    def _on_firewall_status(self, ok, vpn):
        self._fw_ok = ok
        self._update_fw_pill()
        if ok:
            self._clear_firewall_banner()
            return
        sub = (f"Windows Firewall is dropping inbound traffic on UDP {PORT}, so phones "
               "time out. One click adds the allow rule (asks for admin once).")
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

    def _fix_firewall(self):
        self._fw_btn.config(text="Requesting admin…")

        def work():
            ok = ensure_firewall_rule(allow_elevate=True)
            self._root.after(0, lambda: self._after_fix_firewall(ok))
        threading.Thread(target=work, daemon=True).start()

    def _after_fix_firewall(self, ok):
        self._fw_btn.config(text="Allow through firewall")
        self._fw_ok = ok
        self._update_fw_pill()
        if ok:
            self._clear_firewall_banner()
            self._log(f"Firewall allowed — inbound UDP {PORT} open for phones", "ok")
        else:
            self._log("Firewall rule not added — admin prompt declined?", "warn")

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
            val.config(fg=_C["accent"], cursor="hand2")
            val.bind("<Button-1>", lambda e: self._open_releases())
        else:
            val.config(fg=_C["faint"], cursor="")
            val.unbind("<Button-1>")

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
        if self._fw_ok is None:
            dot.config(fg=_C["faint"]); txt.config(text="checking…")
        elif self._fw_ok:
            dot.config(fg=_C["ok"]); txt.config(text="inbound allowed")
        else:
            dot.config(fg=_C["warn"]); txt.config(text="blocked — click Allow above")

    # ── friendly connect card: just the QR + pairing code ────────────────────
    def _build_connect_card(self, parent, uri, token):
        tk = self._tk
        card = self._card(parent, fill="x")
        self._connect_border = card.master   # the bordered frame, for show/hide
        pad = tk.Frame(card, bg=_C["card"], padx=20, pady=18)
        pad.pack(fill="x")

        tk.Label(pad, text="Connect your phone", bg=_C["card"], fg=_C["fg"],
                 font=self.f_hero).pack()
        tk.Label(pad, text="Open the LazeR app and scan this code",
                 bg=_C["card"], fg=_C["dim"], font=self.f_md).pack(pady=(3, 0))

        self._qr_img_label = None
        self._make_qr_photo = None
        try:
            import qrcode
            from PIL import ImageTk
            self._make_qr_photo = self._build_qr_maker(qrcode, ImageTk)
            self._photo = self._make_qr_photo(uri)
            holder = tk.Frame(pad, bg="#FFFFFF", padx=10, pady=10)
            holder.pack(pady=(16, 14))
            self._qr_img_label = tk.Label(holder, image=self._photo, bg="#FFFFFF")
            self._qr_img_label.pack()
        except ImportError:
            tk.Label(pad, text="Install Pillow for the QR image:\npip install pillow",
                     bg=_C["card"], fg=_C["dim"], font=self.f_sm,
                     width=24, height=10, justify="center").pack(pady=16)

        # pairing code chip — friendly framing of the token, for manual entry
        tk.Label(pad, text="No scanner? Enter this code in the app",
                 bg=_C["card"], fg=_C["faint"], font=self.f_sm).pack()
        chip = tk.Frame(pad, bg=_C["card2"])
        chip.pack(pady=(8, 0))
        self._token_chip = tk.Label(chip, text=token, bg=_C["card2"], fg=_C["accent"],
                                    font=self.f_tok, padx=16, pady=6)
        self._token_chip.pack(side="left")
        self._copy_btn = tk.Label(chip, text="Copy", bg=_C["accent2"], fg=_C["fg"],
                                  font=self.f_sm, cursor="hand2", padx=14, pady=6)
        self._copy_btn.pack(side="left")
        self._copy_btn.bind("<Button-1>", lambda e: self._copy(self._wire.token))
        tk.Label(pad, text="(Manual entry is plaintext — scan the QR for encryption)",
                 bg=_C["card"], fg=_C["faint"], font=self.f_lbl).pack(pady=(6, 0))
        self._copy_btn.bind("<Enter>",
                            lambda e: self._copy_btn.config(bg=_C["accent"]))
        self._copy_btn.bind("<Leave>",
                            lambda e: self._copy_btn.config(bg=_C["accent2"]))

    # ── connected panel: shown instead of the QR once a phone is paired ───────
    def _build_connected_panel(self, parent):
        tk = self._tk
        card = self._card(parent, fill="x")
        self._connected_border = card.master
        pad = tk.Frame(card, bg=_C["card"], padx=20, pady=22)
        pad.pack(fill="x")

        check = tk.Canvas(pad, width=44, height=44, bg=_C["card"],
                          highlightthickness=0, bd=0)
        check.pack()
        check.create_oval(2, 2, 42, 42, fill=_C["card2"], outline=_C["ok"])
        check.create_line(13, 23, 20, 30, fill=_C["ok"], width=3,
                          capstyle="round")
        check.create_line(20, 30, 32, 15, fill=_C["ok"], width=3,
                          capstyle="round")

        tk.Label(pad, text="Phone connected", bg=_C["card"], fg=_C["fg"],
                 font=self.f_hero).pack(pady=(10, 0))
        self._connected_sub = tk.Label(
            pad, text="Your phone is now controlling this computer",
            bg=_C["card"], fg=_C["dim"], font=self.f_md)
        self._connected_sub.pack(pady=(3, 0))

        show_qr = tk.Label(pad, text="Show QR code", bg=_C["card2"],
                           fg=_C["dim"], font=self.f_sm, cursor="hand2",
                           padx=16, pady=7)
        show_qr.pack(pady=(16, 0))
        show_qr.bind("<Button-1>", lambda e: self._show_qr_view())
        show_qr.bind("<Enter>", lambda e: show_qr.config(fg=_C["accent"]))
        show_qr.bind("<Leave>", lambda e: show_qr.config(fg=_C["dim"]))
        tk.Label(pad, text="Disconnected? Scan again to reconnect",
                 bg=_C["card"], fg=_C["faint"], font=self.f_sm).pack(pady=(8, 0))

    def _build_qr_maker(self, qrcode, ImageTk):
        """Return a fn uri -> PhotoImage, so the QR can be rebuilt after an IP change."""
        def make(uri):
            qr = qrcode.QRCode(border=2, box_size=7,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(uri)
            qr.make(fit=True)
            pil = qr.make_image(fill_color="#0F0F14",
                                back_color="#FFFFFF").convert("RGB")
            return ImageTk.PhotoImage(pil)
        return make

    def refresh_network(self, new_ip, new_uri):
        """Laptop resumed on a new IP — repaint the QR and IP so the phone reconnects."""
        self._ip = new_ip
        try:
            self._ip_value.config(text=new_ip)
        except Exception:
            pass
        self._repaint_qr(new_uri)

    def _repaint_qr(self, uri):
        if self._make_qr_photo and self._qr_img_label is not None:
            try:
                self._photo = self._make_qr_photo(uri)
                self._qr_img_label.config(image=self._photo)
            except Exception:
                pass

    def _regenerate(self):
        rotate_secrets(self._wire)
        self._token = self._wire.token
        uri = build_uri(self._ip, self._wire.token, self._hostname,
                        self._wire.key)
        self._repaint_qr(uri)
        try:
            self._token_chip.config(text=self._wire.token)
            self._fullcode_value.config(text=self._wire.token)
        except Exception:
            pass
        self._show_qr_view()
        self._log("New pairing code generated — old phones must rescan", "ok")

    def _toggle_secure(self):
        self._require_secure = not self._require_secure
        self._wire.require_secure = self._require_secure
        self._refresh_secure_btn()
        self._log("Require encryption: " + ("on" if self._require_secure else "off"),
                  "ok" if self._require_secure else "warn")

    def _refresh_secure_btn(self):
        on = self._require_secure
        self._secure_btn.config(
            text="On" if on else "Off",
            bg=_C["accent"] if on else _C["card2"],
            fg=_C["bg"] if on else _C["dim"])

    # ── swap QR <-> connected ─────────────────────────────────────────────────
    def _show_qr_view(self):
        try:
            self._connected_border.pack_forget()
        except Exception:
            pass
        self._connect_border.pack(fill="x")
        self._resize()

    def _show_connected_view(self):
        try:
            self._connect_border.pack_forget()
        except Exception:
            pass
        self._connected_border.pack(fill="x")
        self._resize()

    def _resize(self):
        self._root.geometry("")
        self._root.after(10, self._center)

    # ── details toggle (hides all the technical stuff) ────────────────────────
    def _build_details_toggle(self, parent):
        tk = self._tk
        bar = tk.Frame(parent, bg=_C["bg"])
        bar.pack(fill="x", pady=(12, 0))
        self._toggle = tk.Label(bar, text="Show details  ▾", bg=_C["bg"],
                                fg=_C["faint"], font=self.f_sm, cursor="hand2")
        self._toggle.pack()
        self._toggle.bind("<Button-1>", lambda e: self._toggle_details())
        self._toggle.bind("<Enter>", lambda e: self._toggle.config(fg=_C["dim"]))
        self._toggle.bind("<Leave>", lambda e: self._toggle.config(fg=_C["faint"]))

    # ── details panel: network info, services, activity ───────────────────────
    def _build_details(self, parent, ip, port, token):
        tk = self._tk
        card = self._card(parent, fill="both", expand=True, pady=(12, 0))
        pad = tk.Frame(card, bg=_C["card"], padx=16, pady=14)
        pad.pack(fill="both", expand=True)
        self._section_label(pad, "Network")

        def row(label, value):
            f = tk.Frame(pad, bg=_C["card"])
            f.pack(fill="x", pady=(8, 0))
            tk.Label(f, text=label, bg=_C["card"], fg=_C["dim"],
                     font=self.f_md, width=9, anchor="w").pack(side="left")
            val = tk.Label(f, text=value, bg=_C["card"], fg=_C["fg"],
                           font=self.f_val, anchor="w")
            val.pack(side="left")
            return val

        self._ip_value = row("IP address", ip)
        row("Port", str(port))
        self._fullcode_value = row("Full code", token)

        # More than one LAN address (Wi-Fi + Ethernet, or a virtual adapter) means
        # we had to CHOOSE which to advertise, and choosing wrong is the classic
        # invisible "phone times out but the server looks fine" failure. Show the
        # alternates so it is diagnosable instead of a mystery.
        others = [a for a, _ in candidate_ips() if a != ip]
        if others:
            row("Also at", ", ".join(others[:3]))

        tk.Frame(pad, bg=_C["border"], height=1).pack(fill="x", pady=(14, 12))

        vol_ok = VOLUME_BACKEND is not None
        self._pill(pad, "Volume control",
                   VOLUME_BACKEND if vol_ok else "unavailable", vol_ok)
        try:
            import zeroconf  # noqa: F401
            disc_ok, disc_txt = True, "broadcasting on Wi-Fi"
        except ImportError:
            disc_ok, disc_txt = False, "zeroconf not installed"
        self._pill(pad, "Auto-discovery", disc_txt, disc_ok)
        self._pill(pad, "Encryption",
                   "AES-256-GCM (QR scan)" if _HAVE_CRYPTO
                   else "unavailable — pip install cryptography", _HAVE_CRYPTO)
        if sys.platform.startswith("win"):
            self._fw_pill = self._mutable_pill(pad, "Firewall", "checking…")
        self._upd_pill = self._mutable_pill(
            pad, "Version", f"v{APP_VERSION} — checking for updates…")

        self._build_security_controls(pad)

        if sys.platform.startswith("win"):
            self._build_startup_toggle(pad)

        self._build_activity(parent)

    def _build_security_controls(self, parent):
        """Require-encryption toggle + regenerate-code button + panic-key hint."""
        tk = self._tk
        tk.Frame(parent, bg=_C["border"], height=1).pack(fill="x", pady=(14, 12))

        f = tk.Frame(parent, bg=_C["card"])
        f.pack(fill="x")
        col = tk.Frame(f, bg=_C["card"])
        col.pack(side="left", anchor="w")
        tk.Label(col, text="Require encryption", bg=_C["card"], fg=_C["fg"],
                 font=self.f_sm).pack(anchor="w")
        tk.Label(col, text="Block plaintext (manual-code) phones — QR only",
                 bg=_C["card"], fg=_C["faint"], font=self.f_sm).pack(anchor="w")
        self._secure_btn = tk.Label(f, bg=_C["card2"], fg=_C["dim"],
                                    font=self.f_sm, cursor="hand2", padx=14, pady=5)
        self._secure_btn.pack(side="right")
        self._secure_btn.bind("<Button-1>", lambda e: self._toggle_secure())
        if not _HAVE_CRYPTO:
            self._secure_btn.config(text="n/a")
        else:
            self._refresh_secure_btn()

        r = tk.Frame(parent, bg=_C["card"])
        r.pack(fill="x", pady=(10, 0))
        col2 = tk.Frame(r, bg=_C["card"])
        col2.pack(side="left", anchor="w")
        tk.Label(col2, text="Pairing code", bg=_C["card"], fg=_C["fg"],
                 font=self.f_sm).pack(anchor="w")
        tk.Label(col2, text="Generate a new code and kick every paired phone",
                 bg=_C["card"], fg=_C["faint"], font=self.f_sm).pack(anchor="w")
        regen = tk.Label(r, text="Regenerate", bg=_C["accent2"], fg=_C["fg"],
                         font=self.f_sm, cursor="hand2", padx=14, pady=5)
        regen.pack(side="right")
        regen.bind("<Button-1>", lambda e: self._regenerate())

        tk.Label(parent, text="Panic: press  Ctrl+Alt+Shift+L  to instantly stop the remote",
                 bg=_C["card"], fg=_C["faint"], font=self.f_sm).pack(anchor="w", pady=(10, 0))

    def _build_startup_toggle(self, parent):
        """Clickable row: launch LazeR automatically when Windows starts."""
        tk = self._tk
        tk.Frame(parent, bg=_C["border"], height=1).pack(fill="x", pady=(14, 12))
        f = tk.Frame(parent, bg=_C["card"])
        f.pack(fill="x")
        col = tk.Frame(f, bg=_C["card"])
        col.pack(side="left", anchor="w")
        tk.Label(col, text="Start with Windows", bg=_C["card"], fg=_C["fg"],
                 font=self.f_sm).pack(anchor="w")
        tk.Label(col, text="Launch LazeR automatically at login",
                 bg=_C["card"], fg=_C["faint"], font=self.f_sm).pack(anchor="w")
        self._startup_btn = tk.Label(f, bg=_C["accent2"], fg=_C["fg"],
                                     font=self.f_sm, cursor="hand2", padx=14, pady=5)
        self._startup_btn.pack(side="right")
        self._startup_btn.bind("<Button-1>", lambda e: self._toggle_startup())
        self._refresh_startup_btn()

    def _refresh_startup_btn(self):
        on = startup_enabled()
        self._startup_btn.config(
            text="On" if on else "Off",
            bg=_C["accent"] if on else _C["card2"],
            fg=_C["bg"] if on else _C["dim"])

    def _toggle_startup(self):
        if set_startup(not startup_enabled()):
            self._refresh_startup_btn()
            self._log("Start with Windows: "
                      + ("on" if startup_enabled() else "off"), "info")
        else:
            reason = getattr(set_startup, "last_error", "") or "unknown error"
            self._log(f"Couldn't change startup setting: {reason}", "info")

    def _pill(self, parent, name, detail, ok):
        tk = self._tk
        f = tk.Frame(parent, bg=_C["card"])
        f.pack(fill="x", pady=3)
        tk.Label(f, text="●", bg=_C["card"],
                 fg=_C["ok"] if ok else _C["faint"], font=self.f_sm).pack(side="left")
        tk.Label(f, text=name, bg=_C["card"], fg=_C["fg"],
                 font=self.f_sm).pack(side="left", padx=(7, 0))
        tk.Label(f, text=detail, bg=_C["card"], fg=_C["faint"],
                 font=self.f_sm).pack(side="left", padx=(7, 0))

    def _mutable_pill(self, parent, name, detail):
        """Like _pill but returns (dot, detail) labels so status can change later."""
        tk = self._tk
        f = tk.Frame(parent, bg=_C["card"])
        f.pack(fill="x", pady=3)
        dot = tk.Label(f, text="●", bg=_C["card"], fg=_C["faint"], font=self.f_sm)
        dot.pack(side="left")
        tk.Label(f, text=name, bg=_C["card"], fg=_C["fg"],
                 font=self.f_sm).pack(side="left", padx=(7, 0))
        val = tk.Label(f, text=detail, bg=_C["card"], fg=_C["faint"], font=self.f_sm)
        val.pack(side="left", padx=(7, 0))
        return dot, val

    # ── activity feed ─────────────────────────────────────────────────────────
    def _build_activity(self, parent):
        tk = self._tk
        card = self._card(parent, fill="both", expand=True, pady=(14, 0))
        pad = tk.Frame(card, bg=_C["card"], padx=16, pady=12)
        pad.pack(fill="both", expand=True)
        self._section_label(pad, "Activity")

        wrap = tk.Frame(pad, bg=_C["card"])
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        sb = tk.Scrollbar(wrap, orient="vertical")
        sb.pack(side="right", fill="y")
        self._log_txt = tk.Text(
            wrap, bg=_C["card"], fg=_C["dim"], font=self.f_log,
            height=6, width=74, state="disabled", relief="flat", bd=0,
            cursor="arrow", highlightthickness=0, padx=0, pady=0,
            spacing1=1, spacing3=1, yscrollcommand=sb.set,
        )
        self._log_txt.pack(side="left", fill="both", expand=True)
        sb.config(command=self._log_txt.yview)
        self._log_txt.tag_config("ts", foreground=_C["faint"])
        self._log_txt.tag_config("ok", foreground=_C["ok"])
        self._log_txt.tag_config("act", foreground=_C["fg"])
        self._log_txt.tag_config("info", foreground=_C["dim"])
        self._log_txt.tag_config("warn", foreground=_C["warn"])

    # ── helpers ───────────────────────────────────────────────────────────────
    def _toggle_details(self):
        if self._details_open:
            self._details.pack_forget()
            self._toggle.config(text="Show details  ▾")
            self._details_open = False
        else:
            self._details.pack(fill="both", expand=True)
            self._toggle.config(text="Hide details  ▴")
            self._details_open = True
        self._root.geometry("")   # let the window resize to fit
        self._root.after(10, self._center)

    def _center(self):
        self._root.update_idletasks()
        w, h = self._root.winfo_width(), self._root.winfo_height()
        sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        # Never let the window grow past the screen (the details panel can make it
        # very tall). Cap the height — the activity log scrolls to absorb the rest —
        # and clamp the position so the whole window, especially the bottom controls,
        # stays on screen above the taskbar.
        avail = sh - 80
        if h > avail:
            h = avail
            self._root.geometry(f"{w}x{h}")
            self._root.update_idletasks()
        x = max(0, (sw - w) // 2)
        y = max(0, min((sh - h) // 3, sh - h - 48))
        self._root.geometry(f"+{x}+{y}")

    def _copy(self, text):
        self._root.clipboard_clear()
        self._root.clipboard_append(text)
        self._copy_btn.config(text="Copied", fg=_C["ok"])
        self._root.after(1100,
                         lambda: self._copy_btn.config(text="Copy", fg=_C["fg"]))

    def _log(self, msg, tag="info"):
        import time
        ts = time.strftime("%H:%M:%S")
        t = self._log_txt
        t.configure(state="normal")
        t.insert("end", f"{ts}  ", "ts")
        t.insert("end", f"{msg}\n", tag)
        t.see("end")
        t.configure(state="disabled")
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 400:
            t.configure(state="normal")
            t.delete("1.0", f"{lines - 400}.0")
            t.configure(state="disabled")

    def _set_status(self, connected, who="", secure=True):
        if connected:
            self._avatar.itemconfig(self._dotid, fill=_C["ok"])
            txt = "Connected · encrypted" if secure else "Connected · PLAINTEXT"
            self._status.config(fg=_C["ok"] if secure else _C["warn"], text=txt)
            self._show_connected_view()
        else:
            self._avatar.itemconfig(self._dotid, fill=_C["faint"])
            self._status.config(fg=_C["dim"], text="Waiting for phone to connect…")
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
            self._log(f"New IP {ev[1]} — rescan the QR", "ok")
        elif kind == "blocked":
            self._log(f"Blocked control attempt from {ev[1]} "
                      "(a phone is already paired)", "warn")
        elif kind == "warn":
            self._log(ev[1], "warn")
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
        else:
            self._real_quit()

    def run(self):
        self._root.mainloop()


# ── terminal mode ─────────────────────────────────────────────────────────────
def run_terminal(token, key, ip, require_secure, update_check=True):
    hostname = socket.gethostname()
    uri = build_uri(ip, token, hostname, key)
    zc, mdns_info = start_mdns(ip, hostname)
    net = {"ip": ip, "zc": zc, "info": mdns_info}

    print("=" * 44)
    print("  LazeR - server running")
    print("=" * 44)
    print(f"  Laptop    : {hostname}")
    print(f"  Laptop IP : {ip}")
    print(f"  Port      : {PORT}")
    print(f"  Token     : {token}")
    sec = "ON (QR scan)" if _HAVE_CRYPTO else "unavailable (pip install cryptography)"
    print(f"  Encryption: {sec}" + ("  · plaintext blocked" if require_secure else ""))
    print("=" * 44)
    print("  Scan this QR in the LazeR app:")
    print()
    show_qr(uri)
    print()
    print("  ...or auto-discover, or enter IP + token manually. Ctrl+C to quit.\n")

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
    if sys.platform.startswith("win") and not ensure_firewall_rule():
        print(f"  [firewall] Inbound UDP {PORT} appears blocked — phones may not connect.")
        print("             Fix once (accepts a UAC prompt):")
        print("               python remote_server.py --setup-firewall")
        vpn = detect_vpn()
        if vpn:
            print(f"             VPN '{vpn}' is active — if it blocks LAN, also enable "
                  "'allow local network' in the VPN.")
        print()

    def emit(kind, *a):
        if kind == "connected":
            print(f"[handshake] paired: {a[0]}  ({'secure' if a[1] else 'PLAINTEXT'})")
        elif kind == "disconnected":
            print("[handshake] phone left")
        elif kind == "blocked":
            print(f"[security] blocked control attempt from {a[0]} (a phone is already paired)")
        elif kind == "warn":
            print(f"[security] {a[0]}")
        elif kind == "paused":
            print("[takeover] local input detected — remote paused")
        elif kind == "resumed":
            print("[takeover] remote resumed")
        elif kind == "panic":
            print("[takeover] PANIC hotkey — remote latched off; press it again is not needed, restart to re-enable")
        elif kind == "netchange":
            print(f"\n[resume] new IP {a[0]} — rescan:\n")
            show_qr(a[1])
            print()
        elif kind == "log":
            print(f"[info] {a[0]}")

    wire = Wire(token, key, require_secure)
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
        _stop.set()
        if net["zc"]:
            try:
                net["zc"].unregister_service(net["info"])
                net["zc"].close()
            except Exception:
                pass
    print("\nServer stopped.")


# ── local-takeover event helpers (shared by GUI + terminal) ──────────────────
def _physical_event(emit):
    """A physical mouse/key event: pause the remote if a phone is driving."""
    if not _client_connected.is_set():
        return
    _last_physical_ts[0] = time.monotonic()
    if not _remote_paused.is_set():
        _remote_paused.set()
        appswitch_reset()
        emit("paused")


def _panic_event(emit):
    """Panic chord: latch the remote off until the user explicitly resumes."""
    _last_physical_ts[0] = time.monotonic()
    if not _panic_latched.is_set():
        _panic_latched.set()
        _remote_paused.set()
        appswitch_reset()
        emit("panic")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="LazeR server")
    ap.add_argument("--no-gui", action="store_true", help="terminal/headless mode")
    ap.add_argument("--secure-only", action="store_true",
                    help="(now the default) reject plaintext v1 clients; kept so "
                         "existing scripts and shortcuts keep working")
    ap.add_argument("--no-update-check", action="store_true",
                    help="never contact GitHub to see if a newer release exists. "
                         "This is the only outbound internet request LazeR makes; "
                         "everything else is LAN-only")
    ap.add_argument("--allow-plaintext", action="store_true",
                    help="permit manual-code (plaintext v1) pairing. Trusted LANs "
                         "only — v1 offers no confidentiality and is replayable")
    ap.add_argument("--setup-firewall", action="store_true",
                    help="add the Windows Firewall inbound rule (self-elevates) and exit")
    ap.add_argument("--enable-startup", action="store_true",
                    help="register LazeR to launch at Windows login (Startup folder) and exit")
    ap.add_argument("--disable-startup", action="store_true",
                    help="remove the launch-at-login registration and exit")
    args = ap.parse_args()

    if args.enable_startup or args.disable_startup:
        if not sys.platform.startswith("win"):
            print("[startup] launch-at-login is Windows-only.")
            return
        want = args.enable_startup
        if set_startup(want):
            print(f"[startup] launch-at-login {'enabled' if want else 'disabled'}."
                  + ("\n[startup] registered under HKCU\\...\\Run as 'LazeR' — it now "
                     "appears in Task Manager → Startup apps." if want else ""))
        else:
            reason = getattr(set_startup, "last_error", "") or "unknown error"
            print(f"[startup] could not change launch-at-login: {reason}")
        return

    if args.setup_firewall:
        if not sys.platform.startswith("win"):
            print("[firewall] --setup-firewall is Windows-only; on macOS/Linux allow "
                  f"inbound UDP {PORT} in your firewall.")
            return
        if ensure_firewall_rule(allow_elevate=True):
            print(f"[firewall] inbound rule '{FW_RULE_NAME}' is in place — phones can reach UDP {PORT}.")
        else:
            print("[firewall] could not add the rule (UAC declined?). Re-run and accept the prompt.")
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
    if args.allow_plaintext and args.secure_only:
        print("[security] --secure-only and --allow-plaintext conflict; honouring "
              "--secure-only (encryption required).")
    elif args.allow_plaintext:
        print("[security] plaintext (manual-code) pairing ALLOWED — trusted LAN "
              "only. Omit --allow-plaintext to require encryption.")
    ip = lan_ip()
    # Say so loudly when the default route is NOT the address we advertise: it means
    # a virtual/VPN adapter owns the route, which used to silently put an
    # unreachable IP in the QR.
    _probe = _default_route_ip()
    if _probe and _probe != ip:
        print(f"[network] default route is {_probe}, which phones cannot reach "
              f"(virtual/VPN adapter) — advertising {ip} instead.")
    hostname = socket.gethostname()

    if not args.no_gui:
        try:
            import tkinter
        except ImportError:
            print("[gui] tkinter unavailable — falling back to terminal mode")
        else:
            inst_kind, inst_lsock = singleton_acquire()
            if inst_kind == "existing":
                print("LazeR is already running — opened the existing window.")
                return

            eq = queue.Queue()
            if inst_lsock is not None:
                threading.Thread(target=singleton_serve, args=(inst_lsock, eq),
                                 daemon=True).start()
            zc, mdns_info = start_mdns(ip, hostname)
            net = {"ip": ip, "zc": zc, "info": mdns_info}
            wire = Wire(token, key, require_secure)

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
                LazeRWindow(ip, PORT, wire, eq, require_secure,
                            update_check=not args.no_update_check).run()
            except KeyboardInterrupt:
                pass
            finally:
                _stop.set()
                t.join(timeout=2)
                if net["zc"]:
                    try:
                        net["zc"].unregister_service(net["info"])
                        net["zc"].close()
                    except Exception:
                        pass
                print("Server stopped.")
            return

    run_terminal(token, key, ip, require_secure,
                 update_check=not args.no_update_check)


if __name__ == "__main__":
    main()
