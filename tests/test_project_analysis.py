"""Project source analysis: scanning, real analyzers (skipped when the tool
isn't installed), mocked analyzers for tools that aren't, coverage
reporting, safety rules, deduplication, resolution, and project switching.

Tests marked "real" run the actual tool on files written to tmp_path.
Tests marked "mocked" feed sample output in the tool's documented format
through a fake runner; they prove our parsing/wiring, not the tool."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from screenshot_logger.coding import analyzers as analyzers_module
from screenshot_logger.coding.analyzers import all_analyzers, compute_coverage
from screenshot_logger.coding.analyzers import base as base_module
from screenshot_logger.coding.analyzers.base import (
    AnalyzerRun,
    CommandResult,
    RunContext,
    UnsafeCommand,
    check_command,
    run_command,
)
from screenshot_logger.coding.analyzers.csharp import CSharpCompilerAnalyzer, find_roslyn, parse_csproj
from screenshot_logger.coding.analyzers.data import JsonAnalyzer, XmlAnalyzer
from screenshot_logger.coding.analyzers.native import ClangGccAnalyzer, GofmtAnalyzer, JavacAnalyzer
from screenshot_logger.coding.analyzers.python import PythonSyntaxAnalyzer, RuffAnalyzer, parse_pyright_json
from screenshot_logger.coding.analyzers.web import NodeSyntaxAnalyzer, TypeScriptAnalyzer, parse_node_check
from screenshot_logger.coding.dedupe import merge_cross_tool
from screenshot_logger.coding.diagnostic import Diagnostic
from screenshot_logger.coding.issue_tracker import IssueTracker
from screenshot_logger.coding.project import UnsafeProjectRoot, find_project_root, scan_project, validate_project_root
from screenshot_logger.coding.providers.project_analysis import ProjectAnalysisProvider

HAS_RUFF = shutil.which("ruff") is not None
HAS_NODE = shutil.which("node") is not None
HAS_ROSLYN = find_roslyn() is not None


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def context_for(root: Path, runner=run_command) -> RunContext:
    temp = root.parent / f"{root.name}-tmp"
    temp.mkdir(exist_ok=True)
    return RunContext(scan_project(str(root)), str(temp), runner)


def run(analyzer, root: Path, runner=run_command) -> AnalyzerRun:
    context = context_for(root, runner)
    files = [f for f in context.profile.files.values() if analyzer.applies_to(f)]
    return analyzer.run(context, files)


def make_provider(root, analyzers=None, runner=run_command, overrides=None):
    tracker = IssueTracker(debounce_seconds=0)
    provider = ProjectAnalysisProvider(lambda: str(root) if root else None, analyzers=analyzers, runner=runner,
                                       overrides=overrides)
    provider.tracker = tracker
    provider.temp_dir = str(Path(root).parent / "analysis-tmp") if root else None
    return provider, tracker


# -- scanning -----------------------------------------------------------------------------


def test_scan_detects_languages_manifests_and_project_types(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    write(tmp_path, "src/app.py", "x = 1\n")
    write(tmp_path, "web/package.json", "{}")
    write(tmp_path, "web/a.ts", "let a = 1;\n")
    write(tmp_path, "Api/Api.csproj", "<Project Sdk='Microsoft.NET.Sdk'/>")
    write(tmp_path, "Api/Program.cs", "class P {}\n")
    profile = scan_project(str(tmp_path))
    languages = profile.languages()
    assert languages["Python"] == 1 and languages["TypeScript"] == 1 and languages["C#"] == 1
    assert {"Python (pyproject.toml)", "Node.js (package.json)", "C# project"} <= set(profile.project_types)
    assert len(profile.manifests) == 3


def test_scan_ignores_dependency_build_and_vcs_folders(tmp_path):
    write(tmp_path, "main.py", "x = 1\n")
    for ignored in ("node_modules/pkg/index.js", ".venv/Lib/site.py", "bin/Debug/x.cs", "obj/y.cs",
                    ".git/hooks/h.py", "build/out.js", "dist/bundle.js", "__pycache__/m.py", "custom_vendor/z.py"):
        write(tmp_path, ignored, "broken (((")
    profile = scan_project(str(tmp_path), extra_excludes=["custom_vendor"])
    assert [os.path.basename(p) for p in profile.files] == ["main.py"]
    assert profile.skipped_dirs >= 9


def test_scan_stops_at_file_limit_and_says_so(tmp_path):
    for i in range(30):
        write(tmp_path, f"m{i}.py", "x = 1\n")
    profile = scan_project(str(tmp_path), max_files=10)
    assert len(profile.files) == 10 and profile.truncated


def test_scan_does_not_follow_links_out_of_the_project(tmp_path):
    outside = tmp_path / "outside"
    write(outside, "secret.py", "x = (\n")
    project = tmp_path / "project"
    write(project, "main.py", "x = 1\n")
    try:  # a directory junction needs no admin rights on Windows
        subprocess.run(["cmd", "/c", "mklink", "/J", str(project / "link"), str(outside)], check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError):
        pytest.skip("could not create a junction")
    profile = scan_project(str(project))
    assert all("secret" not in p for p in profile.files)


@pytest.mark.parametrize("root", ["C:\\", os.path.expanduser("~"), os.environ.get("SystemRoot", "C:\\Windows")])
def test_whole_drive_home_and_system_folders_are_refused(root):
    with pytest.raises(UnsafeProjectRoot):
        validate_project_root(root)


def test_find_project_root_from_an_open_file(tmp_path):
    write(tmp_path, "repo/App.sln", "")
    write(tmp_path, "repo/Api/Api.csproj", "<Project/>")
    deep = write(tmp_path, "repo/Api/Controllers/Home.cs", "class H {}")
    assert find_project_root(str(deep)) == str(tmp_path / "repo")  # highest manifest wins
    write(tmp_path, "git/.git/HEAD", "")
    write(tmp_path, "git/pkg/package.json", "{}")
    file = write(tmp_path, "git/pkg/index.js", "")
    assert find_project_root(str(file)) == str(tmp_path / "git")  # .git wins
    assert find_project_root(str(write(tmp_path, "loose/notes.txt", ""))) is None


# -- real analyzers ---------------------------------------------------------------------------


def test_real_python_syntax_check_finds_error_without_running_code(tmp_path):
    marker = tmp_path / "executed.txt"
    write(tmp_path, "ok.py", f"open(r'{marker}', 'w').write('ran')\n")
    write(tmp_path, "bad.py", "def f(x:\n    return x\n")
    result = run(PythonSyntaxAnalyzer(), tmp_path)
    [error] = result.diagnostics
    assert error.file.endswith("bad.py") and error.line == 1 and error.severity == "error"
    assert not marker.exists(), "compile() must never execute the code"
    assert not list(tmp_path.rglob("*.pyc"))


@pytest.mark.skipif(not HAS_RUFF, reason="ruff is not installed")
def test_real_ruff_reports_undefined_name_and_writes_no_cache(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    write(tmp_path, "main.py", "import os\n\ndef f():\n    return missing_name\n")
    result = run(RuffAnalyzer(), tmp_path)
    by_code = {d.code: d for d in result.diagnostics}
    assert by_code["F821"].severity == "error" and by_code["F821"].line == 4
    assert by_code["F401"].severity == "warning"
    assert not (tmp_path / ".ruff_cache").exists()


@pytest.mark.skipif(not HAS_ROSLYN, reason=".NET SDK is not installed")
def test_real_csharp_compiler_finds_semantic_errors_without_msbuild(tmp_path):
    write(tmp_path, "App/App.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><OutputType>Exe</OutputType>"
          "<TargetFramework>net8.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>")
    write(tmp_path, "App/Program.cs", "var items = new List<int>();\nint n = \"3\";\nConsole.WriteLine(itemz);\n")
    result = run(CSharpCompilerAnalyzer(), tmp_path)
    assert result.error is None and result.partial_reason is None
    assert sorted(d.code for d in result.diagnostics) == ["CS0029", "CS0103"]
    assert not (tmp_path / "App" / "bin").exists() and not (tmp_path / "App" / "obj").exists()


@pytest.mark.skipif(not HAS_ROSLYN, reason=".NET SDK is not installed")
def test_real_csharp_with_nuget_packages_is_syntax_only_and_marked_partial(tmp_path):
    write(tmp_path, "App/App.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
          "</PropertyGroup><ItemGroup><PackageReference Include=\"Newtonsoft.Json\" Version=\"13.0.3\"/></ItemGroup></Project>")
    write(tmp_path, "App/Code.cs", "using Newtonsoft.Json;\nclass C { void M() { int x = 1 } }\n")
    result = run(CSharpCompilerAnalyzer(), tmp_path)
    assert [d.code for d in result.diagnostics] == ["CS1002"], "only the syntax error; unresolved packages aren't errors"
    assert "NuGet" in result.partial_reason


@pytest.mark.skipif(not HAS_NODE, reason="Node.js is not installed")
def test_real_node_check_finds_js_syntax_error_and_skips_jsx(tmp_path):
    write(tmp_path, "a.js", "const a = 1;\nfunction g( {\n  return a;\n}\n")
    write(tmp_path, "ok.mjs", "export const x = 1;\n")
    write(tmp_path, "component.js", "export default function A() { return (<div>hi</div>); }\n")
    result = run(NodeSyntaxAnalyzer(), tmp_path)
    [error] = result.diagnostics
    assert error.file.endswith("a.js") and error.message.startswith("SyntaxError")
    assert any("JSX" in reason for reason in result.failed_files.values())
    assert result.partial_reason


def test_real_json_and_xml_checks(tmp_path):
    write(tmp_path, "config.json", '{\n  "a": 1,\n}\n')
    write(tmp_path, "tsconfig.json", '{ // comments are allowed here\n}\n')
    write(tmp_path, "App.csproj", "<Project><PropertyGroup></Project>")
    json_run = run(JsonAnalyzer(), tmp_path)
    assert [os.path.basename(d.file) for d in json_run.diagnostics] == ["config.json"]
    assert any("tsconfig" in p for p in json_run.failed_files)
    [xml_error] = run(XmlAnalyzer(), tmp_path).diagnostics
    assert xml_error.line == 1 and "mismatched tag" in xml_error.message


# -- mocked analyzers (tools not installed here) ------------------------------------------------


def fake_runner(output: str, returncode: int = 1, calls: list | None = None):
    def runner(argv, cwd, timeout):
        check_command(argv)
        if calls is not None:
            calls.append(list(argv))
        return CommandResult(returncode, output)

    return runner


def test_mocked_tsc_output_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(base_module, "which", lambda name: f"C:/tools/{name}.exe")
    monkeypatch.setattr("screenshot_logger.coding.analyzers.web.which", lambda name: f"C:/tools/{name}.exe")
    write(tmp_path, "tsconfig.json", "{}")
    write(tmp_path, "src/a.ts", "let n: number = 'x';\n")
    calls = []
    result = run(TypeScriptAnalyzer(), tmp_path, fake_runner(
        "src/a.ts(1,5): error TS2322: Type 'string' is not assignable to type 'number'.", calls=calls))
    [d] = result.diagnostics
    assert (d.code, d.line, d.language) == ("TS2322", 1, "TypeScript")
    assert "--noEmit" in calls[0]


def test_mocked_pyright_json_is_parsed():
    output = '{"generalDiagnostics": [{"file": "C:\\\\p\\\\a.py", "severity": "error", "message": "\\"x\\" is not defined", ' \
             '"range": {"start": {"line": 2, "character": 4}}, "rule": "reportUndefinedVariable"}]}'
    [d] = parse_pyright_json(output)
    assert (d.line, d.column, d.code, d.severity) == (3, 5, "reportUndefinedVariable", "error")


def test_mocked_javac_maven_project_reports_syntax_only(tmp_path, monkeypatch):
    monkeypatch.setattr("screenshot_logger.coding.analyzers.native.which", lambda name: "C:/jdk/bin/javac.exe")
    write(tmp_path, "pom.xml", "<project/>")
    main = write(tmp_path, "src/Main.java", "class Main {}")
    output = f"{main}:3: error: ';' expected\n{main}:1: error: package org.junit does not exist\n"
    result = run(JavacAnalyzer(), tmp_path, fake_runner(output))
    assert [d.message for d in result.diagnostics] == ["';' expected"]
    assert "Maven/Gradle" in result.partial_reason


def test_mocked_gcc_missing_headers_are_coverage_notes_not_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("screenshot_logger.coding.analyzers.native.which", lambda name: "C:/llvm/clang.exe" if "clang" in name else None)
    source = write(tmp_path, "main.c", "int main(){}")
    output = f"{source}:1:10: fatal error: 'lib.h' file not found\n{source}:5:3: error: use of undeclared identifier 'y'\n"
    result = run(ClangGccAnalyzer(), tmp_path, fake_runner(output))
    assert [d.message for d in result.diagnostics] == ["use of undeclared identifier 'y'"]
    assert "header" in result.partial_reason


def test_mocked_gofmt_syntax_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("screenshot_logger.coding.analyzers.native.which", lambda name: "C:/go/bin/gofmt.exe")
    source = write(tmp_path, "main.go", "package main\nfunc main( {}\n")
    result = run(GofmtAnalyzer(), tmp_path, fake_runner(f"{source}:2:12: expected ')', found '{{'\n"))
    [d] = result.diagnostics
    assert (d.line, d.column, d.language) == (2, 12, "Go")


def test_node_check_output_parser():
    output = "C:\\p\\a.js:3\n  return a;\n         ^\n\nSyntaxError: Unexpected identifier 'a'\n    at wrapSafe\n"
    [d] = parse_node_check(output, "C:\\p\\a.js")
    assert (d.line, d.column) == (3, 10)


# -- missing analyzers and unsupported languages -------------------------------------------------------


def test_missing_tool_and_unsupported_language_are_reported_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr("screenshot_logger.coding.analyzers.web.which", lambda name: None)
    write(tmp_path, "a.ts", "let a = 1;\n")
    write(tmp_path, "main.rs", "fn main() {}\n")
    write(tmp_path, "README.md", "# hi\n")
    provider, tracker = make_provider(tmp_path)
    provider.poll_once()
    coverage = {c.language: c for c in provider.coverage()}
    assert coverage["TypeScript"].status == "not checked"
    assert any("npm install -g typescript" in s for s in coverage["TypeScript"].suggestions)
    assert coverage["Rust"].status == "no analyzer" and coverage["Rust"].suggestions
    assert coverage["Markdown"].status == "not code"
    assert provider.is_partial()
    assert "not checked: TypeScript, Rust" in provider.status().detail


def test_failing_analyzer_is_reported_and_others_still_run(tmp_path):
    class Exploding(RuffAnalyzer):
        name = "exploding"
        display_name = "Exploding"

        def availability(self, profile):
            return base_module.Availability(True)

        def run(self, context, files):
            raise TimeoutError("took too long")

    write(tmp_path, "bad.py", "x = (\n")
    provider, tracker = make_provider(tmp_path, analyzers=[Exploding(), PythonSyntaxAnalyzer()])
    provider.poll_once()
    assert tracker.counts().errors == 1  # the syntax checker still worked
    [python] = provider.coverage()
    assert python.status == "partial" and any("took too long" in n for n in python.notes)


def test_compiler_failure_is_an_error_not_zero_issues(tmp_path, monkeypatch):
    monkeypatch.setattr("screenshot_logger.coding.analyzers.csharp.find_roslyn", lambda: ("dotnet", "csc.dll"))
    write(tmp_path, "a.cs", "class A {}")
    result = run(CSharpCompilerAnalyzer(), tmp_path, fake_runner("error CS2019: Invalid target type", returncode=1))
    assert result.error and "CS2019" in result.error
    assert result.diagnostics == []


# -- safety ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    "ruff check .",                                   # a shell string
    ["cmd", "/c", "del", "*"],
    ["powershell.exe", "-Command", "Remove-Item x"],
    ["bash", "-c", "rm -rf /"],
    ["C:/Python/python.exe", "-c", "import os"],
    ["node", "-e", "require('fs')"],
    ["python", "-m", "http.server"],
    ["tsc.cmd", "--noEmit", "C:/p/a&calc.ts"],          # cmd.exe metacharacters into a .cmd launcher
    [],
])
def test_unsafe_commands_are_refused(argv):
    with pytest.raises(UnsafeCommand):
        check_command(argv)


def test_analyzer_flags_that_look_like_inline_code_are_fine_for_non_interpreters():
    check_command(["gofmt", "-e", "-l", "main.go"])
    check_command(["node", "--check", "a.js"])


def test_commands_never_use_a_shell(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(base_module.subprocess, "run", fake_run)
    run_command(["ruff", "check"], str(tmp_path), 5)
    assert seen["shell"] is False and seen["stdin"] == subprocess.DEVNULL


def test_opt_in_analyzers_are_off_by_default_and_never_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("screenshot_logger.coding.analyzers.csharp.which", lambda name: "C:/dotnet/dotnet.exe")
    monkeypatch.setattr("screenshot_logger.coding.analyzers.web.which", lambda name: f"C:/npm/{name}.exe")
    monkeypatch.setattr("screenshot_logger.coding.analyzers.python.which", lambda name: f"C:/py/{name}.exe")
    write(tmp_path, "App.csproj", "<Project Sdk='Microsoft.NET.Sdk'/>")
    write(tmp_path, "Program.cs", "class P {}\n")
    write(tmp_path, "a.py", "x = 1\n")
    write(tmp_path, "b.js", "const b = 1;\n")
    opt_in = [a for a in all_analyzers() if not a.default_enabled]
    assert {a.name for a in opt_in} == {"dotnet-build", "eslint", "mypy"}
    assert all(a.consent_reason for a in opt_in)
    provider, _ = make_provider(tmp_path, analyzers=opt_in, runner=fake_runner("", 0, calls))
    provider.poll_once()
    assert calls == []
    coverage = {c.language: c for c in provider.coverage()}
    assert any("available but off" in n for n in coverage["C#"].notes)


def test_analysis_never_modifies_project_files(tmp_path):
    write(tmp_path, "main.py", "x = (\n")
    write(tmp_path, "data.json", "{,}")
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    provider, _ = make_provider(tmp_path)
    provider.poll_once()
    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    assert before == after


# -- dedup, resolution, monitoring cycle ------------------------------------------------------------------


def test_same_syntax_error_from_two_tools_counts_once():
    a = Diagnostic("error", "SyntaxError: '(' was never closed", file="C:/p/a.py", line=1, code="syntax", source="python-syntax")
    b = Diagnostic("error", "Expected `)`, found newline", file="C:/p/a.py", line=2, code="invalid-syntax", source="ruff")
    [merged] = merge_cross_tool([a, b], {"ruff": 60, "python-syntax": 40})
    assert merged.source == "ruff (also: python-syntax)"


def test_same_code_from_compiler_and_build_counts_once_but_different_problems_stay():
    csc = Diagnostic("error", "The name 'foo' does not exist", file="C:/p/a.cs", line=3, code="CS0103", source="csharp-compiler")
    build = Diagnostic("error", "The name 'foo' does not exist", file="C:/p/a.cs", line=3, code="CS0103", source="dotnet-build")
    other = Diagnostic("error", "The name 'bar' does not exist", file="C:/p/a.cs", line=3, code="CS0104", source="dotnet-build")
    assert len(merge_cross_tool([csc, build, other], {"dotnet-build": 90})) == 2


def test_visual_studio_and_project_analysis_report_counts_once():
    tracker = IssueTracker(debounce_seconds=0)
    tracker.update("project", "C:/p", [])
    tracker.update("visualstudio", "vs1", [])
    error = dict(severity="error", message="The name 'foo' does not exist in the current context", file="C:/p/a.cs", line=3)
    tracker.update("project", "C:/p", [Diagnostic(**error, code="CS0103")])
    tracker.update("visualstudio", "vs1", [Diagnostic(**error)])  # EnvDTE gives no code
    counts = tracker.counts()
    assert counts.errors == 1 and counts.new_errors == 1


def test_repeated_scans_do_not_recount_and_fixes_resolve(tmp_path):
    bad = write(tmp_path, "bad.py", "x = (\n")
    provider, tracker = make_provider(tmp_path, analyzers=[PythonSyntaxAnalyzer()])
    tracker.update("project", str(tmp_path), [])  # monitoring started on a clean project
    provider.poll_once()
    for _ in range(3):
        provider.request_run_all()
        provider.poll_once()
    assert tracker.counts().errors == 1 and tracker.counts().new_errors == 1
    bad.write_text("x = (1)\n", encoding="utf-8")
    os.utime(bad, (bad.stat().st_atime, bad.stat().st_mtime + 5))
    assert provider.poll_once() is False  # change seen; waits for saves to settle
    assert provider.poll_once() is True
    assert tracker.counts().errors == 0 and tracker.counts().new_errors == 1


def test_only_changed_files_are_rechecked(tmp_path):
    write(tmp_path, "a.py", "x = 1\n")
    changed = write(tmp_path, "b.py", "y = 2\n")
    checked = []

    class Spy(PythonSyntaxAnalyzer):
        def run(self, context, files):
            checked.append(sorted(os.path.basename(f.path) for f in files))
            return super().run(context, files)

    provider, _ = make_provider(tmp_path, analyzers=[Spy()])
    provider.poll_once()
    changed.write_text("y = 3\n", encoding="utf-8")
    os.utime(changed, (changed.stat().st_atime, changed.stat().st_mtime + 5))
    provider.poll_once()
    provider.poll_once()
    assert checked == [["a.py", "b.py"], ["b.py"]]


def test_deleting_a_broken_file_resolves_its_issue(tmp_path):
    bad = write(tmp_path, "bad.py", "x = (\n")
    write(tmp_path, "ok.py", "x = 1\n")
    provider, tracker = make_provider(tmp_path, analyzers=[PythonSyntaxAnalyzer()])
    provider.poll_once()
    bad.unlink()
    provider.poll_once()
    provider.poll_once()
    assert tracker.counts().errors == 0


def test_diagnostics_in_ignored_folders_are_dropped_even_if_a_tool_reports_them(tmp_path):
    write(tmp_path, "main.py", "x = 1\n")
    ignored = str(tmp_path / "node_modules" / "x.py")

    class Leaky(PythonSyntaxAnalyzer):
        scope = "project"

        def run(self, context, files):
            return AnalyzerRun(diagnostics=[Diagnostic("error", "boom", file=ignored, line=1)])

    provider, tracker = make_provider(tmp_path, analyzers=[Leaky()])
    provider.poll_once()
    assert tracker.counts().errors == 0


def test_switching_projects_clears_the_old_projects_issues(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write(first, "bad.py", "x = (\n")
    write(second, "ok.py", "x = 1\n")
    current = {"root": str(first)}
    tracker = IssueTracker(debounce_seconds=0)
    provider = ProjectAnalysisProvider(lambda: current["root"], analyzers=[PythonSyntaxAnalyzer()])
    provider.tracker = tracker
    provider.poll_once()
    assert tracker.counts().errors == 1
    current["root"] = str(second)
    provider.poll_once()
    assert tracker.counts().errors == 0
    assert provider.root == str(second)
    current["root"] = None
    provider.poll_once()
    assert "no project selected" in provider.status().detail


def test_no_project_means_no_scanning():
    provider, tracker = make_provider(None)
    assert provider.poll_once() is False
    assert provider.profile is None


# -- coverage function directly -------------------------------------------------------------------------


def test_coverage_marks_partial_when_files_could_not_be_checked(tmp_path):
    write(tmp_path, "a.json", "{}")
    profile = scan_project(str(tmp_path))
    json_analyzer = JsonAnalyzer()
    runs = {"json-syntax": AnalyzerRun(failed_files={str(tmp_path / "a.json"): "unreadable"})}
    [cov] = compute_coverage(profile, [json_analyzer], {}, {"json-syntax": base_module.Availability(True)}, runs)
    assert cov.status == "partial"


def test_csproj_parsing_reads_properties_without_msbuild(tmp_path):
    path = write(tmp_path, "A.csproj", "<Project Sdk=\"Microsoft.NET.Sdk.Web\"><PropertyGroup><TargetFramework>net8.0"
                 "</TargetFramework><Nullable>enable</Nullable><ImplicitUsings>enable</ImplicitUsings></PropertyGroup>"
                 "<ItemGroup><Using Include=\"MyApp.Shared\"/><ProjectReference Include=\"..\\B\\B.csproj\"/></ItemGroup></Project>")
    project = parse_csproj(str(path))
    assert project.sdk == "Microsoft.NET.Sdk.Web" and project.output_type == "Exe"
    assert project.implicit_usings and project.nullable == "enable"
    assert project.usings_added == ["MyApp.Shared"] and project.project_references


def test_registry_lists_languages():
    languages = {lang for a in analyzers_module.all_analyzers() for lang in a.languages}
    assert {"Python", "C#", "JavaScript", "TypeScript", "Java", "Go", "JSON"} <= languages
