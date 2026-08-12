"""
test_keyboard_mapping.py — Unit tests for keyboard mapping logic
================================================================

These tests cover the pure-Python logic layer (keyboard_mapping.py) and
require NO Windows API, NO Qt, and NO physical hardware.

The keyboard_device module is mocked where needed so the tests run on any
platform and in CI without a display.

Run with:
    cd src/pc_app
    python -m pytest tests/test_keyboard_mapping.py -v
"""

import queue
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock keyboard_device before importing keyboard_mapping (avoids ctypes/Win32)
# ---------------------------------------------------------------------------

_mock_kbd_device = types.ModuleType("keyboard_device")

class _FakeKeyEvent:
    def __init__(self, device_id, scan_key, is_down, vk=0):
        self.device_id = device_id
        self.scan_key  = scan_key
        self.is_down   = is_down
        self.vk        = vk

class _FakeKeyboardDeviceInfo:
    def __init__(self, device_id, display_name="TestKbd"):
        self.handle       = 0
        self.device_id    = device_id
        self.display_name = display_name

_mock_kbd_device.KeyEvent             = _FakeKeyEvent
_mock_kbd_device.KeyboardDeviceInfo   = _FakeKeyboardDeviceInfo
_mock_kbd_device.RawInputThread       = MagicMock
_mock_kbd_device.enumerate_keyboards  = MagicMock(return_value=[])
_mock_kbd_device.scancode_to_name     = lambda sc, ext: f"Key0x{(sc | (0x100 if ext else 0)):03X}"
_mock_kbd_device.scancode_key         = lambda sc, ext: (sc | 0x100) if ext else sc
_mock_kbd_device.set_suppression_state = MagicMock()

sys.modules["keyboard_device"] = _mock_kbd_device

# Also mock PySide6.QtCore.QSettings for keyboard_mapping
_mock_pyside6 = types.ModuleType("PySide6")
_mock_qtcore  = types.ModuleType("PySide6.QtCore")
_mock_pyside6.QtCore = _mock_qtcore
_mock_qtcore.QSettings = MagicMock

sys.modules.setdefault("PySide6", _mock_pyside6)
sys.modules.setdefault("PySide6.QtCore", _mock_qtcore)

# Now import the module under test
sys.path.insert(0, ".")
from keyboard_mapping import (
    KeyboardMappingSettings,
    KeyboardButtonService,
    ServiceMode,
    LONG_PRESS_THRESHOLD,
    _ButtonTimer,
)

KeyEvent           = _FakeKeyEvent
KeyboardDeviceInfo = _FakeKeyboardDeviceInfo

# Convenience scan key constants
A_KEY    = 0x1E   # A (non-extended)
S_KEY    = 0x1F   # S
CTRL_KEY = 0x1D   # Left Ctrl
SPACE    = 0x39   # Space

DEV1 = "\\\\?\\HID#VID_046D&PID_0001#DEVICE1"
DEV2 = "\\\\?\\HID#VID_046D&PID_0002#DEVICE2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**kwargs) -> KeyboardMappingSettings:
    defaults = dict(
        enabled=True,
        device_id=DEV1,
        device_name="TestKbd",
        b1_keys=[],
        b1b2_keys=[],
        b2_keys=[],
    )
    defaults.update(kwargs)
    return KeyboardMappingSettings(**defaults)


def _make_service(settings=None) -> tuple[KeyboardButtonService, queue.Queue]:
    eq = queue.Queue()
    if settings is None:
        settings = _make_settings()
    svc = KeyboardButtonService(eq, settings)
    # Don't start the real Raw Input thread in tests
    return svc, eq


def _feed(svc: KeyboardButtonService, events: list, now: float = 0.0):
    """Feed a list of KeyEvent objects directly into the service's raw queue."""
    for ev in events:
        svc._raw_queue.put(ev)
    svc.tick(now)


def _drain(eq: queue.Queue) -> list[str]:
    out = []
    while not eq.empty():
        out.append(eq.get_nowait())
    return out


def _key_down(device_id, scan_key):
    return KeyEvent(device_id, scan_key, True)

def _key_up(device_id, scan_key):
    return KeyEvent(device_id, scan_key, False)


# ---------------------------------------------------------------------------
# Tests: KeyboardMappingSettings
# ---------------------------------------------------------------------------

