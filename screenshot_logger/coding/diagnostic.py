"""Provider-neutral diagnostic model.

Every detection provider (VS Code bridge, Visual Studio, build tools,
screen detection) converts what it observes into :class:`Diagnostic`
objects, so the issue tracker never needs to know where a diagnostic came
from in order to deduplicate and count it.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from typing import Optional

ERROR = "error"
WARNING = "warning"
INFO = "info"
SEVERITIES = (ERROR, WARNING, INFO)

# Diagnostics from an editor API or a compiler are facts; screen detection
# is a guess. Anything below the configured threshold is shown as
# "possible" and never counted as an error or recorded as an incident.
CONFIRMED = 1.0

_EXTENSION_LANGUAGES = {
    ".cs": "C#", ".csx": "C#", ".vb": "Visual Basic", ".fs": "F#",
    ".py": "Python", ".pyw": "Python", ".ipynb": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".json": "JSON", ".xml": "XML",
    ".xaml": "XAML", ".razor": "Razor", ".cshtml": "Razor", ".sql": "SQL",
    ".ps1": "PowerShell", ".sh": "Shell", ".yaml": "YAML", ".yml": "YAML", ".md": "Markdown",
    ".csproj": "MSBuild", ".vbproj": "MSBuild", ".sln": "Solution",
}

_WHITESPACE_RE = re.compile(r"\s+")


def language_for_path(path: Optional[str]) -> Optional[str]:
    """Best-effort language name from a file extension."""
    if not path:
        return None
    return _EXTENSION_LANGUAGES.get(os.path.splitext(path)[1].lower())


def normalize_severity(value: object) -> str:
    """Map assorted severity spellings onto error/warning/info."""
    text = str(value or "").strip().lower()
    if text in ("error", "fatal", "critical", "e", "err", "high"):
        return ERROR
    if text in ("warning", "warn", "w", "medium"):
        return WARNING
    return INFO


def normalize_path(path: Optional[str]) -> str:
    """Case- and separator-insensitive form of a path, for fingerprints."""
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path))


@dataclass(frozen=True)
class Diagnostic:
    """One error/warning as reported by a provider."""

    severity: str
    message: str
    file: Optional[str] = None
    line: Optional[int] = None  # 1-based
    column: Optional[int] = None  # 1-based
    source: Optional[str] = None  # e.g. "Pylance", "ruff", "CSC"
    code: Optional[str] = None  # e.g. "CS0103", "F821"
    language: Optional[str] = None
    application: Optional[str] = None  # e.g. "vscode", "visualstudio"
    confidence: float = CONFIRMED
    # Overrides the message in the fingerprint. Used for OCR text, which
    # varies slightly between frames ("CS0103" vs "CS01O3").
    identity: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", normalize_severity(self.severity))
        if self.language is None and self.file:
            object.__setattr__(self, "language", language_for_path(self.file))

    def with_changes(self, **changes) -> "Diagnostic":
        return replace(self, **changes)

    def base_fingerprint(self) -> str:
        """Identity of a diagnostic, deliberately excluding line/column.

        Editing lines above an error moves it without changing what it is;
        including the line number would make every such edit look like a
        brand-new error. Two identical messages in the same file are told
        apart by the tracker with an occurrence index instead.
        """
        message = self.identity or _WHITESPACE_RE.sub(" ", self.message).strip()
        parts = (
            normalize_path(self.file),
            self.severity,
            (self.code or "").strip(),
            message,
            # File-less diagnostics (screen detection, some build output)
            # are scoped to the application so two editors never merge.
            "" if self.file else (self.application or ""),
        )
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]

    def location_text(self) -> str:
        if not self.file:
            return ""
        location = self.file
        if self.line:
            location += f":{self.line}"
            if self.column:
                location += f":{self.column}"
        return location
