from .base import BaseScope, _strip_ieee_header
import pyvisa


class KeysightScope(BaseScope):
    """SCPI implementation for Keysight / Agilent oscilloscopes.

    Covers the 2000, 3000, 4000, and 6000 X-Series as well as most other
    Keysight/Agilent models that follow the standard InfiniiVision command set.
    The 7000 Series uses a slightly different command set and is handled by
    Keysight7000Scope (a subclass).

    Reference: Keysight InfiniiVision Programmer's Guide (various series).
    """

    def identify(self, enable: bool):
        # :SYST:DSP writes a text message into the scope's display area.
        # Cleared by sending an empty string.
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
        # :SINGLE arms for one acquisition and stops automatically after the trigger.
        self.scope.write(":SINGLE")

    def trigger_auto(self):
        # SWEep AUTO — scope triggers periodically even without a valid signal edge.
        self.scope.write(":TRIG:SWE AUTO")

    def trigger_force(self):
        # :TRIG:FORC causes an immediate acquisition regardless of trigger conditions.
        # Note: not supported on 7000A series — see Keysight7000Scope.trigger_force().
        self.scope.write(":TRIG:FORC")

    def trigger_normal(self):
        # SWEep NORM — scope only triggers on a valid signal edge.
        self.scope.write(":TRIG:SWE NORM")

    def is_running(self) -> bool:
        # :OPER:COND? returns the Operation Status Condition register (IEEE 488.2).
        # Bit 3 (0b1000) = "measuring / running".  Falls back to the cached flag
        # on communication error to avoid disrupting the UI.
        try:
            cond = int(self.scope.query(":OPER:COND?"))
            return bool(cond & 0b1000)
        except Exception as e:
            self.log(f"Runstate error: {e}")
            return self.running

    # ---------------------------------------------------------------------------
    # Screenshot
    # ---------------------------------------------------------------------------

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:
        palette  = "COLor" if color else "GRAYscale"
        inksaver = "ON"    if inverted else "OFF"

        # Binary transfers require empty terminators so pyvisa doesn't truncate
        # the data at the first newline character in the binary payload.
        # The try/finally guarantees ASCII mode is always restored even on error.
        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination  = ''
        self.scope.timeout = 10000   # screenshots can take several seconds on older models
        try:
            self.scope.write(f":HARDcopy:INKSaver {inksaver}")
            raw = self.scope.query_binary_values(
                f":DISPlay:DATA? PNG,{palette}",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = '\n'
            self.scope.read_termination  = '\n'
            self.scope.timeout = old_timeout

        return _strip_ieee_header(raw)

    # ---------------------------------------------------------------------------
    # Setup save / restore
    # ---------------------------------------------------------------------------

    def get_setup(self) -> bytes:
        # :SYSTem:SETup? returns the full instrument configuration as a binary blob.
        # The blob is scope-model-specific and should only be restored to the same
        # model (or a compatible one from the same series).
        old_timeout = self.scope.timeout
        self.scope.write_termination = ''
        self.scope.read_termination  = ''
        self.scope.timeout = 5000
        try:
            raw = self.scope.query_binary_values(
                ":SYSTem:SETup?",
                datatype='B',
                container=bytes
            )
        finally:
            self.scope.write_termination = '\n'
            self.scope.read_termination  = '\n'
            self.scope.timeout = old_timeout

        return _strip_ieee_header(raw)

    def write_setup_data(self, data: bytes) -> bool:
        # Reconstruct the IEEE-488.2 binary block header that was stripped on read,
        # then send the whole payload as a raw write (no string encoding / newlines).
        # flush() is not implemented in pyvisa-py over TCP, so it is omitted —
        # write_raw() on a TCP socket sends data immediately without buffering.
        try:
            header  = f"#{len(str(len(data)))}{len(data)}".encode()
            payload = header + data

            self.scope.write_termination = ''
            self.scope.read_termination  = ''
            try:
                self.scope.write_raw(b":SYSTem:SETup " + payload)
            finally:
                self.scope.write_termination = '\n'
                self.scope.read_termination  = '\n'

            return True

        except Exception as e:
            self.log(f"Setup write error: {e}")
            return False
