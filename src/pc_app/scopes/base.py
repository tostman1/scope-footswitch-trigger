import pyvisa


def _strip_ieee_header(raw: bytes) -> bytes:
    """Strip an IEEE-488.2 definite-length binary block header (#<n><length><data>).

    Validates that the raw buffer is long enough before indexing to avoid
    ValueError / IndexError on truncated responses.  Returns the data payload
    unchanged if no valid header is detected.
    """
    if not raw.startswith(b"#") or len(raw) < 2:
        return raw
    try:
        n = int(raw[1:2])           # number of length digits
        if len(raw) < 2 + n:
            return raw              # truncated — return as-is
        length = int(raw[2:2 + n])
        return raw[2 + n:2 + n + length]
    except (ValueError, IndexError):
        return raw                  # malformed header — return as-is


# ----------------------------
# Base Scope
# ----------------------------
class BaseScope:

    def __init__(self, scope: pyvisa.resources.Resource, log, username: str = ""):
        self.scope = scope
        self.log = log
        self.username = username or "Unknown"
        self.running = False

    def identify(self, enable: bool):
        raise NotImplementedError

    def run(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def single(self):
        raise NotImplementedError

    def trigger_auto(self):
        raise NotImplementedError

    def trigger_force(self):
        raise NotImplementedError

    def trigger_normal(self):
        raise NotImplementedError

    def is_running(self) -> bool:
        return self.running

    def get_screenshot_png(self, color: bool, inverted: bool) -> bytes:
        raise NotImplementedError

    def get_setup(self) -> bytes:
        raise NotImplementedError

    def write_setup_data(self, data: bytes) -> bool:
        raise NotImplementedError