class TestSettings(unittest.TestCase):

    def test_assign_key_removes_from_other_lists(self):
        """Assigning A to B2 when it's already in B1 removes it from B1."""
        s = _make_settings(b1_keys=[A_KEY], b2_keys=[])
        s.assign_key(A_KEY, "b2")
        self.assertNotIn(A_KEY, s.b1_keys, "A should be removed from B1")
        self.assertIn(A_KEY, s.b2_keys, "A should now be in B2")

    def test_assign_key_no_duplicate(self):
        """Adding the same key twice to a list results in only one entry."""
        s = _make_settings(b1_keys=[])
        s.assign_key(A_KEY, "b1")
        s.assign_key(A_KEY, "b1")
        self.assertEqual(s.b1_keys.count(A_KEY), 1)

    def test_validate_requires_b1(self):
        s = _make_settings(b1_keys=[], b2_keys=[S_KEY])
        errors = s.validate()
        self.assertTrue(any("B1" in e for e in errors))

    def test_validate_requires_b2(self):
        s = _make_settings(b1_keys=[A_KEY], b2_keys=[])
        errors = s.validate()
        self.assertTrue(any("B2" in e for e in errors))

    def test_validate_b1b2_optional(self):
        s = _make_settings(b1_keys=[A_KEY], b1b2_keys=[], b2_keys=[S_KEY])
        errors = s.validate()
        self.assertEqual(errors, [])

    def test_validate_valid(self):
        s = _make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY])
        self.assertEqual(s.validate(), [])

    def test_copy_is_independent(self):
        s = _make_settings(b1_keys=[A_KEY])
        c = s.copy()
        c.b1_keys.append(S_KEY)
        self.assertNotIn(S_KEY, s.b1_keys)

    def test_remove_key(self):
        s = _make_settings(b1_keys=[A_KEY, S_KEY])
        s.remove_key(A_KEY)
        self.assertNotIn(A_KEY, s.b1_keys)
        self.assertIn(S_KEY, s.b1_keys)

    def test_clear_list(self):
        s = _make_settings(b1_keys=[A_KEY, S_KEY])
        s.clear_list("b1")
        self.assertEqual(s.b1_keys, [])


# ---------------------------------------------------------------------------
# Tests: _ButtonTimer
# ---------------------------------------------------------------------------

class TestButtonTimer(unittest.TestCase):

    def test_no_long_before_threshold(self):
        t = _ButtonTimer()
        t.activate(0.0)
        self.assertFalse(t.check_long(0.799))

    def test_long_at_threshold(self):
        t = _ButtonTimer()
        t.activate(0.0)
        self.assertTrue(t.check_long(0.800))

    def test_long_after_threshold(self):
        t = _ButtonTimer()
        t.activate(0.0)
        self.assertTrue(t.check_long(0.801))

    def test_long_fires_only_once(self):
        t = _ButtonTimer()
        t.activate(0.0)
        t.check_long(0.800)
        # Second call should not fire again
        self.assertFalse(t.check_long(1.0))

    def test_short_on_release_before_long(self):
        t = _ButtonTimer()
        t.activate(0.0)
        t.check_long(0.5)
        self.assertTrue(t.short_on_release())

    def test_no_short_after_long(self):
        t = _ButtonTimer()
        t.activate(0.0)
        t.check_long(0.800)
        self.assertFalse(t.short_on_release())

    def test_deactivate_resets(self):
        t = _ButtonTimer()
        t.activate(0.0)
        t.check_long(0.800)
        t.deactivate()
        t.activate(2.0)
        self.assertFalse(t.check_long(2.5))   # 500ms < 800ms


# ---------------------------------------------------------------------------
# Tests: KeyboardButtonService — device isolation
# ---------------------------------------------------------------------------

class TestDeviceIsolation(unittest.TestCase):

    def test_primary_keyboard_events_ignored(self):
        """Events from DEV2 (not the selected aux device DEV1) produce nothing."""
        svc, eq = _make_service(_make_settings(device_id=DEV1, b1_keys=[A_KEY]))
        _feed(svc, [_key_down(DEV2, A_KEY), _key_up(DEV2, A_KEY)])
        self.assertEqual(_drain(eq), [])

    def test_aux_keyboard_events_processed(self):
        """Events from the selected aux device produce B1S on short press."""
        svc, eq = _make_service(_make_settings(device_id=DEV1, b1_keys=[A_KEY], b2_keys=[S_KEY]))
        now = 0.0
        _feed(svc, [_key_down(DEV1, A_KEY)], now)
        _feed(svc, [_key_up(DEV1, A_KEY)], now + 0.1)
        # tick to check long (nothing should fire yet)
        svc.tick(now + 0.1)
        events = _drain(eq)
        self.assertIn("B1S", events)


