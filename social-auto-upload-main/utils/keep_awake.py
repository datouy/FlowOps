"""Keep Windows awake while the unattended automation service is running."""
from __future__ import annotations

import atexit
import ctypes
import os
from collections.abc import Callable


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_held = False


def _set_execution_state(
    flags: int,
    setter: Callable[[int], int] | None = None,
) -> bool:
    if setter is not None:
        return bool(setter(flags))
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.SetThreadExecutionState
        function.argtypes = [wintypes.ULONG]
        function.restype = wintypes.ULONG
        return bool(function(flags))
    except (AttributeError, OSError, ValueError):
        return False


def hold_system_awake(platform_name: str | None = None) -> bool:
    """Disable automatic system sleep for this process lifetime.

    ``ES_SYSTEM_REQUIRED`` prevents idle sleep while the backend is running.
    It does not prevent the display from turning off and it is released when
    the backend process exits.
    """
    global _held
    if _held:
        return True
    if (platform_name or os.name) != "nt":
        return False
    _held = _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    if _held:
        atexit.register(release_system_awake)
    return _held


def release_system_awake() -> bool:
    """Clear the execution-state request owned by the current thread."""
    global _held
    if not _held:
        return True
    released = _set_execution_state(ES_CONTINUOUS)
    if released:
        _held = False
    return released
