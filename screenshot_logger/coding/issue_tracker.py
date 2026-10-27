"""Issue tracker: the single source of truth for the error counter.

Providers push *complete snapshots* of what they currently see, per
``(provider, scope)`` -- for example one VS Code window, one Visual Studio
instance, or one configured linter. The tracker diffs each snapshot against
the previous one, so it never matters how often a provider polls: the same
diagnostic reported on every cycle is still one issue.

Three distinct views are maintained, as the UI needs all of them:

1. **Current issues** -- what editors/tools report right now. Goes up and
   down as errors appear and are fixed. This is what ``ERROR n`` shows.
2. **New this session** -- distinct issues first seen *after* monitoring
   started (and after the debounce period). Never decremented when an
   issue is fixed, only by "Reset session count".
3. **History** -- persisted incident records. Not stored here: the tracker
   calls ``on_new_issue`` and the controller writes the incident.

Pre-existing diagnostics: the first snapshot from each scope is a
*baseline*. Its issues are current, but not "new", so starting the monitor
(or switching to an editor that already had errors) never inflates the
new-issue count.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Iterable, Optional

from .dedupe import same_problem
from .diagnostic import ERROR, WARNING, Diagnostic, normalize_path

_logger = logging.getLogger("screenshot_logger")

ScopeKey = tuple[str, str]


@dataclass
class TrackedIssue:
    key: str
    diagnostic: Diagnostic
    provider: str
    scope: str
    first_seen: float
    first_seen_at: datetime = field(default_factory=datetime.now)
    last_seen: float = 0.0
    stale: bool = False
    baseline: bool = False
    possible: bool = False  # below the confidence threshold: never counted
    resolved: bool = False  # only meaningful for session-new issues


@dataclass(frozen=True)
class Counts:
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    possible: int = 0
    new_errors: int = 0
    new_warnings: int = 0
    stale_scopes: int = 0


@dataclass(frozen=True)
class UpdateResult:
    added: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()


def assign_keys(diagnostics: Iterable[Diagnostic]) -> dict[str, Diagnostic]:
    """Give each diagnostic a stable key: ``<fingerprint>#<occurrence>``.

    Identical diagnostics in the same file (same message, different
    lines) are ordered by position and numbered, so two copies of one
    error are two issues -- yet inserting a line above them keeps both
    keys unchanged.
    """
    groups: dict[str, list[Diagnostic]] = defaultdict(list)
    for diagnostic in diagnostics:
        groups[diagnostic.base_fingerprint()].append(diagnostic)
    keyed: dict[str, Diagnostic] = {}
    for fingerprint, group in groups.items():
        group.sort(key=lambda d: (d.line or 0, d.column or 0))
        for index, diagnostic in enumerate(group):
            keyed[f"{fingerprint}#{index}"] = diagnostic
    return keyed


class IssueTracker:
    """Thread-safe; providers call :meth:`update` from their own threads."""

    def __init__(
        self,
        debounce_seconds: float = 3.0,
        min_confidence: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
        on_new_issue: Optional[Callable[[TrackedIssue], None]] = None,
    ) -> None:
        self.debounce_seconds = max(0.0, debounce_seconds)
        self.min_confidence = min_confidence
        self._clock = clock
        self.on_new_issue = on_new_issue
        self._lock = threading.RLock()
        self._version = 0
        self.start_session()

    # -- session lifecycle ---------------------------------------------

    def start_session(self) -> None:
        """Forget everything and begin a fresh monitoring session."""
        with self._lock:
            self._scopes: dict[ScopeKey, dict[str, TrackedIssue]] = {}
            self._stale: set[ScopeKey] = set()
            self._reported: set[ScopeKey] = set()
            self._seen: set[str] = set()
            self._pending: dict[str, float] = {}
            self._new: dict[str, TrackedIssue] = {}
            self.session_started_at = datetime.now()
            self._version += 1

    def reset_session_count(self) -> None:
        """Zero the "new this session" counter. Issues present right now
        are treated as acknowledged, so they will not count as new again."""
        with self._lock:
            self._new.clear()
            self._pending.clear()
            for issues in self._scopes.values():
                self._seen.update(key for key, issue in issues.items() if not issue.possible)
            self._version += 1

    @property
    def version(self) -> int:
        """Increments on every change; lets the UI skip redundant redraws."""
        return self._version

    # -- provider input -------------------------------------------------

    def update(self, provider: str, scope: str, diagnostics: Iterable[Diagnostic]) -> UpdateResult:
        """Replace the full set of diagnostics for one provider scope."""
        now = self._clock()
        scope_key = (provider, scope)
        keyed = assign_keys(diagnostics)
        with self._lock:
            previous = self._scopes.get(scope_key, {})
            is_baseline = scope_key not in self._reported
            self._reported.add(scope_key)
            self._stale.discard(scope_key)

            current: dict[str, TrackedIssue] = {}
            added: list[str] = []
            for key, diagnostic in keyed.items():
                possible = diagnostic.confidence < self.min_confidence
                existing = previous.get(key)
                if existing is not None:
                    issue = replace(existing, diagnostic=diagnostic, last_seen=now, stale=False, possible=possible)
                    if existing.possible and not possible and key not in self._seen and key not in self._pending:
                        # Confirmed only now (e.g. screen text seen in a
                        # second frame): it becomes a candidate new issue.
                        self._pending[key] = now
                else:
                    issue = TrackedIssue(
                        key=key,
                        diagnostic=diagnostic,
                        provider=provider,
                        scope=scope,
                        first_seen=now,
                        last_seen=now,
                        baseline=is_baseline,
                        possible=possible,
                    )
                    added.append(key)
                    if not possible:
                        if is_baseline:
                            self._seen.add(key)
                        elif key not in self._seen and key not in self._pending:
                            self._pending[key] = now
                current[key] = issue

            resolved = [key for key in previous if key not in current]
            self._scopes[scope_key] = current
            self._forget_unpresent_pending(resolved)
            if added or resolved:
                self._version += 1

        self._promote_due()
        return UpdateResult(tuple(added), tuple(resolved))

    def mark_stale(self, provider: str, scope: Optional[str] = None) -> None:
        """A provider lost contact (editor busy/closed, tool failed). Its
        last-known issues stay listed, flagged stale, but are not counted."""
        with self._lock:
            for scope_key in self._scopes:
                if scope_key[0] == provider and (scope is None or scope_key[1] == scope):
                    if scope_key not in self._stale:
                        self._stale.add(scope_key)
                        self._version += 1

    def clear_scope(self, provider: str, scope: Optional[str] = None) -> None:
        """The scope is gone (window closed): its issues are resolved."""
        with self._lock:
            removed: list[str] = []
            for scope_key in list(self._scopes):
                if scope_key[0] == provider and (scope is None or scope_key[1] == scope):
                    removed.extend(self._scopes.pop(scope_key))
                    self._stale.discard(scope_key)
                    self._reported.discard(scope_key)
            self._forget_unpresent_pending(removed)
            if removed:
                self._version += 1

    def scopes(self, provider: str) -> list[str]:
        with self._lock:
            return [scope for (prov, scope) in self._scopes if prov == provider]

    def tick(self) -> list[TrackedIssue]:
        """Promote pending issues that survived the debounce period."""
        return self._promote_due()

    # -- queries ------------------------------------------------------------

    def current_issues(self, include_stale: bool = True) -> list[TrackedIssue]:
        """Unique current issues (merged across providers), errors first."""
        with self._lock:
            merged: dict[str, TrackedIssue] = {}
            for scope_key, issues in self._scopes.items():
                stale = scope_key in self._stale
                if stale and not include_stale:
                    continue
                for key, issue in issues.items():
                    candidate = replace(issue, stale=stale)
                    existing = merged.get(key)
                    if existing is None or (existing.stale and not stale) or (
                        existing.stale == stale and candidate.diagnostic.confidence > existing.diagnostic.confidence
                    ):
                        merged[key] = candidate
        order = {ERROR: 0, WARNING: 1}
        return sorted(
            _merge_across_providers(list(merged.values())),
            key=lambda i: (i.stale, i.possible, order.get(i.diagnostic.severity, 2), i.diagnostic.file or "", i.diagnostic.line or 0),
        )

    def session_new_issues(self) -> list[TrackedIssue]:
        with self._lock:
            present = self._present_keys()
            issues = [replace(issue, resolved=key not in present) for key, issue in self._new.items()]
        return sorted(issues, key=lambda i: i.first_seen, reverse=True)

    def counts(self) -> Counts:
        fresh = self.current_issues(include_stale=False)
        with self._lock:
            new_issues = list(self._new.values())
            stale_scopes = len(self._stale)
        confirmed = [i for i in fresh if not i.possible]
        return Counts(
            errors=sum(1 for i in confirmed if i.diagnostic.severity == ERROR),
            warnings=sum(1 for i in confirmed if i.diagnostic.severity == WARNING),
            infos=sum(1 for i in confirmed if i.diagnostic.severity not in (ERROR, WARNING)),
            possible=sum(1 for i in fresh if i.possible),
            new_errors=sum(1 for i in new_issues if i.diagnostic.severity == ERROR),
            new_warnings=sum(1 for i in new_issues if i.diagnostic.severity == WARNING),
            stale_scopes=stale_scopes,
        )

    # -- internals ----------------------------------------------------------

    def _present_keys(self) -> set[str]:
        """Keys currently reported by at least one non-stale scope."""
        return {
            key
            for scope_key, issues in self._scopes.items()
            if scope_key not in self._stale
            for key, issue in issues.items()
            if not issue.possible
        }

    def _already_known_elsewhere(self, issue: TrackedIssue) -> bool:
        """Same problem already counted, or already present (not new), as
        reported by a *different* provider."""
        for other in self._new.values():
            if other.provider != issue.provider and same_problem(other.diagnostic, issue.diagnostic):
                return True
        for scope_key, issues in self._scopes.items():
            if scope_key[0] == issue.provider or scope_key in self._stale:
                continue
            for key, other in issues.items():
                if key in self._seen and key not in self._new and not other.possible and same_problem(other.diagnostic, issue.diagnostic):
                    return True
        return False

    def _forget_unpresent_pending(self, keys: Iterable[str]) -> None:
        # A diagnostic that vanishes before the debounce elapses was
        # transient (e.g. a half-typed identifier) and is dropped.
        if not keys:
            return
        present = self._present_keys()
        for key in keys:
            if key in self._pending and key not in present:
                del self._pending[key]

    def _promote_due(self) -> list[TrackedIssue]:
        now = self._clock()
        promoted: list[TrackedIssue] = []
        with self._lock:
            if not self._pending:
                return promoted
            present = self._present_keys()
            for key, since in list(self._pending.items()):
                if key not in present:
                    continue  # only present in a stale scope; wait
                if now - since < self.debounce_seconds:
                    continue
                del self._pending[key]
                self._seen.add(key)
                issue = next(
                    issues[key]
                    for scope_key, issues in self._scopes.items()
                    if scope_key not in self._stale and key in issues
                )
                if self._already_known_elsewhere(issue):
                    continue  # e.g. Visual Studio already reported it
                self._new[key] = issue
                promoted.append(issue)
            if promoted:
                self._version += 1

        if self.on_new_issue is not None:
            for issue in promoted:
                try:
                    self.on_new_issue(issue)
                except Exception:
                    _logger.warning("on_new_issue callback failed.", exc_info=True)
        return promoted


def _merge_across_providers(issues: list[TrackedIssue]) -> list[TrackedIssue]:
    """Collapse the same problem reported by two providers (e.g. Visual
    Studio's Error List and the project analysis) into one issue."""
    kept: list[TrackedIssue] = []
    buckets: dict[tuple[str, str], list[int]] = {}
    # Fresh, confirmed, and higher-confidence reports win.
    for issue in sorted(issues, key=lambda i: (i.stale, i.possible, -i.diagnostic.confidence)):
        bucket = buckets.setdefault((normalize_path(issue.diagnostic.file), issue.diagnostic.severity), [])
        for index in bucket:
            other = kept[index]
            if other.provider != issue.provider and same_problem(other.diagnostic, issue.diagnostic):
                break
        else:
            bucket.append(len(kept))
            kept.append(issue)
    return kept