# ---------------------------------------------------------------------------
# Tests: KeyboardButtonService — OS key repeat suppression
# ---------------------------------------------------------------------------

class TestRepeatSuppression(unittest.TestCase):

    def test_repeat_produces_single_logical_press(self):
        """Multiple key-down events for the same key produce only one B1 activation."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY]))
        now = 0.0
        # Simulate: down, repeat×3, up
        for _ in range(4):
            _feed(svc, [_key_down(DEV1, A_KEY)], now)
            now += 0.05
        _feed(svc, [_key_up(DEV1, A_KEY)], now)

        events = _drain(eq)
        # Should get exactly one B1S (not B1L since held < 800ms total)
        self.assertEqual(events.count("B1S"), 1)
        self.assertEqual(events.count("B1L"), 0)


# ---------------------------------------------------------------------------
# Tests: KeyboardButtonService — multiple keys same button
# ---------------------------------------------------------------------------

class TestMultipleKeysSameButton(unittest.TestCase):

    def test_b1_stays_active_until_last_key_released(self):
        """A -> B1, Ctrl -> B1: releasing A while Ctrl is held keeps B1 active."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY, CTRL_KEY], b2_keys=[S_KEY]))
        now = 0.0

        _feed(svc, [_key_down(DEV1, A_KEY)], now)
        now += 0.05
        _feed(svc, [_key_down(DEV1, CTRL_KEY)], now)
        now += 0.05
        _feed(svc, [_key_up(DEV1, A_KEY)], now)    # A released; Ctrl still held
        now += 0.05

        # B1 should NOT have fired a short/long event yet (Ctrl still held)
        events = _drain(eq)
        self.assertNotIn("B1S", events)
        self.assertNotIn("B1L", events)

        # Now release Ctrl — B1 should fire short (total time < 800ms)
        _feed(svc, [_key_up(DEV1, CTRL_KEY)], now)
        events = _drain(eq)
        self.assertIn("B1S", events)

    def test_long_press_across_multiple_keys(self):
        """
        0ms    A down
        400ms  Ctrl down
        600ms  A up
        900ms  Ctrl up
        => B1 continuously held for 900ms => long press
        """
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY, CTRL_KEY], b2_keys=[S_KEY]))

        t0 = 0.0
        _feed(svc, [_key_down(DEV1, A_KEY)], t0)
        _feed(svc, [_key_down(DEV1, CTRL_KEY)], t0 + 0.4)
        _feed(svc, [_key_up(DEV1, A_KEY)], t0 + 0.6)

        # Tick at 800ms — long press should fire
        svc.tick(t0 + 0.8)
        events_at_800 = _drain(eq)

        _feed(svc, [_key_up(DEV1, CTRL_KEY)], t0 + 0.9)
        events_at_900 = _drain(eq)

        all_events = events_at_800 + events_at_900
        self.assertIn("B1L", all_events, "Expected B1L after 900ms continuous hold")
        self.assertNotIn("B1S", all_events, "Should not get B1S when long press fired")


# ---------------------------------------------------------------------------
# Tests: KeyboardButtonService — long-press boundary
# ---------------------------------------------------------------------------

class TestLongPressBoundary(unittest.TestCase):

    def _run_press(self, hold_duration: float):
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY]))
        t0 = 0.0
        _feed(svc, [_key_down(DEV1, A_KEY)], t0)
        svc.tick(t0 + hold_duration)   # check long
        _feed(svc, [_key_up(DEV1, A_KEY)], t0 + hold_duration + 0.001)
        return _drain(eq)

    def test_799ms_is_short(self):
        events = self._run_press(0.799)
        self.assertIn("B1S", events)
        self.assertNotIn("B1L", events)

    def test_800ms_is_long(self):
        events = self._run_press(0.800)
        self.assertIn("B1L", events)
        self.assertNotIn("B1S", events)

    def test_801ms_is_long(self):
        events = self._run_press(0.801)
        self.assertIn("B1L", events)
        self.assertNotIn("B1S", events)


# ---------------------------------------------------------------------------
# Tests: KeyboardButtonService — B1+B2 mapped key
# ---------------------------------------------------------------------------

