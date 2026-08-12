from .keysight import KeysightScope
from .base import _strip_ieee_header


class Keysight7000Scope(KeysightScope):
    """SCPI implementation for Keysight / Agilent InfiniiVision 7000A Series.

    The 7000A shares most commands with the standard Keysight implementation but
    has two differences handled here:

    1. trigger_force(): :TRIG:FORC is not supported — replaced with a sweep-mode
       workaround (AUTO briefly to force one acquisition, then back to NORM).

    2. get_screenshot_png(): the :DISPlay:DATA? command requires an additional
       'SCReen' argument that is not present in earlier series.

    Detection: IDN string contains "MSO70" or "DSO70".
    Reference: Agilent InfiniiVision 7000A Programmer's Guide (9018-06630).
    """

    def trigger_force(self):
        # :TRIG:FORC is not implemented on the 7000A — sending it causes an
        # "Undefined header" error.  The workaround: switch to AUTO sweep
        # (which triggers immediately without a valid edge) then back to NORM
        # so the scope stops after the forced acquisition — equivalent result.
        self.scope.write(":TRIG:SWE AUTO")
        self.scope.write(":TRIG:SWE NORM")

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:
        palette  = "COLor" if color else "GRAYscale"
        inksaver = "ON"    if inverted else "OFF"

        # Same binary-transfer pattern as KeysightScope but with 'SCReen' added
        # to the :DISPlay:DATA? command — required by the 7000A firmware.
        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination  = ''
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
            self.scope.read_termination  = '\n'
            self.scope.timeout = old_timeout

        return _strip_ieee_header(raw)
