"""Project source analysis: the primary coding-error detector.

While monitoring, this provider:

1. Re-scans the project's file *metadata* every ``poll_seconds`` to notice
   added, changed and deleted source files (no file contents are read for
   this).
2. When files changed (and stopped changing for one poll, so a save of
   many files is one analysis), reruns only what is affected: per-file
   analyzers for the changed files, project-wide analyzers for languages
   with changes. Heavy, opt-in analyzers run at most every
   ``min_interval_seconds`` or on "Run Checks Now".
3. Merges duplicates reported by different tools, keeps only diagnostics
   for files inside the project, and hands one full snapshot to the issue
   tracker -- which counts each unresolved issue once and drops fixed ones.

It never modifies project files: analyzers are read-only (see
``analyzers/base.py``), and compiler output goes to a private temp folder
deleted on stop.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Callable, Optional

from ..analyzers import LanguageCoverage, all_analyzers, compute_coverage, is_enabled
from ..analyzers.base import Analyzer, AnalyzerRun, Availability, RunContext, Runner, run_command
from ..dedupe import merge_cross_tool
from ..diagnostic import Diagnostic, language_for_path
from ..project import ProjectProfile, SourceFile, UnsafeProjectRoot, scan_project
from .base import STATE_ACTIVE, STATE_ERROR, STATE_IDLE, Provider

_logger = logging.getLogger("screenshot_logger")


class ProjectAnalysisProvider(Provider):
    name = "project"
    display_name = "Project source analysis"

    def __init__(
        self,
        project_fn: Callable[[], Optional[str]],
        analyzers: Optional[list[Analyzer]] = None,
        overrides: Optional[dict] = None,
        extra_excludes: tuple[str, ...] = (),
        max_files: int = 20_000,
        poll_seconds: float = 3.0,
        runner: Runner = run_command,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 180.0,
        buffers_fn: Optional[Callable[[], dict[str, str]]] = None,
    ) -> None:
        super().__init__()
        self.project_fn = project_fn
        # Unsaved editor text ({path: text}), e.g. from Visual Studio. Files
        # with unsaved changes are analyzed from that text, not the disk.
        self.buffers_fn = buffers_fn
        self._buffers: dict[str, str] = {}
        self._buffer_hashes: dict[str, int] = {}
        self.analyzers = analyzers if analyzers is not None else all_analyzers()
        self.overrides = dict(overrides or {})
        self.extra_excludes = tuple(extra_excludes)
        self.max_files = max_files
        self.poll_seconds = max(0.5, poll_seconds)
        self.runner = runner
        self._clock = clock
        self.timeout = timeout

        self.root: Optional[str] = None
        self.profile: Optional[ProjectProfile] = None
        self._availability: dict[str, Availability] = {}
        self._file_results: dict[str, dict[str, list[Diagnostic]]] = {}  # analyzer -> file -> diags
        self._project_results: dict[str, list[Diagnostic]] = {}  # analyzer -> diags
        self._runs: dict[str, AnalyzerRun] = {}
        self._last_heavy_run: dict[str, float] = {}
        self._pending_changes: Optional[set[str]] = None
        self._run_all = False
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._coverage: list[LanguageCoverage] = []
        self.last_analysis_seconds: Optional[float] = None
        self.last_error: Optional[str] = None
        self.temp_dir: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------------

    def start(self, tracker) -> None:
        super().start(tracker)
        self.temp_dir = tempfile.mkdtemp(prefix="sel-analysis-")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="project-analysis", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None
        super().stop()

    def request_run_all(self) -> None:
        """"Run Checks Now": rerun every enabled analyzer, heavy ones too."""
        self._run_all = True
        self._wake.set()

    def wake(self) -> None:
        """Poll now (e.g. the project was switched)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                _logger.warning("Project analysis failed.", exc_info=True)
                self.last_error = str(exc)
                self.set_status(STATE_ERROR, f"analysis failed: {exc}")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    # -- one cycle (public for tests) ---------------------------------------------------

    def poll_once(self) -> bool:
        """Scan for changes and analyze if needed. Returns True if analyzed."""
        root = self.project_fn()
        if root != self.root:
            self._switch(root)
        if root is None:
            self.set_status(STATE_IDLE, "no project selected — choose a folder in the settings window")
            return False
        try:
            profile = scan_project(root, self.extra_excludes, self.max_files)
        except UnsafeProjectRoot as exc:
            self.set_status(STATE_ERROR, str(exc))
            return False

        previous = self.profile
        run_all, self._run_all = self._run_all, False
        if previous is None:
            changed = set(profile.files)
            first = True
        else:
            changed = {p for p, f in profile.files.items() if previous.files.get(p) != f}
            changed |= set(previous.files) - set(profile.files)  # deleted
            first = False
        self.profile = profile
        changed |= self._update_buffers(profile)

        if not first and not run_all:
            # Settle: wait one quiet poll after the last change, so saving
            # many files (or a formatter run) triggers a single analysis.
            if changed:
                self._pending_changes = (self._pending_changes or set()) | changed
                self._refresh_status(analyzing=False, waiting=True)
                return False
            if not self._pending_changes:
                return False
            changed, self._pending_changes = self._pending_changes, None
        self.analyze(profile, changed, full=first or run_all, run_heavy=run_all)
        return True

    def _update_buffers(self, profile: ProjectProfile) -> set[str]:
        """Refresh unsaved editor text; return project files whose unsaved
        text appeared, changed, or went away (saved or discarded)."""
        raw: dict[str, str] = {}
        if self.buffers_fn is not None:
            try:
                raw = self.buffers_fn() or {}
            except Exception:
                _logger.debug("Could not read unsaved editor text.", exc_info=True)
        # Editors may report a path with different letter case; only files
        # inside the project that the scan permitted are used.
        by_norm = {_norm(p): p for p in profile.files}
        buffers = {by_norm[_norm(path)]: text for path, text in raw.items() if _norm(path) in by_norm}
        hashes = {path: hash(text) for path, text in buffers.items()}
        changed = {p for p in set(hashes) | set(self._buffer_hashes) if hashes.get(p) != self._buffer_hashes.get(p)}
        self._buffers, self._buffer_hashes = buffers, hashes
        return changed

    def unsaved_files(self) -> list[str]:
        return sorted(self._buffers)

    def _switch(self, root: Optional[str]) -> None:
        old = self.root
        with self._lock:
            self.root = root
            self.profile = None
            self._file_results.clear()
            self._project_results.clear()
            self._runs.clear()
            self._pending_changes = None
            self._coverage = []
            self._buffers, self._buffer_hashes = {}, {}
        if self.tracker is not None and old is not None:
            self.tracker.clear_scope(self.name, old)  # old project's issues are no longer current
        _logger.info("Project analysis: project is now %s", root or "(none)")

    # -- analysis -------------------------------------------------------------------------

    def analyze(self, profile: ProjectProfile, changed: set[str], full: bool, run_heavy: bool = False) -> None:
        started = self._clock()
        self._refresh_status(analyzing=True)
        context = RunContext(profile, self.temp_dir or tempfile.gettempdir(), self.runner, self.timeout,
                             buffers=dict(self._buffers))
        changed_sources = [profile.files[p] for p in changed if p in profile.files]
        deleted = {p for p in changed if p not in profile.files}

        for analyzer in self.analyzers:
            # Availability is checked even for opt-in analyzers (it only looks
            # for the executable), so coverage can say "available but off".
            if analyzer.name not in self._availability or full:
                try:
                    self._availability[analyzer.name] = analyzer.availability(profile)
                except Exception as exc:
                    self._availability[analyzer.name] = Availability(False, str(exc))
            if not is_enabled(analyzer, self.overrides) or not self._availability[analyzer.name].available:
                continue
            all_files = [f for f in profile.files.values() if analyzer.applies_to(f)]
            if not all_files:
                self._runs.pop(analyzer.name, None)
                self._project_results.pop(analyzer.name, None)
                self._file_results.pop(analyzer.name, None)
                continue
            if analyzer.scope == "file":
                self._run_per_file(analyzer, context, all_files if full else
                                   [f for f in changed_sources if analyzer.applies_to(f)], deleted, profile)
            else:
                affected = full or any(analyzer.applies_to(f) for f in changed_sources) or any(
                    language_for_path(p) in analyzer.languages for p in deleted)
                if not affected:
                    continue
                if analyzer.min_interval_seconds and not run_heavy:
                    last = self._last_heavy_run.get(analyzer.name)
                    if last is not None and self._clock() - last < analyzer.min_interval_seconds:
                        continue
                self._run_project(analyzer, context, all_files)

        self._publish(profile)
        self.last_analysis_seconds = self._clock() - started
        self._refresh_status(analyzing=False)

    def _run_per_file(self, analyzer, context, files: list[SourceFile], deleted: set[str], profile) -> None:
        cache = self._file_results.setdefault(analyzer.name, {})
        for path in list(cache):
            if path in deleted or path not in profile.files:
                del cache[path]
        if not files:
            return
        run = self._safe_run(analyzer, context, files)
        for source in files:
            cache[source.path] = [d for d in run.diagnostics if d.file == source.path]
        previous = self._runs.get(analyzer.name)
        failed = dict(previous.failed_files) if previous else {}
        for source in files:
            failed.pop(source.path, None)
        failed.update(run.failed_files)
        self._runs[analyzer.name] = AnalyzerRun(failed_files=failed, error=run.error,
                                                partial_reason=run.partial_reason or (previous.partial_reason if previous and failed else None))

    def _run_project(self, analyzer, context, files) -> None:
        run = self._safe_run(analyzer, context, files)
        self._last_heavy_run[analyzer.name] = self._clock()
        self._runs[analyzer.name] = run
        if run.error is None:
            self._project_results[analyzer.name] = run.diagnostics
        # On error the previous results are kept (the tool may just be
        # briefly unavailable); coverage reports the failure.

    def _safe_run(self, analyzer, context, files) -> AnalyzerRun:
        try:
            return analyzer.run(context, files)
        except Exception as exc:  # timeout, crash, bad output: never fatal
            _logger.warning("Analyzer %s failed: %s", analyzer.name, exc)
            return AnalyzerRun(error=f"{type(exc).__name__}: {exc}")

    def _publish(self, profile: ProjectProfile) -> None:
        diagnostics: list[Diagnostic] = []
        for per_file in self._file_results.values():
            for items in per_file.values():
                diagnostics.extend(items)
        for items in self._project_results.values():
            diagnostics.extend(items)
        # Only files the scan permitted: never report into excluded folders
        # (a tool may have ignored our exclusions) or outside the project.
        allowed = {_norm(p) for p in profile.files}
        diagnostics = [d for d in diagnostics if d.file and _norm(d.file) in allowed]
        if self._buffers:
            # For a file with unsaved changes, only analyzers that checked
            # the editor text count; others reported on the stale disk copy.
            reads_unsaved = {a.name for a in self.analyzers if a.reads_unsaved}
            unsaved = {_norm(p) for p in self._buffers}
            diagnostics = [
                (d.with_changes(source=f"{d.source} (unsaved)") if _norm(d.file) in unsaved else d)
                for d in diagnostics
                if _norm(d.file) not in unsaved or (d.source or "").split(" ")[0] in reads_unsaved
            ]
        priority = {a.name: a.priority for a in self.analyzers}
        merged = merge_cross_tool(diagnostics, priority)
        with self._lock:
            self._coverage = compute_coverage(profile, self.analyzers, self.overrides, self._availability, self._runs)
        if self.tracker is not None:
            self.tracker.update(self.name, profile.root, merged)

    # -- reporting ---------------------------------------------------------------------------

    def coverage(self) -> list[LanguageCoverage]:
        with self._lock:
            return list(self._coverage)

    def is_partial(self) -> bool:
        return any(c.status in ("partial", "not checked", "no analyzer") for c in self.coverage())

    def _refresh_status(self, analyzing: bool, waiting: bool = False) -> None:
        if self.root is None:
            return
        if analyzing:
            self.set_status(STATE_ACTIVE, f"analyzing {self.root} …")
            return
        coverage = self.coverage()
        checked = [c.language for c in coverage if c.status == "checked"]
        partial = [c.language for c in coverage if c.status == "partial"]
        unchecked = [c.language for c in coverage if c.status in ("not checked", "no analyzer")]
        parts = [self.root]
        if checked:
            parts.append("checked: " + ", ".join(checked))
        if partial:
            parts.append("partially: " + ", ".join(partial))
        if unchecked:
            parts.append("not checked: " + ", ".join(unchecked))
        if self.profile is not None and self.profile.truncated:
            parts.append(f"file limit reached ({self.max_files}); some files not scanned")
        if self._buffers:
            parts.append(f"{len(self._buffers)} unsaved file(s) checked from the editor")
        if waiting:
            parts.append("changes detected, waiting until you pause")
        self.set_status(STATE_ACTIVE, " | ".join(parts))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))
