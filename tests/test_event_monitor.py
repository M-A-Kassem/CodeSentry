"""Tests for the Windows Event Log monitor: parsing PowerShell's JSON
output, duplicate filtering, severity mapping, and incident field
building.

None of these tests touch the real Windows Event Log or spawn a real
PowerShell process -- ``_run_powershell_query`` / ``_get_latest_record_id``
are monkeypatched wherever a monitor instance is exercised, so the suite
runs the same way on any machine and never depends on what is actually in
the local Application/System logs.
"""
from __future__ import annotations

import json

from screenshot_logger import event_monitor as event_monitor_module
from screenshot_logger import incident_logger as incident_logger_module
from screenshot_logger.config import AppConfig
from screenshot_logger.event_monitor import (
    EventLogMonitor,
    RawEvent,
    build_incident_fields,
    filter_new_events,
    map_level_to_severity,
    parse_powershell_events,
)
from screenshot_logger.incident_logger import IncidentLogger


def _make_logger(tmp_path, monkeypatch):
    # Never take a real screenshot while testing the event monitor.
    monkeypatch.setattr(
        incident_logger_module, "capture_screenshot", lambda incident_id, directory: None
    )
    config = AppConfig(screenshot_enabled=True)
    return IncidentLogger(
        config=config,
        incidents_dir=tmp_path / "incidents",
        screenshots_dir=tmp_path / "screenshots",
    )


# -- parse_powershell_events ------------------------------------------------


def test_parse_powershell_events_handles_single_object():
    # Get-WinEvent | ConvertTo-Json returns a bare object (not a 1-item
    # array) when exactly one event matches -- this is the classic
    # PowerShell gotcha the parser must handle.
    raw = json.dumps(
        {
            "LogName": "Application",
            "RecordId": 101,
            "Id": 1000,
            "Level": 2,
            "ProviderName": "Application Error",
            "Message": "Faulting application notepad.exe",
            "TimeCreated": "2026-10-17T10:00:00",
        }
    )

    events = parse_powershell_events(raw)

    assert len(events) == 1
    assert events[0].record_id == 101
    assert events[0].log_name == "Application"
    assert events[0].severity() == "ERROR"


def test_parse_powershell_events_handles_array():
    raw = json.dumps(
        [
            {"LogName": "System", "RecordId": 5, "Id": 41, "Level": 1, "ProviderName": "Kernel-Power", "Message": "msg1"},
            {"LogName": "System", "RecordId": 6, "Id": 41, "Level": 1, "ProviderName": "Kernel-Power", "Message": "msg2"},
        ]
    )

    events = parse_powershell_events(raw)

    assert [event.record_id for event in events] == [5, 6]


def test_parse_powershell_events_skips_malformed_entries():
    raw = json.dumps(
        [
            {"LogName": "Application", "RecordId": 1, "Id": 1000, "Level": 2, "Message": "ok"},
            {"LogName": "Application", "Id": 1000, "Level": 2, "Message": "missing RecordId"},
        ]
    )

    events = parse_powershell_events(raw)

    assert len(events) == 1
    assert events[0].record_id == 1


def test_parse_powershell_events_handles_empty_and_invalid_input():
    assert parse_powershell_events("") == []
    assert parse_powershell_events("   ") == []
    assert parse_powershell_events("{not valid json") == []


def test_parse_powershell_events_converts_dotnet_date_format():
    # Windows PowerShell 5.1's ConvertTo-Json renders DateTime as
    # /Date(<ms-since-epoch>)/ rather than ISO 8601 -- confirmed against
    # this machine's real Application log output.
    raw = json.dumps(
        {
            "LogName": "Application",
            "RecordId": 1,
            "Id": 1000,
            "Level": 2,
            "ProviderName": "Test",
            "Message": "msg",
            "TimeCreated": "/Date(1700000000000)/",
        }
    )

    events = parse_powershell_events(raw)

    assert events[0].time_created_text is not None
    assert "/Date(" not in events[0].time_created_text


def test_parse_powershell_events_leaves_non_dotnet_date_unchanged():
    raw = json.dumps(
        {
            "LogName": "Application",
            "RecordId": 1,
            "Id": 1000,
            "Level": 2,
            "ProviderName": "Test",
            "Message": "msg",
            "TimeCreated": "2026-10-17T10:00:00",
        }
    )

    events = parse_powershell_events(raw)

    assert events[0].time_created_text == "2026-10-17T10:00:00"


