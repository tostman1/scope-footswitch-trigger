"""
keyboard_mapping_ui.py — Keyboard Mapping UI components
=========================================================

KeyboardMappingPanel (QWidget)
    The main settings panel shown when the user opens "Keyboard Mapping"
    from the Button Mapping menu.  Contains:
      - Enable checkbox
      - Selected keyboard display + status
      - [Identify Keyboard]  [Remove Keyboard]  [Configure Mapping…]
      - Countdown label during identification
      - Windows-key suppression experimental notice

KeyboardMappingDialog (QDialog)
    The "Configure Mapping…" dialog.
    Three columns:  B1 | B1+B2 | B2
    Capture fields accept key presses only from the selected aux keyboard.
    Implements the temporary-copy / Save / Cancel pattern.

_MappingField (QWidget)
    A single capture field: shows mapped keys, highlights when active,
    accepts key additions from the service in CONFIGURING mode.
"""

from __future__ import annotations

from typing import Optional, Callable

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget, QCheckBox, QScrollArea,
)

from keyboard_mapping import KeyboardMappingSettings, KeyboardButtonService, ServiceMode
from keyboard_device import enumerate_keyboards, KeyboardDeviceInfo, scancode_to_name


# ---------------------------------------------------------------------------
# Shared styling constants (match existing app palette)
# ---------------------------------------------------------------------------

_BTN_STYLE = "font-size: 10pt; padding: 4px 12px;"
_ACTIVE_FIELD_STYLE  = "background: #1a3a5c; color: #ffffff; border: 2px solid #5599dd;"
_DEFAULT_FIELD_STYLE = "background: palette(base); color: palette(text); border: 1px solid palette(mid);"
_KEY_ITEM_STYLE = "font-family: Consolas, monospace; font-size: 10pt;"


# ---------------------------------------------------------------------------
# _MappingField — one capture field (B1 / B1+B2 / B2)
# ---------------------------------------------------------------------------

class _MappingField(QWidget):
    """Displays a list of mapped scan keys and highlights when active.

    The field itself is not editable by normal typing — keys are added
    programmatically from the service callback while in CONFIGURING mode.
    """

    def __init__(self, label: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._active = False
        self._keys: list[int] = []      # canonical scan keys
        self._names: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.NoSelection)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setMinimumHeight(90)
        self._list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._list.setStyleSheet(_DEFAULT_FIELD_STYLE)
        layout.addWidget(self._list)

        self._label_widget = QLabel(label)
        self._label_widget.setAlignment(Qt.AlignCenter)
        layout.insertWidget(0, self._label_widget)

    def set_active(self, active: bool) -> None:
        self._active = active
        self._list.setStyleSheet(_ACTIVE_FIELD_STYLE if active else _DEFAULT_FIELD_STYLE)

    def load_keys(self, keys: list[int]) -> None:
        """Populate field from a list of canonical scan keys."""
        self._keys = list(keys)
        self._rebuild_list()

    def add_key(self, scan_key: int, name: str) -> bool:
        """Add a key if not already present.  Returns True if added."""
        if scan_key in self._keys:
            return False
        self._keys.append(scan_key)
        self._names[scan_key] = name
        self._rebuild_list()
        return True

    def remove_key(self, scan_key: int) -> None:
        if scan_key in self._keys:
            self._keys.remove(scan_key)
            self._rebuild_list()

    def clear_keys(self) -> None:
        self._keys.clear()
        self._rebuild_list()

    def get_keys(self) -> list[int]:
        return list(self._keys)

    def _rebuild_list(self) -> None:
        self._list.clear()
        for sk in self._keys:
            name = self._names.get(sk) or _key_display_name(sk)
            item = QListWidgetItem(name)
            item.setFont(QFont("Consolas", 10))
            self._list.addItem(item)


def _key_display_name(scan_key: int) -> str:
    extended = bool(scan_key & 0x100)
    raw_sc   = scan_key & 0xFF
    return scancode_to_name(raw_sc, extended)


# ---------------------------------------------------------------------------
# KeyboardMappingDialog — Configure Mapping…
# ---------------------------------------------------------------------------

