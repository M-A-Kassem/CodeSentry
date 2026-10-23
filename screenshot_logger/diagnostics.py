"""Diagnostic helpers: foreground window title and process information.

Every function here degrades gracefully (returns ``None``) when information
is unavailable instead of raising. Diagnostics are "nice to have" context on
an incident -- they must never be the reason an incident fails to be saved.
"""
from __future__ import annotations

import ctypes
import logging
import os
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None  # type: ignore[assignment]

_logger = logging.getLogger("screenshot_logger")


def get_current_process_id() -> int:
    """Return the current Python process ID."""
    return os.getpid()


def get_foreground_window_title() -> Optional[str]:
    """Return the title of the current Windows foreground window, if any.

    Uses only the built-in ``ctypes`` access to the Win32 API, so no extra
    dependency is required for this. Returns ``None`` on any failure (for
    example when there is no foreground window, or when running outside a
    desktop session).
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return None
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value or None
    except Exception:
        _logger.debug("Could not read foreground window title.", exc_info=True)
        return None


def get_foreground_process_name() -> Optional[str]:
    """Return the process name owning the foreground window, if determinable."""
    if psutil is None:
        return None
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        return psutil.Process(pid.value).name()
    except Exception:
        _logger.debug("Could not determine foreground process name.", exc_info=True)
        return None