# -- filter_new_events -------------------------------------------------------


def test_filter_new_events_excludes_already_seen_record_ids():
    events = [
        RawEvent("Application", 10, 1000, 2, "Provider", "msg", None),
        RawEvent("Application", 11, 1000, 2, "Provider", "msg", None),
    ]

    new_events = filter_new_events(events, last_record_ids={"Application": 11})

    assert new_events == []


def test_filter_new_events_includes_newer_and_sorts_oldest_first():
    events = [
        RawEvent("Application", 15, 1000, 2, "Provider", "msg", None),
        RawEvent("Application", 12, 1000, 2, "Provider", "msg", None),
        RawEvent("Application", 13, 1000, 2, "Provider", "msg", None),
    ]

    new_events = filter_new_events(events, last_record_ids={"Application": 11})

    assert [event.record_id for event in new_events] == [12, 13, 15]


def test_filter_new_events_treats_unseen_log_as_baseline_zero():
    events = [RawEvent("System", 3, 41, 1, "Kernel-Power", "msg", None)]

    new_events = filter_new_events(events, last_record_ids={})

    assert len(new_events) == 1


# -- map_level_to_severity ---------------------------------------------------


def test_map_level_to_severity_known_levels():
    assert map_level_to_severity(1) == "CRITICAL"
    assert map_level_to_severity(2) == "ERROR"
    assert map_level_to_severity(3) == "WARNING"
    assert map_level_to_severity(4) == "INFO"


def test_map_level_to_severity_unknown_level_defaults_to_error():
    assert map_level_to_severity(99) == "ERROR"


# -- build_incident_fields ---------------------------------------------------


def test_build_incident_fields_contains_key_details():
    event = RawEvent(
        log_name="Application",
        record_id=42,
        event_id=1000,
        level=2,
        provider="Application Error",
        message="Faulting application notepad.exe",
        time_created_text="2026-10-17T10:00:00",
    )

    title, description, severity, source = build_incident_fields(event)

    assert "Application Error" in title
    assert "1000" in title
    assert "42" in description
    assert "Faulting application notepad.exe" in description
    assert severity == "ERROR"
    assert "Application" in source


# -- EventLogMonitor: duplicate filtering + lifecycle ------------------------


def test_monitor_does_not_report_the_same_event_twice(tmp_path, monkeypatch):
    logger = _make_logger(tmp_path, monkeypatch)
    raw_json = json.dumps(
        {
            "LogName": "Application",
            "RecordId": 7,
            "Id": 1000,
            "Level": 2,
            "ProviderName": "Application Error",
            "Message": "Faulting application demo.exe",
            "TimeCreated": "2026-10-17T10:00:00",
        }
    )
    monkeypatch.setattr(event_monitor_module, "_run_powershell_query", lambda *a, **k: raw_json)

    monitor = EventLogMonitor(logger, state_file=tmp_path / "state.json")
    monitor._last_record_ids = {"Application": 0}  # skip baseline initialization

    monitor._poll_once()
    monitor._poll_once()  # same canned event again -- must be filtered out

    assert logger.count() == 1


def test_monitor_state_persists_across_instances(tmp_path, monkeypatch):
    logger = _make_logger(tmp_path, monkeypatch)
    state_file = tmp_path / "state.json"

    monitor_1 = EventLogMonitor(logger, state_file=state_file)
    monitor_1._last_record_ids = {"Application": 9, "System": 3}
    monitor_1._save_state()

    monitor_2 = EventLogMonitor(logger, state_file=state_file)
    loaded = monitor_2._load_state()

    assert loaded == {"Application": 9, "System": 3}


def test_monitor_start_and_stop_lifecycle(tmp_path, monkeypatch):
    logger = _make_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(event_monitor_module, "_run_powershell_query", lambda *a, **k: "")
    monkeypatch.setattr(event_monitor_module, "_get_latest_record_id", lambda log_name: 0)

    monitor = EventLogMonitor(logger, poll_seconds=5, state_file=tmp_path / "state.json")

    assert not monitor.is_running
    monitor.start()
    assert monitor.is_running
    monitor.stop(timeout=5)
    assert not monitor.is_running
