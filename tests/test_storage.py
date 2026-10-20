"""Tests for the local JSON storage layer (no database involved)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from screenshot_logger import storage


def test_generate_incident_id_is_unique():
    ids = {storage.generate_incident_id() for _ in range(100)}
    assert len(ids) == 100


def test_write_and_read_incident_json_round_trip(tmp_path: Path):
    incidents_dir = tmp_path / "incidents"
    record = {
        "incident_id": "20260101120000000000-abc12345",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "severity": "ERROR",
        "title": "Unicode check: café ☃ 你好",
        "message": "boom",
    }

    saved_path = storage.write_incident_json(incidents_dir, record)

    assert saved_path.exists()
    loaded = storage.read_incident_json(saved_path)
    assert loaded == record


def test_read_incident_json_handles_malformed_file(tmp_path: Path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{this is not json", encoding="utf-8")

    assert storage.read_incident_json(bad_file) is None


def test_read_incident_json_handles_missing_file(tmp_path: Path):
    assert storage.read_incident_json(tmp_path / "missing.json") is None


def test_list_incidents_skips_malformed_entries(tmp_path: Path):
    incidents_dir = tmp_path / "incidents"
    incidents_dir.mkdir(parents=True)
    storage.write_incident_json(
        incidents_dir,
        {
            "incident_id": "good-1",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "severity": "INFO",
            "title": "Fine",
        },
    )
    (incidents_dir / "broken.json").write_text("not json at all", encoding="utf-8")

    records = storage.list_incidents(incidents_dir)

    assert len(records) == 1
    assert records[0]["incident_id"] == "good-1"


def test_delete_expired_disabled_by_default(tmp_path: Path):
    incidents_dir = tmp_path / "incidents"
    screenshots_dir = tmp_path / "screenshots"
    old_timestamp = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    storage.write_incident_json(
        incidents_dir,
        {"incident_id": "old-1", "timestamp": old_timestamp, "severity": "INFO", "title": "Old"},
    )

    deleted = storage.delete_expired(incidents_dir, screenshots_dir, retention_days=0)

    assert deleted == 0
    assert (incidents_dir / "old-1.json").exists()


def test_delete_expired_removes_old_incidents_and_screenshots(tmp_path: Path):
    incidents_dir = tmp_path / "incidents"
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir(parents=True)
    screenshot_path = screenshots_dir / "old.png"
    screenshot_path.write_bytes(b"fake png data")

    old_timestamp = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    storage.write_incident_json(
        incidents_dir,
        {
            "incident_id": "old-1",
            "timestamp": old_timestamp,
            "severity": "INFO",
            "title": "Old",
            "screenshot_path": str(screenshot_path),
        },
    )
    recent_timestamp = datetime.now().isoformat(timespec="seconds")
    storage.write_incident_json(
        incidents_dir,
        {
            "incident_id": "recent-1",
            "timestamp": recent_timestamp,
            "severity": "INFO",
            "title": "Recent",
        },
    )

    deleted = storage.delete_expired(incidents_dir, screenshots_dir, retention_days=1)

    assert deleted == 1
    assert not (incidents_dir / "old-1.json").exists()
    assert not screenshot_path.exists()
    assert (incidents_dir / "recent-1.json").exists()
