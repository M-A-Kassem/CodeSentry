"""Build/lint provider: output parsers (fed with real captured tool
output), configuration safety rules, and failure handling. The tool
runner is faked except for one test that proves missing tools are
reported, never installed."""
from __future__ import annotations

import json

import pytest

from screenshot_logger.coding.issue_tracker import IssueTracker
from screenshot_logger.coding.providers.build_tools import (
    BuildToolSpec,
    BuildToolsProvider,
    parse_tool_output,
    run_tool,
)

# Captured from `dotnet build` (SDK 8) on a project with two errors. The
# SDK prints each error twice: inline and again in the summary.
DOTNET_OUTPUT = r"""
C:\proj\cs\Program.cs(1,40): error CS0029: Cannot implicitly convert type 'string' to 'int' [C:\proj\cs\cs.csproj]
C:\proj\cs\Program.cs(1,63): error CS0103: The name 'foo' does not exist in the current context [C:\proj\cs\cs.csproj]
Build FAILED.
C:\proj\cs\Program.cs(1,40): error CS0029: Cannot implicitly convert type 'string' to 'int' [C:\proj\cs\cs.csproj]
C:\proj\cs\Program.cs(1,63): error CS0103: The name 'foo' does not exist in the current context [C:\proj\cs\cs.csproj]
    0 Warning(s)
    2 Error(s)
"""

# Captured from ruff 0.15 (`ruff check --output-format json`). Note that
# ruff labels every finding "severity": "error", even unused imports.
RUFF_OUTPUT = json.dumps([
    {"code": "invalid-syntax", "severity": "error", "filename": "C:\\proj\\py\\bad.py",
     "location": {"row": 7, "column": 1}, "message": "unexpected EOF while parsing"},
    {"code": "F821", "severity": "error", "filename": "C:\\proj\\py\\ok.py",
     "location": {"row": 4, "column": 12}, "message": "Undefined name `undefined_name`"},
    {"code": "F401", "severity": "error", "filename": "C:\\proj\\py\\ok.py",
     "location": {"row": 1, "column": 8}, "message": "`os` imported but unused"},
])


def spec(parser, cwd=None, **kw):
    return BuildToolSpec(name=kw.pop("name", parser), command=["tool"], parser=parser, cwd=cwd, enabled=True, **kw)


def test_dotnet_output_is_parsed_and_summary_duplicates_dropped():
    diagnostics = parse_tool_output(spec("msbuild"), DOTNET_OUTPUT)
    assert [(d.code, d.line, d.column) for d in diagnostics] == [("CS0029", 1, 40), ("CS0103", 1, 63)]
    assert all(d.severity == "error" and d.language == "C#" for d in diagnostics)
    assert not diagnostics[0].message.endswith("]"), "project suffix must be stripped"


def test_typescript_tsc_output_uses_msbuild_format():
    out = "src/app.ts(3,5): error TS2322: Type 'string' is not assignable to type 'number'."
    [d] = parse_tool_output(spec("msbuild", cwd="C:\\proj"), out)
    assert d.code == "TS2322" and d.language == "TypeScript"
    assert d.file == "C:\\proj\\src\\app.ts"  # relative paths resolved against cwd


def test_ruff_severity_policy():
    by_code = {d.code: d.severity for d in parse_tool_output(spec("ruff-json"), RUFF_OUTPUT)}
    assert by_code == {"invalid-syntax": "error", "F821": "error", "F401": "warning"}


def test_eslint_json():
    out = json.dumps([{"filePath": "C:\\proj\\a.js", "messages": [
        {"ruleId": "no-undef", "severity": 2, "message": "'x' is not defined.", "line": 2, "column": 1},
        {"ruleId": "no-unused-vars", "severity": 1, "message": "'y' is unused.", "line": 3, "column": 7},
    ]}])
    diagnostics = parse_tool_output(spec("eslint-json"), out)
    assert [(d.code, d.severity) for d in diagnostics] == [("no-undef", "error"), ("no-unused-vars", "warning")]


