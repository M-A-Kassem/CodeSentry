"""VS Code bridge: payload validation, local-interface security, client
expiry, and an end-to-end run of the real extension.js (under a fake
``vscode`` module, via Node) against the real bridge server."""
from __future__ import annotations

import http.client
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from screenshot_logger.coding.issue_tracker import IssueTracker
from screenshot_logger.coding.providers.vscode_bridge import (
    FORGET_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    PayloadError,
    VSCodeBridgeProvider,
    parse_snapshot,
)

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "vscode-extension"


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def bridge(tmp_path):
    tracker = IssueTracker(debounce_seconds=0)
    clock = Clock()
    provider = VSCodeBridgeProvider(port=0, discovery_file=tmp_path / "vscode-bridge.json", clock=clock)
    provider.start(tracker)
    yield provider, tracker, clock
    provider.stop()


def request(provider, method, path, body=None, headers=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"Content-Type": "application/json"}
    if token:
        all_headers["Authorization"] = f"Bearer {provider.token}"
    all_headers.update(headers or {})
    conn.request(method, path, body=data, headers=all_headers)
    response = conn.getresponse()
    return response.status, json.loads(response.read() or b"{}")


def snapshot(client_id="w1", diagnostics=None):
    return {
        "client": {"id": client_id, "app": "vscode", "appName": "Visual Studio Code", "workspace": "proj", "focused": True},
        "diagnostics": diagnostics if diagnostics is not None else [
            {"file": "C:\\proj\\a.py", "line": 3, "column": 5, "severity": "error", "message": "boom", "source": "Pylance"}
        ],
    }


# -- payload validation --------------------------------------------------------------


def test_parse_snapshot_reads_fields_and_skips_bad_entries():
    client_id, info, diagnostics = parse_snapshot(
        snapshot(diagnostics=[
            {"file": "C:\\a.ts", "line": 2, "column": 1, "severity": "warning", "message": "w", "code": "6133", "language": "typescript"},
            {"severity": "error"},  # no message: skipped
            "not an object",
        ])
    )
    assert client_id == "w1"
    assert info["workspace"] == "proj"
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "warning"
    assert diagnostics[0].code == "6133"
    assert diagnostics[0].application == "vscode"


@pytest.mark.parametrize("payload", [[], {"diagnostics": []}, {"client": {}}, {"client": {"id": "x"}, "diagnostics": "no"}])
def test_parse_snapshot_rejects_malformed_bodies(payload):
    with pytest.raises(PayloadError):
        parse_snapshot(payload)


# -- security of the local interface ------------------------------------------------------


def test_discovery_file_has_port_and_token_and_is_removed_on_stop(tmp_path):
    provider = VSCodeBridgeProvider(discovery_file=tmp_path / "d.json")
    provider.start(IssueTracker())
    data = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert data["port"] == provider.port and data["token"] == provider.token
    assert len(provider.token) >= 32
    provider.stop()
    assert not (tmp_path / "d.json").exists()


def test_token_changes_on_every_start(tmp_path):
    provider = VSCodeBridgeProvider(discovery_file=tmp_path / "d.json")
    provider.start(IssueTracker())
    first = provider.token
    provider.stop()
    provider.start(IssueTracker())
    assert provider.token != first
    provider.stop()


def test_server_listens_on_loopback_only(bridge):
    provider, _, _ = bridge
    assert provider._server.server_address[0] == "127.0.0.1"


def test_missing_or_wrong_token_is_rejected(bridge):
    provider, tracker, _ = bridge
    assert request(provider, "POST", "/v1/diagnostics", snapshot(), token=False)[0] == 401
    assert request(provider, "POST", "/v1/diagnostics", snapshot(), token=False,
                   headers={"Authorization": "Bearer wrong"})[0] == 401
    assert tracker.counts().errors == 0


def test_browser_origin_is_rejected(bridge):
    provider, tracker, _ = bridge
    status, _ = request(provider, "POST", "/v1/diagnostics", snapshot(), headers={"Origin": "https://evil.example"})
    assert status == 403
    assert tracker.counts().errors == 0


