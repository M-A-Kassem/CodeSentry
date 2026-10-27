"""Explanations (and their honesty), issue details text, and controller-level
project selection/detection with monitoring start/stop."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from screenshot_logger.coding.app_detector import KNOWN_EDITORS
from screenshot_logger.coding.controller import MonitorController, default_providers
from screenshot_logger.coding.diagnostic import Diagnostic
from screenshot_logger.coding.explain import HIGH, LOW, MEDIUM, explain
from screenshot_logger.coding.issue_tracker import TrackedIssue
from screenshot_logger.coding.project import UnsafeProjectRoot
from screenshot_logger.coding.providers.base import Provider
from screenshot_logger.config import AppConfig
from screenshot_logger.issues_window import explanation_text, issue_details


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def diag(path, message, line, **kw):
    return Diagnostic(kw.pop("severity", "error"), message, file=str(path), line=line, **kw)


# -- explanations ---------------------------------------------------------------------------------


def test_typo_is_suggested_with_evidence(tmp_path):
    path = write(tmp_path, "Program.cs", "var items = new List<string>();\nConsole.WriteLine(itemz.Count);\n")
    result = explain(diag(path, "The name 'itemz' does not exist in the current context", 2, code="CS0103", language="C#"), str(tmp_path))
    assert result.confidence == MEDIUM
    assert "`items`" in result.summary and "line 1" in result.summary


def test_missing_stdlib_import(tmp_path):
    path = write(tmp_path, "a.py", "data = json.loads('{}')\n")
    result = explain(diag(path, "Undefined name `json`", 1, code="F821", language="Python"), str(tmp_path))
    assert result.confidence == MEDIUM and "import json" in result.summary


def test_type_mismatch_restates_the_message_with_a_conversion_hint(tmp_path):
    path = write(tmp_path, "P.cs", 'int count = "3";\n')
    result = explain(diag(path, "Cannot implicitly convert type 'string' to 'int'", 1, code="CS0029", language="C#"), str(tmp_path))
    assert result.confidence == HIGH
    assert "`string`" in result.summary and "`int`" in result.summary
    assert any("int.Parse" in d for d in result.details)


def test_unclosed_bracket_points_at_the_right_line(tmp_path):
    # Regression: scanning only up to the error line blamed the `{`, which is closed.
    path = write(tmp_path, "a.js", "const a = 1;\nfunction g( {\n  return a;\n}\n")
    result = explain(diag(path, "SyntaxError: Unexpected identifier 'a'", 3, language="JavaScript"), str(tmp_path))
    assert result.confidence == MEDIUM
    assert "`(` opened on line 2" in result.summary


def test_vague_syntax_error_without_evidence_gets_no_explanation(tmp_path):
    path = write(tmp_path, "a.js", "let = 5;\n")
    assert explain(diag(path, "SyntaxError: Unexpected token '='", 1, language="JavaScript"), str(tmp_path)) is None


def test_json_trailing_comma(tmp_path):
    path = write(tmp_path, "c.json", '{\n  "a": 1,\n}\n')
    d = diag(path, "Invalid JSON: Expecting property name enclosed in double quotes", 3, source="json-syntax")
    result = explain(d, str(tmp_path))
    assert result.confidence == MEDIUM and "Line 2" in result.summary


def test_unrecognised_message_has_no_explanation(tmp_path):
    path = write(tmp_path, "a.py", "x = 1\n")
    assert explain(diag(path, "Some tool-specific complaint nobody has a rule for", 1), str(tmp_path)) is None


def test_files_outside_the_project_are_never_read(tmp_path):
    outside = write(tmp_path, "outside/a.cs", "var items = 1;\nitemz;\n")
    project = tmp_path / "project"
    project.mkdir()
    result = explain(diag(outside, "The name 'itemz' does not exist in the current context", 2, code="CS0103"), str(project))
    assert result.confidence == HIGH  # message only: no source evidence was used
    assert "items" not in result.summary


def test_explanation_text_marks_uncertainty():
    assert explanation_text(None).startswith("(no explanation")
    from screenshot_logger.coding.explain import Explanation

    assert explanation_text(Explanation("x", LOW)).startswith("[uncertain]")
    assert explanation_text(Explanation("x", MEDIUM)).startswith("[likely]")


def test_issue_details_show_source_message_and_confidence(tmp_path):
    path = write(tmp_path, "a.py", "def total(values):\n    totl = 0\n    return total_sum\n")
    d = diag(path, "Undefined name `total_sum`", 3, code="F821", source="ruff", language="Python")
    issue = TrackedIssue(key="k", diagnostic=d, provider="project", scope=str(tmp_path), first_seen=0.0)
    text = issue_details(issue, str(tmp_path))
    assert "Undefined name `total_sum`" in text  # original message kept
    assert " > 3 |     return total_sum" in text  # source line marked
    assert "Confidence: high" in text
    assert "Language: Python" in text and "Source: ruff" in text


# -- controller: project selection, detection, start/stop ----------------------------------------------


class FakeVisualStudio(Provider):
    name = "visualstudio"
    display_name = "VS"

    def __init__(self, paths):
        super().__init__()
        self.paths = paths

    def active_paths(self):
        return list(self.paths)


def project_controller(config, extra=None):
    def factory(cfg):
        providers = default_providers(cfg)
        for name in ("vscode", "visualstudio", "buildtools", "screen"):
            providers.pop(name, None)
        providers["project"].poll_seconds = 0.5
        providers.update(extra or {})
        return providers

    return MonitorController(config, provider_factory=factory, foreground_fn=lambda: None,
                             running_fn=lambda: {"devenv.exe": KNOWN_EDITORS["devenv.exe"]})


def wait_for(predicate, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_selected_project_is_analyzed_and_switching_clears_old_issues(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write(first, "bad.py", "x = (\n")
    write(second, "ok.py", "x = 1\n")
    config = AppConfig(new_issue_debounce_seconds=0, project_path=str(first), analyzers={"ruff": False})
    controller = project_controller(config)
    controller.start()
    try:
        assert wait_for(lambda: controller.tracker.counts().errors == 1)
        assert controller.snapshot().project_mode == "selected"
        controller.set_project(str(second))
        assert wait_for(lambda: controller.project_provider().root == str(second) and controller.tracker.counts().errors == 0)
    finally:
        controller.stop()
    assert controller.project_provider().temp_dir is None  # temp folder cleaned up


def test_unsafe_project_selection_is_refused_and_keeps_previous(tmp_path):
    config = AppConfig(project_path=str(tmp_path))
    controller = project_controller(config)
    with pytest.raises(UnsafeProjectRoot):
        controller.set_project("C:\\")
    assert config.project_path == str(tmp_path)


def test_project_is_detected_from_the_file_open_in_the_editor(tmp_path):
    write(tmp_path, "repo/App.sln", "")
    open_file = write(tmp_path, "repo/src/main.py", "x = (\n")
    config = AppConfig(new_issue_debounce_seconds=0, analyzers={"ruff": False})
    controller = project_controller(config, extra={"visualstudio": FakeVisualStudio([str(open_file)])})
    controller.start()
    try:
        controller.tick()
        assert controller.effective_project() == str(tmp_path / "repo")
        assert controller.snapshot().project_mode == "detected"
        assert wait_for(lambda: controller.tracker.counts().errors == 1)
        # A chosen folder overrides detection; clearing it returns to detection.
        other = tmp_path / "other"
        other.mkdir()
        controller.set_project(str(other))
        assert controller.effective_project() == str(other)
        controller.set_project(None)
        assert controller.effective_project() == str(tmp_path / "repo")
    finally:
        controller.stop()


def test_stopping_monitoring_stops_analysis(tmp_path):
    write(tmp_path, "a.py", "x = 1\n")
    controller = project_controller(AppConfig(project_path=str(tmp_path), analyzers={"ruff": False}))
    controller.start()
    provider = controller.project_provider()
    assert wait_for(lambda: provider.profile is not None)
    controller.stop()
    assert not provider._thread
    write(tmp_path, "b.py", "y = (\n")
    time.sleep(1.2)
    assert controller.tracker.counts().errors == 0  # nothing analyzed after stop


def test_run_checks_now_reaches_project_analysis(tmp_path):
    write(tmp_path, "a.py", "x = 1\n")
    controller = project_controller(AppConfig(project_path=str(tmp_path)))
    assert controller.run_checks_now() is False  # not monitoring
    controller.start()
    try:
        assert controller.run_checks_now() is True
    finally:
        controller.stop()