def test_gcc_clang_and_javac_formats():
    gcc = "C:\\proj\\main.c:5:10: error: 'y' undeclared (first use in this function)\nmain.c:2:1: warning: unused [-Wunused]"
    first, second = parse_tool_output(spec("gcc", cwd="C:\\proj"), gcc)
    assert (first.severity, first.line, first.column, first.language) == ("error", 5, 10, "C")
    assert (second.severity, second.code) == ("warning", "-Wunused")
    [java] = parse_tool_output(spec("javac"), "C:\\proj\\Main.java:3: error: cannot find symbol")
    assert (java.line, java.language) == (3, "Java")


def test_flake8_codes_split_errors_from_style():
    out = "a.py:1:1: F821 undefined name 'x'\na.py:2:80: E501 line too long (90 > 79 characters)"
    diagnostics = parse_tool_output(spec("flake8"), out)
    assert [(d.code, d.severity) for d in diagnostics] == [("F821", "error"), ("E501", "warning")]


# -- configuration safety ------------------------------------------------------------


def test_string_commands_are_refused_no_shell():
    with pytest.raises(ValueError, match="list"):
        BuildToolSpec.from_config({"command": "ruff check . && del *", "parser": "ruff-json"})


def test_unknown_parser_is_refused():
    with pytest.raises(ValueError, match="parser"):
        BuildToolSpec.from_config({"command": ["x"], "parser": "magic"})


def test_tools_are_disabled_unless_explicitly_enabled():
    ran = []
    provider = BuildToolsProvider(
        [{"name": "ruff", "command": ["ruff"], "parser": "ruff-json"}],  # no "enabled": true
        runner=lambda s: ran.append(s) or "[]",
    )
    assert provider.specs == []
    provider.start(IssueTracker())
    provider.request_run_all()
    provider.stop()
    assert ran == []
    assert provider.status().state == "stopped"


def test_invalid_entries_are_reported_not_fatal():
    provider = BuildToolsProvider([{"name": "bad", "command": "oops", "parser": "msbuild"}, "garbage"])
    provider.start(IssueTracker())
    assert provider.status().state == "error" and "bad" in provider.status().detail
    provider.stop()


def test_missing_tool_is_reported_never_installed():
    with pytest.raises(FileNotFoundError, match="not installed"):
        run_tool(BuildToolSpec(name="x", command=["definitely-not-a-real-tool-xyz"], parser="gcc"))


# -- running ----------------------------------------------------------------------------


def test_run_spec_updates_tracker_and_failures_mark_stale():
    outputs = [DOTNET_OUTPUT, RuntimeError("compiler crashed")]

    def runner(_spec):
        value = outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    tracker = IssueTracker(debounce_seconds=0)
    provider = BuildToolsProvider([{"name": "dotnet", "enabled": True, "command": ["dotnet", "build"], "parser": "msbuild"}], runner=runner)
    provider.tracker = tracker
    [tool] = provider.specs
    provider.run_spec(tool)
    assert tracker.counts().errors == 2
    provider.run_spec(tool)
    assert tracker.counts().errors == 0 and tracker.counts().stale_scopes == 1
    assert "compiler crashed" in provider.status().detail


def test_run_checks_now_runs_manual_only_tools():
    ran = []
    provider = BuildToolsProvider(
        [{"name": "dotnet", "enabled": True, "command": ["dotnet", "build"], "parser": "msbuild"}],
        runner=lambda s: ran.append(s.name) or "",
    )
    provider.start(IssueTracker())
    import time

    time.sleep(1.2)
    assert ran == [], "interval_seconds defaults to 0: never runs on its own"
    provider.request_run_all()
    deadline = time.time() + 3
    while not ran and time.time() < deadline:
        time.sleep(0.05)
    provider.stop()
    assert ran == ["dotnet"]
