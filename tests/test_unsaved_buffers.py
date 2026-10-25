"""Unsaved editor text: errors count before the file is saved.

Covers reading unsaved documents from the Visual Studio helper's output,
analyzing that text instead of the disk copy (real Ruff, C# compiler and
node runs when installed), never writing project files, and resolving
issues when the change is saved or discarded."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from screenshot_logger.coding.analyzers.base import AnalyzerRun, CommandResult, RunContext, check_command
from screenshot_logger.coding.analyzers.csharp import CSharpCompilerAnalyzer, find_roslyn
from screenshot_logger.coding.analyzers.python import PythonSyntaxAnalyzer, RuffAnalyzer
from screenshot_logger.coding.analyzers.web import NodeSyntaxAnalyzer
from screenshot_logger.coding.app_detector import KNOWN_EDITORS
from screenshot_logger.coding.controller import MonitorController
from screenshot_logger.coding.diagnostic import Diagnostic
from screenshot_logger.coding.explain import explain
from screenshot_logger.coding.issue_tracker import IssueTracker
from screenshot_logger.coding.project import scan_project
from screenshot_logger.coding.providers.base import Provider
from screenshot_logger.coding.providers.project_analysis import ProjectAnalysisProvider
from screenshot_logger.coding.providers.visual_studio import VisualStudioProvider, parse_dte_output
from screenshot_logger.config import AppConfig


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def snapshot_of(root: Path) -> dict:
    return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def make_provider(root: Path, buffers: dict, analyzers=None):
    tracker = IssueTracker(debounce_seconds=0)
    provider = ProjectAnalysisProvider(lambda: str(root), analyzers=analyzers, buffers_fn=lambda: dict(buffers))
    provider.tracker = tracker
    provider.temp_dir = str(root.parent / f"{root.name}-analysis-tmp")
    return provider, tracker


def settle(provider):
    """One poll notices the change, the next (quiet) one analyzes it."""
    provider.poll_once()
    provider.poll_once()


# -- Visual Studio helper output ---------------------------------------------------------------


def test_unsaved_documents_are_parsed_from_helper_output():
    raw = json.dumps({"ok": True, "instances": {"moniker": "!VisualStudio.DTE.17.0:1", "ok": True, "solution": "",
                                                 "items": [], "unsaved": {"path": "C:\\p\\a.py", "text": "x = (\n"}}})
    [instance] = parse_dte_output(raw)
    assert instance.unsaved == {"C:\\p\\a.py": "x = (\n"}


class FakePoller:
    def __init__(self, output):
        self.output = output
        self.requests = []

    def poll(self, include_unsaved=False):
        self.requests.append(include_unsaved)
        return self.output

    def close(self):
        pass


def test_visual_studio_provider_exposes_unsaved_text_only_when_enabled_and_running():
    output = json.dumps({"ok": True, "instances": [{"moniker": "m", "ok": True, "solution": "", "items": [],
                                                    "unsaved": [{"path": "C:\\p\\a.py", "text": "x = 1"}]}]})
    poller = FakePoller(output)
    provider = VisualStudioProvider(poller=poller, include_unsaved=True)
    provider.tracker = IssueTracker()
    provider.on_context(None, {"devenv.exe": KNOWN_EDITORS["devenv.exe"]})
    provider.poll_once()
    assert poller.requests == [True]
    assert provider.unsaved_buffers() == {"C:\\p\\a.py": "x = 1"}
    provider.on_context(None, {})  # Visual Studio closed
    assert provider.unsaved_buffers() == {}

    off = VisualStudioProvider(poller=FakePoller(output.replace("unsaved", "ignored")))
    off.tracker = IssueTracker()
    off.poll_once()
    assert off.poller.requests == [False]


# -- analysis of unsaved text ------------------------------------------------------------------------


def test_error_in_unsaved_text_counts_and_resolves_when_discarded(tmp_path):
    project = tmp_path / "p"
    path = write(project, "main.py", "x = 1\n")
    buffers = {}
    provider, tracker = make_provider(project, buffers, [PythonSyntaxAnalyzer()])
    provider.poll_once()
    assert tracker.counts().errors == 0

    buffers[str(path)] = "x = (\n"  # typed, not saved
    settle(provider)
    [issue] = tracker.current_issues()
    assert issue.diagnostic.file == str(path)
    assert issue.diagnostic.source.endswith("(unsaved)")

    buffers.clear()  # user undid the change / closed without saving
    settle(provider)
    assert tracker.counts().errors == 0


def test_saving_the_unsaved_error_does_not_count_it_twice(tmp_path):
    project = tmp_path / "p"
    path = write(project, "main.py", "x = 1\n")
    buffers = {}
    provider, tracker = make_provider(project, buffers, [PythonSyntaxAnalyzer()])
    provider.poll_once()
    buffers[str(path)] = "x = (\n"
    settle(provider)
    assert tracker.counts().new_errors == 1

    path.write_text("x = (\n", encoding="utf-8")  # saved
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    buffers.clear()
    settle(provider)
    counts = tracker.counts()
    assert counts.errors == 1 and counts.new_errors == 1


def test_editor_path_case_differences_are_matched(tmp_path):
    project = tmp_path / "p"
    path = write(project, "Main.py", "x = 1\n")
    provider, tracker = make_provider(project, {str(path).upper(): "x = (\n"}, [PythonSyntaxAnalyzer()])
    provider.poll_once()
    assert tracker.counts().errors == 1


def test_unsaved_text_outside_the_project_is_ignored(tmp_path):
    project = tmp_path / "p"
    write(project, "main.py", "x = 1\n")
    outside = write(tmp_path, "elsewhere/other.py", "x = 1\n")
    provider, tracker = make_provider(project, {str(outside): "x = (\n"}, [PythonSyntaxAnalyzer()])
    provider.poll_once()
    assert tracker.counts().errors == 0
    assert provider.unsaved_files() == []


def test_stale_disk_results_from_tools_that_cant_read_unsaved_text_are_dropped(tmp_path):
    project = tmp_path / "p"
    path = write(project, "main.py", "x = 1\n")

    class DiskOnly(PythonSyntaxAnalyzer):
        name = "disk-only"
        scope = "project"
        reads_unsaved = False

        def run(self, context, files):
            return AnalyzerRun(diagnostics=[Diagnostic("error", "old error from disk", file=str(path), line=1,
                                                       source="disk-only")])

    provider, tracker = make_provider(project, {str(path): "x = 2\n"}, [DiskOnly(), PythonSyntaxAnalyzer()])
    provider.poll_once()
    assert tracker.counts().errors == 0


def test_project_files_are_never_written(tmp_path):
    project = tmp_path / "p"
    path = write(project, "main.py", "x = 1\n")
    write(project, "a.json", "{}")
    before = snapshot_of(project)
    provider, _ = make_provider(project, {str(path): "x = (\n"})
    provider.poll_once()
    assert snapshot_of(project) == before


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is not installed")
def test_real_ruff_checks_unsaved_text_via_stdin(tmp_path):
    project = tmp_path / "p"
    write(project, "pyproject.toml", "[project]\nname='x'\n")
    path = write(project, "main.py", "def f():\n    return missing_name\n")  # error on disk
    context = RunContext(scan_project(str(project)), str(tmp_path / "t"),
                         buffers={str(path): "def f():\n    return 1\n\ndef g():\n    return other_missing\n"})
    result = RuffAnalyzer().run(context, list(context.profile.files.values()))
    messages = [d.message for d in result.diagnostics]
    assert any("other_missing" in m for m in messages), messages  # from the editor text
    assert not any("missing_name" in m and "other" not in m for m in messages)  # disk version ignored
    assert all(d.file == str(path) for d in result.diagnostics)
    assert result.diagnostics[0].line == 5


@pytest.mark.skipif(find_roslyn() is None, reason=".NET SDK is not installed")
def test_real_csharp_compiler_checks_unsaved_text(tmp_path):
    project = tmp_path / "p"
    write(project, "App/App.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><OutputType>Exe</OutputType>"
          "<TargetFramework>net8.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>")
    path = write(project, "App/Program.cs", "Console.WriteLine(1);\n")
    temp = tmp_path / "t"
    temp.mkdir()
    context = RunContext(scan_project(str(project)), str(temp),
                         buffers={str(path): "var items = 1;\nConsole.WriteLine(itemz);\n"})
    result = CSharpCompilerAnalyzer().run(context, [f for f in context.profile.files.values() if f.language == "C#"])
    [error] = [d for d in result.diagnostics if d.severity == "error"]
    assert error.code == "CS0103" and error.line == 2
    assert error.file == str(path)  # mapped back from the temporary copy
    assert all(d.file == str(path) for d in result.diagnostics)  # the CS0219 warning too


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_real_node_check_checks_unsaved_text(tmp_path):
    project = tmp_path / "p"
    path = write(project, "a.js", "const a = 1;\n")
    temp = tmp_path / "t"
    temp.mkdir()
    context = RunContext(scan_project(str(project)), str(temp), buffers={str(path): "const a = ;\n"})
    result = NodeSyntaxAnalyzer().run(context, list(context.profile.files.values()))
    [error] = result.diagnostics
    assert error.file == str(path) and error.line == 1


def test_stdin_is_passed_safely(tmp_path):
    calls = []

    def runner(argv, cwd, timeout, input_text=None):
        check_command(argv)
        calls.append((argv, input_text))
        return CommandResult(0, "[]")

    project = tmp_path / "p"
    path = write(project, "a.py", "x = 1\n")
    context = RunContext(scan_project(str(project)), str(tmp_path), runner, buffers={str(path): "y = 2\n"})
    RuffAnalyzer().run(context, list(context.profile.files.values()))
    stdin_call = next(c for c in calls if c[1] is not None)
    assert stdin_call[0][-3:] == ["--stdin-filename", str(path), "-"] and stdin_call[1] == "y = 2\n"


# -- explanations and controller wiring -------------------------------------------------------------


def test_explanations_use_the_unsaved_text(tmp_path):
    path = write(tmp_path, "P.cs", "Console.WriteLine(1);\n")
    d = Diagnostic("error", "The name 'itemz' does not exist in the current context", file=str(path), line=2, code="CS0103")
    result = explain(d, str(tmp_path), {str(path): "var items = 1;\nConsole.WriteLine(itemz);\n"})
    assert "`items`" in result.summary


class FakeEditor(Provider):
    name = "editor"
    display_name = "Editor"

    def unsaved_buffers(self):
        return {"C:\\p\\a.py": "x = (\n"}


def _controller(config):
    return MonitorController(config, provider_factory=lambda cfg: {
        "editor": FakeEditor(),
        "project": ProjectAnalysisProvider(project_fn=None, analyzers=[]),  # type: ignore[arg-type]
    }, foreground_fn=lambda: None, running_fn=dict)


def test_controller_collects_unsaved_text_and_respects_the_setting(tmp_path):
    controller = _controller(AppConfig(project_path=str(tmp_path)))
    controller.start()
    try:
        assert controller.unsaved_buffers() == {"C:\\p\\a.py": "x = (\n"}
        assert controller.project_provider().buffers_fn == controller.unsaved_buffers
    finally:
        controller.stop()
    off = _controller(AppConfig(project_path=str(tmp_path), analyze_unsaved_editor_text=False))
    off.start()
    try:
        assert off.project_provider().buffers_fn is None
    finally:
        off.stop()
