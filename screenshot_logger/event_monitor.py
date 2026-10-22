"""Background Windows Event Log monitor.

Watches the **Application** and **System** Windows Event Logs for new
Error/Critical entries and reports each one as an incident through the
existing :class:`~screenshot_logger.incident_logger.IncidentLogger` --
re-using its screenshot capture, JSON storage, and operational logging
exactly as-is. No new incident schema, no database.

Events are read with PowerShell's ``Get-WinEvent`` cmdlet, run as a short
-lived subprocess. This avoids adding a new Python dependency (no
``pywin32`` required) and, because only the Application and System logs
are read (never the Security log), it works for a standard, non-admin
user -- no elevation is requested or required.

This module only ever *reads* event logs; it never deletes, clears, or
modifies them, and it never runs unless explicitly started (see
:class:`EventLogMonitor` and ``main.py --monitor`` / the GUI's
"Start Monitoring" button).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as config_module
from .incident_logger import IncidentLogger

_logger = logging.getLogger("screenshot_logger")

DEFAULT_LOG_NAMES: tuple[str, ...] = ("Application", "System")
DEFAULT_LEVELS: tuple[int, ...] = (1, 2)  # Windows event levels: 1=Critical, 2=Error
DEFAULT_POLL_SECONDS = 30
STATE_FILE = config_module.LOGS_DIR / "event_monitor_state.json"

_LEVEL_TO_SEVERITY = {
    1: "CRITICAL",
    2: "ERROR",
    3: "WARNING",
    4: "INFO",
    5: "INFO",
}


_DOTNET_DATE_RE = re.compile(r"^/Date\((\d+)\)/$")


def _format_time_created(raw_value: str) -> str:
    """Make a ``TimeCreated`` value from PowerShell human-readable.

    Windows PowerShell 5.1's ``ConvertTo-Json`` renders .NET ``DateTime``
    values in the legacy ``/Date(<milliseconds-since-epoch>)/`` format
    rather than ISO 8601. This converts that into a plain local
    timestamp; any other format (e.g. an ISO string, from PowerShell 7)
    is returned unchanged.
    """
    match = _DOTNET_DATE_RE.match(raw_value.strip())
    if not match:
        return raw_value
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return raw_value


def map_level_to_severity(level: int) -> str:
    """Map a Windows Event Log level (1=Critical .. 5=Verbose) to one of
    this application's incident severities. Unknown levels default to
    ERROR rather than being silently dropped."""
    return _LEVEL_TO_SEVERITY.get(level, "ERROR")


@dataclass
class RawEvent:
    """One Windows Event Log entry, as returned by ``Get-WinEvent``."""

    log_name: str
    record_id: int
    event_id: int
    level: int
    provider: str
    message: str
    time_created_text: Optional[str]

    def severity(self) -> str:
        return map_level_to_severity(self.level)


def parse_powershell_events(raw_json: str) -> list[RawEvent]:
    """Parse the JSON text produced by the PowerShell query into
    :class:`RawEvent` objects.

    Tolerates a single JSON object (``Get-WinEvent`` returns a bare object,
    not a one-item array, when exactly one event matches) as well as a
    JSON array of several events. Any entry missing the fields needed to
    safely identify and de-duplicate it is skipped rather than failing the
    whole batch; empty or invalid input returns an empty list instead of
    raising.
    """
    raw_json = (raw_json or "").strip()
    if not raw_json:
        return []

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        _logger.warning("Could not parse Windows Event Log query output as JSON.")
        return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    events: list[RawEvent] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            events.append(
                RawEvent(
                    log_name=str(entry["LogName"]),
                    record_id=int(entry["RecordId"]),
                    event_id=int(entry.get("Id") or 0),
                    level=int(entry.get("Level") or 2),
                    provider=str(entry.get("ProviderName") or "Unknown"),
                    message=str(entry.get("Message") or "(no message provided)"),
                    time_created_text=(
                        _format_time_created(str(entry["TimeCreated"]))
                        if entry.get("TimeCreated")
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            _logger.debug("Skipping malformed event log entry: %r", entry)
            continue
    return events


def filter_new_events(events: list[RawEvent], last_record_ids: dict[str, int]) -> list[RawEvent]:
    """Return only events that are newer than the last one already
    processed for their log, sorted oldest-first.

    This is the duplicate-filtering logic: consecutive polls use
    overlapping time windows on purpose (to never miss an event near the
    boundary), so the same event can appear in more than one query result.
    Comparing against the highest ``RecordId`` already handled per log
    (``0`` for a log that has never been seen before) ensures each event
    is only ever turned into one incident.
    """
    new_events = [
        event for event in events if event.record_id > last_record_ids.get(event.log_name, 0)
    ]
    return sorted(new_events, key=lambda event: (event.log_name, event.record_id))


def build_incident_fields(event: RawEvent) -> tuple[str, str, str, str]:
    """Build the ``(title, description, severity, source)`` to pass into
    :meth:`IncidentLogger.log_manual` for one event."""
    title = f"{event.log_name} log: {event.provider} (Event ID {event.event_id})"
    description = (
        f"Event ID: {event.event_id}\n"
        f"Provider: {event.provider}\n"
        f"Log: {event.log_name}\n"
        f"Record ID: {event.record_id}\n"
        f"Event time: {event.time_created_text or 'unknown'}\n\n"
        f"{event.message}"
    )
    source = f"Windows Event Log ({event.log_name})"
    return title, description, event.severity(), source


def _run_powershell(script: str, timeout: int = 30) -> str:
    """Run a PowerShell script and return its stdout text.

    Never raises: any failure (PowerShell missing, access denied, timeout,
    ...) is logged and reported as an empty string, which callers treat as
    "nothing to report" rather than crashing the monitor loop.
    """
    try:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creation_flags,
        )
    except Exception:
        _logger.warning("Could not run PowerShell for Windows Event Log access.", exc_info=True)
        return ""

    if result.returncode != 0 and result.stderr:
        _logger.debug("PowerShell exited with code %s: %s", result.returncode, result.stderr.strip())
    return result.stdout


def _run_powershell_query(
    log_names: tuple[str, ...], levels: tuple[int, ...], lookback_seconds: int, max_events: int = 200
) -> str:
    """Query recent matching events from the given logs via ``Get-WinEvent``."""
    log_name_list = ",".join(f"'{name}'" for name in log_names)
    level_list = ",".join(str(level) for level in levels)
    script = (
        "$events = Get-WinEvent -FilterHashtable @{"
        f"LogName={log_name_list}; Level={level_list}; "
        f"StartTime=(Get-Date).AddSeconds(-{lookback_seconds})"
        "} -MaxEvents " + str(max_events) + " -ErrorAction SilentlyContinue; "
        "if ($events) { $events | Select-Object LogName, RecordId, Id, Level, "
        "ProviderName, Message, TimeCreated | ConvertTo-Json -Depth 3 }"
    )
    return _run_powershell(script)


def _get_latest_record_id(log_name: str) -> int:
    """Return the most recent RecordId currently in a log, or 0 if the log
    is empty or could not be read. Used only to establish a starting
    baseline so monitoring reports new events going forward, instead of
    flooding incidents for a log's entire pre-existing history."""
    script = (
        f"$e = Get-WinEvent -LogName '{log_name}' -MaxEvents 1 -ErrorAction SilentlyContinue; "
        "if ($e) { $e | Select-Object RecordId | ConvertTo-Json }"
    )
    raw = _run_powershell(script).strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
        return int(data["RecordId"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0


class EventLogMonitor:
    """Polls the Application and System Windows Event Logs for new
    Error/Critical entries and reports each as an incident.

    Runs on its own background thread; call :meth:`start` / :meth:`stop`
    to control it (mirrors :class:`~screenshot_logger.hotkey_service.HotkeyService`).
    Never does anything unless explicitly started.
    """

    def __init__(
        self,
        incident_logger: IncidentLogger,
        log_names: tuple[str, ...] = DEFAULT_LOG_NAMES,
        levels: tuple[int, ...] = DEFAULT_LEVELS,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        state_file: Optional[Path] = None,
    ) -> None:
        self.incident_logger = incident_logger
        self.log_names = log_names
        self.levels = levels
        self.poll_seconds = max(5, poll_seconds)
        self.state_file = state_file or STATE_FILE
        self._last_record_ids: dict[str, int] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start polling on a background thread. Safe to call if already
        running (it is then a no-op)."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._last_record_ids = self._load_state()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _logger.info(
            "Event log monitor started (logs=%s, levels=%s, poll=%ss).",
            self.log_names,
            self.levels,
            self.poll_seconds,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop polling and wait for the background thread to exit."""
        if not self.is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        _logger.info("Event log monitor stopped.")

    def _run(self) -> None:
        if not self._last_record_ids:
            self._initialize_baseline()
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                # A single bad poll must never kill the monitoring loop.
                _logger.warning("Event log monitor poll failed.", exc_info=True)
            self._stop_event.wait(self.poll_seconds)

    def _initialize_baseline(self) -> None:
        for log_name in self.log_names:
            self._last_record_ids[log_name] = _get_latest_record_id(log_name)
        self._save_state()
        _logger.info("Event log monitor baseline initialized: %s", self._last_record_ids)

    def _poll_once(self) -> None:
        # Look back further than the poll interval so a slow poll or a
        # missed tick can never create a gap where an event is lost.
        lookback_seconds = max(self.poll_seconds * 3, 60)
        raw_json = _run_powershell_query(self.log_names, self.levels, lookback_seconds)
        events = parse_powershell_events(raw_json)
        new_events = filter_new_events(events, self._last_record_ids)

        for event in new_events:
            self._report_event(event)
            self._last_record_ids[event.log_name] = max(
                self._last_record_ids.get(event.log_name, 0), event.record_id
            )

        if new_events:
            self._save_state()

    def _report_event(self, event: RawEvent) -> None:
        title, description, severity, source = build_incident_fields(event)
        incident = self.incident_logger.log_manual(
            title=title, description=description, severity=severity, source=source
        )
        _logger.info("Event log incident recorded: %s (%s)", incident.incident_id, title)

    def _load_state(self) -> dict[str, int]:
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                return {str(key): int(value) for key, value in data.items()}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            _logger.warning("Could not read event monitor state file; starting fresh.", exc_info=True)
        return {}

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(self._last_record_ids, indent=2), encoding="utf-8"
            )
        except OSError:
            _logger.warning("Could not save event monitor state file.", exc_info=True)
