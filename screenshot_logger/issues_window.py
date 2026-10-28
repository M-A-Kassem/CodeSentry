"""Detected-issues window: Current / New this session / History tabs."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from .coding.explain import Explanation, explain, load_source
from .coding.issue_tracker import IssueTracker, TrackedIssue

HISTORY_SOURCE_PREFIX = "Coding assistant"
CONFIDENCE_TAGS = {"high": "", "medium": "[likely] ", "low": "[uncertain] "}


def explanation_text(explanation: Optional[Explanation]) -> str:
    if explanation is None:
        return "(no explanation: not enough evidence)"
    return CONFIDENCE_TAGS[explanation.confidence] + explanation.summary


def issue_status(issue: TrackedIssue, session_tab: bool = False) -> str:
    flags = []
    if session_tab:
        flags.append("fixed" if issue.resolved else "still present")
    else:
        if issue.stale:
            flags.append("stale")
        if issue.possible:
            flags.append(f"possible ({issue.diagnostic.confidence:.0%})")
        if issue.baseline:
            flags.append("pre-existing")
    return ", ".join(flags) or "current"


def issue_row(issue: TrackedIssue, session_tab: bool = False, explanation: Optional[Explanation] = None) -> tuple[str, ...]:
    d = issue.diagnostic
    return (
        d.severity.upper(),
        d.message.splitlines()[0][:200] if d.message else "",
        explanation_text(explanation),
        d.location_text(),
        d.language or "",
        d.source or issue.provider,
        issue_status(issue, session_tab),
        issue.first_seen_at.strftime("%H:%M:%S"),
    )


def issue_details(issue: TrackedIssue, project_root: Optional[str] = None,
                  buffers: Optional[dict[str, str]] = None) -> str:
    d = issue.diagnostic
    lines = [
        f"Severity: {d.severity}      Source: {d.source or issue.provider}      Language: {d.language or 'unknown'}",
        f"File: {d.file or 'n/a'}",
        f"Line/column: {d.line or '-'}:{d.column or '-'}      Code: {d.code or 'n/a'}",
        "",
        "Diagnostic message (original):",
        f"  {d.message}",
        "",
    ]
    source = load_source(d, project_root, buffers)
    if source and source.snippet():
        lines.append("Source:")
        width = len(str(source.snippet()[-1][0]))
        for number, text in source.snippet():
            marker = ">" if number == d.line else " "
            lines.append(f" {marker} {number:>{width}} | {text}")
        lines.append("")
    explanation = explain(d, project_root, buffers)
    lines.append("Possible explanation:")
    if explanation is None:
        lines.append("  None. There is not enough evidence to explain this reliably; the diagnostic message above is "
                     "the best available information.")
    else:
        lines.append(f"  {explanation.summary}")
        lines += [f"  - {detail}" for detail in explanation.details]
        lines.append(f"  Confidence: {explanation.confidence} ({explanation.label})")
    lines += [
        "",
        f"Reported by: {issue.provider}   Scope: {issue.scope}",
        f"Detection confidence: {d.confidence:.2f}" + ("  (below threshold: not counted)" if issue.possible else ""),
        f"First seen: {issue.first_seen_at.isoformat(timespec='seconds')}   Status: {issue_status(issue)}",
    ]
    return "\n".join(lines)


class IssuesWindow(tk.Toplevel):
    COLUMNS = ("severity", "message", "explanation", "location", "language", "source", "status", "seen")
    WIDTHS = (70, 320, 360, 220, 80, 150, 110, 60)

    def __init__(
        self,
        master: tk.Misc,
        tracker: IssueTracker,
        load_history: Callable[[], list[dict[str, Any]]],
        on_reset: Callable[[], None],
        on_open_history_record: Callable[[dict[str, Any]], None],
        project_root_fn: Callable[[], Optional[str]] = lambda: None,
        coverage_fn: Callable[[], str] = lambda: "",
        buffers_fn: Callable[[], dict[str, str]] = dict,
    ) -> None:
        super().__init__(master)
        self.title("Detected Issues")
        self.geometry("1280x520")
        self.minsize(700, 300)
        self.tracker = tracker
        self.load_history = load_history
        self.on_reset = on_reset
        self.on_open_history_record = on_open_history_record
        self.project_root_fn = project_root_fn
        self.coverage_fn = coverage_fn
        self.buffers_fn = buffers_fn
        self._explanations: dict[tuple, Optional[Explanation]] = {}
        self._rendered_version: Optional[int] = None
        self._current: list[TrackedIssue] = []
        self._session: list[TrackedIssue] = []
        self._history: list[dict[str, Any]] = []

        self.summary = tk.Label(self, anchor="w", font=("Segoe UI", 10))
        self.summary.pack(fill="x", padx=10, pady=(8, 2))
        self.coverage = tk.Label(self, anchor="w", justify="left", fg="#555555", wraplength=1200)
        self.coverage.pack(fill="x", padx=10, pady=(0, 2))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=4)
        self.current_tree = self._make_tree("Current issues")
        self.session_tree = self._make_tree("New this session")
        history_frame = ttk.Frame(self.notebook)
        self.notebook.add(history_frame, text="History (saved)")
        self.history_tree = ttk.Treeview(history_frame, columns=("timestamp", "severity", "title", "source"), show="headings")
        for column, width in (("timestamp", 160), ("severity", 80), ("title", 600), ("source", 220)):
            self.history_tree.heading(column, text=column.capitalize())
            self.history_tree.column(column, width=width, anchor="w")
        self.history_tree.pack(fill="both", expand=True)
        self.history_tree.bind("<Double-1>", lambda _e: self._open_history())
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab())

        bar = tk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 8))
        tk.Button(bar, text="Details", command=self._details).pack(side="left", padx=3)
        tk.Button(bar, text="Reset session count", command=self._reset).pack(side="left", padx=3)
        tk.Button(bar, text="Refresh", command=lambda: self.refresh(force=True)).pack(side="left", padx=3)
        tk.Label(
            bar,
            text="Stale = editor not responding/closed (not counted).  Possible = low-confidence screen detection (not counted).",
            fg="#666666",
        ).pack(side="right")
        self.refresh(force=True)

    def _make_tree(self, label: str) -> ttk.Treeview:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)
        tree = ttk.Treeview(frame, columns=self.COLUMNS, show="headings", selectmode="browse")
        for column, width in zip(self.COLUMNS, self.WIDTHS):
            tree.heading(column, text=column.capitalize())
            tree.column(column, width=width, anchor="w")
        tree.tag_configure("error", foreground="#c42b1c")
        tree.tag_configure("warning", foreground="#9d5d00")
        tree.tag_configure("dim", foreground="#8a8a8a")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda _e: self._details())
        return tree

    def refresh(self, force: bool = False) -> None:
        if not force and self.tracker.version == self._rendered_version:
            return
        self._rendered_version = self.tracker.version
        self._current = self.tracker.current_issues()
        self._session = self.tracker.session_new_issues()
        self._fill(self.current_tree, self._current, False, self._explain)
        self._fill(self.session_tree, self._session, True, self._explain)
        self.coverage.configure(text=self.coverage_fn())
        counts = self.tracker.counts()
        self.summary.configure(
            text=(
                f"Current: {counts.errors} error(s), {counts.warnings} warning(s)"
                f"   ·   New this session: {counts.new_errors} error(s), {counts.new_warnings} warning(s)"
                f"   ·   Possible (unconfirmed): {counts.possible}"
                f"   ·   Session started {self.tracker.session_started_at:%H:%M:%S}"
            )
        )
        if force:
            self._load_history()

    def _explain(self, issue: TrackedIssue) -> Optional[Explanation]:
        d = issue.diagnostic
        try:
            stamp = os.path.getmtime(d.file) if d.file else 0
        except OSError:
            stamp = 0
        cache_key = (issue.key, d.line, d.message, stamp)
        if cache_key not in self._explanations:
            if len(self._explanations) > 5000:
                self._explanations.clear()
            self._explanations[cache_key] = explain(d, self.project_root_fn(), self.buffers_fn())
        return self._explanations[cache_key]

    @staticmethod
    def _fill(tree: ttk.Treeview, issues: list[TrackedIssue], session_tab: bool,
              explain_fn: Callable[[TrackedIssue], Optional[Explanation]]) -> None:
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for index, issue in enumerate(issues):
            dim = issue.stale or issue.possible or (session_tab and issue.resolved)
            tag = "dim" if dim else issue.diagnostic.severity
            tree.insert("", "end", iid=str(index), values=issue_row(issue, session_tab, explain_fn(issue)), tags=(tag,))
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])

    def _on_tab(self) -> None:
        if self.notebook.index("current") == 2:
            self._load_history()

    def _load_history(self) -> None:
        self._history = [
            r for r in self.load_history() if str(r.get("source") or "").startswith(HISTORY_SOURCE_PREFIX)
        ]
        self.history_tree.delete(*self.history_tree.get_children())
        for index, record in enumerate(self._history):
            self.history_tree.insert(
                "", "end", iid=str(index),
                values=(record.get("timestamp", ""), record.get("severity", ""), record.get("title", ""), record.get("source", "")),
            )

    def _selected_issue(self) -> Optional[TrackedIssue]:
        tab = self.notebook.index("current")
        if tab == 2:
            self._open_history()
            return None
        tree, issues = (self.current_tree, self._current) if tab == 0 else (self.session_tree, self._session)
        selection = tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return issues[index] if index < len(issues) else None

    def _details(self) -> None:
        issue = self._selected_issue()
        if issue is None:
            return
        window = tk.Toplevel(self)
        window.title("Issue details")
        window.geometry("820x520")
        text = tk.Text(window, wrap="word", font=("Consolas", 10))
        text.insert("1.0", issue_details(issue, self.project_root_fn(), self.buffers_fn()))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=8)

    def _open_history(self) -> None:
        selection = self.history_tree.selection()
        if selection:
            self.on_open_history_record(self._history[int(selection[0])])

    def _reset(self) -> None:
        self.on_reset()
        self.refresh(force=True)
