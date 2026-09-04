"""Small, dependency-free queries for macOS' current default audio routes."""
import ctypes
import sys
from functools import lru_cache


def fourcc(value):
    """Return the integer representation used by Core Audio selectors."""
    return int.from_bytes(value.encode("ascii"), "big")


DEFAULT_INPUT = fourcc("dIn ")
DEFAULT_OUTPUT = fourcc("dOut")
GLOBAL_SCOPE = fourcc("glob")
SYSTEM_OBJECT = 1


class PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _core_audio():
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
    except OSError:
        return None


def _default_device(selector):
    api = _core_audio()
    if api is None:
        return None
    address = PropertyAddress(selector, GLOBAL_SCOPE, 0)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
    device = ctypes.c_uint32()
    try:
        status = api.AudioObjectGetPropertyData(
            SYSTEM_OBJECT,
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(device),
        )
    except (AttributeError, OSError):
        return None
    return device.value if status == 0 and device.value else None


def default_input_device():
    """Core Audio object ID for the current macOS default input, if known."""
    return _default_device(DEFAULT_INPUT)


def default_output_device():
    """Core Audio object ID for the current macOS default output, if known."""
    return _default_device(DEFAULT_OUTPUT)
