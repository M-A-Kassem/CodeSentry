"""Visual Studio Error List provider: parsing the EnvDTE helper's output and
handling busy/closed/unreachable instances. Uses a fake poller; the live
COM test at the bottom only runs when explicitly requested."""
from __future__ import annotations

import json
import os

import pytest

from screenshot_logger.coding.app_detector import KNOWN_EDITORS
from screenshot_logger.coding.issue_tracker import IssueTracker
from screenshot_logger.coding.providers.visual_studio import (
    FAILURES_BEFORE_STALE,
    PowerShellDtePoller,
    VisualStudioProvider,
    parse_dte_output,
)

MONIKER = "!VisualStudio.DTE.17.0:9016"


def helper_output(items, ok=True, instance_ok=True, moniker=MONIKER):
    instance = {"moniker": moniker, "ok": instance_ok, "solution": "D:\\code\\App.sln", "items": items}
    if not instance_ok:
        instance = {"moniker": moniker, "ok": False, "error": "Call was rejected by callee. (RPC_E_CALL_REJECTED)"}
    return json.dumps({"ok": ok, "instances": [instance]})


ERROR_ITEM = {"level": 4, "description": "The name 'foo' does not exist in the current context",
              "file": "D:\\code\\Program.cs", "line": 12, "column": 17, "project": "App"}
WARNING_ITEM = {"level": 2, "description": "The variable 'x' is assigned but its value is never used",
                "file": "D:\\code\\Program.cs", "line": 3, "column": 9, "project": "App"}
MESSAGE_ITEM = {"level": 1, "description": "Use 'var'", "file": "D:\\code\\Program.cs", "line": 1, "column": 1, "project": "App"}


class FakePoller:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.closed = 0

    def poll(self):
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed += 1


def make(outputs):
    tracker = IssueTracker(debounce_seconds=0)
    provider = VisualStudioProvider(poller=FakePoller(outputs))
    provider.tracker = tracker
    return provider, tracker


def test_error_levels_map_to_severities():
    # EnvDTE80.vsBuildErrorLevel: High=4, Medium=2, Low=1 (checked against envdte80.dll)
    [instance] = parse_dte_output(helper_output([ERROR_ITEM, WARNING_ITEM, MESSAGE_ITEM]))
    assert [d.severity for d in instance.diagnostics] == ["error", "warning", "info"]
    error = instance.diagnostics[0]
    assert (error.file, error.line, error.column) == ("D:\\code\\Program.cs", 12, 17)
    assert error.language == "C#"
    assert error.source == "App"


def test_powershell_single_object_quirks_are_tolerated():
    # PowerShell 5.1 emits a bare object instead of a one-element array.
    raw = json.dumps({"ok": True, "instances": {"moniker": MONIKER, "ok": True, "solution": "", "items": ERROR_ITEM}})
    [instance] = parse_dte_output(raw)
    assert len(instance.diagnostics) == 1


def test_helper_failure_raises():
    with pytest.raises(ValueError):
        parse_dte_output(json.dumps({"ok": False, "error": "boom"}))
    with pytest.raises(ValueError):
        parse_dte_output("not json")


def test_poll_updates_and_resolves():
    provider, tracker = make([helper_output([]), helper_output([ERROR_ITEM]), helper_output([])])
    provider.poll_once()  # baseline: empty
    provider.poll_once()
    assert tracker.counts().errors == 1 and tracker.counts().new_errors == 1
    assert provider.status().state == "active"
    provider.poll_once()  # user fixed it
    assert tracker.counts().errors == 0


def test_busy_instance_keeps_last_results_as_stale():
    provider, tracker = make([helper_output([ERROR_ITEM]), helper_output([], instance_ok=False)])
    provider.poll_once()
    provider.poll_once()
    assert tracker.counts().errors == 0
    assert tracker.current_issues()[0].stale


def test_transient_failures_do_not_mark_stale_until_threshold():
    failures = [TimeoutError("slow")] * FAILURES_BEFORE_STALE
    provider, tracker = make([helper_output([ERROR_ITEM]), *failures])
    provider.poll_once()
    for _ in range(FAILURES_BEFORE_STALE - 1):
        provider.poll_once()
    assert tracker.counts().errors == 1, "a single slow poll must not drop the count"
    provider.poll_once()
    assert tracker.counts().errors == 0
    assert provider.status().state == "error"


def test_closed_instance_is_cleared():
    other = "!VisualStudio.DTE.17.0:1234"
    both = json.dumps({"ok": True, "instances": [
        {"moniker": MONIKER, "ok": True, "solution": "", "items": [ERROR_ITEM]},
        {"moniker": other, "ok": True, "solution": "", "items": [WARNING_ITEM]},
    ]})
    provider, tracker = make([both, helper_output([ERROR_ITEM])])
    provider.poll_once()
    assert (tracker.counts().errors, tracker.counts().warnings) == (1, 1)
    provider.poll_once()  # second instance closed
    assert (tracker.counts().errors, tracker.counts().warnings) == (1, 0)


def test_unreachable_instance_reports_elevation_hint():
    provider, _ = make([json.dumps({"ok": True, "instances": []})])
    provider.poll_once()
    assert "Administrator" in provider.status().detail


def test_visual_studio_closing_clears_and_releases_helper():
    provider, tracker = make([helper_output([ERROR_ITEM])])
    provider.poll_once()
    provider.on_context(None, {})  # devenv.exe no longer running
    provider._on_not_running()
    assert tracker.current_issues() == []
    assert provider.poller.closed == 1


def test_context_tracks_devenv_process():
    provider, _ = make([])
    provider.on_context(None, {"devenv.exe": KNOWN_EDITORS["devenv.exe"]})
    assert provider._devenv_running


@pytest.mark.skipif(os.environ.get("SEL_LIVE_VS") != "1", reason="set SEL_LIVE_VS=1 with Visual Studio open")
def test_live_visual_studio_error_list():
    poller = PowerShellDtePoller()
    try:
        instances = parse_dte_output(poller.poll())
    finally:
        poller.close()
    assert instances, "no Visual Studio instance found in the Running Object Table"
    assert all(i.ok for i in instances)
