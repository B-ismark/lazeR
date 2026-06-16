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
SINGLETON_PORT = 50506  # loopback-only: a 2nd launch uses it to surface the running window
TOKEN_LEN = 6
RESUME_GAP_S = 8        # recv-loop tick gap larger than this ⇒ the laptop slept; recover net
CLIENT_IDLE_S = 8       # no packet from the pinned phone for this long ⇒ it left (it pings ~1.5s)
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

# Verbs that actually drive the machine. While the user has taken over locally
# (or after a panic), these are dropped; PING/VGET/HELLO/BYE still flow.
CONTROL_VERBS = {
    "MOVE", "SCROLL", "CLICK", "RCLICK", "MCLICK", "MDOWN", "MUP",
    "COMBO", "ASW", "SYS", "PRES", "VOL", "MEDIA", "KEY", "KEYSP",
    "BRIGHT", "CLIP",
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
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            vol = getattr(devices, "EndpointVolume", None)
            if vol is None:
                from ctypes import cast, POINTER
                from comtypes import CLSCTX_ALL
                iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol = cast(iface, POINTER(IAudioEndpointVolume))

            def get_win():
                return int(round(vol.GetMasterVolumeLevelScalar() * 100))

            def set_win(pct):
                vol.SetMasterVolumeLevelScalar(pct / 100.0, None)

            return get_win, set_win, "pycaw"
        except Exception as e:
            print(f"[volume] pycaw unavailable ({e}); pip install pycaw")
            return None, None, None

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
            return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                                  capture_output=True, text=True, timeout=4,
                                  creationflags=_flags)

        def get_win():
            try:
                out = _ps("(Get-CimInstance -Namespace root/WMI "
                          "-ClassName WmiMonitorBrightness).CurrentBrightness")
                return int(out.stdout.strip().splitlines()[0])
            except Exception:
                return 0

        def set_win(pct):
            # Must PIPE the instance into Invoke-CimMethod — a CIM instance does not
            # expose .WmiSetBrightness() as a callable (unlike legacy Get-WmiObject),
            # so the dotted form silently no-ops and the screen never changes.
            try:
                _ps("Get-CimInstance -Namespace root/WMI -ClassName "
                    "WmiMonitorBrightnessMethods | Invoke-CimMethod -MethodName "
                    f"WmiSetBrightness -Arguments @{{Timeout=1; Brightness={int(pct)}}}")
            except Exception:
                pass

        # Probe once: laptops expose it, most desktops don't.
        try:
            chk = _ps("(Get-CimInstance -Namespace root/WMI "
                      "-ClassName WmiMonitorBrightness).CurrentBrightness")
            if chk.stdout.strip():
                return get_win, set_win, "wmi"
        except Exception:
            pass
        return None, None, None

    if plat == "darwin":
        import shutil, subprocess
        if shutil.which("brightness"):           # the `brightness` CLI (brew)
            def get_mac():
                try:
                    out = subprocess.check_output(["brightness", "-l"]).decode()
                    import re
                    m = re.search(r"brightness\s+([0-9.]+)", out)
                    return int(round(float(m.group(1)) * 100)) if m else 0
                except Exception:
                    return 0

            def set_mac(pct):
                subprocess.run(["brightness", str(max(0, min(100, int(pct))) / 100.0)],
                               check=False)
            return get_mac, set_mac, "brightness"
        return None, None, None

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
                return int(round(cur / mx * 100)) if mx else 0
            except Exception:
                return 0

        def set_linux(pct):
            try:
                with open(f"{bl}/max_brightness") as f:
                    mx = int(f.read().strip())
                with open(f"{bl}/brightness", "w") as f:
                    f.write(str(int(round(max(0, min(100, pct)) / 100 * mx))))
            except Exception:
                pass
        return get_linux, set_linux, "sysfs"

    return None, None, None


get_brightness, set_brightness, BRIGHTNESS_BACKEND = make_brightness()


