"""JavaScript / TypeScript analyzers."""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

from ..diagnostic import ERROR, Diagnostic
from ..providers.build_tools import BuildToolSpec, _parse_eslint_json, parse_tool_output
from .base import Analyzer, AnalyzerRun, Availability, RunContext, which

JAVASCRIPT = "JavaScript"
TYPESCRIPT = "TypeScript"

# node --check can't parse JSX/Flow; such files are skipped (not "passed").
_JSX_RE = re.compile(r"(return|=|\(|=>)\s*\(?\s*<[A-Za-z][\w.]*[\s/>]")
_NODE_LOCATION_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)\s*$")
_NODE_ERROR_RE = re.compile(r"^(?P<type>\w*Error): (?P<msg>.+)$")


def parse_node_check(output: str, path: str) -> list[Diagnostic]:
    """Parse ``node --check`` output, e.g.::

        C:\\proj\\a.js:3
          let x = ;
                  ^
        SyntaxError: Unexpected token ';'
    """
    lines = output.splitlines()
    line_no = column = None
    for index, text in enumerate(lines):
        location = _NODE_LOCATION_RE.match(text)
        if location and line_no is None and os.path.normcase(location.group("file")) == os.path.normcase(path):
            line_no = int(location.group("line"))
            # Node echoes the source line unchanged, then a caret under the error.
            if index + 2 < len(lines) and "^" in lines[index + 2]:
                column = lines[index + 2].index("^") + 1
        error = _NODE_ERROR_RE.match(text.strip())
        if error:
            return [Diagnostic(ERROR, f"{error.group('type')}: {error.group('msg')}", file=path, line=line_no,
                               column=column, source="node-check", code="syntax", language=JAVASCRIPT)]
    return []


class NodeSyntaxAnalyzer(Analyzer):
    """``node --check`` parses a file without running it."""

    name = "node-check"
    display_name = "Node.js syntax check (node --check)"
    languages = (JAVASCRIPT,)
    scope = "file"
    kind = "syntax"
    priority = 40
    reads_unsaved = True
    install_hint = "install Node.js (https://nodejs.org)"
    max_files_per_run = 400

    def availability(self, profile) -> Availability:
        path = which("node")
        return Availability(path is not None, path or "Node.js is not installed")

    def applies_to(self, source) -> bool:
        return source.language == JAVASCRIPT and source.path.lower().endswith((".js", ".mjs", ".cjs"))

    def run(self, context: RunContext, files) -> AnalyzerRun:
        result = AnalyzerRun()
        node = which("node") or "node"
        candidates = []
        for source in files:
            text = context.text_of(source.path)
            if text is None:
                result.failed_files[source.path] = "too large or unreadable"
            elif _JSX_RE.search(text):
                result.failed_files[source.path] = "looks like JSX/Flow, which node --check can't parse"
            else:
                candidates.append(source)
        if len(candidates) > self.max_files_per_run:
            for source in candidates[self.max_files_per_run:]:
                result.failed_files[source.path] = "skipped: too many files changed at once"
            candidates = candidates[: self.max_files_per_run]

        # Unsaved editor text is checked from a temporary copy.
        targets = {source.path: context.materialize(source.path) for source in candidates}

        def check(source):
            return source, context.runner([node, "--check", targets[source.path]], context.profile.root, 30).output

        with ThreadPoolExecutor(max_workers=4) as pool:
            for source, output in pool.map(check, candidates):
                result.checked_files.add(source.path)
                result.diagnostics += [d.with_changes(file=source.path)
                                       for d in parse_node_check(output, targets[source.path])]
        if result.failed_files:
            result.partial_reason = f"{len(result.failed_files)} JavaScript file(s) not checked (JSX/Flow or too large)"
        return result


class TypeScriptAnalyzer(Analyzer):
    name = "tsc"
    display_name = "TypeScript compiler (tsc --noEmit)"
    languages = (TYPESCRIPT,)
    kind = "types"
    priority = 70
    install_hint = "npm install -g typescript"

    def availability(self, profile) -> Availability:
        # Only a globally installed tsc: a project's node_modules/.bin tool
        # would be code installed by the project.
        path = which("tsc")
        return Availability(path is not None, path or "the TypeScript compiler (tsc) is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        tsc = which("tsc") or "tsc"
        configs = [p for p in context.profile.manifests if os.path.basename(p) == "tsconfig.json"]
        result = AnalyzerRun(checked_files={f.path for f in files})
        if configs:
            argv = [tsc, "--noEmit", "--pretty", "false", "-p", min(configs, key=len)]
        else:
            argv = [tsc, "--noEmit", "--pretty", "false", "--skipLibCheck", *[f.path for f in files[:500]]]
            result.partial_reason = "no tsconfig.json: files checked with default compiler options"
        output = context.runner(argv, context.profile.root, context.timeout).output
        spec = BuildToolSpec(name=self.name, command=["tsc"], parser="msbuild", cwd=context.profile.root)
        result.diagnostics = [d.with_changes(source=self.name, application=None) for d in parse_tool_output(spec, output)]
        return result


class ESLintAnalyzer(Analyzer):
    name = "eslint"
    display_name = "ESLint"
    languages = (JAVASCRIPT, TYPESCRIPT)
    kind = "lint"
    priority = 60
    default_enabled = False
    consent_reason = "ESLint executes the project's JavaScript configuration file (eslint.config.js)"
    install_hint = "npm install -g eslint"

    def availability(self, profile) -> Availability:
        path = which("eslint")
        return Availability(path is not None, path or "eslint is not installed")

    def run(self, context: RunContext, files) -> AnalyzerRun:
        output = context.runner([which("eslint") or "eslint", "-f", "json", context.profile.root],
                                context.profile.root, context.timeout).output
        start = output.find("[")
        diagnostics = _parse_eslint_json(output[start:output.rfind("]") + 1] if start >= 0 else "[]", context.profile.root)
        return AnalyzerRun(diagnostics=[d.with_changes(source=self.name) for d in diagnostics],
                           checked_files={f.path for f in files})
