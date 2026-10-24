"""Analyzer framework: discovery, safe execution, coverage reporting.

An analyzer checks the files of one or more languages and returns
:class:`~screenshot_logger.coding.diagnostic.Diagnostic` objects. To add a
language, write an :class:`Analyzer` subclass and add it to
``analyzers/__init__.py``.

Safety rules every analyzer follows:

- Only tools that are already installed are used; nothing is installed or
  downloaded. A missing tool is reported with an optional suggestion.
- Commands are argument lists run without a shell (:func:`run_command`),
  with a timeout, in the project folder, and never via a shell or script
  interpreter given inline code.
- Analyzers must be read-only: syntax checks, static analysis, or a
  compiler writing only into a private temporary folder. Anything that
  would execute project code (MSBuild targets, ESLint's JavaScript config,
  mypy plugins, build scripts) is **off by default** and declares why in
  ``consent_reason``; the user can enable it in ``config.json``.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..diagnostic import Diagnostic
from ..project import ProjectProfile, SourceFile

MAX_OUTPUT_CHARS = 5_000_000

# Shells and interpreters that could run arbitrary inline code. Analyzer
# commands may never start with these.
UNSAFE_EXECUTABLES = frozenset({
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "bash", "bash.exe", "sh", "sh.exe", "zsh", "wsl", "wsl.exe", "wscript", "cscript",
    "mshta", "rundll32", "regsvr32", "start",
})
# Interpreters may run analyzers (node --check) but never inline code.
INTERPRETERS = frozenset({"python", "python3", "py", "pythonw", "node", "deno", "bun", "ruby", "perl", "php"})
INLINE_CODE_ARGUMENTS = frozenset({"-c", "-e", "-p", "--eval", "--print", "-r", "--require", "-m"})


class UnsafeCommand(ValueError):
    pass


def check_command(argv: Sequence[str]) -> None:
    """Refuse commands that could run arbitrary code or need a shell."""
    if isinstance(argv, str) or not argv or not all(isinstance(a, str) for a in argv):
        raise UnsafeCommand("command must be a non-empty list of strings (no shell strings)")
    executable = os.path.basename(argv[0]).lower()
    if executable in UNSAFE_EXECUTABLES or os.path.splitext(executable)[0] in UNSAFE_EXECUTABLES:
        raise UnsafeCommand(f"'{argv[0]}' is a shell or script host and is never run")
    if os.path.splitext(executable)[0] in INTERPRETERS and any(a.lower() in INLINE_CODE_ARGUMENTS for a in argv[1:]):
        raise UnsafeCommand("interpreters may not be given inline code or modules to run")
    for arg in argv:
        if any(ch in arg for ch in "\n\r\0"):
            raise UnsafeCommand("control characters in arguments are not allowed")
    # npm-installed tools are .cmd launchers, which Windows runs through
    # cmd.exe; refuse arguments (e.g. odd folder names) that cmd would
    # interpret as commands.
    if executable.endswith((".cmd", ".bat")) and any(any(ch in a for ch in '&|<>^%!"') for a in argv[1:]):
        raise UnsafeCommand("arguments contain characters that cmd.exe would interpret")


@dataclass
class CommandResult:
    returncode: int
    output: str


Runner = Callable[[Sequence[str], str, float], CommandResult]


def run_command(argv: Sequence[str], cwd: str, timeout: float, input_text: Optional[str] = None) -> CommandResult:
    """Run an analyzer: checked, no shell, no console window, time-limited.
    ``input_text`` is fed on stdin (e.g. unsaved editor text for Ruff)."""
    check_command(argv)
    stdin_args = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        **stdin_args,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or ""))[:MAX_OUTPUT_CHARS]
    return CommandResult(result.returncode, output)


def which(name: str) -> Optional[str]:
    return shutil.which(name)


@dataclass
class RunContext:
    profile: ProjectProfile
    temp_dir: str
    runner: Runner = run_command
    timeout: float = 120.0
    # Unsaved editor text, keyed by the project file's path (as in
    # ``profile.files``). Analyzers check this text instead of the disk.
    buffers: dict[str, str] = field(default_factory=dict)
    _shadows: dict[str, str] = field(default_factory=dict)

    def text_of(self, path: str) -> Optional[str]:
        """Current content of a project file: unsaved text if any, else disk."""
        if path in self.buffers:
            return self.buffers[path]
        return read_text(path)

    def materialize(self, path: str) -> str:
        """Path a command-line tool should read for ``path``. For a file with
        unsaved changes, that is a private temporary copy of the editor
        text (the project file itself is never written)."""
        if path not in self.buffers:
            return path
        digest = hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:12]
        folder = os.path.join(self.temp_dir, "unsaved", digest)
        os.makedirs(folder, exist_ok=True)
        shadow = os.path.join(folder, os.path.basename(path))
        with open(shadow, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.buffers[path])
        self._shadows[os.path.normcase(os.path.abspath(shadow))] = path
        return shadow

    def real_path(self, path: Optional[str]) -> Optional[str]:
        """Map a temporary copy's path in tool output back to the project file."""
        if not path:
            return path
        return self._shadows.get(os.path.normcase(os.path.abspath(path)), path)


@dataclass
class AnalyzerRun:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    checked_files: set[str] = field(default_factory=set)
    failed_files: dict[str, str] = field(default_factory=dict)
    # Set when the check could not be complete, e.g. "syntax only".
    partial_reason: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Availability:
    available: bool
    detail: str = ""


class Analyzer:
    name = "analyzer"
    display_name = "Analyzer"
    languages: tuple[str, ...] = ()
    # "file": rerun only for changed files; "project": rerun as a whole.
    scope = "project"
    # What it checks; used for coverage ("syntax" < "lint" < "types"/"compiler").
    kind = "lint"
    # Higher wins when two analyzers report the same problem.
    priority = 50
    default_enabled = True
    consent_reason: Optional[str] = None
    install_hint = ""
    # Heavy analyzers run on "Run Checks Now" or at most every N seconds.
    min_interval_seconds = 0.0
    # True if run() checks unsaved editor text (RunContext.buffers) instead
    # of the file on disk for files with unsaved changes.
    reads_unsaved = False

    def availability(self, profile: ProjectProfile) -> Availability:
        return Availability(True)

    def applies_to(self, source: SourceFile) -> bool:
        return source.language in self.languages

    def run(self, context: RunContext, files: list[SourceFile]) -> AnalyzerRun:
        raise NotImplementedError


def read_text(path: str, limit: int = 2_000_000) -> Optional[str]:
    """Read a project file for analysis; None if too big or unreadable."""
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    for encoding in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]