class BrightnessService:
    """Non-blocking brightness access for the UDP loop.

    Reading brightness (e.g. Windows WMI via PowerShell) costs ~0.5–1s, and the
    phone polls it ~every 1.5s. Doing that read inline would stall the single recv
    loop, delaying MOVE/PING/VGET enough that the phone thinks the link died and
    reconnects — the whole app flickers. So we serve BGET from a cached value kept
    fresh by a background thread, and apply BRIGHT writes on a worker thread."""

    def __init__(self, get_fn, set_fn):
        self._get = get_fn
        self._set = set_fn
        self.available = get_fn is not None
        self._lock = threading.Lock()
        self._val = 0
        self._target = None                  # latest requested brightness, or None
        self._wake = threading.Event()        # signals the setter there's work
        if self.available:
            self._val = self._safe_get()
            threading.Thread(target=self._poll, daemon=True).start()
            if self._set is not None:
                threading.Thread(target=self._setter_loop, daemon=True).start()

    def _safe_get(self):
        try:
            return max(0, min(100, int(self._get())))
        except Exception:
            return self._val

    def _poll(self):
        # Refresh the cache only while a phone is connected, and gently (5s) — each
        # read spawns PowerShell (~0.5s); no need to churn it. Skip while a write is
        # pending so we don't read a value the panel is mid-change to.
        while not _stop.is_set():
            if _client_connected.is_set() and self._target is None:
                v = self._safe_get()
                with self._lock:
                    self._val = v
            _stop.wait(5.0)

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


brightness_svc = BrightnessService(get_brightness, set_brightness)


# ── clipboard ───────────────────────────────────────────────────────────────
def set_clipboard(text):
    """Put text on the OS clipboard (no extra dependency). Returns True on success."""
    plat = sys.platform
    try:
        if plat.startswith("win"):
            import ctypes
            from ctypes import wintypes
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            # Pin signatures: without these, 64-bit HANDLEs are truncated to c_int
            # and the lock/SetClipboardData calls silently corrupt the handle.
            u32.OpenClipboard.argtypes = [wintypes.HWND]
            u32.OpenClipboard.restype = wintypes.BOOL
            k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            k32.GlobalAlloc.restype = wintypes.HGLOBAL
            k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            u32.SetClipboardData.restype = wintypes.HANDLE
            if not u32.OpenClipboard(None):
                return False
            try:
                u32.EmptyClipboard()
                buf = text.encode("utf-16-le") + b"\x00\x00"
                h = k32.GlobalAlloc(GMEM_MOVEABLE, len(buf))
                ptr = k32.GlobalLock(h)
                if not ptr:
                    return False
                ctypes.memmove(ptr, buf, len(buf))
                k32.GlobalUnlock(h)
                # On success the system owns the memory block — don't free it.
                return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
            finally:
                u32.CloseClipboard()
        import subprocess
        if plat == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
            return True
        import shutil
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode("utf-8"), check=False)
                return True
    except Exception:
        pass
    return False


def do_clip(text):
    """Set the clipboard to text, then paste it (Ctrl+V / Cmd+V)."""
    if not text or not set_clipboard(text):
        return
    paste_mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
    keyboard.press(paste_mod)
    keyboard.press("v")
    keyboard.release("v")
    keyboard.release(paste_mod)


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

