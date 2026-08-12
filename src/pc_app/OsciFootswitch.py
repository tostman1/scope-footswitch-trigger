import os
import sys
import socket
import threading
import queue
import subprocess
import serial
import serial.tools.list_ports
import time
import pyvisa
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QPixmap, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QLayout, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QComboBox, QCheckBox, QTextEdit, QSpinBox,
    QFileDialog, QSizePolicy, QGridLayout, QFrame, QGroupBox
)

# Oscilloscope Implementations
from scopes.base import BaseScope
from scopes.keysight import KeysightScope
from scopes.keysight7000 import Keysight7000Scope
from scopes.lecroy import LeCroyScope


# ----------------------------
# Version
# ----------------------------

APP_VERSION = "1.3"

def get_git_version():
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL
        ).decode().strip()

        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            stderr=subprocess.DEVNULL
        ).decode().strip()

        return f"{APP_VERSION} build {count} ({commit})"
    except Exception:
        return APP_VERSION

VERSION = get_git_version()

# ----------------------------
# Serial Reader Thread
# ----------------------------
class SerialReader(threading.Thread):
    def __init__(self, port, baudrate, event_queue):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.queue = event_queue
        self._running = True
        self.ser = None

    def run(self):
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
            while self._running:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    self.queue.put(line)
        except Exception as e:
            self.queue.put(f"ERROR:{e}")

    def stop(self):
        self._running = False
        if self.ser:
            try:
                self.ser.cancel_read()
            except Exception:
                pass
            self.ser.close()

# ============================================================
# Scope Controller (Factory + Delegation)
# ============================================================

class ScopeController:
    RECONNECT_INTERVAL = 10   # seconds between reconnect attempts
    RECONNECT_MAX_TRIES = 6   # give up after this many failed reconnects (~1 min)

    def __init__(self, ui_queue: queue.Queue):
        """All communication back to the UI goes through ui_queue so that
        background threads never touch Qt widgets directly.

        Message tuples posted to ui_queue:
          ("scope_log",        str)          — log message
          ("scope_disconnected", None)       — connection lost
          ("scope_reconnected",  str)        — reconnected, IDN string
        """
        self.rm = pyvisa.ResourceManager("@py")
        self.scope = None
        self.device: BaseScope | None = None
        self._ui_queue = ui_queue

        self._last_ip = ""          # remembered for auto-reconnect
        self._reconnecting = False  # True while reconnect attempts are running
        self._stop_reconnect = False  # set by disconnect() to abort reconnect loop

        # schützt VISA Zugriff gegen parallele Threads
        self.lock = threading.Lock()

        self.keep_alive_running = True
        self.keep_alive_thread = threading.Thread(
            target=self._keep_alive_loop,
            daemon=True
        )
        self.keep_alive_thread.start()

    def _log(self, msg: str):
        """Thread-safe logging: post to UI queue instead of calling Qt directly."""
        self._ui_queue.put(("scope_log", msg))

    def connect(self, ip, timeout_ms: int = 5000):
        self._last_ip = ip
        with self.lock:
            # Always create a fresh ResourceManager for each connection attempt.
            try:
                self.rm.close()
            except Exception:
                pass
            self.rm = pyvisa.ResourceManager("@py")
            self.scope = self.rm.open_resource(f"TCPIP0::{ip}::INSTR")
            self.scope.timeout = timeout_ms

            idn = self.scope.query("*IDN?")
            idn_u = idn.upper()

            username = os.getenv("USERNAME") or os.getenv("USER") or "Unknown"

            if "LECROY" in idn_u:
                self.device = LeCroyScope(self.scope, self._log, username)
                self._log("Detected LeCroy oscilloscope")

            elif "KEYSIGHT" in idn_u or "AGILENT" in idn_u:
                if "MSO70" in idn_u or "DSO70" in idn_u:
                    self.device = Keysight7000Scope(self.scope, self._log, username)
                    self._log("Detected Keysight/Agilent 7000 oscilloscope")
                else:
                    self.device = KeysightScope(self.scope, self._log, username)
                    self._log("Detected Keysight/Agilent oscilloscope")
            else:
                self.device = KeysightScope(self.scope, self._log, username)
                self._log("Unknown oscilloscope. Using Keysight/Agilent commands as default.")

            return idn

    # ---------- Keep Alive ----------

    def _keep_alive_loop(self):
        while self.keep_alive_running:
            time.sleep(self.RECONNECT_INTERVAL)
            # _reconnecting is only written by this thread or disconnect() on the
            # main thread. Python's GIL makes the boolean read safe here.
            if self.scope is None or self._reconnecting:
                continue
            try:
                with self.lock:
                    self.scope.query("*IDN?")
            except Exception as e:
                self._log(f"Scope connection lost: {e}")
                self._disconnect(notify=False)
                if self._last_ip:
                    self._auto_reconnect()
                else:
                    self._ui_queue.put(("scope_disconnected", None))

    def _disconnect(self, notify=True):
        """Close the scope resource and clear state.
        Must NOT be called while self.lock is held (lock is not reentrant)."""
        with self.lock:
            try:
                if self.scope:
                    self.scope.close()
            except Exception:
                pass
            self.scope = None
            self.device = None

        self._log("Scope disconnected")
        if notify:
            self._ui_queue.put(("scope_disconnected", None))

    def disconnect(self):
        """Manual disconnect — clears last IP so auto-reconnect does not trigger."""
        self._last_ip = ""
        self._stop_reconnect = True   # abort any running reconnect loop
        self._disconnect()

    def shutdown(self):
        """Stop background threads cleanly. Call from closeEvent.
        Sets flags first so any in-progress reconnect worker exits at its next
        checkpoint, then closes the VISA resource without holding the lock
        (to avoid deadlocking if connect() is mid-execution in the reconnect worker)."""
        self.keep_alive_running = False
        self._stop_reconnect = True
        self._last_ip = ""
        # Give the reconnect worker one iteration to notice the stop flag.
        # We do NOT acquire self.lock here to avoid deadlock if connect() is
        # currently holding it on the reconnect thread.
        try:
            if self.scope:
                self.scope.close()
        except Exception:
            pass
        self.scope = None
        self.device = None

    def _auto_reconnect(self):
        """Try to reconnect to the last known IP in the background.
        Attempts RECONNECT_MAX_TRIES times with RECONNECT_INTERVAL seconds between.
        Posts scope_disconnected or scope_reconnected to ui_queue when done."""
        self._reconnecting = True
        self._stop_reconnect = False
        # Notify UI immediately that we are disconnected and reconnecting
        self._ui_queue.put(("scope_disconnected", None))

        def worker():
            for attempt in range(1, self.RECONNECT_MAX_TRIES + 1):
                if not self.keep_alive_running or self._stop_reconnect:
                    break
                self._log(f"Reconnect attempt {attempt}/{self.RECONNECT_MAX_TRIES}…")
                time.sleep(self.RECONNECT_INTERVAL)
                if not self.keep_alive_running or self._stop_reconnect:
                    break
                try:
                    idn = self.connect(self._last_ip)
                    self._log(f"Reconnected: {idn.strip()}")
                    self._reconnecting = False
                    self._ui_queue.put(("scope_reconnected", idn.strip()))
                    return
                except Exception as e:
                    self._log(f"Reconnect failed: {e}")

            # All attempts exhausted or aborted
            self._reconnecting = False
            if not self._stop_reconnect:
                self._log("Auto-reconnect gave up. Please reconnect manually.")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Device Check ----------

    def _require_device(self) -> bool:
        if not self.device:
            self._log("Scope not connected")
            return False
        return True

    @property
    def is_connected(self) -> bool:
        return self.device is not None

    # ---------- Delegation ----------

    def identify(self, enable):
        if not self._require_device():
            return
        with self.lock:
            self.device.identify(enable)

    def run(self):
        if not self._require_device():
            return
        with self.lock:
            self.device.run()

    def stop(self):
        if not self._require_device():
            return
        with self.lock:
            self.device.stop()

    def single(self):
        if not self._require_device():
            return
        with self.lock:
            self.device.single()

    def trigger_auto(self):
        if not self._require_device():
            return
        with self.lock:
            self.device.trigger_auto()

    def trigger_force(self):
        if not self._require_device():
            return
        with self.lock:
            self.device.trigger_force()

    def trigger_normal(self):
        if self._require_device():
            with self.lock:
                self.device.trigger_normal()

    def is_running(self):
        if not self._require_device():
            return False
        with self.lock:
            return self.device.is_running()

    def get_screenshot_png(self, color, inverted):
        if not self._require_device():
            return None
        with self.lock:
            return self.device.get_screenshot_png(color, inverted)

    def get_setup(self) -> bytes:
        if not self._require_device():
            return b""
        with self.lock:
            return self.device.get_setup()

    def write_setup_data(self, data: bytes) -> bool:
        if not self._require_device():
            return False
        with self.lock:
            return self.device.write_setup_data(data)

