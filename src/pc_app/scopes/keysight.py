from .base import BaseScope
import pyvisa


# ----------------------------
# Keysight / Agilent Scope
# ----------------------------
class KeysightScope(BaseScope):

    def identify(self, enable: bool):
        if enable:
            self.scope.write(f':SYST:DSP "{self.username} has connected via OsciFootswitch Tool"')
        else:
            self.scope.write(':SYST:DSP ""')

    def run(self):
        self.scope.write(":RUN")
        self.running = True

    def stop(self):
        self.scope.write(":STOP")
        self.running = False

    def single(self):
        self.scope.write(":SINGLE")

    def trigger_auto(self):
        self.scope.write(":TRIG:SWE AUTO")

    def trigger_force(self):
        self.scope.write(":TRIG:FORC")

    def trigger_normal(self):
        self.scope.write(":TRIG:SWE NORM")

    def is_running(self) -> bool:
        try:
            cond = int(self.scope.query(":OPER:COND?"))
            return bool(cond & 0b1000)      # Bit 3
        except Exception as e:
            self.log(f"Runstate error: {e}")
            return self.running

    # ---------- Screenshot / Setup ----------

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:

        palette = "COLor" if color else "GRAYscale"
        inksaver = "ON" if inverted else "OFF"

        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination = ''
        self.scope.timeout = 10000
        try:
            self.scope.write(f":HARDcopy:INKSaver {inksaver}")
            raw = self.scope.query_binary_values(
                f":DISPlay:DATA? PNG,{palette}",
                datatype='B',
                container=bytes
            )
        finally:
            # Always restore ASCII mode even if the query raises an exception,
            # otherwise all subsequent ASCII queries (IDN, keep-alive) will fail.
            self.scope.write_termination = '\n'
            self.scope.read_termination = '\n'
            self.scope.timeout = old_timeout

        # Strip IEEE-488.2 binary block header (#<n><length><data>)
        if raw.startswith(b"#"):
            n = int(raw[1:2])
            length = int(raw[2:2 + n])
            data = raw[2 + n:2 + n + length]
        else:
            data = raw

        return data

    def get_setup(self) -> bytes:
        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination = ''
        self.scope.timeout = 5000
        try:
            raw = self.scope.query_binary_values(
                ":SYSTem:SETup?",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = '\n'
            self.scope.read_termination = '\n'
            self.scope.timeout = old_timeout

        # Strip IEEE-488.2 binary block header
        if raw.startswith(b"#"):
            n = int(raw[1:2])
            length = int(raw[2:2 + n])
            data = raw[2 + n:2 + n + length]
        else:
            data = raw

        return data

    def write_setup_data(self, data: bytes) -> bool:
        try:
            header = f"#{len(str(len(data)))}{len(data)}".encode()
            payload = header + data

            self.scope.write_termination = ''
            self.scope.read_termination = ''
            try:
                self.scope.write_raw(b":SYSTem:SETup " + payload)
                self.scope.flush(pyvisa.constants.VI_WRITE_BUF)
            finally:
                self.scope.write_termination = '\n'
                self.scope.read_termination = '\n'

            return True

        except Exception:
            return False