# Presentation actions -> the key they emit (works in PowerPoint / Keynote / Slides).
PRES_KEYS = {
    "start": Key.f5, "end": Key.esc,
    "next": Key.right, "prev": Key.left,
    "blank": "b",   # toggles a black screen in most presenters
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
            set_volume(0 if get_volume() > 0 else 30)
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


def do_presentation(action):
    key = PRES_KEYS.get(action)
    if key is None:
        return
    keyboard.press(key)
    keyboard.release(key)


# ── crypto: secure wire (v2) ──────────────────────────────────────────────────
# Two wire formats coexist so existing flows keep working:
#   v1 (legacy, PLAINTEXT):  "<TOKEN> <VERB> [args]"   — trusted LAN only.
#   v2 (SECURE):  b"L2" || sid(4) || counter(8 BE) || AES-256-GCM(ct+tag)
#     nonce = sid||counter (12 B) · AAD = b"L2"||sid||counter · plaintext = "VERB [args]"
# The 256-bit key is shipped in the QR (and auto-discovery never carries it). A valid
# GCM tag *is* the authentication (proves key possession) — no token on the wire —
# and the monotonic counter gives replay protection. Sniffing/forgery/replay all fail.
MAGIC_V2 = b"L2"
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
    wire.cli_sid, wire.cli_ctr, wire.secure_client = None, -1, False
    return token, key


class Wire:
    """Per-server auth + encrypt/decrypt + replay state for one controller."""

    def __init__(self, token, key, require_secure):
        self.token = token
        self.key = key
        self.require_secure = require_secure
        self.aes = AESGCM(key) if (key and _HAVE_CRYPTO) else None
        self.srv_sid = secrets.token_bytes(4)
        self.srv_ctr = 0
        # pinned client's secure session (set when a v2 HELLO is accepted)
        self.cli_sid = None
        self.cli_ctr = -1
        self.secure_client = False

    def parse(self, data, addr):
        """Authenticate one datagram. Returns (verb, rest, secure) or None if rejected.
        Replay/reorder on the pinned secure session is rejected here."""
        if data[:2] == MAGIC_V2 and self.aes is not None and len(data) >= 14 + 16:
            sid = data[2:6]
            ctr = int.from_bytes(data[6:14], "big")
            try:
                pt = self.aes.decrypt(data[2:14], data[14:], data[0:14])
            except Exception:
                return None                     # bad tag ⇒ forged/corrupt ⇒ drop
            text = pt.decode("utf-8", "ignore")
            verb, rest = _split_verb(text)
            if verb != "HELLO":
                if sid != self.cli_sid or ctr <= self.cli_ctr:
                    return None                 # wrong session or replay/reorder
                self.cli_ctr = ctr
            else:
                self._pending = (sid, ctr)      # loop commits on accept
            return verb, rest, True
        # plaintext v1
        if self.require_secure:
            return None                         # secure-only mode: drop plaintext
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

    def commit_hello(self, secure):
        """Pin the just-accepted HELLO's session as the active controller."""
        self.secure_client = secure
        if secure and getattr(self, "_pending", None):
            self.cli_sid, self.cli_ctr = self._pending
        else:
            self.cli_sid, self.cli_ctr = None, -1

    def reply(self, sock, addr, text):
        """Send a reply, encrypted iff the client is on the secure wire."""
        try:
            if self.secure_client and self.aes is not None:
                self.srv_ctr += 1
                nonce = self.srv_sid + self.srv_ctr.to_bytes(8, "big")
                hdr = MAGIC_V2 + nonce
                ct = self.aes.encrypt(nonce, text.encode("utf-8"), hdr)
                sock.sendto(hdr + ct, addr)
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

    PANIC_VKS = frozenset({0x10, 0x11, 0x12, 0x4C})  # shift, ctrl, alt, L

    def __init__(self, on_physical, on_panic):
        self._on_physical = on_physical
        self._on_panic = on_panic
        self._down = set()
        self._thread = None
        self._tid = None

    def start(self):
        if not sys.platform.startswith("win"):
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

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

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        class MSLL(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        def kb_proc(nCode, wParam, lParam):
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBD)).contents
                if not (kb.flags & LLKHF_INJECTED):       # physical only
                    vk = kb.vkCode
                    if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self._down.add(vk)
                        if self.PANIC_VKS.issubset(self._down):
                            self._on_panic()
                        else:
                            self._on_physical()
                    elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                        self._down.discard(vk)
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
        kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, hmod, 0)
        ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb, hmod, 0)
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        while not _stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(kb_hook)
        user32.UnhookWindowsHookEx(ms_hook)


