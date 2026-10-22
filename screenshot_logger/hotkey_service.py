"""Optional global hotkey service.

Uses the third-party ``keyboard`` package to listen for a global hotkey
(default: Ctrl+Shift+F9) even while another application has focus, so a
manual incident can be captured without switching to this app first.

This is deliberately isolated in its own small module, separate from the
rest of the application, because global keyboard hooks are inherently less
portable and more failure-prone than everything else here:

- The ``keyboard`` package is an *optional* dependency. If it is not
  installed, the hotkey feature is simply disabled.
- Registration can fail (for example, some security software blocks
  low-level keyboard hooks, or another application already owns the same
  combination). That is treated as a soft failure: it is logged and the
  rest of the application keeps working normally.
- The hotkey only listens for one specific combination; it hooks that one
  shortcut and otherwise does not touch -- or block -- any other keyboard
  input.

Known limitation: on some locked-down systems, global low-level keyboard
hooks may not fire at all (e.g. inside certain sandboxes, remote desktop
sessions, or elevated/administrator windows). If the hotkey does not
respond, use the in-app "Capture Manual Incident" button instead.
"""
from __future__ import annotations

import logging
from typing import Callable

_logger = logging.getLogger("screenshot_logger")


class HotkeyService:
    """Registers and unregisters a single global hotkey combination."""

    def __init__(self, combo: str, callback: Callable[[], None]) -> None:
        self.combo = combo
        self.callback = callback
        self._registered = False

    def start(self) -> bool:
        """Attempt to register the hotkey. Returns True on success, False
        if the feature is unavailable or registration failed (both are
        handled gracefully; neither raises)."""
        try:
            import keyboard  # imported lazily: optional dependency
        except ImportError:
            _logger.warning(
                "Global hotkey disabled: the 'keyboard' package is not installed."
            )
            return False

        try:
            keyboard.add_hotkey(self.combo, self.callback)
            self._registered = True
            _logger.info("Global hotkey '%s' registered.", self.combo)
            return True
        except Exception:
            _logger.warning("Could not register global hotkey '%s'.", self.combo, exc_info=True)
            return False

    def stop(self) -> None:
        """Unregister the hotkey if it was registered. Safe to call even if
        ``start()`` was never called or failed."""
        if not self._registered:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self.combo)
        except Exception:
            _logger.debug("Could not cleanly unregister hotkey '%s'.", self.combo, exc_info=True)
        finally:
            self._registered = False