class KeyboardMappingDialog(QDialog):
    """Three-column mapping editor.

    Works on a temporary copy of KeyboardMappingSettings so Cancel is always
    safe.  Calls back into the service's CONFIGURING mode for key capture.
    """

    def __init__(
        self,
        service:  KeyboardButtonService,
        settings: KeyboardMappingSettings,
        parent:   Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Configure Keyboard Mapping")
        self.setMinimumWidth(540)
        self.setModal(True)

        self._service  = service
        # Work on a temporary copy — only committed on Save
        self._temp     = settings.copy()
        self._result:  Optional[KeyboardMappingSettings] = None
        self._active_field: Optional[str] = None   # "b1" / "b1b2" / "b2"

        self._build_ui()
        self._load_from_temp()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(14, 12, 14, 12)

        # Instruction
        instr = QLabel("Select a field and press the keys that should be mapped.\n"
                        "Keys are captured only from the selected auxiliary keyboard.")
        instr.setWordWrap(True)
        instr.setAlignment(Qt.AlignCenter)
        outer.addWidget(instr)

        outer.addWidget(_hline())

        # Three-column grid
        col_grid = QGridLayout()
        col_grid.setSpacing(10)
        for c in range(3):
            col_grid.setColumnStretch(c, 1)

        headers = ["B1", "B1+B2", "B2"]
        targets = ["b1", "b1b2", "b2"]
        self._fields: dict[str, _MappingField] = {}
        self._select_btns: dict[str, QPushButton] = {}

        for col, (hdr, target) in enumerate(zip(headers, targets)):
            field = _MappingField(hdr)
            self._fields[target] = field
            col_grid.addWidget(field, 0, col)

            sel_btn = QPushButton(f"Capture {hdr}")
            sel_btn.setStyleSheet(_BTN_STYLE)
            sel_btn.setCheckable(True)
            sel_btn.clicked.connect(lambda checked, t=target: self._select_field(t))
            self._select_btns[target] = sel_btn
            col_grid.addWidget(sel_btn, 1, col)

            clr_btn = QPushButton(f"Clear {hdr}")
            clr_btn.setStyleSheet(_BTN_STYLE)
            clr_btn.clicked.connect(lambda checked, t=target: self._clear_field(t))
            col_grid.addWidget(clr_btn, 2, col)

        outer.addLayout(col_grid)
        outer.addWidget(_hline())

        # Dialog buttons
        btn_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._on_save)
        btn_box.rejected.connect(self._on_cancel)
        outer.addWidget(btn_box)

    def _load_from_temp(self) -> None:
        self._fields["b1"].load_keys(self._temp.b1_keys)
        self._fields["b1b2"].load_keys(self._temp.b1b2_keys)
        self._fields["b2"].load_keys(self._temp.b2_keys)

    # ------------------------------------------------------------------
    # Field selection / capture
    # ------------------------------------------------------------------

    def _select_field(self, target: str) -> None:
        # Deactivate previous
        if self._active_field and self._active_field != target:
            self._fields[self._active_field].set_active(False)
            self._select_btns[self._active_field].setChecked(False)

        btn = self._select_btns[target]
        if btn.isChecked():
            self._active_field = target
            self._fields[target].set_active(True)
            # Enter CONFIGURING mode in service
            self._service.start_configuration(target, self._on_key_captured)
        else:
            self._active_field = None
            self._fields[target].set_active(False)
            self._service.stop_configuration()

    def _on_key_captured(self, scan_key: int, name: str) -> None:
        """Called by service (via Qt main thread callback) when a key is pressed
        on the aux keyboard while in CONFIGURING mode."""
        if self._active_field is None:
            return
        target = self._active_field

        # Enforce exclusivity: remove from other fields first
        for other_target, field in self._fields.items():
            if other_target != target:
                field.remove_key(scan_key)
                # Keep _temp in sync
                self._temp.remove_key(scan_key)

        added = self._fields[target].add_key(scan_key, name)
        if added:
            self._temp.assign_key(scan_key, target)

    def _clear_field(self, target: str) -> None:
        self._fields[target].clear_keys()
        self._temp.clear_list(target)

    # ------------------------------------------------------------------
    # Save / Cancel
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        # Sync field state into temp
        self._temp.b1_keys   = self._fields["b1"].get_keys()
        self._temp.b1b2_keys = self._fields["b1b2"].get_keys()
        self._temp.b2_keys   = self._fields["b2"].get_keys()

        errors = self._temp.validate()
        if errors:
            QMessageBox.warning(self, "Validation Error", "\n".join(errors))
            return

        self._result = self._temp
        self._service.stop_configuration()
        self.accept()

    def _on_cancel(self) -> None:
        self._service.stop_configuration()
        self.reject()

    def closeEvent(self, event):
        self._service.stop_configuration()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Result accessor
    # ------------------------------------------------------------------

    def get_result(self) -> Optional[KeyboardMappingSettings]:
        """Returns updated settings if saved, None if cancelled."""
        return self._result


