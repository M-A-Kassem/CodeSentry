"""Monitor lifecycle: Auto Monitor, manual start/stop, provider failures,
and history recording. Uses fake providers, foreground and process data;
nothing here touches real editors or the screen."""
from __future__ import annotations

from screenshot_logger import incident_logger as incident_logger_module
from screenshot_logger.coding.app_detector import KNOWN_EDITORS, ForegroundApp
from screenshot_logger.coding.controller import MonitorController
from screenshot_logger.coding.diagnostic import Diagnostic
from screenshot_logger.coding.providers.base import STATE_ERROR, STATE_STOPPED, Provider
from screenshot_logger.config import AppConfig
from screenshot_logger.incident_logger import IncidentLogger


class FakeProvider(Provider):
    def __init__(self, name="fake", fail_start=False, fail_context=False):
        super().__init__()
        self.name = name
        self.display_name = name
        self.fail_start = fail_start
        self.fail_context = fail_context
        self.started = 0
        self.stopped = 0
        self.contexts = []

    def start(self, tracker):
        if self.fail_start:
            raise OSError("port already in use")
        super().start(tracker)
        self.started += 1

    def stop(self):
        self.stopped += 1
        super().stop()

    def on_context(self, foreground, running):
        if self.fail_context:
            raise RuntimeError("provider bug")
        self.contexts.append((foreground, dict(running)))


class Env:
    """Controls what the controller 'sees' as running/foreground."""

    def __init__(self):
        self.running = {}
        self.foreground = None
        self.made = []

    def factory(self, providers=None):
        def build(_config):
            built = providers() if providers else {"fake": FakeProvider()}
            self.made.append(built)
            return built

        return build


def make_controller(config=None, providers=None, incident_logger=None):
    env = Env()
    config = config or AppConfig()
    controller = MonitorController(
        config,
        incident_logger=incident_logger,
        provider_factory=env.factory(providers),
        foreground_fn=lambda: env.foreground,
        running_fn=lambda: dict(env.running),
        process_scan_seconds=0,
    )
    return controller, env


def open_vscode(env):
    env.running = {"code.exe": KNOWN_EDITORS["code.exe"]}


def test_auto_monitor_is_off_by_default():
    assert AppConfig().auto_monitor_enabled is False


def test_without_auto_monitor_nothing_starts_even_with_an_editor_open():
    controller, env = make_controller()
    open_vscode(env)
    for _ in range(3):
        controller.tick()
    assert not controller.running
    assert env.made == []


def test_auto_monitor_starts_when_a_coding_app_opens():
    controller, env = make_controller(AppConfig(auto_monitor_enabled=True))
    controller.tick()
    assert not controller.running  # no editor yet
    open_vscode(env)
    controller.tick()
    assert controller.running
    assert controller.started_by == "auto"


def test_auto_monitor_starts_when_editor_becomes_foreground():
    controller, env = make_controller(AppConfig(auto_monitor_enabled=True))
    env.foreground = ForegroundApp(1, 2, "devenv.exe", "x - Microsoft Visual Studio", KNOWN_EDITORS["devenv.exe"])
    controller.tick()
    assert controller.running


def test_manual_stop_pauses_auto_monitor_until_start():
    controller, env = make_controller(AppConfig(auto_monitor_enabled=True))
    open_vscode(env)
    controller.tick()
    controller.stop(manual=True)
    controller.tick()
    controller.tick()
    assert not controller.running, "must not restart behind the user's back"
    assert controller.auto_paused
    controller.start(manual=True)
    assert controller.running and not controller.auto_paused


def test_re_enabling_auto_monitor_re_arms_it():
    controller, env = make_controller(AppConfig(auto_monitor_enabled=True))
    open_vscode(env)
    controller.tick()
    controller.stop(manual=True)
    controller.set_auto_monitor(False)
    controller.set_auto_monitor(True)
    controller.tick()
    assert controller.running


def test_disabling_auto_monitor_does_not_stop_a_running_session():
    controller, env = make_controller(AppConfig(auto_monitor_enabled=True))
    open_vscode(env)
    controller.tick()
    controller.set_auto_monitor(False)
    controller.tick()
    assert controller.running


def test_manual_start_and_stop_drive_providers():
    controller, env = make_controller()
    controller.start()
    provider = env.made[0]["fake"]
    assert controller.running and provider.started == 1
    controller.start()  # idempotent
    assert provider.started == 1
    controller.stop()
    assert not controller.running and provider.stopped == 1
    assert provider.status().state == STATE_STOPPED


