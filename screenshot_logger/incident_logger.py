"""Core incident recording API.

This is the main entry point other Python scripts use to report
exceptions or manually record incidents:

    from screenshot_logger import IncidentLogger

    logger = IncidentLogger()
    try:
        ...
    except Exception as exc:
        logger.log_exception(title="Calculation failed", exception=exc)

See ``examples/demo_errors.py`` for a complete runnable example.
"""
from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config as config_module
from . import diagnostics
from . import storage
from .screenshot_service import capture_screenshot

VALID_SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass
class Incident:
    """A single recorded incident. Mirrors the JSON record saved to disk."""

    incident_id: str
    timestamp: str
    severity: str
    title: str
    message: str
    exception_type: Optional[str] = None
    traceback_text: Optional[str] = None
    screenshot_path: Optional[str] = None
    process_id: Optional[int] = None
    foreground_window_title: Optional[str] = None
    foreground_process_name: Optional[str] = None
    source: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "exception_type": self.exception_type,
            "traceback": self.traceback_text,
            "screenshot_path": self.screenshot_path,
            "process_id": self.process_id,
            "foreground_window_title": self.foreground_window_title,
            "foreground_process_name": self.foreground_process_name,
            "source": self.source,
        }


class IncidentLogger:
    """Records incidents (exceptions or manual reports) as JSON files, with
    an optional screenshot of the desktop at the time of the incident.

    Safe to share across threads: a lock serializes the file-writing and
    operational-logging step of each call.
    """

    def __init__(
        self,
        config: Optional[config_module.AppConfig] = None,
        incidents_dir: Optional[Path] = None,
        screenshots_dir: Optional[Path] = None,
    ) -> None:
        self.config = config or config_module.load_config()
        self.incidents_dir = incidents_dir or config_module.INCIDENTS_DIR
        self.screenshots_dir = screenshots_dir or config_module.SCREENSHOTS_DIR
        config_module.ensure_data_dirs()
        self._logger = config_module.configure_logging(self.config.log_level)
        self._lock = threading.Lock()

    def log_exception(
        self,
        title: str,
        exception: BaseException,
        severity: str = "ERROR",
        source: Optional[str] = None,
        capture: Optional[bool] = None,
    ) -> Incident:
        """Record an incident for a caught exception.

        Formats the exception type and full traceback, collects available
        diagnostic metadata, optionally captures a screenshot, and saves an
        incident JSON file plus an operational log entry.

        Always returns the in-memory :class:`Incident`, even if writing the
        JSON file to disk failed. A disk-write failure is reported through
        the operational log only (never re-raised, and never routed back
        through this same method, to avoid an error-reporting loop) so the
        caller still gets back the incident ID and details.
        """
        message = str(exception) or exception.__class__.__name__
        exception_type = type(exception).__name__
        traceback_text = "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )
        return self._record_incident(
            title=title,
            message=message,
            severity=severity,
            source=source,
            exception_type=exception_type,
            traceback_text=traceback_text,
            capture=capture,
        )

    def log_manual(
        self,
        title: str,
        description: str,
        severity: str = "INFO",
        source: Optional[str] = None,
        capture: Optional[bool] = None,
    ) -> Incident:
        """Record a manually reported incident, with no exception involved."""
        return self._record_incident(
            title=title,
            message=description,
            severity=severity,
            source=source,
            exception_type=None,
            traceback_text=None,
            capture=capture,
        )

    def _record_incident(
        self,
        title: str,
        message: str,
        severity: str,
        source: Optional[str],
        exception_type: Optional[str],
        traceback_text: Optional[str],
        capture: Optional[bool],
    ) -> Incident:
        severity = severity.upper() if severity else "ERROR"
        if severity not in VALID_SEVERITIES:
            severity = "ERROR"

        incident_id = storage.generate_incident_id()
        should_capture = self.config.screenshot_enabled if capture is None else capture

        screenshot_path: Optional[Path] = None
        if should_capture:
            # capture_screenshot() never raises -- failures are handled
            # internally and reported as None, so they never prevent the
            # incident below from being saved.
            screenshot_path = capture_screenshot(incident_id, self.screenshots_dir)

        incident = Incident(
            incident_id=incident_id,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            severity=severity,
            title=title,
            message=message,
            exception_type=exception_type,
            traceback_text=traceback_text,
            screenshot_path=str(screenshot_path) if screenshot_path else None,
            process_id=diagnostics.get_current_process_id(),
            foreground_window_title=diagnostics.get_foreground_window_title(),
            foreground_process_name=diagnostics.get_foreground_process_name(),
            source=source,
        )

        with self._lock:
            try:
                storage.write_incident_json(self.incidents_dir, incident.to_dict())
            except Exception:
                # Never let a storage failure raise back into caller code,
                # and never call back into this logger (that would recurse).
                # Report it through the plain operational logger only.
                self._logger.error(
                    "Failed to save incident %s to disk.", incident_id, exc_info=True
                )

            log_fn = getattr(self._logger, severity.lower(), self._logger.error)
            log_fn(
                "Incident %s [%s] %s: %s",
                incident.incident_id,
                incident.severity,
                incident.title,
                incident.message,
            )

        return incident

    def list_recent(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """Return recent incident records (newest first), skipping any
        malformed incident files."""
        records = storage.list_incidents(self.incidents_dir)
        return records[:limit] if limit else records

    def count(self) -> int:
        """Return the total number of incident files currently stored."""
        return len(storage.list_incident_files(self.incidents_dir))

    def apply_retention(self) -> int:
        """Delete expired incidents/screenshots if retention is configured
        (a no-op, returning 0, when retention is disabled)."""
        return storage.delete_expired(
            self.incidents_dir, self.screenshots_dir, self.config.retention_days
        )
