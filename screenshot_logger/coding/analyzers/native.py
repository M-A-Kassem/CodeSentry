"""C/C++, Java and Go analyzers (only used if the tools are installed).

- C/C++: ``clang``/``gcc`` with ``-fsyntax-only`` (parses and type-checks,
  writes nothing). Include paths from the build system are unknown, so a
  "file not found" for a header is reported as a coverage note, not an
  error.
- Java: ``javac -proc:none`` (annotation processors, which are code, are
  disabled) with output to a private temp folder. In Maven/Gradle
  projects dependencies can't be resolved without running the build, so
  only syntax errors are reported there.
- Go: ``gofmt -e -l`` reports syntax errors without rewriting files.
"""
from __future__ import annotations

import os
import re

from ..diagnostic import Diagnostic
from ..providers.build_tools import _GCC_RE, _JAVAC_RE, _regex_parser
from .base import Analyzer, AnalyzerRun, Availability, RunContext, chunks, which

_MISSING_HEADER_RE = re.compile(r"file not found|No such file or directory", re.I)
_JAVA_SYNTAX_RE = re.compile(
    r"expected|illegal start|not a statement|unclosed|reached end of file|class, interface, enum, or record expected|"
    r"orphaned|illegal character|malformed", re.I)
_GO_ERROR_RE = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:]+\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")


def first_available(*names: str):
    for name in names:
        path = which(name)
        if path:
            return path
    return None


class ClangGccAnalyzer(Analyzer):
    name = "c-cpp-syntax"
    display_name = "C/C++ compiler check (-fsyntax-only)"
    languages = ("C", "C++", "C/C++")
    scope = "file"
    kind = "compiler"
    priority = 60
    install_hint = "install LLVM clang or MinGW-w64 gcc"

    def availability(self, profile) -> Availability:
        path = first_available("clang", "gcc")
        return Availability(path is not None, path or "no clang or gcc found")

    def applies_to(self, source) -> bool:
        return source.path.lower().endswith((".c", ".cc", ".cpp", ".cxx"))  # headers are checked via sources

    def run(self, context: RunContext, files) -> AnalyzerRun:
        c_compiler = first_available("clang", "gcc")
        cpp_compiler = first_available("clang++", "g++") or c_compiler
        result = AnalyzerRun()
        include_dirs = sorted({os.path.dirname(f.path) for f in context.profile.files.values()
                               if f.path.lower().endswith((".h", ".hpp"))})
        includes = [f"-I{d}" for d in include_dirs[:50]]
        missing = 0
        for source in files:
            compiler = c_compiler if source.path.lower().endswith(".c") else cpp_compiler
            output = context.runner([compiler, "-fsyntax-only", *includes, source.path], context.profile.root, 60).output
            result.checked_files.add(source.path)
            for diagnostic in _regex_parser(_GCC_RE)(output, context.profile.root):
                if _MISSING_HEADER_RE.search(diagnostic.message):
                    missing += 1
                    continue
                result.diagnostics.append(diagnostic.with_changes(source=self.name))
        if missing:
            result.partial_reason = f"{missing} header(s) not found (include paths come from the build system)"
        return result


class JavacAnalyzer(Analyzer):
    name = "javac"
    display_name = "Java compiler (javac, no annotation processing)"
    languages = ("Java",)
    kind = "compiler"
    priority = 70
    install_hint = "install a JDK (e.g. Eclipse Temurin)"

    def availability(self, profile) -> Availability:
        path = which("javac")
        return Availability(path is not None, path or "javac (JDK) is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        build_tool = any(os.path.basename(m) in ("pom.xml", "build.gradle", "build.gradle.kts") for m in context.profile.manifests)
        result = AnalyzerRun(checked_files={f.path for f in files})
        out_dir = os.path.join(context.temp_dir, "javac")
        os.makedirs(out_dir, exist_ok=True)
        for group in chunks([f.path for f in files], 500):
            argfile = os.path.join(context.temp_dir, "javac-files.txt")
            with open(argfile, "w", encoding="utf-8") as handle:
                handle.write("\n".join(f'"{p}"'.replace("\\", "/") for p in group))
            output = context.runner([which("javac") or "javac", "-proc:none", "-encoding", "UTF-8", "-Xlint:none",
                                     "-d", out_dir, f"@{argfile}"], context.profile.root, context.timeout).output
            for diagnostic in _regex_parser(_JAVAC_RE)(output, context.profile.root):
                if build_tool and not _JAVA_SYNTAX_RE.search(diagnostic.message):
                    continue
                result.diagnostics.append(diagnostic.with_changes(source=self.name))
        if build_tool:
            result.partial_reason = "Maven/Gradle project: dependencies aren't resolved without running the build, so only syntax errors are reported"
        return result


class GofmtAnalyzer(Analyzer):
    name = "gofmt"
    display_name = "Go syntax check (gofmt -e)"
    languages = ("Go",)
    kind = "syntax"
    priority = 40
    install_hint = "install Go (https://go.dev)"

    def availability(self, profile) -> Availability:
        path = which("gofmt")
        return Availability(path is not None, path or "Go is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        result = AnalyzerRun(checked_files={f.path for f in files},
                             partial_reason="syntax only (type checking needs `go vet`, which may download modules)")
        for group in chunks([f.path for f in files], 200):
            output = context.runner([which("gofmt") or "gofmt", "-e", "-l", *group], context.profile.root, context.timeout).output
            for line in output.splitlines():
                match = _GO_ERROR_RE.match(line.strip())
                if match:
                    result.diagnostics.append(Diagnostic("error", match.group("msg"), file=match.group("file"),
                                                         line=int(match.group("line")), column=int(match.group("col")),
                                                         source=self.name, code="syntax", language="Go"))
        return result