class TestB1B2MappedKey(unittest.TestCase):

    def test_b1b2_key_short_press(self):
        """A mapped B1+B2 key produces BBS on short press."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b1b2_keys=[SPACE], b2_keys=[S_KEY]))
        t0 = 0.0
        _feed(svc, [_key_down(DEV1, SPACE)], t0)
        svc.tick(t0 + 0.1)
        _feed(svc, [_key_up(DEV1, SPACE)], t0 + 0.2)
        events = _drain(eq)
        self.assertIn("BBS", events)

    def test_b1b2_key_long_press(self):
        """A mapped B1+B2 key produces BBL after >=800ms hold."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b1b2_keys=[SPACE], b2_keys=[S_KEY]))
        t0 = 0.0
        _feed(svc, [_key_down(DEV1, SPACE)], t0)
        svc.tick(t0 + 0.8)
        events = _drain(eq)
        self.assertIn("BBL", events)

    def test_separate_b1_b2_keys_together(self):
        """Holding A (B1) + S (B2) simultaneously produces combo short event."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY]))
        t0 = 0.0
        _feed(svc, [_key_down(DEV1, A_KEY)], t0)
        _feed(svc, [_key_down(DEV1, S_KEY)], t0 + 0.05)
        svc.tick(t0 + 0.1)

        _feed(svc, [_key_up(DEV1, A_KEY)], t0 + 0.2)
        _feed(svc, [_key_up(DEV1, S_KEY)], t0 + 0.25)
        events = _drain(eq)
        self.assertIn("BBS", events, "A+S combo should produce BBS")


# ---------------------------------------------------------------------------
# Tests: KeyboardButtonService — disconnect/disable cleanup
# ---------------------------------------------------------------------------

class TestCleanup(unittest.TestCase):

    def test_release_kbd_contributions_releases_held_b1(self):
        """If A->B1 is held and release_keyboard_contributions() is called, B1 fires B1S."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY]))
        t0 = 0.0
        _feed(svc, [_key_down(DEV1, A_KEY)], t0)

        # Simulate disconnect — release all contributions
        svc.release_keyboard_contributions()

        events = _drain(eq)
        # B1 should have short-fired (held < 800ms at time of release call)
        self.assertIn("B1S", events)

    def test_disable_releases_contributions(self):
        """Disabling mapping while a key is held releases B1."""
        settings = _make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY])
        svc, eq = _make_service(settings)
        t0 = 0.0
        _feed(svc, [_key_down(DEV1, A_KEY)], t0)

        # Disable mapping via apply_settings
        new_settings = settings.copy()
        new_settings.enabled = False
        svc.apply_settings(new_settings)

        events = _drain(eq)
        self.assertIn("B1S", events)


# ---------------------------------------------------------------------------
# Tests: Save/Cancel semantics (settings copy)
# ---------------------------------------------------------------------------

class TestSaveCancelSemantics(unittest.TestCase):

    def test_copy_preserves_original(self):
        """Modifying a copy does not affect the original."""
        original = _make_settings(b1_keys=[A_KEY])
        temp = original.copy()
        temp.assign_key(A_KEY, "b2")
        # Original unchanged
        self.assertIn(A_KEY, original.b1_keys)
        self.assertNotIn(A_KEY, original.b2_keys)

    def test_cancel_leaves_service_unchanged(self):
        """Applying a new settings object only happens on save; cancel means no apply."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY]))
        original_b1 = list(svc.settings.b1_keys)

        # Simulate opening dialog: make temp copy, modify, but DON'T apply (cancel)
        temp = svc.settings.copy()
        temp.b1_keys = [S_KEY]
        # Not calling svc.apply_settings(temp) — that's what Cancel does

        self.assertEqual(svc.settings.b1_keys, original_b1)

    def test_save_applies_new_mapping(self):
        """Calling apply_settings updates the active mapping."""
        svc, eq = _make_service(_make_settings(b1_keys=[A_KEY], b2_keys=[S_KEY]))

        new_settings = svc.settings.copy()
        new_settings.b1_keys = [CTRL_KEY]
        new_settings.b2_keys = [S_KEY]
        svc.apply_settings(new_settings)

        # Old A key should no longer trigger B1
        _feed(svc, [_key_down(DEV1, A_KEY), _key_up(DEV1, A_KEY)], 0.0)
        events = _drain(eq)
        self.assertNotIn("B1S", events)

        # New Ctrl key should trigger B1
        _feed(svc, [_key_down(DEV1, CTRL_KEY)], 1.0)
        svc.tick(1.1)
        _feed(svc, [_key_up(DEV1, CTRL_KEY)], 1.1)
        events = _drain(eq)
        self.assertIn("B1S", events)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
