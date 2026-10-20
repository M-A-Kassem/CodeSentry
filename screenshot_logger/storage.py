"""Local file storage for incident JSON records.

No database, no server -- incidents are plain UTF-8 JSON files on disk
inside the application's own ``data/incidents`` directory, one file per
incident. This module is the only place that touches those files, so
reliability concerns (atomic writes, malformed files, retention) live in
one spot.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("screenshot_logger")


def generate_incident_id() -> str:
    """Generate a unique incident ID combining a sortable timestamp and a
    short UUID suffix.

    The timestamp keeps IDs human-readable and roughly time-ordered; the
    UUID suffix guarantees uniqueness even if two incidents are created
    within the same microsecond (e.g. from two threads at once).
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def incident_file_path(incidents_dir: Path, incident_id: str) -> Path:
    """Return the JSON file path for a given incident ID."""
    return incidents_dir / f"{incident_id}.json"


def write_incident_json(incidents_dir: Path, record: dict[str, Any]) -> Path:
    """Write an incident record to disk as UTF-8 JSON.

    Writes to a temporary file first and then renames it into place, so a
    crash or interruption mid-write can never leave a half-written,
    unreadable incident file behind.
    """
    incidents_dir.mkdir(parents=True, exist_ok=True)
    incident_id = record["incident_id"]
    final_path = incident_file_path(incidents_dir, incident_id)
    tmp_path = final_path.with_suffix(".json.tmp")

    tmp_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp_path.replace(final_path)
    return final_path


def read_incident_json(path: Path) -> Optional[dict[str, Any]]:
    """Read a single incident JSON file.

    Returns ``None`` (instead of raising) if the file is missing, not valid
    JSON, or not valid UTF-8, so one corrupted record can never break the
    whole incident list.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _logger.warning("Could not read incident file: %s", path, exc_info=True)
        return None


def list_incident_files(incidents_dir: Path) -> list[Path]:
    """Return all incident JSON files, newest first.

    Filenames start with a sortable timestamp, so reverse alphabetical sort
    is also reverse chronological.
    """
    if not incidents_dir.exists():
        return []
    return sorted(incidents_dir.glob("*.json"), reverse=True)


def list_incidents(incidents_dir: Path) -> list[dict[str, Any]]:
    """Read every readable incident record (newest first), skipping any
    malformed files rather than failing the whole listing."""
    records = []
    for path in list_incident_files(incidents_dir):
        record = read_incident_json(path)
        if record is not None:
            records.append(record)
    return records


def delete_expired(incidents_dir: Path, screenshots_dir: Path, retention_days: int) -> int:
    """Delete incidents (and their screenshots) older than ``retention_days``.

    Disabled by default: returns 0 without deleting anything when
    ``retention_days`` is 0 or negative. Only ever deletes files that live
    inside ``incidents_dir`` or ``screenshots_dir``, and only deletes a
    screenshot when the incident that references it is itself being
    deleted (so a screenshot still referenced by a retained incident is
    never removed).

    Returns the number of incidents deleted.
    """
    if retention_days <= 0:
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted_count = 0

    for path in list_incident_files(incidents_dir):
        record = read_incident_json(path)
        if record is None:
            continue
        try:
            timestamp = datetime.fromisoformat(record["timestamp"])
        except (KeyError, ValueError, TypeError):
            continue
        if timestamp >= cutoff:
            continue

        screenshot_path = record.get("screenshot_path")
        if screenshot_path:
            screenshot_file = Path(screenshot_path)
            try:
                if screenshot_file.exists() and screenshots_dir.resolve() in screenshot_file.resolve().parents:
                    screenshot_file.unlink()
            except OSError:
                _logger.warning("Could not delete screenshot: %s", screenshot_file, exc_info=True)

        try:
            if incidents_dir.resolve() in path.resolve().parents:
                path.unlink()
                deleted_count += 1
        except OSError:
            _logger.warning("Could not delete incident file: %s", path, exc_info=True)

    return deleted_count