# ── packet handler ────────────────────────────────────────────────────────────
def handle_packet(verb, rest, sock, addr):
    if verb == "MOVE":
        try:
            dx, dy = (int(x) for x in rest.split())
        except ValueError:
            return
        mouse.move(dx, dy)

    elif verb == "SCROLL":
        try:
            dx, dy = (int(x) for x in rest.split())
        except ValueError:
            return
        mouse.scroll(dx, dy)

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

    elif verb == "PING":         # heartbeat / liveness probe
        try:
            sock.sendto(b"PONG", addr)
        except OSError:
            pass

    elif verb == "COMBO":
        do_combo(rest)

    elif verb == "ASW":          # app-switch session: next | prev | end
        do_appswitch(rest.strip().lower())

    elif verb == "SYS":
        do_system(rest.strip().lower())

    elif verb == "PRES":
        do_presentation(rest.strip().lower())

    elif verb == "VOL":
        if set_volume is None:
            return
        try:
            pct = max(0, min(100, int(rest.split()[0])))
        except (IndexError, ValueError):
            return
        set_volume(pct)

    elif verb == "VGET":
        if get_volume is not None:
            try:
                sock.sendto(f"VOL {get_volume()}".encode("utf-8"), addr)
            except OSError:
                pass

    elif verb == "BRIGHT":
        if not brightness_svc.available:
            return
        try:
            pct = max(0, min(100, int(rest.split()[0])))
        except (IndexError, ValueError):
            return
        brightness_svc.set_async(pct)   # offload the slow WMI write off the loop

    elif verb == "CLIP":
        do_clip(rest)

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
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def open_socket():
    """Fresh bound UDP socket. Recreated after resume — a socket bound before the
    laptop slept can stop receiving once the NIC cycles, so we rebind to recover."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.5)
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


def start_mdns(ip, hostname):
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except ImportError:
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
        zc.register_service(info)
        return zc, info
    except Exception as e:
        print(f"[discovery] mDNS failed ({e}); QR + manual still work.")
        return None, None


# ── Windows "start with Windows" (Startup folder shortcut) ────────────────────
SCRIPT_PATH = os.path.abspath(__file__)


def startup_lnk_path():
    """Path of the LazeR shortcut in the per-user Startup folder (Windows)."""
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", "LazeR.lnk")


def startup_enabled():
    return sys.platform.startswith("win") and os.path.exists(startup_lnk_path())


def _startup_target():
    """(target, args, workdir) to launch LazeR — handles frozen .exe and .py."""
    if getattr(sys, "frozen", False):              # PyInstaller bundle
        exe = sys.executable
        return exe, "", os.path.dirname(exe)
    # Source run: prefer pythonw so no console window pops at login.
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand
    return pyw, f'"{SCRIPT_PATH}"', os.path.dirname(SCRIPT_PATH)


def set_startup(enabled):
    """Create/remove the Startup shortcut. Returns True on success (Windows only)."""
    if not sys.platform.startswith("win"):
        return False
    lnk = startup_lnk_path()
    if not enabled:
        try:
            if os.path.exists(lnk):
                os.remove(lnk)
            return True
        except OSError:
            return False
    target, args, workdir = _startup_target()
    icon = ICON_FILE if os.path.exists(ICON_FILE) else target
    # Build the .lnk via WScript.Shell through PowerShell — no pywin32 needed.
    ps = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut('{lnk}');"
        f"$s.TargetPath='{target}';"
        f"$s.Arguments='{args}';"
        f"$s.WorkingDirectory='{workdir}';"
        f"$s.IconLocation='{icon}';"
        "$s.Description='LazeR - LAN remote control server';"
        "$s.Save()"
    )
    try:
        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                        "-Command", ps], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return os.path.exists(lnk)
    except Exception:
        return False


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
_ACTION_LABELS = {
    "CLICK":  lambda r: ("Left click", "act"),
    "RCLICK": lambda r: ("Right click", "act"),
    "MCLICK": lambda r: ("Middle click", "act"),
    "MDOWN":  lambda r: ("Drag start", "act"),
    "MUP":    lambda r: ("Drag end", "act"),
    "MEDIA":  lambda r: (f"Media · {r.strip()}", "act"),
    "KEY":    lambda r: (f'Type · “{r[:24]}”', "act") if r else None,
    "KEYSP":  lambda r: (f"Key · {r.strip()}", "act"),
    "COMBO":  lambda r: (f"Shortcut · {r.strip()}", "act"),
    "ASW":    lambda r: (f"Switch app · {r.strip()}", "act"),
    "SYS":    lambda r: (f"System · {r.strip()}", "act"),
    "PRES":   lambda r: (f"Slides · {r.strip()}", "act"),
    "VOL":    lambda r: (f"Volume → {r.strip()}%", "act"),
    "BRIGHT": lambda r: (f"Brightness → {r.strip()}%", "act"),
    "CLIP":   lambda r: (f'Paste · “{r[:24]}”', "act") if r else None,
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
    _client_connected.clear()

    def drop_client(reason=None):
        nonlocal client
        if client is not None:
            appswitch_reset()
            client = None
            _client_connected.clear()
            emit("disconnected")

    while not _stop.is_set():
        now = time.time()
        mono = time.monotonic()

        # Resume detection: a tick gap far longer than the 0.5s recv timeout means
        # the process was frozen (laptop slept). Rebind + re-announce so a phone
        # can reach us again without a restart.
        if now - last_tick > RESUME_GAP_S:
            new_ip = lan_ip()
            try:
                sock.close()
            except OSError:
                pass
            try:
                sock = open_socket()
            except OSError:
                time.sleep(1.0)
                last_tick = time.time()
                continue
            # Keep the phone pinned across the rebind: its socket + secure session
            # survived our sleep, so its very next packet (same addr, advancing
            # counter) is still accepted and the laptop stays "connected" — no
            # spurious "Waiting" flash. Just release any Alt held mid-gesture and
            # give the idle timer a fresh window for the phone to speak again.
            appswitch_reset()
            last_pkt = time.time()
            if net is not None:
                old_ip = net.get("ip")
                if net.get("zc"):
                    try:
                        net["zc"].unregister_service(net["info"])
                        net["zc"].close()
                    except Exception:
                        pass
                net["zc"], net["info"] = start_mdns(new_ip, hostname)
                net["ip"] = new_ip
                if new_ip != old_ip:
                    emit("netchange", new_ip,
                         build_uri(new_ip, wire.token, hostname, wire.key))
            emit("log", "Network recovered after sleep")
        last_tick = now

        # The phone pings ~every 1.5s; prolonged silence means it left without a
        # BYE (app killed, Wi-Fi dropped). Reflect that instead of showing it
        # "connected" forever — so the status is always truthful.
        if client is not None and now - last_pkt > CLIENT_IDLE_S:
            drop_client("idle")

        if now - bad_win > RATE_WINDOW_S:
            bad, bad_win, warned = 0, now, False

        # Local takeover auto-resume: once physical input has been quiet for the
        # grace period (and no panic latch), let the remote drive again.
        if (_remote_paused.is_set() and not _panic_latched.is_set()
                and (mono - _last_physical_ts[0]) > PHYSICAL_RESUME_GRACE_S):
            _remote_paused.clear()
            emit("resumed")

        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        if not data:
            continue

        res = wire.parse(data, addr)
        if res is None:
            bad += 1
            if bad > RATE_MAX_BAD and not warned:
                emit("warn", "High rate of rejected packets — possible brute-force / flood")
                warned = True
            continue
        verb, rest, secure = res

        if verb == "HELLO":
            # A secure HELLO is authenticated by its GCM tag (only the key holder
            # can forge one), so re-pinning to it — e.g. a reconnect from a new
            # source port — is always safe. A *plaintext* re-pin from a different
            # address only proves token possession (sniffable), so we still accept
            # it (so trusted-LAN reconnects work) but flag it loudly.
            repin = client is not None and addr != client
            appswitch_reset()
            client = addr
            last_pkt = now
            wire.commit_hello(secure)
            _client_connected.set()
            emit("connected", f"{addr[0]}:{addr[1]}", secure)
            if repin and not secure:
                emit("warn", f"Control moved to {addr[0]}:{addr[1]} over PLAINTEXT "
                             "— turn on Require encryption to prevent takeovers")
            wire.reply(sock, addr, "OK")
            continue

        if verb == "BYE":
            if addr == client:
                drop_client()
            continue

        if addr != client:
            continue
        last_pkt = now   # pinned phone is alive — keep the idle timer fed

        # PING/VGET answer over the same (encrypted) wire the client used.
        if verb == "PING":
            wire.reply(sock, addr, "PONG")
            continue
        if verb == "VGET":
            if get_volume is not None:
                wire.reply(sock, addr, f"VOL {get_volume()}")
            continue
        if verb == "BGET":
            if brightness_svc.available:
                wire.reply(sock, addr, f"BRI {brightness_svc.get_cached()}")   # cached: never blocks the loop
            continue

        # Local input wins: while the user has taken over (or after a panic),
        # every machine-driving verb is dropped on the floor.
        if verb in CONTROL_VERBS and (_remote_paused.is_set() or _panic_latched.is_set()):
            continue

        emit("action", verb, rest)
        handle_packet(verb, rest, sock, addr)

    try:
        sock.close()
    except OSError:
        pass


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
    def __init__(self, ip, port, wire, event_q, require_secure):
        import tkinter as tk
        from tkinter import font as tkf

        self._tk = tk
        self._eq = event_q
        self._wire = wire
        self._token = wire.token
        self._require_secure = require_secure
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
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

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
        uri = build_uri(self._ip, self._wire.token, self._hostname, self._wire.key)
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
            self._log("Couldn't change startup setting", "info")

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
        self._log_txt = tk.Text(
            wrap, bg=_C["card"], fg=_C["dim"], font=self.f_log,
            height=8, width=74, state="disabled", relief="flat", bd=0,
            cursor="arrow", highlightthickness=0, padx=0, pady=0,
            spacing1=1, spacing3=1,
        )
        self._log_txt.pack(fill="both", expand=True)
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
        self._root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

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
def run_terminal(token, key, ip, require_secure):
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
        serve_loop(wire, emit, net, hostname)
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
                    help="reject plaintext (v1) clients — require the encrypted QR wire")
    ap.add_argument("--setup-firewall", action="store_true",
                    help="add the Windows Firewall inbound rule (self-elevates) and exit")
    args = ap.parse_args()

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
    require_secure = args.secure_only
    ip = lan_ip()
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

            t = threading.Thread(target=serve_loop, args=(wire, emit, net, hostname),
                                 daemon=True)
            t.start()

            try:
                LazeRWindow(ip, PORT, wire, eq, require_secure).run()
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

    run_terminal(token, key, ip, require_secure)


if __name__ == "__main__":
    main()