def test_dns_rebinding_host_is_rejected(bridge):
    provider, _, _ = bridge
    status, _ = request(provider, "GET", "/v1/health", headers={"Host": f"evil.example:{provider.port}"})
    assert status == 403


def test_wrong_content_type_and_oversized_bodies_are_rejected(bridge):
    provider, _, _ = bridge
    assert request(provider, "POST", "/v1/diagnostics", snapshot(), headers={"Content-Type": "text/plain"})[0] == 415
    assert request(provider, "POST", "/v1/diagnostics", snapshot(), headers={"Content-Length": str(10**9)})[0] == 413


def test_invalid_json_is_a_400_not_a_crash(bridge):
    provider, _, _ = bridge
    conn = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=5)
    conn.request("POST", "/v1/diagnostics", body=b"{nope", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {provider.token}"})
    assert conn.getresponse().status == 400
    assert request(provider, "GET", "/v1/health")[0] == 200  # still serving


# -- snapshots, goodbye, expiry ---------------------------------------------------------


def test_snapshot_updates_tracker_and_fix_resolves(bridge):
    provider, tracker, _ = bridge
    assert request(provider, "POST", "/v1/diagnostics", snapshot())[0] == 200
    assert tracker.counts().errors == 1
    assert "1 window(s) connected" in provider.status().detail
    request(provider, "POST", "/v1/diagnostics", snapshot(diagnostics=[]))
    assert tracker.counts().errors == 0


def test_goodbye_clears_that_window(bridge):
    provider, tracker, _ = bridge
    request(provider, "POST", "/v1/diagnostics", snapshot("w1"))
    request(provider, "POST", "/v1/diagnostics", snapshot("w2", [
        {"file": "C:\\b.py", "line": 1, "severity": "error", "message": "other"}]))
    assert tracker.counts().errors == 2
    request(provider, "POST", "/v1/goodbye", {"client": {"id": "w1"}})
    assert tracker.counts().errors == 1


def test_silent_window_goes_stale_then_is_forgotten(bridge):
    provider, tracker, clock = bridge
    request(provider, "POST", "/v1/diagnostics", snapshot())
    clock.now += STALE_AFTER_SECONDS + 1
    provider.expire_clients()
    assert tracker.counts().errors == 0 and tracker.counts().stale_scopes == 1
    clock.now += FORGET_AFTER_SECONDS
    provider.expire_clients()
    assert tracker.current_issues() == []
    assert provider.connected_clients() == {}


def test_port_in_use_is_reported_as_start_failure(bridge, tmp_path):
    provider, _, _ = bridge
    second = VSCodeBridgeProvider(port=provider.port, discovery_file=tmp_path / "other.json")
    with pytest.raises(OSError):
        second.start(IssueTracker())
    assert second.status().state == "error"


# -- end to end with the real extension code ------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_real_extension_code_reports_to_real_bridge(bridge):
    provider, tracker, _ = bridge
    script = str(EXTENSION_DIR / "test" / "e2e_send.js")
    node = subprocess.Popen(
        ["node", script, str(provider.discovery_file)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = node.stdout.readline()
        assert line.startswith("SENT") and "pulse" in line, line + node.stderr.read()  # status bar: connected
        counts = tracker.counts()
        assert (counts.errors, counts.warnings) == (1, 1)
        issue = next(i for i in tracker.current_issues() if i.diagnostic.severity == "error")
        assert (issue.diagnostic.line, issue.diagnostic.column) == (4, 5)  # 0-based -> 1-based
        assert issue.diagnostic.code == "reportUndefinedVariable"
        assert issue.diagnostic.file == "C:\\proj\\main.py"

        node.stdin.write("fix\n")
        node.stdin.flush()
        assert node.stdout.readline().strip() == "FIXED"
        assert (tracker.counts().errors, tracker.counts().warnings) == (0, 1)  # the error was fixed

        node.stdin.write("quit\n")
        node.stdin.flush()
        assert node.stdout.readline().strip() == "BYE"
        assert tracker.current_issues() == []  # window closed: goodbye clears it
    finally:
        node.kill()
