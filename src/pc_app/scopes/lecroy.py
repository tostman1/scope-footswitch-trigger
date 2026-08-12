from .base import BaseScope
import pyvisa
from PIL import Image
import io


# ----------------------------
# LeCroy Scope
# ----------------------------
class LeCroyScope(BaseScope):

    def identify(self, enable: bool):
        if enable:
            self.scope.write(f'MESSAGE "{self.username} has connected via OsciFootswitch Tool"')
        else:
            self.scope.write('MESSAGE ""')

    def run(self):
        self.scope.write("TRIG_MODE NORM")
        self.running = True

    def stop(self):
        self.scope.write("TRIG_MODE STOP")
        self.running = False

    def single(self):
        self.scope.write("TRIG_MODE SINGLE")

    def trigger_auto(self):
        self.scope.write("TRIG_MODE AUTO")

    def trigger_force(self):
        self.scope.write("TRIG_MODE AUTO")
        self.scope.write("WAIT")
        self.scope.write("TRIG_MODE STOP")

    def trigger_normal(self):
        self.scope.write("TRIG_MODE NORM")

    def is_running(self) -> bool:
        """Query the scope live for its current trigger mode.
        NORM / AUTO = running, STOP / SINGLE = stopped."""
        try:
            resp = self.scope.query("TRIG_MODE?").strip().upper()
            # Running states: NORM (normal trigger), AUTO (auto trigger)
            self.running = resp in ("NORM", "AUTO")
            return self.running
        except Exception as e:
            self.log(f"Runstate error: {e}")
            return self.running

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:
        bckg = "WHITE" if inverted else "BLACK"

        old_write_term = self.scope.write_termination
        old_read_term  = self.scope.read_termination
        old_timeout    = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination  = ''
        self.scope.timeout = 10000
        try:
            # configure hardcopy for screenshot (TIFF, GPIB port, background color)
            self.scope.write(f"HCSU DEV,TIFF,PORT,GPIB,BCKG,{bckg}")
            # trigger screenshot
            data = self.scope.query_binary_values(
                "SCDP",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = old_write_term
            self.scope.read_termination  = old_read_term
            self.scope.timeout           = old_timeout

        # convert TIFF -> PNG
        image = Image.open(io.BytesIO(data))
        png_buffer = io.BytesIO()
        image.save(png_buffer, format="PNG")

        return png_buffer.getvalue()

    def get_setup(self) -> bytes:
        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination = ''
        self.scope.timeout = 5000
        try:
            # PNSU? = Panel Setup — returns binary setup data
            raw = self.scope.query_binary_values(
                "PNSU?",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = '\n'
            self.scope.read_termination = '\n'
            self.scope.timeout = old_timeout

        return raw

    def write_setup_data(self, data: bytes) -> bool:
        try:
            self.scope.write_termination = ''
            self.scope.read_termination = ''
            try:
                # Send raw binary — already in the correct LeCroy binary format.
                # flush() is not implemented in pyvisa-py over TCP — omitted intentionally.
                self.scope.write_raw(data)
            finally:
                self.scope.write_termination = '\n'
                self.scope.read_termination = '\n'

            return True

        except Exception as e:
            self.log(f"Setup write error: {e}")
            return False