"""
keyboard_mapping.py — Keyboard mapping settings model + runtime service
=======================================================================

KeyboardMappingSettings
-----------------------
  Pure data class.  Holds the persisted configuration:
    - enabled            bool
    - device_id          str   HID device path (stable per port)
    - device_name        str   display name (informational)
    - b1_keys            list[int]  canonical scan keys (scancode_key values)
    - b1b2_keys          list[int]
    - b2_keys            list[int]

  load_from_settings / save_to_settings use QSettings keys under the
  "keyboard_mapping/" prefix so they coexist cleanly with existing keys.

KeyboardButtonService
---------------------
  Orchestrates the three operating modes (RUNTIME / IDENTIFYING / CONFIGURING)
  and the B1/B2 long-press state machine.

  Long-press semantics (mirror of Arduino firmware):
    - B1/B2 down fires when the *logical* button transitions from released to
      active (first mapped key pressed).
    - At exactly 800 ms of continuous logical activation the long event fires
      while the key is still held (same as firmware: "B1L" emitted at 800 ms,
      not on release).
    - If released before 800 ms the short event fires on release.
    - The combo (B1+B2 both active simultaneously) mirrors the firmware's
      comboActive logic: once both are simultaneously active, individual short
      events are suppressed and the combo timer governs instead.

  Designed to be driven by the existing 50 ms QTimer tick in MainWindow._tick().
  No timers of its own — tick() is called by the host every 50 ms.

  Key repeat suppression: tracks physically-pressed scan keys per device;
  a key-down event for a key already in the pressed set is silently dropped.

  Multiple keys → same button: implemented via reference-counted active sets.
  B1 stays active as long as at least one mapped key (from any source) is held.

  Multiple input sources (footswitch + keyboard): the service maintains a
  separate contribution count for the physical footswitch (incremented by
  footswitch_b1_down() etc.) so that releasing the keyboard does not release
  B1 while the footswitch also holds it, and vice versa.

  Disconnect / disable cleanup: release_keyboard_contributions() clears all
  pending keyboard key contributions and fires synthetic up events as needed.

Connecting to the existing pipeline
-------------------------------------
  KeyboardButtonService.tick() is called from MainWindow._tick().
  When a logical event fires, it is placed into the host's event_queue as a
  plain string ("B1S", "B1L", etc.) — identical to the serial footswitch path.
  This means handle_event() is called exactly as before, with no duplication.
"""

from __future__ import annotations

import json
import queue
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

from PySide6.QtCore import QSettings

