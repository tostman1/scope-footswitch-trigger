from .keysight import KeysightScope
from .base import _strip_ieee_header


# ----------------------------
# Keysight / Agilent 7000 Scope
# ----------------------------
class Keysight7000Scope(KeysightScope):

    def trigger_force(self):
        # The 7000A series does not support :TRIG:FORC.
        # Forcing a trigger is achieved by briefly switching the sweep mode to
        # AUTO (which triggers immediately) and then back to NORMAL so the scope
        # stops after the forced acquisition.
        self.scope.write(":TRIG:SWE AUTO")
        self.scope.write(":TRIG:SWE NORM")

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
                f":DISPlay:DATA? PNG,SCReen,{palette}",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = '\n'
            self.scope.read_termination = '\n'
            self.scope.timeout = old_timeout

        return _strip_ieee_header(raw)