# ---------------------------------------------------------------------------
# KeyboardMappingPanel — the top-level settings widget
# ---------------------------------------------------------------------------

class KeyboardMappingPanel(QWidget):
    """Settings panel for keyboard mapping.

    Shown as the content of the "Keyboard Mapping" dialog opened from the
    Button Mapping menu.  Contains all controls needed to enable/disable
    keyboard mapping, identify a device, configure mappings, etc.

    The panel talks to KeyboardButtonService and persists changes via QSettings.
    """

    # Emitted when settings change (so MainWindow can persist and re-apply)
    settings_changed = Signal(KeyboardMappingSettings)

    def __init__(
        self,
        service:    KeyboardButtonService,
        get_qsettings: Callable,    # () -> QSettings
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._service       = service
        self._get_qs        = get_qsettings
        self._settings      = service.settings.copy()

        # Countdown tick timer for identification mode
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(50)   # driven by same 50ms cadence
        self._countdown_timer.timeout.connect(self._tick)

        self._build_ui()
        self._refresh_display()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 14, 16, 14)

        # ---- Enable checkbox ----
        self._enable_cb = QCheckBox("Use a keyboard as button input")
        self._enable_cb.toggled.connect(self._on_enable_toggled)
        layout.addWidget(self._enable_cb)

        layout.addWidget(_hline())

        # ---- Status group ----
        status_box = QGroupBox("Selected Keyboard")
        status_layout = QVBoxLayout(status_box)
        status_layout.setSpacing(6)

        self._device_name_lbl = QLabel("None")
        self._device_name_lbl.setWordWrap(True)
        status_layout.addWidget(self._device_name_lbl)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray;")
        status_layout.addWidget(self._status_lbl)

        # Button row: Identify / Remove / Configure
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._identify_btn = QPushButton("Identify Keyboard")
        self._identify_btn.setStyleSheet(_BTN_STYLE)
        self._identify_btn.clicked.connect(self._on_identify)
        btn_row.addWidget(self._identify_btn)

        self._remove_btn = QPushButton("Remove Keyboard")
        self._remove_btn.setStyleSheet(_BTN_STYLE)
        self._remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self._remove_btn)

        self._configure_btn = QPushButton("Configure Mapping…")
        self._configure_btn.setStyleSheet(_BTN_STYLE)
        self._configure_btn.clicked.connect(self._on_configure)
        btn_row.addWidget(self._configure_btn)

        status_layout.addLayout(btn_row)

        # Countdown label (hidden unless identifying)
        self._countdown_lbl = QLabel("")
        self._countdown_lbl.setAlignment(Qt.AlignCenter)
        self._countdown_lbl.setStyleSheet("font-size: 12pt; color: #e0a020;")
        self._countdown_lbl.hide()
        status_layout.addWidget(self._countdown_lbl)

        layout.addWidget(status_box)

        # ---- Windows key suppression notice ----
        win_box = QGroupBox("Windows Key Suppression  [Experimental]")
        win_layout = QVBoxLayout(win_box)
        notice = QLabel(
            "When keyboard mapping is active, Windows key presses on the selected "
            "auxiliary keyboard are suppressed via a low-level keyboard hook to "
            "prevent Start menu activation and Windows shortcuts.\n\n"
            "Limitation: On some Windows\u202010/11 configurations the shell may "
            "intercept the Windows key at a level below user-space hooks. "
            "In those cases the Start menu may still open despite this feature "
            "being active. This is an OS-level restriction that cannot be "
            "overcome without a kernel driver.\n\n"
            "The primary keyboard is not affected in any way."
        )
        notice.setWordWrap(True)
        notice.setStyleSheet("color: palette(text);")
        win_layout.addWidget(notice)
        layout.addWidget(win_box)

        layout.addStretch()

    # ------------------------------------------------------------------
    # Display refresh
    # ------------------------------------------------------------------

    def _refresh_display(self) -> None:
        s = self._settings
        self._enable_cb.blockSignals(True)
        self._enable_cb.setChecked(s.enabled)
        self._enable_cb.blockSignals(False)

        enabled = s.enabled
        has_device = bool(s.device_id)

        if not has_device:
            self._device_name_lbl.setText("Selected keyboard: None")
            self._status_lbl.setText("")
        else:
            self._device_name_lbl.setText(f"Selected keyboard:\n{s.device_name}")
            connected = self._service.is_device_connected()
            if connected:
                self._status_lbl.setText("Status: Connected")
                self._status_lbl.setStyleSheet("color: #1a9e7a;")
            else:
                self._status_lbl.setText("Status: Disconnected")
                self._status_lbl.setStyleSheet("color: #cc4444;")

        self._remove_btn.setEnabled(enabled and has_device)
        self._configure_btn.setEnabled(enabled and has_device)
        self._identify_btn.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Enable toggle
    # ------------------------------------------------------------------

    def _on_enable_toggled(self, checked: bool) -> None:
        self._settings.enabled = checked
        self._save_and_apply()
        self._refresh_display()

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    def _on_identify(self) -> None:
        if self._service.mode == ServiceMode.IDENTIFYING:
            # Cancel ongoing identification
            self._service.cancel_identification()
            self._countdown_timer.stop()
            self._countdown_lbl.hide()
            self._identify_btn.setText("Identify Keyboard")
            self._refresh_display()
            return

        self._identify_btn.setText("Cancel Identification")
        self._countdown_lbl.setText("Waiting for keyboard input… 10")
        self._countdown_lbl.show()
        self._countdown_timer.start()

        self._service.start_identification(
            on_result=self._on_identify_result,
            on_tick=self._on_identify_tick,
        )

    def _on_identify_tick(self, remaining: int) -> None:
        self._countdown_lbl.setText(f"Waiting for keyboard input… {remaining}")

    def _on_identify_result(self, device: Optional[KeyboardDeviceInfo]) -> None:
        self._countdown_timer.stop()
        self._countdown_lbl.hide()
        self._identify_btn.setText("Identify Keyboard")

        if device is None:
            # Timeout — leave previous selection unchanged
            self._countdown_lbl.setText("Keyboard identification timed out.")
            self._countdown_lbl.show()
            QTimer.singleShot(3000, lambda: self._countdown_lbl.hide())
            return

        # Success — update settings
        self._settings.device_id   = device.device_id
        self._settings.device_name = device.display_name
        self._save_and_apply()
        self._refresh_display()

    def _tick(self) -> None:
        """Drive the service tick (countdown + identification) from this panel's timer."""
        # The service is also ticked by MainWindow._tick every 50ms —
        # this timer is only for the countdown label update during identify.
        # Duplicate ticks are harmless (the service handles re-entrant calls).
        pass

    # ------------------------------------------------------------------
    # Remove keyboard
    # ------------------------------------------------------------------

    def _on_remove(self) -> None:
        self._service.release_keyboard_contributions()
        self._settings.device_id   = ""
        self._settings.device_name = ""
        self._save_and_apply()
        self._refresh_display()

    # ------------------------------------------------------------------
    # Configure mapping
    # ------------------------------------------------------------------

    def _on_configure(self) -> None:
        dlg = KeyboardMappingDialog(
            service=self._service,
            settings=self._settings,
            parent=self,
        )
        dlg.exec()
        result = dlg.get_result()
        if result is not None:
            # Preserve enabled/device fields; only update key lists
            result.enabled     = self._settings.enabled
            result.device_id   = self._settings.device_id
            result.device_name = self._settings.device_name
            self._settings = result
            self._save_and_apply()
            self._refresh_display()

    # ------------------------------------------------------------------
    # Persistence + apply
    # ------------------------------------------------------------------

    def _save_and_apply(self) -> None:
        qs = self._get_qs()
        self._settings.save_to_settings(qs)
        self._service.apply_settings(self._settings)
        self.settings_changed.emit(self._settings)

    def refresh_status(self) -> None:
        """Called from MainWindow._tick() to update connected/disconnected status."""
        self._refresh_display()


# ---------------------------------------------------------------------------
# KeyboardMappingWindow — standalone dialog wrapper
# ---------------------------------------------------------------------------

class KeyboardMappingWindow(QDialog):
    """A simple dialog wrapping the KeyboardMappingPanel, opened from the menu."""

    def __init__(
        self,
        service:       KeyboardButtonService,
        get_qsettings: Callable,
        parent:        Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Mapping")
        self.setMinimumWidth(500)
        self.setModal(False)   # non-modal so the main window stays usable

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._panel = KeyboardMappingPanel(service, get_qsettings, self)
        layout.addWidget(self._panel)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(_BTN_STYLE)
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        layout.setContentsMargins(0, 0, 8, 8)

    @property
    def panel(self) -> KeyboardMappingPanel:
        return self._panel


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f
