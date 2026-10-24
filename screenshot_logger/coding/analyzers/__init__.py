"""Analyzer registry and coverage reporting.

To support another language, implement an
:class:`~screenshot_logger.coding.analyzers.base.Analyzer` and add it to
:func:`all_analyzers`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..project import ProjectProfile
from .base import Analyzer, AnalyzerRun, Availability
from .csharp import CSharpCompilerAnalyzer, DotnetBuildAnalyzer
from .data import JsonAnalyzer, XmlAnalyzer
from .native import ClangGccAnalyzer, GofmtAnalyzer, JavacAnalyzer
from .python import MypyAnalyzer, PyrightAnalyzer, PythonSyntaxAnalyzer, RuffAnalyzer
from .web import ESLintAnalyzer, NodeSyntaxAnalyzer, TypeScriptAnalyzer

# Detected-but-unanalyzable languages get these suggestions.
SUGGESTIONS = {
    "Rust": "cargo check (not run automatically: it executes build.rs scripts; add it under build_tools if you accept that)",
    "Kotlin": "a Gradle build (not run automatically: it executes build scripts)",
    "PHP": "php -l (install PHP)",
    "Ruby": "ruby -wc (install Ruby)",
    "Swift": "swiftc -parse (install Swift)",
    "Visual Basic": "dotnet build (enable the 'dotnet-build' analyzer)",
    "F#": "dotnet build (enable the 'dotnet-build' analyzer)",
    "PowerShell": "PSScriptAnalyzer (Install-Module PSScriptAnalyzer)",
    "Shell": "shellcheck",
    "SQL": "a database-specific linter such as sqlfluff",
    "YAML": "yamllint",
    "HTML": "an HTML validator",
    "CSS": "stylelint",
    "SCSS": "stylelint",
}

# Documentation and solution files: listed, but not expected to be checked.
NOT_CODE = frozenset({"Markdown", "Solution"})


def all_analyzers() -> list[Analyzer]:
    return [
        PythonSyntaxAnalyzer(), RuffAnalyzer(), PyrightAnalyzer(), MypyAnalyzer(),
        CSharpCompilerAnalyzer(), DotnetBuildAnalyzer(),
        NodeSyntaxAnalyzer(), TypeScriptAnalyzer(), ESLintAnalyzer(),
        ClangGccAnalyzer(), JavacAnalyzer(), GofmtAnalyzer(),
        JsonAnalyzer(), XmlAnalyzer(),
    ]


def is_enabled(analyzer: Analyzer, overrides: dict) -> bool:
    value = overrides.get(analyzer.name)
    return analyzer.default_enabled if value is None else bool(value)


@dataclass
class LanguageCoverage:
    language: str
    files: int
    status: str  # "checked" | "partial" | "not checked" | "no analyzer" | "not code"
    used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{self.language} ({self.files} file{'s' if self.files != 1 else ''}): {self.status}"
        if self.used:
            text += " — " + ", ".join(self.used)
        return text


def compute_coverage(
    profile: ProjectProfile,
    analyzers: list[Analyzer],
    overrides: dict,
    availability: dict[str, Availability],
    runs: dict[str, AnalyzerRun],
) -> list[LanguageCoverage]:
    report: list[LanguageCoverage] = []
    for language, count in profile.languages().items():
        sample = next(f for f in profile.files.values() if f.language == language)
        relevant = [a for a in analyzers if a.applies_to(sample) or language in a.languages]
        coverage = LanguageCoverage(language, count, "no analyzer")
        if language in NOT_CODE:
            coverage.status = "not code"
            report.append(coverage)
            continue
        if not relevant:
            hint = SUGGESTIONS.get(language)
            coverage.notes.append("no analyzer for this language is built in")
            if hint:
                coverage.suggestions.append(hint)
            report.append(coverage)
            continue
        partial = False
        for analyzer in relevant:
            available = availability.get(analyzer.name, Availability(False))
            if not is_enabled(analyzer, overrides):
                if available.available:
                    coverage.notes.append(f"{analyzer.display_name} is available but off: {analyzer.consent_reason}")
                continue
            if not available.available:
                if analyzer.install_hint:
                    coverage.suggestions.append(f"{analyzer.display_name}: {analyzer.install_hint}")
                continue
            run = runs.get(analyzer.name)
            if run is None:
                continue
            if run.error:
                coverage.notes.append(f"{analyzer.display_name} failed: {run.error}")
                partial = True
                continue
            coverage.used.append(analyzer.display_name)
            if run.partial_reason:
                coverage.notes.append(run.partial_reason)
                partial = True
            failed = [p for p in run.failed_files if profile.files.get(p, None) and profile.files[p].language == language]
            if failed:
                partial = True
        if coverage.used:
            coverage.status = "partial" if partial else "checked"
        else:
            coverage.status = "not checked"
            if not coverage.suggestions and not coverage.notes:
                coverage.notes.append("no compatible analyzer ran")
        report.append(coverage)
    return report


__all__ = ["Analyzer", "AnalyzerRun", "LanguageCoverage", "all_analyzers", "compute_coverage", "is_enabled"]
