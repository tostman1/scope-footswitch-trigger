import pyvisa


def _strip_ieee_header(raw: bytes) -> bytes:
    """Strip the IEEE-488.2 definite-length binary block header from a VISA response.

    VISA binary responses are prefixed with '#<n><length><data>' where <n> is the
    number of digits in <length>.  Example: '#6524288<data>' means 524288 bytes follow.
    pyvisa-py sometimes returns this header together with the payload; NI-VISA
    usually strips it automatically.  This function handles both cases safely.

    Returns the raw buffer unchanged if no valid header is present.
    """
    if not raw.startswith(b"#") or len(raw) < 2:
        return raw
    try:
        n = int(raw[1:2])
        if len(raw) < 2 + n:
            return raw          # response was truncated before the length field
        length = int(raw[2:2 + n])
        return raw[2 + n:2 + n + length]
    except (ValueError, IndexError):
        return raw              # malformed header — pass data through unchanged


class BaseScope:
    """Abstract base class for all oscilloscope implementations.

    Each supported scope brand (Keysight, LeCroy, …) inherits from this class
    and implements every method using the brand-specific SCPI command set.

    The `log` callback is thread-safe: implementations call self.log(msg) to
    post messages to the UI queue without touching Qt directly.
    The `running` flag is a software-side cache for is_running(); subclasses
    that can query the scope live should override is_running() instead.
    """

    def __init__(self, scope: pyvisa.resources.Resource, log, username: str = ""):
        self.scope = scope        # open pyvisa resource (TCPIP::IP::INSTR)
        self.log = log            # callable: log(msg: str) -> None, thread-safe
        self.username = username or "Unknown"  # Windows login name, shown on scope screen
        self.running = False      # last-known acquisition state (fallback for is_running)

    # -- Identification & display -----------------------------------------------

    def identify(self, enable: bool):
        """Show or clear the user identification message on the scope screen."""
        raise NotImplementedError

    # -- Acquisition control -----------------------------------------------------

    def run(self):
        """Start continuous acquisition (equivalent to pressing RUN on the scope)."""
        raise NotImplementedError

    def stop(self):
        """Stop acquisition."""
        raise NotImplementedError

    def single(self):
        """Arm the scope for a single acquisition (waits for one trigger event)."""
        raise NotImplementedError

    # -- Trigger control ---------------------------------------------------------

    def trigger_auto(self):
        """Switch to Auto trigger sweep mode (triggers periodically even without signal)."""
        raise NotImplementedError

    def trigger_force(self):
        """Force an immediate trigger regardless of signal conditions."""
        raise NotImplementedError

    def trigger_normal(self):
        """Switch to Normal trigger sweep mode (only triggers on valid signal)."""
        raise NotImplementedError

    # -- State query -------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True if the scope is currently acquiring.

        Default implementation returns the cached software flag.
        Subclasses should override this with a live SCPI query where possible.
        """
        return self.running

    # -- Screenshot / Setup ------------------------------------------------------

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:
        """Capture the scope screen and return it as a PNG byte string.

        color    — True = colour, False = greyscale
        inverted — True = white background (ink-saver), False = black background
        """
        raise NotImplementedError

    def get_setup(self) -> bytes:
        """Download the current instrument setup as a binary blob.

        The returned data can be stored and later sent back via write_setup_data().
        The format is scope-brand-specific; do not mix Keysight and LeCroy files.
        """
        raise NotImplementedError

    def write_setup_data(self, data: bytes) -> bool:
        """Upload a previously saved setup blob to the scope.

        Returns True on success, False on SCPI/IO error.
        """
        raise NotImplementedError
