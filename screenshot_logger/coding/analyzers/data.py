"""In-process well-formedness checks for JSON and XML (incl. .csproj/XAML).

Both use Python's standard parsers on the file text; nothing is executed.
ElementTree does not fetch external entities or DTDs.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET

from ..diagnostic import ERROR, Diagnostic
from .base import Analyzer, AnalyzerRun, RunContext

# JSON files that officially allow comments/trailing commas (JSONC).
_JSONC_NAMES = re.compile(r"^(tsconfig|jsconfig)(\..*)?\.json$|^\.eslintrc\.json$|^devcontainer\.json$|^settings\.json$|^launch\.json$|^tasks\.json$|^extensions\.json$", re.I)
XML_EXTENSIONS = (".xml", ".csproj", ".vbproj", ".fsproj", ".vcxproj", ".props", ".targets", ".xaml", ".resx", ".config", ".nuspec", ".pyproj")


class JsonAnalyzer(Analyzer):
    name = "json-syntax"
    display_name = "JSON syntax check (built in)"
    languages = ("JSON",)
    scope = "file"
    kind = "syntax"
    priority = 40
    reads_unsaved = True

    def run(self, context: RunContext, files) -> AnalyzerRun:
        result = AnalyzerRun()
        for source in files:
            if _JSONC_NAMES.match(os.path.basename(source.path)) or source.path.lower().endswith(".jsonc"):
                result.failed_files[source.path] = "JSON-with-comments file: not checked as strict JSON"
                continue
            text = context.text_of(source.path)
            if text is None:
                result.failed_files[source.path] = "too large or unreadable"
                continue
            result.checked_files.add(source.path)
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                result.diagnostics.append(Diagnostic(ERROR, f"Invalid JSON: {exc.msg}", file=source.path, line=exc.lineno,
                                                     column=exc.colno, source=self.name, code="syntax", language="JSON"))
        return result


class XmlAnalyzer(Analyzer):
    name = "xml-syntax"
    display_name = "XML well-formedness check (built in)"
    languages = ("XML", "MSBuild", "XAML")
    scope = "file"
    kind = "syntax"
    priority = 40
    reads_unsaved = True

    def applies_to(self, source) -> bool:
        return source.path.lower().endswith(XML_EXTENSIONS)

    def run(self, context: RunContext, files) -> AnalyzerRun:
        result = AnalyzerRun()
        for source in files:
            text = context.text_of(source.path)
            if text is None:
                result.failed_files[source.path] = "too large or unreadable"
                continue
            result.checked_files.add(source.path)
            try:
                ET.fromstring(text.encode("utf-8"))
            except ET.ParseError as exc:
                line, column = exc.position
                message = re.sub(r":\s*line \d+, column \d+$", "", str(exc))
                result.diagnostics.append(Diagnostic(ERROR, f"Malformed XML: {message}", file=source.path, line=line,
                                                     column=column + 1, source=self.name, code="syntax",
                                                     language=source.language))
        return result
