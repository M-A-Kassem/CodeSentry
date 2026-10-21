"""Reusable screenshot capture service.

Kept deliberately small and dependency-light: one function that captures
the primary screen and saves it as a PNG, and never raises -- any failure
(no display, permissions issue, disk full, etc.) is logged and reported
back as ``None`` so callers can still record an incident without a
screenshot.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import pyautogui
except ImportError:  # pragma: no cover - pyautogui is a declared dependency
    pyautogui = None  # type: ignore[assignment]

_logger = logging.getLogger("screenshot_logger")


def capture_screenshot(incident_id: str, screenshots_dir: Path) -> Optional[Path]:
    """Capture the full primary screen and save it as a PNG file.

    The filename contains a timestamp and the incident ID so it is unique
    and never overwrites a previous screenshot.

    Returns the saved file path, or ``None`` if capture failed for any
    reason. Failures are logged but never raised.
    """
    if pyautogui is None:
        _logger.warning("pyautogui is not available; skipping screenshot.")
        return None

    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}_{incident_id}.png"
        destination = screenshots_dir / filename

        image = pyautogui.screenshot()
        image.save(destination)

        if not destination.exists():
            _logger.warning("Screenshot save reported success but file is missing: %s", destination)
            return None
        return destination
    except Exception:
        _logger.warning("Screenshot capture failed.", exc_info=True)
        return None
