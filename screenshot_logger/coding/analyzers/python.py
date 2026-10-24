"""Python analyzers: built-in syntax check, Ruff, Pyright, mypy (opt-in)."""
from __future__ import annotations

import json
import os
import sys
import warnings

from ..diagnostic import ERROR, WARNING, Diagnostic
from ..providers.build_tools import _parse_ruff_json
from .base import Analyzer, AnalyzerRun, Availability, RunContext, which

PYTHON = "Python"
_PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


class PythonSyntaxAnalyzer(Analyzer):
    """Parses each file with Python's own compiler, in-process.

    ``compile()`` only parses and compiles to bytecode in memory: the code
    is never executed and no ``.pyc`` file is written. Uses the grammar of
    the Python running this app, so code for a much older/newer Python can
    be misjudged.
    """

    name = "python-syntax"
    display_name = f"Python {_PY_VERSION} syntax check (built in)"
    languages = (PYTHON,)
    scope = "file"
    kind = "syntax"
    priority = 40
    reads_unsaved = True

    def applies_to(self, source) -> bool:
        return source.language == PYTHON and source.path.lower().endswith((".py", ".pyw"))

    def run(self, context: RunContext, files) -> AnalyzerRun:
        result = AnalyzerRun()
        for source in files:
            text = context.text_of(source.path)
            if text is None:
                result.failed_files[source.path] = "too large or not readable as text"
                continue
            result.checked_files.add(source.path)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    compile(text, source.path, "exec", dont_inherit=True)
                except SyntaxError as exc:
                    result.diagnostics.append(
                        Diagnostic(
                            severity=ERROR,
                            message=f"{type(exc).__name__}: {exc.msg}",
                            file=source.path,
                            line=exc.lineno,
                            column=exc.offset,
                            source=self.name,
                            code="syntax",
                            language=PYTHON,
                        )
                    )
                except (ValueError, MemoryError, RecursionError) as exc:
                    result.failed_files[source.path] = f"could not be parsed: {exc}"
            for warning in caught:
                if issubclass(warning.category, SyntaxWarning):
                    result.diagnostics.append(
                        Diagnostic(WARNING, f"SyntaxWarning: {warning.message}", file=source.path,
                                   line=warning.lineno, source=self.name, language=PYTHON)
                    )
        return result


class RuffAnalyzer(Analyzer):
    name = "ruff"
    display_name = "Ruff (lint)"
    languages = (PYTHON,)
    kind = "lint"
    priority = 60
    reads_unsaved = True
    install_hint = "pip install ruff"

    def availability(self, profile) -> Availability:
        path = which("ruff")
        return Availability(path is not None, path or "ruff is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        from ..project import DEFAULT_EXCLUDED_DIRS

        # --no-cache: never write .ruff_cache into the project.
        # --exit-zero: findings are not a failure. Ruff reads the project's
        # own pyproject.toml/ruff.toml, so its rule selection is respected.
        ruff = which("ruff") or "ruff"
        base = [ruff, "check", "--output-format", "json", "--no-cache", "--exit-zero"]
        argv = base + ["--extend-exclude", ",".join(sorted(DEFAULT_EXCLUDED_DIRS)), context.profile.root]
        diagnostics = self._parse(context.runner(argv, context.profile.root, context.timeout).output, context)
        if isinstance(diagnostics, str):
            return AnalyzerRun(error=diagnostics)
        # Files with unsaved editor changes: replace their on-disk results
        # with a check of the editor text, passed on stdin. --stdin-filename
        # keeps the project's config and per-file rules applying to it.
        unsaved = [f.path for f in files if f.path in context.buffers]
        if unsaved:
            diagnostics = [d for d in diagnostics if d.file not in unsaved]
            for path in unsaved:
                output = context.runner(base + ["--stdin-filename", path, "-"], context.profile.root,
                                        context.timeout, input_text=context.buffers[path]).output
                parsed = self._parse(output, context)
                if isinstance(parsed, str):
                    return AnalyzerRun(error=parsed)
                diagnostics += [d.with_changes(file=path) for d in parsed]
        return AnalyzerRun(diagnostics=diagnostics, checked_files={f.path for f in files})

    def _parse(self, output: str, context: RunContext):
        """Diagnostics from Ruff's JSON, or an error message string."""
        start = output.find("[")
        if start < 0:
            return output.strip()[:300] or "no output from ruff"
        return [
            d.with_changes(source=self.name, language=PYTHON)
            for d in _parse_ruff_json(output[start:output.rfind("]") + 1], context.profile.root)
        ]


class PyrightAnalyzer(Analyzer):
    name = "pyright"
    display_name = "Pyright (type check)"
    languages = (PYTHON,)
    kind = "types"
    priority = 70
    install_hint = "npm install -g pyright   (or: pip install pyright)"

    def availability(self, profile) -> Availability:
        path = which("pyright")
        return Availability(path is not None, path or "pyright is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        output = context.runner([which("pyright") or "pyright", "--outputjson", context.profile.root],
                                context.profile.root, context.timeout).output
        return AnalyzerRun(diagnostics=parse_pyright_json(output), checked_files={f.path for f in files})


def parse_pyright_json(output: str) -> list[Diagnostic]:
    start = output.find("{")
    if start < 0:
        return []
    data = json.loads(output[start:output.rfind("}") + 1])
    diagnostics = []
    for item in data.get("generalDiagnostics", []):
        start_pos = (item.get("range") or {}).get("start") or {}
        severity = item.get("severity", "error")
        diagnostics.append(
            Diagnostic(
                severity=ERROR if severity == "error" else (WARNING if severity == "warning" else "info"),
                message=str(item.get("message", "")),
                file=item.get("file"),
                line=int(start_pos.get("line", 0)) + 1,
                column=int(start_pos.get("character", 0)) + 1,
                code=item.get("rule"),
                source="pyright",
                language=PYTHON,
            )
        )
    return diagnostics


class MypyAnalyzer(Analyzer):
    name = "mypy"
    display_name = "mypy (type check)"
    languages = (PYTHON,)
    kind = "types"
    priority = 65
    default_enabled = False
    consent_reason = "mypy loads plugins named in the project's configuration, which runs project-chosen code"
    install_hint = "pip install mypy"

    def availability(self, profile) -> Availability:
        path = which("mypy")
        return Availability(path is not None, path or "mypy is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        from ..providers.build_tools import _GCC_RE, _regex_parser

        argv = [which("mypy") or "mypy", "--show-column-numbers", "--no-error-summary", "--no-color-output",
                "--hide-error-context", "--cache-dir", os.path.join(context.temp_dir, "mypy"), context.profile.root]
        output = context.runner(argv, context.profile.root, context.timeout).output
        diagnostics = [d.with_changes(source=self.name, language=PYTHON)
                       for d in _regex_parser(_GCC_RE)(output, context.profile.root)]
        return AnalyzerRun(diagnostics=diagnostics, checked_files={f.path for f in files})