# ----------------------------
# Serial ports dropdown
# ----------------------------
class SerialPortComboBox(QComboBox):
    def __init__(self, refresh_callback, parent=None):
        super().__init__(parent)
        self.refresh_callback = refresh_callback

    def showPopup(self):
        self.refresh_callback()
        super().showPopup()


# ----------------------------
# path finder for assets (icon)
# ----------------------------
def resource_path(relative):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.abspath("."), relative)

# ----------------------------
# Main GUI
# ----------------------------

LOG_MAX_LINES = 500

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon(resource_path("icon.ico")))
        self.setWindowTitle(f"Footswitch Oscilloscope Controller  v{VERSION}")

        self.settings = QSettings("grafmar", "OsciFootswitch")

        self.event_queue = queue.Queue()
        self.result_queue = queue.Queue()   # worker thread results -> UI
        self.serial_thread = None
        self._busy = False                  # True while a background operation runs
        self._serial_open = False           # True while serial port is open
        self._waiting_for_single = False    # True after B2S/B2L until scope stops
        self._single_poll_counter = 0       # counts 50ms ticks between polls
        self._single_poll_total = 0         # total ticks since SINGLE — for timeout
        self._single_poll_active = False    # True while a poll worker is in flight

        # ScopeController posts all messages to result_queue so background
        # threads never touch Qt widgets directly (thread safety).
        self.scope = ScopeController(ui_queue=self.result_queue)

        self.init_ui()
        self._load_settings()
        self.refresh_serial_ports()
        self._restore_serial_port()   # must run after refresh_serial_ports()

        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    # ---------- Settings ----------

    def _load_settings(self):
        ip = self.settings.value("scope_ip", "10.53.48.1")
        self.ip_combo.setCurrentText(ip)

        # Serial port is restored in _restore_serial_port(), called after
        # refresh_serial_ports() so the combo is already populated.
        self._saved_serial_port = self.settings.value("serial_port", "")

        self._last_save_dir = self.settings.value("save_dir", "")

        auto = self.settings.value("auto_preview", False, type=bool)
        self.auto_preview_cb.setChecked(auto)

        timeout = self.settings.value("scpi_timeout", 5000, type=int)
        self.scpi_timeout_spin.setValue(timeout)

        # Restore window geometry
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _restore_serial_port(self):
        """Restore last-used serial port after refresh_serial_ports() populates the combo."""
        port = getattr(self, "_saved_serial_port", "")
        if port:
            idx = self.serial_combo.findData(port)
            if idx >= 0:
                self.serial_combo.setCurrentIndex(idx)

    def _save_settings(self):
        self.settings.setValue("scope_ip", self._get_ip())
        self.settings.setValue("serial_port", self.serial_combo.currentData() or "")
        self.settings.setValue("save_dir", self._last_save_dir)
        self.settings.setValue("auto_preview", self.auto_preview_cb.isChecked())
        self.settings.setValue("scpi_timeout", self.scpi_timeout_spin.value())
        self.settings.setValue("window_geometry", self.saveGeometry())

    # ---------- UI Init ----------

    def init_ui(self):
        # Outer layout: centers all content horizontally when window is wider
        outer = QVBoxLayout()
        outer.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        # Inner widget holds the actual content at a natural width
        inner = QWidget()
        inner.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ---- Config row: left = connection grid, right = settings panel ----
        config_row = QHBoxLayout()
        config_row.setSpacing(16)
        config_row.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        # -- Left: connection grid --
        config_layout = QGridLayout()
        config_layout.setHorizontalSpacing(10)
        config_layout.setVerticalSpacing(6)
        # Column 1 (ip_combo / serial_combo) stretches with the window;
        # all other columns stay at their natural/fixed width.
        config_layout.setColumnStretch(1, 1)

        label_width = 110
        scope_label = QLabel("Scope IP:")
        scope_label.setFixedWidth(label_width)
        serial_label = QLabel("Footswitch Port:")
        serial_label.setFixedWidth(label_width)

        # Single editable combo: acts as both IP text entry and scan results dropdown.
        # Typing sets the IP directly; scan populates entries; Connect uses current text.
        self.ip_combo = QComboBox()
        self.ip_combo.setEditable(True)
        self.ip_combo.setMinimumWidth(220)
        self.ip_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.ip_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.ip_combo.lineEdit().setPlaceholderText("e.g. 192.168.1.10")
        self.ip_combo.setInsertPolicy(QComboBox.NoInsert)
        self.ip_combo.setToolTip(
            "Type an IP address or click Scan to discover scopes.\n"
            "Hover over a scanned entry to see the full device ID."
        )

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setFixedWidth(55)
        self.scan_btn.setToolTip("Scan the subnet for VISA/SCPI instruments")
        self.scan_btn.clicked.connect(self.scan_for_scopes)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_scope_connection)
        self.connect_btn.setFixedWidth(100)

        self.identify_cb = QCheckBox("Identify")
        self.identify_cb.toggled.connect(self.identify_scope)

        self.serial_combo = SerialPortComboBox(self.refresh_serial_ports)
        self.serial_combo.setMinimumWidth(180)
        self.serial_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.scan_serial_btn = QPushButton("Scan")
        self.scan_serial_btn.setFixedWidth(55)
        self.scan_serial_btn.setToolTip(
            "Scan COM ports and auto-select the footswitch\n"
            "(looks for Arduino Nano / CH340 chip)"
        )
        self.scan_serial_btn.clicked.connect(self.scan_serial_ports)

        self.open_serial_btn = QPushButton("Open")
        self.open_serial_btn.clicked.connect(self.toggle_serial)
        self.open_serial_btn.setFixedWidth(100)

        # Row 0: label | ip_combo | scan_btn | connect_btn | identify
        config_layout.addWidget(scope_label,            0, 0)
        config_layout.addWidget(self.ip_combo,          0, 1)
        config_layout.addWidget(self.scan_btn,          0, 2)
        config_layout.addWidget(self.connect_btn,       0, 3)
        config_layout.addWidget(self.identify_cb,       0, 4)
        # Row 1: label | serial_combo | scan_serial_btn | open_btn
        config_layout.addWidget(serial_label,           1, 0)
        config_layout.addWidget(self.serial_combo,      1, 1)
        config_layout.addWidget(self.scan_serial_btn,   1, 2)
        config_layout.addWidget(self.open_serial_btn,   1, 3)

        config_row.addLayout(config_layout)

        # -- Vertical divider --
        vline = QFrame()
        vline.setFrameShape(QFrame.VLine)
        vline.setFrameShadow(QFrame.Sunken)
        config_row.addWidget(vline)

        # -- Right: settings panel (QGroupBox for future extensibility) --
        settings_box = QGroupBox("Settings")
        settings_layout = QVBoxLayout(settings_box)
        settings_layout.setSpacing(4)
        settings_layout.setContentsMargins(8, 6, 8, 6)
        settings_layout.setAlignment(Qt.AlignTop)

        self.auto_preview_cb = QCheckBox("Auto-preview on trigger")
        self.auto_preview_cb.setChecked(False)
        self.auto_preview_cb.setToolTip(
            "Automatically fetch and display a preview image\n"
            "when a SINGLE acquisition completes (B2S / B2L)."
        )
        settings_layout.addWidget(self.auto_preview_cb)

        # SCPI timeout
        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(6)
        timeout_lbl = QLabel("SCPI timeout (ms):")
        self.scpi_timeout_spin = QSpinBox()
        self.scpi_timeout_spin.setRange(1000, 30000)
        self.scpi_timeout_spin.setSingleStep(500)
        self.scpi_timeout_spin.setValue(5000)
        self.scpi_timeout_spin.setToolTip(
            "Timeout for VISA/SCPI queries.\n"
            "Increase if the scope is slow to respond."
        )
        timeout_row.addWidget(timeout_lbl)
        timeout_row.addWidget(self.scpi_timeout_spin)
        settings_layout.addLayout(timeout_row)

        # Future settings go here — add more QCheckBox / QWidget rows above this comment

        config_row.addWidget(settings_box)
        config_row.addStretch()

        layout.addLayout(config_row)

        # ---- Separator ----
        layout.addWidget(self._make_separator())

        # ---- Footswitch Function Buttons ----
        # Grid layout: col 0 = row labels, cols 1-3 = B1/Both/B2 buttons
        # Row 0 = column headers, rows 1-2 = Short/Long
        layout.addWidget(QLabel("Footswitch Functions:"))

        _FS_BTN = """
            QPushButton {{
                background-color: {bg};
                color: white;
                font-size: 10pt;
                font-weight: bold;
                border-radius: 6px;
                padding: 6px 4px;
                text-align: center;
            }}
            QPushButton:hover   {{ background-color: {hover}; }}
            QPushButton:pressed {{ background-color: {press}; }}
            QPushButton:disabled {{ background-color: #888; }}
        """
        # (bg, hover, press) per button column (cols 1-3)
        _COL_COLORS = [
            ("#2a7bbf", "#3a8ecf", "#1d5f9a"),   # B1   — blue
            ("#7b4bbf", "#8e5ecf", "#5f329a"),   # Both — purple
            ("#1a9e7a", "#2ab88e", "#147a5e"),   # B2   — teal
        ]

        _LABEL_STYLE = "font-weight: bold; font-size: 10pt;"

        fs_grid = QGridLayout()
        fs_grid.setSpacing(6)
        # Give button columns equal stretch, keep label col 0 tight
        fs_grid.setColumnStretch(0, 0)
        for c in range(1, 4):
            fs_grid.setColumnStretch(c, 1)

        # Row 0: empty corner + column headers
        for btn_col, hdr in enumerate(["B1  (Left)", "B1+B2  (Both)", "B2  (Right)"]):
            lbl = QLabel(hdr)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(_LABEL_STYLE)
            fs_grid.addWidget(lbl, 0, btn_col + 1)

        # Row labels in col 0, same font as column headers
        for grid_row, label in [(1, "Short"), (2, "Long")]:
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            lbl.setStyleSheet(_LABEL_STYLE)
            fs_grid.addWidget(lbl, grid_row, 0)

        # Button definitions: (grid_row, btn_col 0-2, event, text)
        _BTN_DEFS = [
            (1, 0, "B1S", "RUN / STOP"),
            (2, 0, "B1L", "RUN + Trigger AUTO"),
            (1, 1, "BBS", "Preview"),
            (2, 1, "BBL", "Save PNG + Setup"),
            (1, 2, "B2S", "SINGLE, Trigger NORMAL"),
            (2, 2, "B2L", "SINGLE, Force TRIGGER"),
        ]

        self._fs_buttons = {}
        for grid_row, btn_col, event, text in _BTN_DEFS:
            bg, hover, press = _COL_COLORS[btn_col]
            btn = QPushButton(text)
            btn.setMinimumHeight(44)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setStyleSheet(_FS_BTN.format(bg=bg, hover=hover, press=press))
            btn.clicked.connect(lambda checked=False, e=event: self.footswitch_btn_clicked(e))
            fs_grid.addWidget(btn, grid_row, btn_col + 1)
            self._fs_buttons[event] = btn

        layout.addLayout(fs_grid)

        # ---- Separator ----
        layout.addWidget(self._make_separator())

        # ---- Screenshot Controls ----
        # Only font/padding — no color override so Qt native hover/press states
        # and dark mode work correctly.
        btn_style = "font-size: 14pt; font-weight: bold; padding: 5px 20px;"
        shot_layout = QHBoxLayout()

        self.preview_btn = QPushButton("Preview")
        self.preview_btn.clicked.connect(self.preview_screenshot)
        self.preview_btn.setMinimumHeight(50)
        self.preview_btn.setStyleSheet(btn_style)
        shot_layout.addWidget(self.preview_btn)

        self.save_btn = QPushButton("Save PNG + Setup")
        self.save_btn.clicked.connect(self.save_screenshot_and_setup)
        self.save_btn.setMinimumHeight(50)
        self.save_btn.setStyleSheet(btn_style)
        shot_layout.addWidget(self.save_btn)

        self.load_setup_btn = QPushButton("Load Setup")
        self.load_setup_btn.clicked.connect(self.load_setup)
        self.load_setup_btn.setMinimumHeight(50)
        self.load_setup_btn.setStyleSheet(btn_style)
        shot_layout.addWidget(self.load_setup_btn)

        shot_layout.addStretch()

        cb_layout = QHBoxLayout()
        cb_layout.setSpacing(5)
        self.color_cb = QCheckBox("Color")
        self.color_cb.setChecked(True)
        cb_layout.addWidget(self.color_cb)
        self.invert_cb = QCheckBox("Inverted")
        cb_layout.addWidget(self.invert_cb)
        shot_layout.addLayout(cb_layout)

        layout.addLayout(shot_layout)

        # ---- Preview image — scales with window ----
        self._current_pixmap = None     # store full-res pixmap for rescaling
        self._current_png_data = None   # store raw PNG bytes for saving preview
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(320, 240)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet("border: 1px solid gray; background-color: gray;")
        layout.addWidget(self.image_label)

        # ---- Capture info row (save preview button left + timestamp) ----
        capture_row = QHBoxLayout()
        capture_row.setSpacing(10)

        # Save Preview: left-aligned, ~0.5x height of the main buttons (50px → 26px)
        self.save_preview_btn = QPushButton("Save Preview")
        self.save_preview_btn.setMinimumHeight(26)
        self.save_preview_btn.setStyleSheet("font-size: 10pt; padding: 2px 14px;")
        self.save_preview_btn.setEnabled(False)   # disabled until a preview exists
        self.save_preview_btn.clicked.connect(self.save_preview_image)
        capture_row.addWidget(self.save_preview_btn)

        self.capture_time_label = QLabel("")
        self.capture_time_label.setStyleSheet("font-size: 11pt;")
        capture_row.addWidget(self.capture_time_label)

        capture_row.addStretch()
        layout.addLayout(capture_row)

        # ---- Log header ----
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Log:"))
        log_header.addStretch()
        clear_btn = QPushButton("Clear Log")
        clear_btn.setFixedHeight(22)
        clear_btn.clicked.connect(self._clear_log)
        log_header.addWidget(clear_btn)
        layout.addLayout(log_header)

        # ---- Log ----
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(6 * 20)
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.log)

        outer.addWidget(inner)
        self.setLayout(outer)

    @staticmethod
    def _make_separator():
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        return sep

    # ---------- Helpers ----------

    def refresh_serial_ports(self):
        current = self.serial_combo.currentData()
        self.serial_combo.clear()
        for port in serial.tools.list_ports.comports():
            self.serial_combo.addItem(
                f"{port.device} - {port.description}", port.device
            )
        # restore previous selection if still present
        if current:
            idx = self.serial_combo.findData(current)
            if idx >= 0:
                self.serial_combo.setCurrentIndex(idx)

    # ---------- COM Port Scan ----------

    # Arduino Nano CH340 USB VID/PID — used for exact footswitch identification
    _FOOTSWITCH_VID = 0x1A86   # QinHeng Electronics (CH340)
    _FOOTSWITCH_PID = 0x7523   # CH340 serial converter

    def scan_serial_ports(self):
        """Refresh the COM port list and auto-select the most likely footswitch port.

        Detection priority:
          1. Exact VID 0x1A86 + PID 0x7523  (Arduino Nano CH340 — definitive match)
          2. VID 0x1A86 only                 (any CH340 device)
          3. Description contains 'CH340'    (string fallback)
          4. Description contains 'Arduino'  (broader fallback)
          5. No match — logs a message, leaves selection unchanged
        """
        self.refresh_serial_ports()
        ports = list(serial.tools.list_ports.comports())

        if not ports:
            self.log_msg("COM Scan: no serial ports found")
            return

        # Log all found ports
        for p in ports:
            self.log_msg(f"COM Scan: {p.device} - {p.description}"
                         + (f" [VID:{p.vid:04X} PID:{p.pid:04X}]"
                            if p.vid is not None else ""))

        best = None
        confidence = ""

        for p in ports:
            if p.vid == self._FOOTSWITCH_VID and p.pid == self._FOOTSWITCH_PID:
                best = p
                confidence = "exact match (CH340 Arduino Nano)"
                break   # highest confidence — stop searching
            if best is None and p.vid == self._FOOTSWITCH_VID:
                best = p
                confidence = "VID match (CH340 device)"
            if best is None and p.description and "CH340" in p.description.upper():
                best = p
                confidence = "description match (CH340)"
            if best is None and p.description and "ARDUINO" in p.description.upper():
                best = p
                confidence = "description match (Arduino)"

        if best:
            idx = self.serial_combo.findData(best.device)
            if idx >= 0:
                self.serial_combo.setCurrentIndex(idx)
            self.log_msg(f"COM Scan: selected {best.device} — {confidence}")
        else:
            self.log_msg("COM Scan: no footswitch port detected automatically")

    # ---------- DSO Discovery ----------

    def scan_for_scopes(self):
        """Probe every host in the same /24 subnet as the currently entered IP
        in parallel.  For each host that responds to a VISA *IDN? query, add it
        to the ip_combo dropdown.  Runs entirely in a background thread."""
        current_ip = self._get_ip()

        # Derive subnet from the current IP (use first three octets).
        # Fall back to a broad default if the IP is not yet set.
        parts = current_ip.split(".")
        if (len(parts) == 4
                and all(p.isdigit() for p in parts)
                and all(0 <= int(p) <= 255 for p in parts)):
            subnet = ".".join(parts[:3])
        else:
            self.log_msg("Enter a valid IP address first (e.g. 10.53.48.1)")
            return

        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("…")
        self.log_msg(f"Scanning {subnet}.1 – {subnet}.254 for SCPI instruments…")

        def probe_port(ip, port, candidates, lock):
            """Phase 1: raw TCP connect to check if the port is open.
            Does not touch the VISA ResourceManager at all."""
            try:
                with socket.create_connection((ip, port), timeout=0.5):
                    with lock:
                        candidates.add(ip)
            except Exception:
                pass

        def query_idn(ip, results, lock, rm):
            """Phase 2: query *IDN? on a confirmed host using the shared scan RM."""
            try:
                inst = rm.open_resource(f"TCPIP0::{ip}::INSTR")
                inst.timeout = 2000
                idn = inst.query("*IDN?").strip()
                inst.close()
                idn_parts = idn.split(",")
                if len(idn_parts) >= 2:
                    short = f"{ip}  —  {idn_parts[0].strip()} {idn_parts[1].strip()}"
                else:
                    short = f"{ip}  —  {idn}"
                with lock:
                    results.append((ip, short, idn))
            except Exception:
                pass

        def worker():
            found = []
            lock = threading.Lock()
            candidates = set()

            try:
                # Phase 1: bounded thread pool sweep (max 50 concurrent workers).
                # 254 hosts × 2 ports = 508 tasks. Per-socket timeout is 0.5s,
                # so worst-case phase 1 takes ~ceil(508/50) × 0.5 ≈ 6 seconds.
                # The pool's __exit__ waits for all submitted tasks to complete —
                # no manual timeout needed here.
                host_port_pairs = [
                    (f"{subnet}.{i}", port)
                    for i in range(1, 255)
                    for port in (111, 5025)
                ]
                with ThreadPoolExecutor(max_workers=50) as pool:
                    for ip, port in host_port_pairs:
                        pool.submit(probe_port, ip, port, candidates, lock)
                # pool.__exit__ blocks here until all tasks finish

                if not candidates:
                    self.result_queue.put(("scan_done", []))
                    return

                # Phase 2: query *IDN? only on confirmed hosts using one shared scan RM.
                # A separate RM from ScopeController.rm ensures scan activity cannot
                # corrupt the connection state of an active scope session.
                self.result_queue.put((
                    "scope_log",
                    f"Port scan complete — {len(candidates)} host(s) responded. "
                    f"Querying *IDN?…"
                ))
                scan_rm = pyvisa.ResourceManager("@py")
                for ip in sorted(candidates, key=lambda x: int(x.split(".")[-1])):
                    query_idn(ip, found, lock, scan_rm)

                found.sort(key=lambda x: int(x[0].split(".")[-1]))
                self.result_queue.put(("scan_done", found))

            except Exception as e:
                self.result_queue.put(("scan_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy: bool):
        self._busy = busy
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
        # disable/enable screenshot and footswitch buttons while an operation runs
        for btn in (self.preview_btn, self.save_btn, self.load_setup_btn):
            btn.setEnabled(not busy)
        for btn in self._fs_buttons.values():
            btn.setEnabled(not busy)

    # ---------- Scope connection ----------

    def _get_ip(self) -> str:
        """Extract just the IP address from the combo field.
        The field may contain a full scan label like '10.53.48.1  —  Agilent MSO7054A'
        — we always want only the first whitespace-delimited token."""
        text = self.ip_combo.currentText().strip()
        return text.split()[0] if text else ""

    def toggle_scope_connection(self):
        if self.scope.is_connected:
            self.scope.disconnect()
            # scope_disconnected message will be posted to result_queue by ScopeController
        else:
            self._connect_scope()

    @staticmethod
    def _is_valid_ip(ip: str) -> bool:
        parts = ip.split(".")
        return (len(parts) == 4
                and all(p.isdigit() for p in parts)
                and all(0 <= int(p) <= 255 for p in parts))

    def _connect_scope(self):
        ip = self._get_ip()
        if not ip:
            self.log_msg("No IP address entered")
            return
        if not self._is_valid_ip(ip):
            self.log_msg(f"Invalid IP address: '{ip}'  (expected format: 192.168.1.10)")
            return
        try:
            idn = self.scope.connect(ip, timeout_ms=self.scpi_timeout_spin.value())
            self.log_msg(f"Connected to scope: {idn.strip()}")
            self._set_scope_connected(True)
            # Flash identify message on the scope screen for 5 seconds,
            # regardless of the checkbox state.
            self._flash_identify()
        except Exception as e:
            self.log_msg(f"Connection failed: {e}")
            self._set_scope_connected(False)

    def _flash_identify(self):
        """Show the identify message on scope immediately after connect.
        After 5 seconds, clear it — unless the Identify checkbox is still checked,
        in which case leave it on (checkbox controls persistent display).
        All SCPI calls run in background threads. The checkbox state is captured
        on the main thread before spawning to avoid Qt access from bg threads."""
        threading.Thread(
            target=lambda: self._try_identify(True), daemon=True
        ).start()

        def _schedule_clear():
            # Capture checkbox state on main thread here, before the lambda runs
            keep_on = self.identify_cb.isChecked()
            if not keep_on:
                threading.Thread(
                    target=lambda: self._try_identify(False), daemon=True
                ).start()

        QTimer.singleShot(5000, _schedule_clear)

    def _try_identify(self, enable: bool):
        """Send identify command from a background thread."""
        try:
            self.scope.identify(enable)
        except Exception:
            pass

    # Stylesheet for a button in "active/connected" state.
    # Includes hover and pressed pseudo-states so it behaves like a normal button,
    # and uses explicit color: white so text is readable in both light and dark mode.
    _CONNECTED_BTN_STYLE = """
        QPushButton           { background-color: #3a9e3a; color: white; font-weight: bold; }
        QPushButton:hover     { background-color: #4cbb4c; color: white; }
        QPushButton:pressed   { background-color: #2d7d2d; color: white; }
    """

    def _set_scope_connected(self, connected: bool):
        if connected:
            self.connect_btn.setText("Disconnect")
            self.connect_btn.setStyleSheet(self._CONNECTED_BTN_STYLE)
            self.ip_combo.setEnabled(False)
            self.scan_btn.setEnabled(False)
        else:
            self.connect_btn.setText("Connect")
            self.connect_btn.setStyleSheet("")
            self.ip_combo.setEnabled(True)
            self.scan_btn.setEnabled(True)
            self.identify_cb.setChecked(False)

    def identify_scope(self, checked: bool):
        try:
            self.scope.identify(checked)
        except Exception as e:
            self.log_msg(str(e))

    # ---------- Serial ----------

    def toggle_serial(self):
        if self._serial_open:
            self._close_serial()
        else:
            self._open_serial()

    def _open_serial(self):
        port = self.serial_combo.currentData()
        if not port:
            self.log_msg("No serial port selected")
            return
        if self.serial_thread:
            self.serial_thread.stop()
            self.serial_thread = None
        self.serial_thread = SerialReader(port, 115200, self.event_queue)
        self.serial_thread.start()
        self._serial_open = True
        self._set_serial_open(True)
        self.log_msg(f"Opened serial {port}")

    def _close_serial(self):
        if self.serial_thread:
            self.serial_thread.stop()
            self.serial_thread = None
        self._serial_open = False
        self._set_serial_open(False)
        self.log_msg("Serial port closed")

    def _set_serial_open(self, open: bool):
        if open:
            self.open_serial_btn.setText("Close")
            self.open_serial_btn.setStyleSheet(self._CONNECTED_BTN_STYLE)
            self.serial_combo.setEnabled(False)
            self.scan_serial_btn.setEnabled(False)
        else:
            self.open_serial_btn.setText("Open")
            self.open_serial_btn.setStyleSheet("")
            self.serial_combo.setEnabled(True)
            self.scan_serial_btn.setEnabled(True)

    def closeEvent(self, event):
        self._save_settings()
        self.scope.shutdown()       # stop keep-alive and reconnect threads cleanly
        if self.serial_thread:
            self.serial_thread.stop()
        event.accept()

    # ---------- Screenshot ----------

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale_preview()

    def _rescale_preview(self):
        if self._current_pixmap and not self._current_pixmap.isNull():
            self.image_label.setPixmap(
                self._current_pixmap.scaled(
                    self.image_label.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

    def update_preview_from_png(self, data: bytes):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data, "PNG"):
            self.log_msg("Warning: received image data could not be decoded as PNG")
            return
        self._current_pixmap = pixmap
        self._current_png_data = data
        self._rescale_preview()
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        self.capture_time_label.setText(f"Captured: {ts}")
        self.save_preview_btn.setEnabled(True)

    def preview_screenshot(self):
        if self._busy:
            return
        self._set_busy(True)

        color = self.color_cb.isChecked()
        inverted = self.invert_cb.isChecked()

        def worker():
            try:
                data = self.scope.get_screenshot_png(color=color, inverted=inverted)
                self.result_queue.put(("preview_done", data))
            except Exception as e:
                self.result_queue.put(("preview_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def save_screenshot_and_setup(self):
        if self._busy:
            return

        # --- Step 1: ask for filename first (on main thread) ---
        default_name = datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")
        start_path = os.path.join(self._last_save_dir, default_name)

        self.showNormal()
        self.raise_()
        self.activateWindow()

        dialog = QFileDialog(self, "Save Screenshot", start_path, "PNG Image (*.png)")
        dialog.setAcceptMode(QFileDialog.AcceptSave)
        dialog.selectFile(default_name)

        if not dialog.exec():
            return  # user cancelled — nothing fetched from scope

        filename = dialog.selectedFiles()[0]
        if not filename.lower().endswith(".png"):
            filename += ".png"

        self._last_save_dir = os.path.dirname(filename)

        # --- Step 2: fetch data from scope in background ---
        self._set_busy(True)

        color = self.color_cb.isChecked()
        inverted = self.invert_cb.isChecked()

        def worker():
            try:
                png_data = self.scope.get_screenshot_png(color=color, inverted=inverted)
                setup_data = self.scope.get_setup()
                self.result_queue.put(("save_done", (filename, png_data, setup_data)))
            except Exception as e:
                self.result_queue.put(("save_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def save_preview_image(self):
        """Save the currently displayed preview PNG to disk — no scope query."""
        if not self._current_png_data:
            return

        default_name = datetime.now().strftime("preview_%Y%m%d_%H%M%S.png")
        start_path = os.path.join(self._last_save_dir, default_name)

        dialog = QFileDialog(self, "Save Preview", start_path, "PNG Image (*.png)")
        dialog.setAcceptMode(QFileDialog.AcceptSave)
        dialog.selectFile(default_name)

        if not dialog.exec():
            return

        filename = dialog.selectedFiles()[0]
        if not filename.lower().endswith(".png"):
            filename += ".png"

        self._last_save_dir = os.path.dirname(filename)

        try:
            with open(filename, "wb") as f:
                f.write(self._current_png_data)
            self.log_msg(f"Preview saved: {filename}")
        except Exception as e:
            self.log_msg(f"Save preview error: {e}")

    def load_setup(self):
        if self._busy:
            return

        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Setup", self._last_save_dir, "Setup Files (*.set *.bin)"
        )
        if not filename:
            return

        self._last_save_dir = os.path.dirname(filename)
        self._set_busy(True)

        # Warn if the setup file extension doesn't match the connected scope type.
        # This is a heuristic — Keysight .set files start with "#", LeCroy with "LSET".
        scope_type = type(self.scope.device).__name__ if self.scope.device else ""

        def worker():
            try:
                with open(filename, "rb") as f:
                    data = f.read()
                # Heuristic mismatch detection
                is_lecroy_file = data[:4] == b"LSET"
                is_lecroy_scope = "LeCroy" in scope_type
                if is_lecroy_file and not is_lecroy_scope:
                    self.result_queue.put(("scope_log",
                        "Warning: setup file appears to be LeCroy format but scope is Keysight"))
                elif not is_lecroy_file and is_lecroy_scope:
                    self.result_queue.put(("scope_log",
                        "Warning: setup file appears to be Keysight format but scope is LeCroy"))
                ok = self.scope.write_setup_data(data)
                self.result_queue.put(("load_setup_done", (filename, ok)))
            except Exception as e:
                self.result_queue.put(("load_setup_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Footswitch button click ----------

    def footswitch_btn_clicked(self, event: str):
        self.handle_event(event)

    # ---------- Timer tick: process both queues ----------

    def _tick(self):
        self._process_serial_events()
        self._process_results()
        self._poll_single_done()

    # Auto-preview polling: give up after 60 seconds (1200 × 50 ms ticks)
    _SINGLE_POLL_TIMEOUT = 1200

    def _poll_single_done(self):
        """Poll scope every ~500 ms after a SINGLE shot to detect when acquisition
        is complete, then automatically fetch and display the preview.
        Gives up after 60 seconds to avoid polling indefinitely if the scope
        never stops (e.g. user pressed RUN without pressing B1S/B1L first)."""
        if not self._waiting_for_single or self._busy or not self.auto_preview_cb.isChecked():
            return

        self._single_poll_total += 1
        if self._single_poll_total >= self._SINGLE_POLL_TIMEOUT:
            # 60 seconds elapsed — give up silently
            self._waiting_for_single = False
            self._single_poll_counter = 0
            self._single_poll_total = 0
            self._single_poll_active = False
            return

        self._single_poll_counter += 1
        if self._single_poll_counter < 10:   # 10 × 50 ms = 500 ms between polls
            return
        self._single_poll_counter = 0

        if self._single_poll_active:
            return  # previous poll worker still in flight — skip this tick
        self._single_poll_active = True

        def worker():
            try:
                running = self.scope.is_running()
                self.result_queue.put(("single_poll", running))
            except Exception:
                pass  # silently skip failed polls
            finally:
                self._single_poll_active = False

        threading.Thread(target=worker, daemon=True).start()

    def _process_serial_events(self):
        while not self.event_queue.empty():
            msg = self.event_queue.get()
            if msg.startswith("ERROR:"):
                self.log_msg(f"Serial error: {msg[6:]}")
                # Reset UI if the port died unexpectedly (e.g. USB unplugged)
                if self._serial_open:
                    self._serial_open = False
                    self.serial_thread = None
                    self._set_serial_open(False)
                    self.log_msg("Footswitch disconnected — port closed")
            elif not self._busy:
                self.handle_event(msg)

    def _process_results(self):
        while not self.result_queue.empty():
            kind, payload = self.result_queue.get()

            if kind == "scope_log":
                # Log message posted from a background thread — safe to call here
                self.log_msg(payload)

            elif kind == "scope_disconnected":
                self._set_scope_connected(False)

            elif kind == "scope_reconnected":
                idn = payload
                self._set_scope_connected(True)
                self.log_msg(f"Auto-reconnected to scope: {idn}")
                self._flash_identify()

            elif kind == "preview_done":
                self._set_busy(False)
                if payload:
                    self.update_preview_from_png(payload)
                    self.log_msg("Screenshot preview updated")
                else:
                    self.log_msg("Preview failed: no data received")

            elif kind == "preview_error":
                self._set_busy(False)
                self.log_msg(f"Preview error: {payload}")

            elif kind == "save_done":
                self._set_busy(False)
                filename, png_data, setup_data = payload
                if not png_data or not setup_data:
                    self.log_msg("Save failed: no data received from scope")
                    continue
                try:
                    self.update_preview_from_png(png_data)
                    with open(filename, "wb") as f:
                        f.write(png_data)
                    base, _ = os.path.splitext(filename)
                    setup_file = base + ".set"
                    with open(setup_file, "wb") as f:
                        f.write(setup_data)
                    self.log_msg(f"Saved: {filename}")
                except Exception as e:
                    self.log_msg(f"Save error: {e}")

            elif kind == "save_error":
                self._set_busy(False)
                self.log_msg(f"Save error: {payload}")

            elif kind == "load_setup_done":
                self._set_busy(False)
                filename, ok = payload
                if ok:
                    self.log_msg(f"Setup loaded: {filename}")
                else:
                    self.log_msg(f"Failed to load setup: {filename} — see log above for details")

            elif kind == "load_setup_error":
                self._set_busy(False)
                self.log_msg(f"Load setup error: {payload}")

            elif kind == "single_poll":
                running = payload
                if not running:
                    # Scope has stopped — acquisition complete, fetch preview
                    self._waiting_for_single = False
                    self._single_poll_counter = 0
                    self._single_poll_total = 0
                    self._single_poll_active = False
                    self.log_msg("Trigger acquired — loading preview")
                    self.preview_screenshot()

            elif kind == "scan_done":
                found = payload   # list of (ip, short_label, full_idn)
                self.scan_btn.setText("Scan")
                self.scan_btn.setEnabled(True)
                # Keep the currently typed text, clear old scan results, repopulate
                current_text = self.ip_combo.currentText().strip()
                self.ip_combo.clear()
                if not found:
                    self.log_msg("Scan complete — no SCPI instruments found")
                else:
                    for ip, short, idn in found:
                        self.ip_combo.addItem(short, ip)
                        idx = self.ip_combo.count() - 1
                        self.ip_combo.setItemData(idx, idn, Qt.ToolTipRole)
                    self.log_msg(f"Scan complete — {len(found)} instrument(s) found")
                # Restore whatever the user had typed (or select matching entry)
                idx = self.ip_combo.findData(current_text)
                if idx >= 0:
                    self.ip_combo.setCurrentIndex(idx)
                else:
                    self.ip_combo.setCurrentText(current_text)

            elif kind == "scan_error":
                self.scan_btn.setText("Scan")
                self.scan_btn.setEnabled(True)
                self.log_msg(f"Scan error: {payload}")

    # ---------- Event Handling ----------

    def handle_event(self, event):
        try:
            if event == "B1S":
                self._waiting_for_single = False
                self._single_poll_total = 0
                if self.scope.is_running():
                    self.scope.stop()
                    self.log_msg(f"Event {event}: STOP")
                else:
                    self.scope.run()
                    self.log_msg(f"Event {event}: RUN")

            elif event == "B1L":
                self._waiting_for_single = False
                self._single_poll_total = 0
                self.scope.run()
                self.scope.trigger_auto()
                self.log_msg(f"Event {event}: RUN, TRIGGER AUTO")

            elif event == "B2S":
                self.scope.trigger_normal()
                self.scope.single()
                self._waiting_for_single = True
                self._single_poll_total = 0
                self.log_msg(f"Event {event}: SINGLE, TRIGGER NORMAL")

            elif event == "B2L":
                self.scope.single()
                self.scope.trigger_force()
                self._waiting_for_single = True
                self._single_poll_total = 0
                self.log_msg(f"Event {event}: SINGLE, TRIGGER FORCE")

            elif event == "BBS":
                self.preview_screenshot()

            elif event == "BBL":
                self.save_screenshot_and_setup()

            else:
                self.log_msg(f"Unknown event received: '{event}' — ignored")

        except Exception as e:
            self.log_msg(str(e))

    # ---------- Log ----------

    def log_msg(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{timestamp}] {msg}")
        # cap log at LOG_MAX_LINES
        doc = self.log.document()
        while doc.blockCount() > LOG_MAX_LINES:
            cursor = self.log.textCursor()
            cursor.movePosition(cursor.Start)
            cursor.select(cursor.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()  # remove the trailing newline
        # always scroll to the latest entry
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum()
        )

    def _clear_log(self):
        self.log.clear()

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(700, 650)
    win.show()
    sys.exit(app.exec())