def test_restart_begins_a_fresh_session():
    controller, _ = make_controller(AppConfig(new_issue_debounce_seconds=0))
    controller.start()
    controller.tracker.update("fake", "s", [])
    controller.tracker.update("fake", "s", [Diagnostic("error", "boom", file="C:/a.py")])
    controller.tracker.tick()
    assert controller.tracker.counts().new_errors == 1
    controller.stop()
    controller.start()
    assert controller.tracker.counts().new_errors == 0


def test_stop_marks_results_stale_instead_of_counting_them():
    controller, _ = make_controller()
    controller.start()
    controller.tracker.update("fake", "s", [Diagnostic("error", "boom", file="C:/a.py")])
    controller.stop()
    assert controller.tracker.counts().errors == 0
    assert controller.tracker.current_issues()[0].stale


def test_failing_provider_does_not_block_other_providers():
    controller, _ = make_controller(
        providers=lambda: {"broken": FakeProvider("broken", fail_start=True), "good": FakeProvider("good")}
    )
    controller.start()
    assert controller.running
    assert controller.providers["good"].started == 1
    assert controller.providers["broken"].status().state == STATE_ERROR
    assert "port already in use" in controller.providers["broken"].status().detail
    controller.tick()  # broken provider is skipped, good one gets context
    assert controller.providers["good"].contexts


def test_provider_exception_during_tick_is_contained():
    controller, _ = make_controller(
        providers=lambda: {"bad": FakeProvider("bad", fail_context=True), "good": FakeProvider("good")}
    )
    controller.start()
    controller.tick()
    controller.tick()
    assert controller.running
    assert len(controller.providers["good"].contexts) == 2


def test_reconfigure_keeps_session_counts():
    controller, _ = make_controller(AppConfig(new_issue_debounce_seconds=0))
    controller.start()
    controller.tracker.update("fake", "s", [])
    controller.tracker.update("fake", "s", [Diagnostic("error", "boom", file="C:/a.py")])
    controller.tracker.tick()
    controller.reconfigure()
    assert controller.tracker.counts().new_errors == 1


def _real_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_logger_module, "capture_screenshot", lambda *_a: None)
    return IncidentLogger(
        config=AppConfig(), incidents_dir=tmp_path / "incidents", screenshots_dir=tmp_path / "shots"
    )


def test_new_errors_are_recorded_to_history_but_baseline_and_warnings_are_not(tmp_path, monkeypatch):
    logger = _real_logger(tmp_path, monkeypatch)
    controller, _ = make_controller(AppConfig(new_issue_debounce_seconds=0), incident_logger=logger)
    controller.launch()
    try:
        controller.start()
        tracker = controller.tracker
        tracker.update("fake", "s", [Diagnostic("error", "pre-existing", file="C:/a.py")])
        tracker.update("fake", "s", [
            Diagnostic("error", "pre-existing", file="C:/a.py"),
            Diagnostic("error", "brand new", file="C:/a.py", line=4, language="Python", application="vscode"),
            Diagnostic("warning", "new warning", file="C:/a.py"),
        ])
    finally:
        controller.shutdown()  # drains the history queue
    records = logger.list_recent()
    assert [r["title"] for r in records] == ["Error: brand new"]
    assert records[0]["severity"] == "ERROR"
    assert records[0]["source"] == "Coding assistant (fake)"
    assert "C:/a.py:4" in records[0]["message"]
    assert records[0]["screenshot_path"] is None  # no screenshot by default


def test_history_recording_can_be_turned_off(tmp_path, monkeypatch):
    logger = _real_logger(tmp_path, monkeypatch)
    config = AppConfig(new_issue_debounce_seconds=0, record_new_errors_to_history=False)
    controller, _ = make_controller(config, incident_logger=logger)
    controller.launch()
    try:
        controller.start()
        controller.tracker.update("fake", "s", [])
        controller.tracker.update("fake", "s", [Diagnostic("error", "x", file="C:/a.py")])
    finally:
        controller.shutdown()
    assert logger.list_recent() == []


def test_background_loop_launch_and_shutdown():
    controller, env = make_controller(AppConfig(auto_monitor_enabled=True))
    open_vscode(env)
    controller.tick_seconds = 0.05
    controller.launch()
    import time

    deadline = time.time() + 3
    while not controller.running and time.time() < deadline:
        time.sleep(0.05)
    assert controller.running
    controller.shutdown()
    assert not controller.running
