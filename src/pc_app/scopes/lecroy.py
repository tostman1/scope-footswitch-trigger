from .base import BaseScope
import pyvisa
from PIL import Image
import io


class LeCroyScope(BaseScope):
    """SCPI implementation for LeCroy / Teledyne LeCroy oscilloscopes.

    Uses LeCroy's proprietary command syntax rather than standard Keysight SCPI.
    Tested on WaveRunner 6100 and WaveRunner 44XI.  VXI-11 (port 111) must be
    enabled on the scope: Utilities → Remote → VXI11.

    Key differences from Keysight:
    - Trigger mode is controlled by a single TRIG_MODE command (NORM / AUTO /
      STOP / SINGLE) rather than separate RUN/STOP/SINGLE commands.
    - Screenshots are captured as TIFF (scope limitation) and converted to PNG
      by Pillow before returning.
    - Setup data is retrieved with PNSU? and sent back as raw binary — no
      IEEE-488.2 header wrapping is needed.

    Reference: LeCroy WaveRunner Remote Control Manual (wr2_rcm_revb.pdf).
    """

    def identify(self, enable: bool):
        # MESSAGE displays a text overlay on the scope screen.
        if enable:
            self.scope.write(f'MESSAGE "{self.username} has connected via OsciFootswitch Tool"')
        else:
            self.scope.write('MESSAGE ""')

    def run(self):
        # TRIG_MODE NORM = normal (triggered) continuous acquisition.
        self.scope.write("TRIG_MODE NORM")
        self.running = True

    def stop(self):
        self.scope.write("TRIG_MODE STOP")
        self.running = False

    def single(self):
        # TRIG_MODE SINGLE arms for one acquisition and halts after the trigger.
        self.scope.write("TRIG_MODE SINGLE")

    def trigger_auto(self):
        # AUTO mode triggers periodically even without a valid signal edge.
        self.scope.write("TRIG_MODE AUTO")

    def trigger_force(self):
        # LeCroy has no direct "force trigger" command.  Workaround:
        # switch to AUTO (triggers immediately), WAIT for the acquisition to
        # complete, then STOP — net result is one forced acquisition.
        self.scope.write("TRIG_MODE AUTO")
        self.scope.write("WAIT")
        self.scope.write("TRIG_MODE STOP")

    def trigger_normal(self):
        self.scope.write("TRIG_MODE NORM")

    def is_running(self) -> bool:
        # Query the scope live rather than relying on the software flag,
        # so that front-panel changes are reflected correctly in the UI.
        try:
            resp = self.scope.query("TRIG_MODE?").strip().upper()
            self.running = resp in ("NORM", "AUTO")
            return self.running
        except Exception as e:
            self.log(f"Runstate error: {e}")
            return self.running

    # ---------------------------------------------------------------------------
    # Screenshot
    # ---------------------------------------------------------------------------

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:
        bckg = "WHITE" if inverted else "BLACK"

        # LeCroy can only output TIFF via SCDP (screen dump) — PNG is not supported.
        # The TIFF is converted to PNG in memory by Pillow before returning.
        # HCSU configures the hardcopy destination: TIFF format, GPIB port (used for
        # network too), and background colour.
        # Binary transfers require empty terminators (see keysight.py for explanation).
        old_write_term = self.scope.write_termination
        old_read_term  = self.scope.read_termination
        old_timeout    = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination  = ''
        self.scope.timeout = 10000
        try:
            self.scope.write(f"HCSU DEV,TIFF,PORT,GPIB,BCKG,{bckg}")
            data = self.scope.query_binary_values(
                "SCDP",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = old_write_term
            self.scope.read_termination  = old_read_term
            self.scope.timeout           = old_timeout

        image = Image.open(io.BytesIO(data))
        png_buffer = io.BytesIO()
        image.save(png_buffer, format="PNG")
        return png_buffer.getvalue()

    # ---------------------------------------------------------------------------
    # Setup save / restore
    # ---------------------------------------------------------------------------

    def get_setup(self) -> bytes:
        # PNSU? = Panel Setup Query — returns the current instrument configuration
        # as a raw binary blob in LeCroy's proprietary format.
        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination  = ''
        self.scope.timeout = 5000
        try:
            raw = self.scope.query_binary_values(
                "PNSU?",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = '\n'
            self.scope.read_termination  = '\n'
            self.scope.timeout = old_timeout

        # No IEEE-488.2 header stripping needed — LeCroy returns the blob directly.
        return raw

    def write_setup_data(self, data: bytes) -> bool:
        # The setup blob is sent back as raw binary without modification.
        # LeCroy identifies the setup command from the binary header embedded in
        # the PNSU data itself, so no SCPI prefix is prepended here.
        try:
            self.scope.write_termination = ''
            self.scope.read_termination  = ''
            try:
                self.scope.write_raw(data)
                # flush() not implemented in pyvisa-py over TCP — omitted intentionally.
            finally:
                self.scope.write_termination = '\n'
                self.scope.read_termination  = '\n'

            return True

        except Exception as e:
            self.log(f"Setup write error: {e}")
            return False
