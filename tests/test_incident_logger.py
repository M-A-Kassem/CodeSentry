"""Tests for IncidentLogger: exception capture, manual capture, and the
'a failed screenshot must never block a saved incident' guarantee.

Screenshot capture is mocked everywhere in this file (by replacing
``capture_screenshot`` in the ``incident_logger`` module's namespace), so
running these tests never takes a real screenshot of the developer's or
CI machine's desktop.
"""
from __future__ import annotations

import json
from pathlib import Path

from screenshot_logger import incident_logger as incident_logger_module
from screenshot_logger.config import AppConfig
from screenshot_logger.incident_logger import IncidentLogger


def _make_logger(tmp_path: Path, **config_overrides) -> IncidentLogger:
    config = AppConfig(screenshot_enabled=True, retention_days=0, hotkey_enabled=False)
    for key, value in config_overrides.items():
        setattr(config, key, value)
    return IncidentLogger(
        config=config,
        incidents_dir=tmp_path / "incidents",
        screenshots_dir=tmp_path / "screenshots",
    )


def test_log_exception_creates_incident_record(tmp_path, monkeypatch):
    monkeypatch.setattr(
        incident_logger_module, "capture_screenshot", lambda incident_id, directory: None
    )
    logger = _make_logger(tmp_path)

    try:
        1 / 0
    except ZeroDivisionError as exc:
        incident = logger.log_exception(title="Calculation failed", exception=exc)

    assert incident.exception_type == "ZeroDivisionError"
    assert incident.traceback_text and "ZeroDivisionError" in incident.traceback_text

    saved_path = tmp_path / "incidents" / f"{incident.incident_id}.json"
    assert saved_path.exists()
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved["incident_id"] == incident.incident_id
    assert saved["severity"] == "ERROR"


def test_log_manual_creates_incident_record(tmp_path, monkeypatch):
    monkeypatch.setattr(
        incident_logger_module, "capture_screenshot", lambda incident_id, directory: None
    )
    logger = _make_logger(tmp_path)

    incident = logger.log_manual(
        title="Manual check", description="Looked fine to the operator.", severity="info"
    )

    assert incident.severity == "INFO"
    assert incident.exception_type is None
    assert incident.traceback_text is None


def test_failed_screenshot_does_not_block_incident(tmp_path, monkeypatch):
    # Simulate a screenshot capture that was unable to produce a file --
    # the logger must still save the incident without a screenshot_path.
    monkeypatch.setattr(
        incident_logger_module, "capture_screenshot", lambda incident_id, directory: None
    )
    logger = _make_logger(tmp_path, screenshot_enabled=True)

    incident = logger.log_manual(title="No screenshot available", description="n/a")

    assert incident.screenshot_path is None
    saved_path = tmp_path / "incidents" / f"{incident.incident_id}.json"
    assert saved_path.exists()


def test_incident_ids_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(
        incident_logger_module, "capture_screenshot", lambda incident_id, directory: None
    )
    logger = _make_logger(tmp_path)

    incidents = [logger.log_manual(title=f"Incident {i}", description="x") for i in range(20)]
    ids = [incident.incident_id for incident in incidents]

    assert len(ids) == 20
    assert len(set(ids)) == 20


def test_list_recent_skips_malformed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        incident_logger_module, "capture_screenshot", lambda incident_id, directory: None
    )
    logger = _make_logger(tmp_path)
    logger.log_manual(title="Valid incident", description="ok")

    malformed = tmp_path / "incidents" / "broken.json"
    malformed.write_text("{not valid json", encoding="utf-8")

    records = logger.list_recent()

    assert len(records) == 1
    assert records[0]["title"] == "Valid incident"


def test_successful_screenshot_path_is_recorded(tmp_path, monkeypatch):
    fake_screenshot = tmp_path / "screenshots" / "fake.png"

    def fake_capture(incident_id: str, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        fake_screenshot.write_bytes(b"fake png bytes")
        return fake_screenshot

    monkeypatch.setattr(incident_logger_module, "capture_screenshot", fake_capture)
    logger = _make_logger(tmp_path, screenshot_enabled=True)

    incident = logger.log_manual(title="With screenshot", description="ok")

    assert incident.screenshot_path == str(fake_screenshot)
