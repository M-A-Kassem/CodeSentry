"""Small Win32 display helpers: DPI awareness, monitors, window styles.

All functions degrade to harmless no-ops off Windows or on failure.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
from typing import Optional

_logger = logging.getLogger("screenshot_logger")

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080  # no taskbar button, no Alt+Tab entry
WS_EX_NOACTIVATE = 0x08000000  # clicking it never takes keyboard focus
WS_EX_TOPMOST = 0x00000008
MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTONEAREST = 2
_MUTEX_HANDLE = None


def enable_dpi_awareness() -> None:
    """Opt in to per-monitor DPI awareness *before* any window exists.

    Without it Windows bitmap-stretches the app on scaled displays (blurry
    text) and reports scaled coordinates, which would make window captures
    for screen detection miss part of the window.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            _logger.debug("Could not enable DPI awareness.", exc_info=True)


def acquire_single_instance(name: str = "Local\\ScreenshotErrorLoggerAssistant") -> bool:
    """Return False if another copy of the tray app is already running."""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, name)
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def toplevel_hwnd(widget) -> Optional[int]:
    """The real top-level Win32 window of a Tk Toplevel (Tk's own
    ``winfo_id`` is an inner child window)."""
    try:
        widget.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        return int(hwnd) or int(widget.winfo_id())
    except Exception:
        return None


def make_non_activating_tool_window(widget) -> bool:
    """Stop a window from stealing keyboard focus or showing in the taskbar."""
    hwnd = toplevel_hwnd(widget)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        return True
    except Exception:
        _logger.debug("Could not set overlay window styles.", exc_info=True)
        return False


def primary_work_area() -> tuple[int, int, int, int]:
    """Primary monitor area excluding the taskbar: (left, top, right, bottom)."""
    rect = ctypes.wintypes.RECT()
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1040


def work_area_for_point(x: int, y: int) -> Optional[tuple[int, int, int, int]]:
    """Work area of the monitor containing (x, y), or None if that point is
    on no monitor (e.g. a saved position on a now-disconnected screen)."""

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork", ctypes.wintypes.RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
        ]

    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
        monitor = user32.MonitorFromPoint(ctypes.wintypes.POINT(x, y), MONITOR_DEFAULTTONULL)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW.argtypes = [ctypes.wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    except Exception:
        return None


def clamp_to_area(x: int, y: int, width: int, height: int, area: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = area
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))
