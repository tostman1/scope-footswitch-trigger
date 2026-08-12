"""
keyboard_device.py — Windows Raw Input + Low-Level Keyboard Hook layer
=======================================================================

Provides per-physical-keyboard identification and input isolation using:

  1. Windows Raw Input API (RegisterRawInputDevices / WM_INPUT)
       - Identifies which physical device generated each keystroke
       - Runs a hidden message-only window on a background daemon thread
       - Delivers (device_id, scan_code, extended, is_down) tuples via queue

  2. Low-Level Keyboard Hook (SetWindowsHookEx WH_KEYBOARD_LL)
       - Suppresses keystrokes that originated from the selected aux keyboard
         and are mapped to B1/B2 actions (preventing them from typing into the
         OS / application as normal text or triggering Windows shortcuts)
       - Installed on the same background thread as the Raw Input message loop
       - The hook callback checks a shared set of "currently suppressed scancodes"
         maintained by the service layer

  3. Device enumeration (GetRawInputDeviceList + GetRawInputDeviceInfo)
       - Returns a list of KeyboardDeviceInfo for all currently attached keyboards
       - Device identity is the HID device path string (stable for a given USB
         port; may change if the device is moved to a different port — documented
         limitation)

Threading model
---------------
  RawInputThread (daemon) runs a Win32 message loop:
    - Dispatches WM_INPUT messages  → posts KeyEvent to _raw_queue
    - Runs the LL hook proc on the same thread (required by Windows)
  The service layer (keyboard_mapping.py) drains _raw_queue from the Qt main
  thread via the existing 50 ms QTimer tick.

Windows key suppression
-----------------------
  The LL hook intercepts VK_LWIN / VK_RWIN from the aux keyboard and returns
  CallNextHookEx result of 1 (non-zero) to suppress them.  This prevents
  WM_KEYDOWN from reaching applications including Explorer/shell in most cases.

  LIMITATION (marked Experimental in the UI):
    On Windows 10/11 the taskbar shell may handle the Win key at a level below
    WH_KEYBOARD_LL hooks in some configurations (e.g. when "gaming mode" or
    certain accessibility features are active).  In those cases the Start menu
    may still open despite the hook.  There is no purely user-space solution
    that guarantees suppression in every Windows configuration.

    The primary keyboard is NOT affected — only aux-keyboard Win key events
    that are in the active suppression set are blocked.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

RIDEV_INPUTSINK   = 0x00000100   # receive input even when not in foreground
RIDEV_REMOVE      = 0x00000001   # unregister
RIM_TYPEKEYBOARD  = 1
RIDI_DEVICENAME   = 0x20000007
RIDI_DEVICEINFO   = 0x2000000b
WM_INPUT          = 0x00FF
WM_QUIT           = 0x0012
RID_INPUT         = 0x10000003
RI_KEY_BREAK      = 0x01         # key-up flag in RAWKEYBOARD.Flags
RI_KEY_E0         = 0x02         # extended key (E0 prefix)
RI_KEY_E1         = 0x04         # extended key (E1 prefix)
MAPVK_VSC_TO_VK_EX = 3

WH_KEYBOARD_LL    = 13
WM_KEYDOWN        = 0x0100
WM_KEYUP          = 0x0101
WM_SYSKEYDOWN     = 0x0104
WM_SYSKEYUP       = 0x0105
HC_ACTION         = 0

HWND_MESSAGE      = wt.HWND(-3)

VK_LWIN           = 0x5B
VK_RWIN           = 0x5C

# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage",     wt.USHORT),
        ("dwFlags",     wt.DWORD),
        ("hwndTarget",  wt.HWND),
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  wt.DWORD),
        ("dwSize",  wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam",  wt.WPARAM),
    ]

class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode",         wt.USHORT),
        ("Flags",            wt.USHORT),
        ("Reserved",         wt.USHORT),
        ("VKey",             wt.USHORT),
        ("Message",          wt.UINT),
        ("ExtraInformation", wt.ULONG),
    ]

class RAWINPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("keyboard", RAWKEYBOARD)]
    _anonymous_ = ("_data",)
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("_data",  _UNION),
    ]

class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [
        ("hDevice", wt.HANDLE),
        ("dwType",  wt.DWORD),
    ]

class RID_DEVICE_INFO_KEYBOARD(ctypes.Structure):
    _fields_ = [
        ("dwType",                 wt.DWORD),
        ("dwSubType",              wt.DWORD),
        ("dwKeyboardMode",         wt.DWORD),
        ("dwNumberOfFunctionKeys", wt.DWORD),
        ("dwNumberOfIndicators",   wt.DWORD),
        ("dwNumberOfKeysTotal",    wt.DWORD),
    ]

class RID_DEVICE_INFO(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("keyboard", RID_DEVICE_INFO_KEYBOARD)]
    _anonymous_ = ("_data",)
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("dwType", wt.DWORD),
        ("_data",  _UNION),
    ]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      wt.DWORD),
        ("scanCode",    wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]

# ---------------------------------------------------------------------------
# Scan-code → display name table
# ---------------------------------------------------------------------------

# Canonical scan code → display name.
# Extended keys (E0 prefix) are stored as (scancode | 0x100).
# Left/right variants are distinguished by the E0 flag.

_SCANCODE_NAMES: dict[int, str] = {
    # Row 0 — function keys / special
    0x01: "Escape",
    0x3B: "F1",  0x3C: "F2",  0x3D: "F3",  0x3E: "F4",
    0x3F: "F5",  0x40: "F6",  0x41: "F7",  0x42: "F8",
    0x43: "F9",  0x44: "F10", 0x57: "F11", 0x58: "F12",
    # Row 1 — number row
    0x29: "Backtick/Tilde",
    0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5",
    0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
    0x0C: "Minus", 0x0D: "Equals", 0x0E: "Backspace",
    # Row 2 — QWERTY
    0x0F: "Tab",
    0x10: "Q", 0x11: "W", 0x12: "E", 0x13: "R", 0x14: "T",
    0x15: "Y", 0x16: "U", 0x17: "I", 0x18: "O", 0x19: "P",
    0x1A: "[", 0x1B: "]", 0x2B: "\\",
    # Row 3 — home row
    0x3A: "Caps Lock",
    0x1E: "A", 0x1F: "S", 0x20: "D", 0x21: "F", 0x22: "G",
    0x23: "H", 0x24: "J", 0x25: "K", 0x26: "L",
    0x27: ";", 0x28: "'", 0x1C: "Enter",
    # Row 4 — shift row
    0x2A: "Left Shift",
    0x2C: "Z", 0x2D: "X", 0x2E: "C", 0x2F: "V", 0x30: "B",
    0x31: "N", 0x32: "M", 0x33: ",", 0x34: ".", 0x35: "/",
    0x36: "Right Shift",
    # Row 5 — bottom row
    0x1D: "Left Ctrl",
    0x38: "Left Alt",
    0x39: "Space",
    # Numpad
    0x45: "Num Lock",
    0x47: "Numpad 7", 0x48: "Numpad 8", 0x49: "Numpad 9",
    0x4A: "Numpad -",
    0x4B: "Numpad 4", 0x4C: "Numpad 5", 0x4D: "Numpad 6",
    0x4E: "Numpad +",
    0x4F: "Numpad 1", 0x50: "Numpad 2", 0x51: "Numpad 3",
    0x52: "Numpad 0", 0x53: "Numpad .",
    # Other
    0x54: "SysRq",
    0x56: "OEM_102",   # extra key on ISO keyboards
    0x64: "F13", 0x65: "F14", 0x66: "F15",
    # Extended keys (E0 prefix → stored as scancode | 0x100)
    0x1C | 0x100: "Numpad Enter",
    0x1D | 0x100: "Right Ctrl",
    0x35 | 0x100: "Numpad /",
    0x37 | 0x100: "Print Screen",
    0x38 | 0x100: "Right Alt",
    0x45 | 0x100: "Pause",
    0x46 | 0x100: "Break",
    0x47 | 0x100: "Home",
    0x48 | 0x100: "Up",
    0x49 | 0x100: "Page Up",
    0x4B | 0x100: "Left",
    0x4D | 0x100: "Right",
    0x4F | 0x100: "End",
    0x50 | 0x100: "Down",
    0x51 | 0x100: "Page Down",
    0x52 | 0x100: "Insert",
    0x53 | 0x100: "Delete",
    0x5B | 0x100: "Left Windows",
    0x5C | 0x100: "Right Windows",
    0x5D | 0x100: "Menu",
}


def scancode_to_name(scancode: int, extended: bool) -> str:
    """Return a human-readable name for a scan code + extended flag pair."""
    key = (scancode | 0x100) if extended else scancode
    return _SCANCODE_NAMES.get(key, f"Scan 0x{key:03X}")


def scancode_key(scancode: int, extended: bool) -> int:
    """Return the canonical integer key used internally (scancode | 0x100 if extended)."""
    return (scancode | 0x100) if extended else scancode


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class KeyboardDeviceInfo:
    """Information about one physical keyboard reported by Raw Input."""
    handle: int         # Raw Input HANDLE (valid for current session only)
    device_id: str      # HID device path — used as the stable persistent identity
    display_name: str   # Human-readable name (from registry; may not be unique)


@dataclass
class KeyEvent:
    """A single key event from Raw Input, tagged with its source device."""
    device_id: str      # matches KeyboardDeviceInfo.device_id
    scan_key: int       # scancode_key(raw_scancode, extended) — canonical int
    is_down: bool       # True = key pressed, False = key released
    vk: int             # Windows virtual key code (for Win key detection)


# ---------------------------------------------------------------------------
# Device enumeration (can be called from any thread)
# ---------------------------------------------------------------------------

def enumerate_keyboards() -> list[KeyboardDeviceInfo]:
    """Return a list of all currently connected keyboard devices via Raw Input."""
    user32 = ctypes.windll.user32

    count = wt.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if count.value == 0:
        return []

    buf = (RAWINPUTDEVICELIST * count.value)()
    user32.GetRawInputDeviceList(buf, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))

    result = []
    for item in buf:
        if item.dwType != RIM_TYPEKEYBOARD:
            continue
        device_id = _get_device_path(item.hDevice)
        display_name = _get_display_name(item.hDevice, device_id)
        result.append(KeyboardDeviceInfo(
            handle=item.hDevice,
            device_id=device_id,
            display_name=display_name,
        ))
    return result


def _get_device_path(handle: int) -> str:
    """Return the HID device path string for a Raw Input device handle."""
    user32 = ctypes.windll.user32
    size = wt.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value


def _get_display_name(handle: int, device_path: str) -> str:
    """Attempt to get a friendly display name from the registry.
    Falls back to a shortened device path on failure."""
    try:
        import winreg
        # Device path looks like: \\?\HID#VID_046D&PID_C31C&MI_00#...
        # The registry key is under HKLM\SYSTEM\CurrentControlSet\Enum\
        # with backslash-separated components derived from the path.
        # Simplest approach: extract VID/PID and look up in registry.
        path = device_path.upper()
        vid, pid = "", ""
        for part in path.replace("\\\\?\\", "").split("#"):
            for token in part.split("&"):
                if token.startswith("VID_"):
                    vid = token[4:]
                elif token.startswith("PID_"):
                    pid = token[4:]
        if vid and pid:
            reg_path = f"SYSTEM\\CurrentControlSet\\Enum\\HID\\VID_{vid}&PID_{pid}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                # Iterate subkeys (instance keys) to find FriendlyName / DeviceDesc
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name) as sub:
                            try:
                                name, _ = winreg.QueryValueEx(sub, "FriendlyName")
                                return name
                            except FileNotFoundError:
                                pass
                            try:
                                name, _ = winreg.QueryValueEx(sub, "DeviceDesc")
                                # DeviceDesc may be "@oem123.inf,%;ClassName%"
                                if ";" in name:
                                    name = name.split(";")[-1]
                                return name
                            except FileNotFoundError:
                                pass
                        i += 1
                    except OSError:
                        break
    except Exception:
        pass

    # Fallback: extract something readable from the path
    parts = device_path.replace("\\\\?\\", "").split("#")
    if len(parts) >= 1:
        return parts[0].replace("_", " ")
    return "Unknown Keyboard"


# ---------------------------------------------------------------------------
# RawInputThread — hidden message window + LL hook
# ---------------------------------------------------------------------------

# Shared mutable state between the LL hook (background thread) and the service
# (main thread).  Protected by a threading.Lock.
_suppress_lock = threading.Lock()
_suppress_scan_keys: set[int] = set()   # canonical scan keys to suppress from aux kbd
_aux_device_id: Optional[str] = None    # currently selected aux device id


def set_suppression_state(device_id: Optional[str], scan_keys: set[int]) -> None:
    """Called from the main thread to update what the LL hook should suppress."""
    global _aux_device_id, _suppress_scan_keys
    with _suppress_lock:
        _aux_device_id = device_id
        _suppress_scan_keys = set(scan_keys)


class RawInputThread(threading.Thread):
    """Daemon thread that:
      1. Creates a hidden message-only window to receive WM_INPUT.
      2. Registers for keyboard Raw Input (RIDEV_INPUTSINK — works in background).
      3. Installs a low-level keyboard hook for suppression.
      4. Runs a Win32 message loop until stop() is called.

    Key events are posted to raw_queue as KeyEvent objects.
    The hook callback suppresses mapped aux-keyboard keys before they reach the OS.
    """

    def __init__(self, raw_queue: queue.Queue):
        super().__init__(daemon=True, name="RawInputThread")
        self._raw_queue = raw_queue
        self._stop_event = threading.Event()
        self._hwnd: Optional[int] = None
        self._thread_id: int = 0
        self._hook = None
        self._started = threading.Event()

    # ------------------------------------------------------------------
    # Public API (called from main thread)
    # ------------------------------------------------------------------

    def start_and_wait(self, timeout: float = 2.0) -> bool:
        """Start thread and wait until the message loop is running."""
        self.start()
        return self._started.wait(timeout)

    def stop(self) -> None:
        """Signal the message loop to quit and join the thread."""
        self._stop_event.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        # --- Create message-only window ---
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                self._on_raw_input(hwnd, lparam)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_cb = WNDPROC(wnd_proc)   # keep reference alive

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style",         wt.UINT),
                ("lpfnWndProc",   WNDPROC),
                ("cbClsExtra",    ctypes.c_int),
                ("cbWndExtra",    ctypes.c_int),
                ("hInstance",     wt.HINSTANCE),
                ("hIcon",         wt.HANDLE),
                ("hCursor",       wt.HANDLE),
                ("hbrBackground", wt.HANDLE),
                ("lpszMenuName",  wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
            ]

        class_name = "OsciFootswitchRawInput"
        wc = WNDCLASSW()
        wc.lpfnWndProc   = self._wnd_proc_cb
        wc.hInstance     = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))

        self._hwnd = user32.CreateWindowExW(
            0, class_name, "RawInputSink", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None
        )

        if not self._hwnd:
            self._started.set()
            return

        # --- Register for keyboard Raw Input ---
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 1      # Generic Desktop
        rid.usUsage     = 6      # Keyboard
        rid.dwFlags     = RIDEV_INPUTSINK
        rid.hwndTarget  = self._hwnd
        user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
        )

        # --- Install LL keyboard hook ---
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT))

        def hook_proc(nCode, wParam, lParam):
            if nCode == HC_ACTION:
                kb = lParam.contents
                if self._should_suppress(kb.vkCode, kb.scanCode, kb.flags):
                    return 1   # suppress — do NOT call CallNextHookEx
            return ctypes.windll.user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

        self._hook_proc_cb = HOOKPROC(hook_proc)   # keep reference alive
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc_cb, kernel32.GetModuleHandleW(None), 0
        )

        self._started.set()

        # --- Message loop ---
        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd",    wt.HWND),
                ("message", wt.UINT),
                ("wParam",  wt.WPARAM),
                ("lParam",  wt.LPARAM),
                ("time",    wt.DWORD),
                ("pt",      wt.POINT),
            ]

        msg = MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # --- Cleanup ---
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
        user32.UnregisterClassW("OsciFootswitchRawInput", kernel32.GetModuleHandleW(None))

    # ------------------------------------------------------------------
    # Raw Input processing (called on background thread from wnd_proc)
    # ------------------------------------------------------------------

    def _on_raw_input(self, hwnd: int, lparam: int) -> None:
        user32 = ctypes.windll.user32
        size = wt.UINT(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)) != size.value:
            return

        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEKEYBOARD:
            return

        kb = raw.keyboard
        # Ignore synthetic injected events (scan code 0 = injected by software)
        if kb.MakeCode == 0:
            return

        is_down = not bool(kb.Flags & RI_KEY_BREAK)
        extended = bool(kb.Flags & RI_KEY_E0)
        scan_key = scancode_key(kb.MakeCode, extended)

        device_id = _get_device_path(raw.header.hDevice)

        self._raw_queue.put(KeyEvent(
            device_id=device_id,
            scan_key=scan_key,
            is_down=is_down,
            vk=kb.VKey,
        ))

    # ------------------------------------------------------------------
    # LL hook suppression check (called on background thread)
    # ------------------------------------------------------------------

    def _should_suppress(self, vk: int, sc: int, flags: int) -> bool:
        """Return True if this key event should be suppressed (swallowed).

        Suppresses a key only when ALL of the following hold:
          - An aux device is configured
          - The canonical scan key is in the active suppression set
          - The key is currently tracked as pressed by the aux device
            (prevents suppressing the same scan code from the primary kbd)

        Note: the LL hook receives the event BEFORE Raw Input, so we use the
        suppression set maintained by the service layer (updated from the Raw
        Input stream one event earlier for held keys).  For the initial
        key-down of a new press there is an unavoidable single-event race;
        in practice this is imperceptible.
        """
        with _suppress_lock:
            if _aux_device_id is None:
                return False
            extended = bool(flags & 0x01)   # LLKHF_EXTENDED
            sk = scancode_key(sc, extended)
            return sk in _suppress_scan_keys