from keyboard_device import (
    KeyEvent, KeyboardDeviceInfo, RawInputThread,
    enumerate_keyboards, scancode_to_name, set_suppression_state,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS_PREFIX = "keyboard_mapping/"

_DEFAULTS = {
    "enabled":      False,
    "device_id":    "",
    "device_name":  "",
    "b1_keys":      "[]",
    "b1b2_keys":    "[]",
    "b2_keys":      "[]",
}


@dataclass
class KeyboardMappingSettings:
    """Persistent keyboard mapping configuration."""
    enabled:      bool       = False
    device_id:    str        = ""
    device_name:  str        = ""
    b1_keys:      list[int]  = field(default_factory=list)
    b1b2_keys:    list[int]  = field(default_factory=list)
    b2_keys:      list[int]  = field(default_factory=list)

    def copy(self) -> "KeyboardMappingSettings":
        return KeyboardMappingSettings(
            enabled=self.enabled,
            device_id=self.device_id,
            device_name=self.device_name,
            b1_keys=list(self.b1_keys),
            b1b2_keys=list(self.b1b2_keys),
            b2_keys=list(self.b2_keys),
        )

    # ---- mutation helpers (enforce exclusivity) ----

    def assign_key(self, scan_key: int, target: str) -> None:
        """Assign scan_key to target ('b1'/'b1b2'/'b2'), removing it from any
        other list first.  Duplicate entries within the target list are prevented."""
        self.remove_key(scan_key)
        lst = self._list_for(target)
        if scan_key not in lst:
            lst.append(scan_key)

    def remove_key(self, scan_key: int) -> None:
        """Remove scan_key from whichever list currently holds it."""
        for lst in (self.b1_keys, self.b1b2_keys, self.b2_keys):
            if scan_key in lst:
                lst.remove(scan_key)

    def clear_list(self, target: str) -> None:
        self._list_for(target).clear()

    def _list_for(self, target: str) -> list[int]:
        if target == "b1":
            return self.b1_keys
        if target == "b1b2":
            return self.b1b2_keys
        if target == "b2":
            return self.b2_keys
        raise ValueError(f"Unknown target: {target!r}")

    def validate(self) -> list[str]:
        """Return a list of validation error messages (empty = valid)."""
        errors = []
        if not self.b1_keys:
            errors.append("Map at least one key for B1.")
        if not self.b2_keys:
            errors.append("Map at least one key for B2.")
        return errors

    # ---- persistence ----

    @classmethod
    def load_from_settings(cls, qs: QSettings) -> "KeyboardMappingSettings":
        def _bool(key: str) -> bool:
            return qs.value(_SETTINGS_PREFIX + key, _DEFAULTS[key], type=bool)
        def _str(key: str) -> str:
            return qs.value(_SETTINGS_PREFIX + key, _DEFAULTS[key])
        def _json_ints(key: str) -> list[int]:
            raw = qs.value(_SETTINGS_PREFIX + key, _DEFAULTS[key])
            try:
                return [int(x) for x in json.loads(raw)]
            except Exception:
                return []

        return cls(
            enabled=_bool("enabled"),
            device_id=_str("device_id"),
            device_name=_str("device_name"),
            b1_keys=_json_ints("b1_keys"),
            b1b2_keys=_json_ints("b1b2_keys"),
            b2_keys=_json_ints("b2_keys"),
        )

    def save_to_settings(self, qs: QSettings) -> None:
        qs.setValue(_SETTINGS_PREFIX + "enabled",      self.enabled)
        qs.setValue(_SETTINGS_PREFIX + "device_id",    self.device_id)
        qs.setValue(_SETTINGS_PREFIX + "device_name",  self.device_name)
        qs.setValue(_SETTINGS_PREFIX + "b1_keys",      json.dumps(self.b1_keys))
        qs.setValue(_SETTINGS_PREFIX + "b1b2_keys",    json.dumps(self.b1b2_keys))
        qs.setValue(_SETTINGS_PREFIX + "b2_keys",      json.dumps(self.b2_keys))


# ---------------------------------------------------------------------------
# Service mode enum
# ---------------------------------------------------------------------------

class ServiceMode(Enum):
    RUNTIME     = auto()   # translate keys → B1/B2 events
    IDENTIFYING = auto()   # listen for next key; determine device identity
    CONFIGURING = auto()   # capture keys for mapping; suppress B1/B2 actions


# ---------------------------------------------------------------------------
# Long-press state machine (per logical button: B1, B2, Combo)
# ---------------------------------------------------------------------------

LONG_PRESS_THRESHOLD = 0.800   # seconds — matches Arduino firmware


class _ButtonTimer:
    """Tracks activation time and long-press state for one logical button."""

    def __init__(self) -> None:
        self._press_time: Optional[float] = None
        self._long_fired: bool = False

    def activate(self, now: float) -> None:
        """Called when logical button transitions 0 → 1 (first key down)."""
        self._press_time = now
        self._long_fired = False

    def deactivate(self) -> None:
        """Called when logical button transitions 1 → 0 (last key up)."""
        self._press_time = None
        self._long_fired = False

    def check_long(self, now: float) -> bool:
        """Return True once when the long-press threshold is crossed.
        Must be called on every tick while the button is held."""
        if self._press_time is None or self._long_fired:
            return False
        if (now - self._press_time) >= LONG_PRESS_THRESHOLD:
            self._long_fired = True
            return True
        return False

    def short_on_release(self) -> bool:
        """Return True if a short-press event should fire on release."""
        return not self._long_fired

    @property
    def is_active(self) -> bool:
        return self._press_time is not None


# ---------------------------------------------------------------------------
# KeyboardButtonService
# ---------------------------------------------------------------------------

class KeyboardButtonService:
    """
    Translates physical keyboard events (from RawInputThread) into logical
    B1/B2 footswitch events and posts them to the host's event_queue.

    Call tick(now) from the main thread every ~50 ms (from the existing QTimer).
    Call process_raw_event(key_event) from tick() to feed new raw events.

    Threading: all public methods are called from the Qt main thread.
    The RawInputThread posts to _raw_queue; tick() drains it.
    """

    IDENTIFY_TIMEOUT = 10.0   # seconds

    def __init__(self, event_queue: queue.Queue, settings: KeyboardMappingSettings):
        self._event_queue   = event_queue
        self._settings      = settings

        # Raw Input thread
        self._raw_queue: queue.Queue = queue.Queue()
        self._raw_thread: Optional[RawInputThread] = None

        # Mode
        self._mode = ServiceMode.RUNTIME

        # Physical key tracking: device_id → set of pressed scan_keys
        # Prevents OS key-repeat from producing multiple logical down events.
        self._pressed: dict[str, set[int]] = {}

        # Logical button active counts (keyboard contribution)
        # B1 is active while _b1_kbd_count > 0 OR _b1_fs_count > 0
        self._b1_kbd_count: int = 0
        self._b2_kbd_count: int = 0
        # Footswitch contribution (incremented/decremented by footswitch_b*_down/up)
        self._b1_fs_count:  int = 0
        self._b2_fs_count:  int = 0

        # Long-press timers
        self._b1_timer    = _ButtonTimer()
        self._b2_timer    = _ButtonTimer()
        self._combo_timer = _ButtonTimer()

        # Combo state: True once both B1 and B2 are simultaneously active
        self._combo_active:    bool = False
        self._combo_long_sent: bool = False

        # Identification mode state
        self._identify_start:    float = 0.0
        self._identify_callback: Optional[Callable[[Optional[KeyboardDeviceInfo]], int]] = None
        # Callback receives None on timeout, KeyboardDeviceInfo on success.
        # Returns remaining seconds (for countdown display) — ignored after first call.
        self._identify_tick_cb: Optional[Callable[[int], None]] = None

        # Configuring mode state
        self._config_target:   str = ""           # "b1" / "b1b2" / "b2"
        self._config_callback: Optional[Callable[[int, str], None]] = None
        # Called with (scan_key, key_name) for each newly captured key.

        # Update suppression set on start
        self._update_suppression()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start the Raw Input background thread.  Returns True on success."""
        if self._raw_thread and self._raw_thread.is_alive():
            return True
        self._raw_thread = RawInputThread(self._raw_queue)
        ok = self._raw_thread.start_and_wait(timeout=2.0)
        return ok

    def stop(self) -> None:
        """Stop the Raw Input thread and release all keyboard contributions."""
        self.release_keyboard_contributions()
        if self._raw_thread:
            self._raw_thread.stop()
            self._raw_thread = None
        set_suppression_state(None, set())

    # ------------------------------------------------------------------
    # Settings update
    # ------------------------------------------------------------------

    def apply_settings(self, settings: KeyboardMappingSettings) -> None:
        """Apply new settings at runtime (called after Save in dialog)."""
        # If device changed or mapping disabled, release all kbd contributions
        if (settings.device_id != self._settings.device_id
                or not settings.enabled):
            self.release_keyboard_contributions()
        self._settings = settings
        self._update_suppression()

    @property
    def settings(self) -> KeyboardMappingSettings:
        return self._settings

    # ------------------------------------------------------------------
    # Mode control
    # ------------------------------------------------------------------

    def start_identification(
        self,
        on_result: Callable[[Optional[KeyboardDeviceInfo]], None],
        on_tick:   Callable[[int], None],
    ) -> None:
        """Enter IDENTIFYING mode.  Fires on_result with the identified device
        (or None on timeout).  Fires on_tick(seconds_remaining) each tick."""
        self.release_keyboard_contributions()
        self._mode = ServiceMode.IDENTIFYING
        self._identify_start    = time.monotonic()
        self._identify_callback = on_result
        self._identify_tick_cb  = on_tick
        self._pressed.clear()

    def cancel_identification(self) -> None:
        if self._mode == ServiceMode.IDENTIFYING:
            self._mode = ServiceMode.RUNTIME
            self._identify_callback = None
            self._identify_tick_cb  = None

    def start_configuration(
        self,
        target:      str,
        on_capture:  Callable[[int, str], None],
    ) -> None:
        """Enter CONFIGURING mode for the given target ('b1'/'b1b2'/'b2').
        on_capture(scan_key, key_name) is called for each newly pressed key."""
        self.release_keyboard_contributions()
        self._mode           = ServiceMode.CONFIGURING
        self._config_target  = target
        self._config_callback = on_capture
        self._pressed.clear()

    def stop_configuration(self) -> None:
        if self._mode == ServiceMode.CONFIGURING:
            self._mode = ServiceMode.RUNTIME
            self._config_callback = None
            self._pressed.clear()

    @property
    def mode(self) -> ServiceMode:
        return self._mode

    # ------------------------------------------------------------------
    # Footswitch B1/B2 contributions (called when physical footswitch fires)
    # ------------------------------------------------------------------

    def footswitch_b1_down(self) -> None:
        self._b1_fs_count += 1
        self._on_b1_change(time.monotonic())

    def footswitch_b1_up(self) -> None:
        self._b1_fs_count = max(0, self._b1_fs_count - 1)
        self._on_b1_change(time.monotonic())

    def footswitch_b2_down(self) -> None:
        self._b2_fs_count += 1
        self._on_b2_change(time.monotonic())

    def footswitch_b2_up(self) -> None:
        self._b2_fs_count = max(0, self._b2_fs_count - 1)
        self._on_b2_change(time.monotonic())

    # ------------------------------------------------------------------
    # Tick — called from Qt main thread every ~50 ms
    # ------------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> None:
        """Drain the raw event queue and advance long-press timers."""
        if now is None:
            now = time.monotonic()

        # Drain raw events
        while True:
            try:
                ev: KeyEvent = self._raw_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_raw_event(ev, now)

        # Advance long-press timers (only in RUNTIME mode)
        if self._mode == ServiceMode.RUNTIME and self._settings.enabled:
            self._advance_timers(now)

        # Identification countdown
        if self._mode == ServiceMode.IDENTIFYING:
            elapsed = now - self._identify_start
            remaining = int(self.IDENTIFY_TIMEOUT - elapsed)
            if self._identify_tick_cb:
                self._identify_tick_cb(max(0, remaining))
            if elapsed >= self.IDENTIFY_TIMEOUT:
                cb = self._identify_callback
                self._mode = ServiceMode.RUNTIME
                self._identify_callback = None
                self._identify_tick_cb  = None
                if cb:
                    cb(None)

    # ------------------------------------------------------------------
    # Release all keyboard contributions (on disconnect / disable)
    # ------------------------------------------------------------------

    def release_keyboard_contributions(self) -> None:
        """Immediately release all B1/B2 contributions from the keyboard.
        Should be called when the device disconnects or mapping is disabled."""
        now = time.monotonic()
        if self._b1_kbd_count > 0:
            self._b1_kbd_count = 0
            self._on_b1_change(now)
        if self._b2_kbd_count > 0:
            self._b2_kbd_count = 0
            self._on_b2_change(now)
        self._pressed.clear()
        self._update_suppression()

    # ------------------------------------------------------------------
    # Internal: handle one raw key event
    # ------------------------------------------------------------------

    def _handle_raw_event(self, ev: KeyEvent, now: float) -> None:
        # --- IDENTIFYING mode ---
        if self._mode == ServiceMode.IDENTIFYING:
            if ev.is_down:
                # Identify the physical device from this event
                devices = enumerate_keyboards()
                info = next((d for d in devices if d.device_id == ev.device_id), None)
                if info is None:
                    info = KeyboardDeviceInfo(
                        handle=0,
                        device_id=ev.device_id,
                        display_name=ev.device_id,
                    )
                cb = self._identify_callback
                self._mode = ServiceMode.RUNTIME
                self._identify_callback = None
                self._identify_tick_cb  = None
                if cb:
                    cb(info)
            return

        # Only process events from the selected aux device
        if not self._settings.enabled:
            return
        if ev.device_id != self._settings.device_id:
            return

        # OS key-repeat suppression
        pressed_set = self._pressed.setdefault(ev.device_id, set())
        if ev.is_down:
            if ev.scan_key in pressed_set:
                return   # repeat — ignore
            pressed_set.add(ev.scan_key)
        else:
            pressed_set.discard(ev.scan_key)

        # --- CONFIGURING mode ---
        if self._mode == ServiceMode.CONFIGURING:
            if ev.is_down and self._config_callback:
                from keyboard_device import scancode_to_name
                # Determine extended flag from scan_key encoding
                extended = bool(ev.scan_key & 0x100)
                raw_sc   = ev.scan_key & 0xFF
                name = scancode_to_name(raw_sc, extended)
                self._config_callback(ev.scan_key, name)
            return

        # --- RUNTIME mode ---
        sk = ev.scan_key
        b1_hit   = sk in self._settings.b1_keys
        b1b2_hit = sk in self._settings.b1b2_keys
        b2_hit   = sk in self._settings.b2_keys

        if ev.is_down:
            if b1_hit:
                self._b1_kbd_count += 1
                self._on_b1_change(now)
            if b2_hit:
                self._b2_kbd_count += 1
                self._on_b2_change(now)
            if b1b2_hit:
                self._b1_kbd_count += 1
                self._b2_kbd_count += 1
                self._on_b1_change(now)
                self._on_b2_change(now)
        else:
            if b1_hit:
                self._b1_kbd_count = max(0, self._b1_kbd_count - 1)
                self._on_b1_change(now)
            if b2_hit:
                self._b2_kbd_count = max(0, self._b2_kbd_count - 1)
                self._on_b2_change(now)
            if b1b2_hit:
                self._b1_kbd_count = max(0, self._b1_kbd_count - 1)
                self._b2_kbd_count = max(0, self._b2_kbd_count - 1)
                self._on_b1_change(now)
                self._on_b2_change(now)

    # ------------------------------------------------------------------
    # Internal: logical button change callbacks
    # ------------------------------------------------------------------

    @property
    def _b1_active(self) -> bool:
        return (self._b1_kbd_count + self._b1_fs_count) > 0

    @property
    def _b2_active(self) -> bool:
        return (self._b2_kbd_count + self._b2_fs_count) > 0

    def _on_b1_change(self, now: float) -> None:
        self._evaluate_combo(now)

    def _on_b2_change(self, now: float) -> None:
        self._evaluate_combo(now)

    def _evaluate_combo(self, now: float) -> None:
        """Re-evaluate logical state after any B1 or B2 change.
        Mirrors the Arduino firmware combo / individual button logic."""
        b1 = self._b1_active
        b2 = self._b2_active

        if b1 and b2:
            # Both active — enter or stay in combo
            if not self._combo_active:
                self._combo_active    = True
                self._combo_long_sent = False
                self._combo_timer.activate(now)
                # Suppress any pending individual short events
                # (individual timers remain active but their short-fire is
                #  suppressed below in _advance_timers while combo is active)
        else:
            if self._combo_active:
                # One or both released — fire combo event if long not yet sent
                self._combo_active = False
                if not self._combo_long_sent:
                    self._emit("BBS")
                self._combo_timer.deactivate()
                # Reset individual buttons to avoid spurious fires after combo
                self._b1_timer.deactivate()
                self._b2_timer.deactivate()
                if not b1:
                    pass   # B1 already inactive — no individual release needed
                if not b2:
                    pass   # B2 already inactive
                return

            # Individual button transitions (no combo)
            if b1 and not self._b1_timer.is_active:
                self._b1_timer.activate(now)
            elif not b1 and self._b1_timer.is_active:
                if self._b1_timer.short_on_release():
                    self._emit("B1S")
                self._b1_timer.deactivate()

            if b2 and not self._b2_timer.is_active:
                self._b2_timer.activate(now)
            elif not b2 and self._b2_timer.is_active:
                if self._b2_timer.short_on_release():
                    self._emit("B2S")
                self._b2_timer.deactivate()

    def _advance_timers(self, now: float) -> None:
        """Check long-press thresholds.  Called every tick."""
        if self._combo_active:
            if self._combo_timer.check_long(now):
                self._combo_long_sent = True
                self._emit("BBL")
            return

        if self._b1_timer.is_active and not self._combo_active:
            if self._b1_timer.check_long(now):
                self._emit("B1L")

        if self._b2_timer.is_active and not self._combo_active:
            if self._b2_timer.check_long(now):
                self._emit("B2L")

    # ------------------------------------------------------------------
    # Internal: emit event to main application
    # ------------------------------------------------------------------

    def _emit(self, event: str) -> None:
        """Post a footswitch-compatible event string to the main event queue."""
        self._event_queue.put(event)

    # ------------------------------------------------------------------
    # Internal: update LL hook suppression state
    # ------------------------------------------------------------------

    def _update_suppression(self) -> None:
        """Tell the LL hook which scan keys to suppress and from which device."""
        if not self._settings.enabled or not self._settings.device_id:
            set_suppression_state(None, set())
            return
        all_mapped = (
            set(self._settings.b1_keys)
            | set(self._settings.b1b2_keys)
            | set(self._settings.b2_keys)
        )
        set_suppression_state(self._settings.device_id, all_mapped)

    # ------------------------------------------------------------------
    # Connected device status helpers
    # ------------------------------------------------------------------

    def is_device_connected(self) -> bool:
        """Return True if the configured aux keyboard is currently present."""
        if not self._settings.device_id:
            return False
        from keyboard_device import enumerate_keyboards
        devices = enumerate_keyboards()
        return any(d.device_id == self._settings.device_id for d in devices)
