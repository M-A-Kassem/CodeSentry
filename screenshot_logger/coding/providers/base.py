"""Common provider interface.

A provider observes one kind of diagnostic source and pushes full
snapshots into an :class:`~screenshot_logger.coding.issue_tracker.IssueTracker`.
Providers own their threads; the controller only starts/stops them and
passes along foreground-application changes. A failing provider reports
its failure through :meth:`status` and must never take down the others.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from ..app_detector import EditorKind, ForegroundApp
from ..issue_tracker import IssueTracker

_logger = logging.getLogger("screenshot_logger")

STATE_DISABLED = "disabled"
STATE_IDLE = "idle"  # running, nothing to watch right now
STATE_ACTIVE = "active"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"


@dataclass(frozen=True)
class ProviderStatus:
    state: str
    detail: str = ""


class Provider:
    name = "provider"
    display_name = "Provider"

    def __init__(self) -> None:
        self.tracker: Optional[IssueTracker] = None
        self._status = ProviderStatus(STATE_STOPPED)
        self._status_lock = threading.Lock()

    def start(self, tracker: IssueTracker) -> None:
        self.tracker = tracker
        self.set_status(STATE_IDLE)

    def stop(self) -> None:
        self.set_status(STATE_STOPPED)

    def on_context(self, foreground: Optional[ForegroundApp], running: dict[str, EditorKind]) -> None:
        """Called about once a second with the current foreground app and
        running editors. Must be cheap; heavy work belongs on the
        provider's own thread."""

    def active_paths(self) -> list[str]:
        """Files/folders the user is working on right now (used to detect
        the active project). Empty if the provider can't tell."""
        return []

    def unsaved_buffers(self) -> dict[str, str]:
        """``{path: text}`` of editor documents with unsaved changes, if
        this provider can read them. Empty otherwise."""
        return {}

    def status(self) -> ProviderStatus:
        with self._status_lock:
            return self._status

    def set_status(self, state: str, detail: str = "") -> None:
        with self._status_lock:
            self._status = ProviderStatus(state, detail)
