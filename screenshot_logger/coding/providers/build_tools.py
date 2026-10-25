"""Explicitly configured compilers, linters and analyzers.

Nothing here runs unless the user lists the tool in ``config.json`` under
``build_tools`` with ``"enabled": true``. Commands are argument lists run
without a shell, so no shell syntax is ever interpreted, and missing
tools are reported -- never installed. Tools with ``interval_seconds: 0``
(the default) only run when the user clicks "Run checks now".

Example::

    "build_tools": [
      {"name": "ruff", "enabled": true, "parser": "ruff-json",
       "command": ["ruff", "check", "--output-format", "json", "."],
       "cwd": "D:/code/myproject", "interval_seconds": 60}
    ]

Supported ``parser`` values and the tools they fit:

==============  =========================================================
``ruff-json``   ``ruff check --output-format json`` (Python)
``eslint-json`` ``eslint -f json`` (JavaScript/TypeScript)
``msbuild``     ``dotnet build``, ``msbuild``, ``csc``, ``tsc --pretty false``
``gcc``         ``gcc``/``g++``/``clang`` and ``file:line:col: error: msg`` tools
``flake8``      ``flake8``, ``pyflakes``, ``pylint --output-format=parseable``
``javac``       ``javac`` (Java)
==============  =========================================================
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..diagnostic import ERROR, WARNING, Diagnostic
from .base import STATE_ACTIVE, STATE_ERROR, STATE_IDLE, Provider

_logger = logging.getLogger("screenshot_logger")

MAX_OUTPUT_CHARS = 5_000_000

_MSBUILD_RE = re.compile(
    r"^\s*(?P<file>[^\s(][^(]*?)\((?P<line>\d+)(?:,(?P<col>\d+))?(?:,\d+,\d+)?\)\s*:\s*"
    r"(?P<sev>error|warning)\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<msg>.*?)(?:\s+\[[^\]]+\])?\s*$",
    re.IGNORECASE,
)
_GCC_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?:fatal\s+)?(?P<sev>error|warning)\s*:\s*(?P<msg>.*?)(?:\s+\[(?P<code>[-\w=]+)\])?\s*$",
    re.IGNORECASE,
)
_FLAKE8_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?:\[?(?P<code>[A-Z]+\d+)\]?)?\s*(?P<msg>.+?)\s*$"
)
_RULE_CODE_RE = re.compile(r"^[A-Z]+[0-9]+$")
_JAVAC_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:]+\.java):(?P<line>\d+):\s*(?P<sev>error|warning)\s*:\s*(?P<msg>.*?)\s*$",
    re.IGNORECASE,
)


@dataclass
class BuildToolSpec:
    name: str
    command: list[str]
    parser: str
    cwd: Optional[str] = None
    enabled: bool = False
    interval_seconds: float = 0.0
    timeout_seconds: float = 120.0
    language: Optional[str] = None

    @classmethod
    def from_config(cls, raw: dict) -> "BuildToolSpec":
        command = raw.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            # A plain string would need a shell to split; refuse it.
            raise ValueError("command must be a non-empty list of strings")
        parser = str(raw.get("parser", ""))
        if parser not in PARSERS:
            raise ValueError(f"unknown parser {parser!r}; expected one of {sorted(PARSERS)}")
        return cls(
            name=str(raw.get("name") or command[0]),
            command=list(command),
            parser=parser,
            cwd=raw.get("cwd") or None,
            enabled=bool(raw.get("enabled", False)),
            interval_seconds=max(0.0, float(raw.get("interval_seconds", 0))),
            timeout_seconds=max(5.0, float(raw.get("timeout_seconds", 120))),
            language=raw.get("language") or None,
        )


def _resolve(path: str, cwd: Optional[str]) -> str:
    path = path.strip()
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _regex_parser(pattern: re.Pattern, default_severity: str = ERROR) -> Callable[[str, Optional[str]], list[Diagnostic]]:
    def parse(output: str, cwd: Optional[str]) -> list[Diagnostic]:
        found = []
        for line in output.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            groups = match.groupdict()
            found.append(
                Diagnostic(
                    severity=groups.get("sev") or default_severity,
                    message=groups["msg"],
                    file=_resolve(groups["file"], cwd),
                    line=int(groups["line"]),
                    column=int(groups["col"]) if groups.get("col") else None,
                    code=groups.get("code"),
                )
            )
        return found

    return parse


def _parse_flake8(output: str, cwd: Optional[str]) -> list[Diagnostic]:
    diagnostics = []
    for diagnostic in _regex_parser(_FLAKE8_RE)(output, cwd):
        code = diagnostic.code or ""
        # E9/F63/F7/F82 are the flake8 codes for real errors (syntax,
        # undefined names); most other codes are style warnings.
        is_error = code.startswith(("E9", "F63", "F7", "F82")) or code.startswith("E0")
        diagnostics.append(diagnostic.with_changes(severity=ERROR if is_error else WARNING))
    return diagnostics


def _parse_ruff_json(output: str, cwd: Optional[str]) -> list[Diagnostic]:
    data = json.loads(output or "[]")
    diagnostics = []
    for item in data if isinstance(data, list) else []:
        code = item.get("code") or ""
        location = item.get("location") or {}
        # Ruff's own "severity" field says "error" for every finding, even
        # unused imports, so it is not used. Syntax errors carry a non-rule
        # code ("invalid-syntax", null, or E999 in old versions); of the
        # rule codes, only undefined names etc. are real errors.
        is_error = not _RULE_CODE_RE.match(code) or code.startswith(("F82", "F63", "F7", "E9"))
        diagnostics.append(
            Diagnostic(
                severity=ERROR if is_error else WARNING,
                message=str(item.get("message", "")),
                file=_resolve(str(item.get("filename", "")), cwd) if item.get("filename") else None,
                line=location.get("row"),
                column=location.get("column"),
                code=code or None,
                source="ruff",
            )
        )
    return diagnostics


def _parse_eslint_json(output: str, cwd: Optional[str]) -> list[Diagnostic]:
    data = json.loads(output or "[]")
    diagnostics = []
    for file_result in data if isinstance(data, list) else []:
        for message in file_result.get("messages", []):
            diagnostics.append(
                Diagnostic(
                    severity=ERROR if message.get("severity") == 2 else WARNING,
                    message=str(message.get("message", "")),
                    file=_resolve(str(file_result.get("filePath", "")), cwd),
                    line=message.get("line"),
                    column=message.get("column"),
                    code=message.get("ruleId"),
                    source="eslint",
                )
            )
    return diagnostics


PARSERS: dict[str, Callable[[str, Optional[str]], list[Diagnostic]]] = {
    "ruff-json": _parse_ruff_json,
    "eslint-json": _parse_eslint_json,
    "msbuild": _regex_parser(_MSBUILD_RE),
    "gcc": _regex_parser(_GCC_RE),
    "flake8": _parse_flake8,
    "javac": _regex_parser(_JAVAC_RE),
}


def parse_tool_output(spec: BuildToolSpec, output: str) -> list[Diagnostic]:
    """Parse tool output, dropping exact repeats (``dotnet build`` prints
    every error twice: inline and again in the summary)."""
    unique: dict[tuple, Diagnostic] = {}
    for diagnostic in PARSERS[spec.parser](output, spec.cwd):
        diagnostic = diagnostic.with_changes(
            source=diagnostic.source or spec.name,
            application=f"tool:{spec.name}",
            language=diagnostic.language or spec.language,
        )
        identity = (diagnostic.file, diagnostic.line, diagnostic.column, diagnostic.code, diagnostic.message, diagnostic.severity)
        unique.setdefault(identity, diagnostic)
    return list(unique.values())


Runner = Callable[[BuildToolSpec], str]


def run_tool(spec: BuildToolSpec) -> str:
    """Run one configured tool (no shell) and return stdout + stderr."""
    executable = shutil.which(spec.command[0])
    if executable is None:
        raise FileNotFoundError(f"'{spec.command[0]}' is not installed or not on PATH")
    if spec.cwd and not os.path.isdir(spec.cwd):
        raise FileNotFoundError(f"working folder does not exist: {spec.cwd}")
    result = subprocess.run(
        [executable, *spec.command[1:]],
        cwd=spec.cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=spec.timeout_seconds,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Linters exit non-zero when they find problems; that is not a failure.
    return ((result.stdout or "") + "\n" + (result.stderr or ""))[:MAX_OUTPUT_CHARS]


class BuildToolsProvider(Provider):
    name = "buildtools"
    display_name = "Build & lint tools"

    def __init__(self, tool_configs: list, runner: Runner = run_tool, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.specs: list[BuildToolSpec] = []
        self.config_errors: list[str] = []
        for raw in tool_configs or []:
            try:
                spec = BuildToolSpec.from_config(raw if isinstance(raw, dict) else {})
            except (ValueError, TypeError) as exc:
                self.config_errors.append(f"{(raw or {}).get('name', '?') if isinstance(raw, dict) else '?'}: {exc}")
                continue
            if spec.enabled:
                self.specs.append(spec)
        self._runner = runner
        self._clock = clock
        self._last_run: dict[str, float] = {}
        self._results: dict[str, str] = {}
        self._run_all_requested = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, tracker) -> None:
        super().start(tracker)
        self._stop.clear()
        if self.specs:
            self._thread = threading.Thread(target=self._run, name="build-tools-provider", daemon=True)
            self._thread.start()
        self._refresh_status()

    def stop(self) -> None:
        self._stop.set()
        self._run_all_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        super().stop()

    def request_run_all(self) -> None:
        """User clicked "Run checks now"."""
        self._run_all_requested.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            run_all = self._run_all_requested.is_set()
            self._run_all_requested.clear()
            now = self._clock()
            for spec in self.specs:
                if self._stop.is_set():
                    return
                last = self._last_run.get(spec.name)
                due = spec.interval_seconds > 0 and (last is None or now - last >= spec.interval_seconds)
                if run_all or due:
                    self.run_spec(spec)
            self._run_all_requested.wait(1.0)

    def run_spec(self, spec: BuildToolSpec) -> None:
        self._last_run[spec.name] = self._clock()
        try:
            output = self._runner(spec)
            diagnostics = parse_tool_output(spec, output)
        except Exception as exc:
            # A broken tool keeps its last results (as stale) and never
            # affects other tools or providers.
            _logger.warning("Build tool '%s' failed: %s", spec.name, exc)
            self._results[spec.name] = f"failed: {exc}"
            if self.tracker is not None:
                self.tracker.mark_stale(self.name, spec.name)
        else:
            self._results[spec.name] = f"{len(diagnostics)} issue(s)"
            if self.tracker is not None:
                self.tracker.update(self.name, spec.name, diagnostics)
        self._refresh_status()

    def _refresh_status(self) -> None:
        if not self.specs:
            detail = "no tools configured"
            if self.config_errors:
                detail = "config errors: " + "; ".join(self.config_errors)
            self.set_status(STATE_ERROR if self.config_errors else STATE_IDLE, detail)
            return
        parts = [f"{spec.name}: {self._results.get(spec.name, 'not run yet')}" for spec in self.specs]
        failed = any(result.startswith("failed") for result in self._results.values())
        state = STATE_ERROR if failed else (STATE_ACTIVE if self._results else STATE_IDLE)
        self.set_status(state, "; ".join(parts))
