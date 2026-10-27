"""Issue tracker: diagnostic updates, deduplication, resolution, session
counts, baselines (pre-existing issues), debouncing and staleness."""
from __future__ import annotations

from screenshot_logger.coding.diagnostic import Diagnostic
from screenshot_logger.coding.issue_tracker import IssueTracker, assign_keys


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def err(message, file="C:/p/main.py", line=1, **kw) -> Diagnostic:
    return Diagnostic(severity="error", message=message, file=file, line=line, **kw)


def warn(message, file="C:/p/main.py", line=1, **kw) -> Diagnostic:
    return Diagnostic(severity="warning", message=message, file=file, line=line, **kw)


def make(debounce=0.0, **kw):
    clock = FakeClock()
    new_issues = []
    tracker = IssueTracker(debounce_seconds=debounce, clock=clock, on_new_issue=new_issues.append, **kw)
    return tracker, clock, new_issues


def started(tracker, provider="vscode", scope="w1"):
    """Deliver an empty baseline so later diagnostics count as new."""
    tracker.update(provider, scope, [])


# -- keys / fingerprints ---------------------------------------------------------


def test_fingerprint_ignores_line_moves_and_path_case():
    a = err("undefined name 'x'", file="C:/Proj/Main.py", line=3)
    b = err("undefined name 'x'", file="c:\\proj\\main.py", line=40)
    assert a.base_fingerprint() == b.base_fingerprint()


def test_identical_messages_in_one_file_are_separate_issues():
    keys = assign_keys([err("unused", line=10), err("unused", line=2)])
    assert len(keys) == 2
    # Ordered by position: the earlier line gets occurrence #0.
    assert keys[sorted(keys)[0]].line in (2, 10)
    assert {d.line for d in keys.values()} == {2, 10}


# -- updates and dedup --------------------------------------------------------------


def test_same_diagnostic_on_every_poll_counts_once():
    tracker, _, new_issues = make()
    started(tracker)
    for _ in range(10):
        tracker.update("vscode", "w1", [err("boom")])
    counts = tracker.counts()
    assert counts.errors == 1
    assert counts.new_errors == 1
    assert len(new_issues) == 1


def test_editing_above_an_error_does_not_create_a_new_issue():
    tracker, _, new_issues = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("boom", line=5)])
    tracker.update("vscode", "w1", [err("boom", line=9)])
    assert tracker.counts().new_errors == 1
    assert len(new_issues) == 1
    assert tracker.current_issues()[0].diagnostic.line == 9  # location kept fresh


def test_errors_and_warnings_are_counted_separately():
    tracker, _, _ = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("e1"), err("e2"), warn("w1")])
    counts = tracker.counts()
    assert (counts.errors, counts.warnings) == (2, 1)
    assert (counts.new_errors, counts.new_warnings) == (2, 1)


# -- resolution --------------------------------------------------------------------------


def test_fixed_error_leaves_current_count_but_stays_in_session_history():
    tracker, _, _ = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("boom"), err("other")])
    result = tracker.update("vscode", "w1", [err("other")])
    assert len(result.resolved) == 1
    counts = tracker.counts()
    assert counts.errors == 1
    assert counts.new_errors == 2  # session count is not decremented
    fixed = [i for i in tracker.session_new_issues() if i.resolved]
    assert [i.diagnostic.message for i in fixed] == ["boom"]


def test_error_that_comes_back_is_not_counted_as_new_again():
    tracker, _, new_issues = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("boom")])
    tracker.update("vscode", "w1", [])
    tracker.update("vscode", "w1", [err("boom")])
    assert tracker.counts().new_errors == 1
    assert len(new_issues) == 1


def test_clear_scope_resolves_its_issues():
    tracker, _, _ = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("boom")])
    tracker.clear_scope("vscode", "w1")
    assert tracker.counts().errors == 0
    assert tracker.counts().new_errors == 1


# -- baselines: pre-existing issues ---------------------------------------------------


def test_first_snapshot_is_baseline_not_new():
    tracker, _, new_issues = make()
    tracker.update("vscode", "w1", [err("already there")])
    counts = tracker.counts()
    assert counts.errors == 1
    assert counts.new_errors == 0
    assert new_issues == []
    assert tracker.current_issues()[0].baseline


def test_switching_between_editors_does_not_recount_existing_issues():
    tracker, _, new_issues = make()
    tracker.update("vscode", "w1", [err("in vscode", file="C:/a.py")])
    tracker.update("visualstudio", "vs1", [err("in vs", file="C:/b.cs")])
    for _ in range(5):  # user alt-tabs back and forth; providers keep polling
        tracker.update("vscode", "w1", [err("in vscode", file="C:/a.py")])
        tracker.update("visualstudio", "vs1", [err("in vs", file="C:/b.cs")])
    assert tracker.counts().errors == 2
    assert tracker.counts().new_errors == 0
    assert new_issues == []


def test_new_window_opened_later_has_its_own_baseline():
    tracker, _, _ = make()
    started(tracker, scope="w1")
    tracker.update("vscode", "w2", [err("pre-existing in second window", file="C:/z.py")])
    assert tracker.counts().new_errors == 0


def test_same_issue_from_two_providers_counts_once():
    tracker, _, _ = make()
    started(tracker, "vscode", "w1")
    started(tracker, "buildtools", "ruff")
    tracker.update("vscode", "w1", [err("undefined name 'x'", code="F821")])
    tracker.update("buildtools", "ruff", [err("undefined name 'x'", code="F821", line=7)])
    assert tracker.counts().errors == 1
    assert tracker.counts().new_errors == 1


# -- debounce ------------------------------------------------------------------------------


def test_transient_error_while_typing_is_not_counted_as_new():
    tracker, clock, new_issues = make(debounce=3.0)
    started(tracker)
    tracker.update("vscode", "w1", [err("half-typed identifier")])
    assert tracker.counts().errors == 1  # visible as current immediately
    clock.advance(1.0)
    tracker.update("vscode", "w1", [])  # fixed before the debounce elapsed
    clock.advance(5.0)
    tracker.tick()
    assert tracker.counts().new_errors == 0
    assert new_issues == []


def test_persistent_error_is_promoted_after_debounce():
    tracker, clock, new_issues = make(debounce=3.0)
    started(tracker)
    tracker.update("vscode", "w1", [err("real error")])
    clock.advance(2.0)
    assert tracker.tick() == []
    clock.advance(1.5)
    promoted = tracker.tick()
    assert [i.diagnostic.message for i in promoted] == ["real error"]
    assert tracker.counts().new_errors == 1
    assert len(new_issues) == 1


# -- reset / session -------------------------------------------------------------------------


def test_reset_session_count_zeroes_new_and_acknowledges_current():
    tracker, _, _ = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("a"), err("b")])
    tracker.reset_session_count()
    assert tracker.counts().new_errors == 0
    assert tracker.counts().errors == 2  # current issues are real; not reset
    tracker.update("vscode", "w1", [err("a"), err("b"), err("c")])
    assert tracker.counts().new_errors == 1  # only the genuinely new one


def test_start_session_forgets_everything():
    tracker, _, _ = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("a")])
    tracker.start_session()
    assert tracker.counts() == tracker.counts().__class__()
    assert tracker.current_issues() == []


# -- staleness and confidence -------------------------------------------------------------------


def test_stale_scope_is_listed_but_not_counted_and_recovers():
    tracker, _, _ = make()
    started(tracker, "visualstudio", "vs1")
    tracker.update("visualstudio", "vs1", [err("x", file="C:/a.cs")])
    tracker.mark_stale("visualstudio")
    assert tracker.counts().errors == 0
    assert tracker.counts().stale_scopes == 1
    assert tracker.current_issues()[0].stale
    tracker.update("visualstudio", "vs1", [err("x", file="C:/a.cs")])
    assert tracker.counts().errors == 1
    assert tracker.counts().stale_scopes == 0
    assert tracker.counts().new_errors == 1  # recovering is not a new issue


def test_low_confidence_issues_are_possible_only():
    tracker, _, new_issues = make(min_confidence=0.8)
    started(tracker, "screen", "s1")
    tracker.update("screen", "s1", [Diagnostic("error", "squiggle", confidence=0.4, application="jetbrains")])
    counts = tracker.counts()
    assert counts.errors == 0
    assert counts.possible == 1
    assert counts.new_errors == 0
    assert new_issues == []


def test_failing_new_issue_callback_does_not_break_tracking():
    def explode(_issue):
        raise RuntimeError("history disk full")

    tracker = IssueTracker(debounce_seconds=0, on_new_issue=explode)
    tracker.update("vscode", "w1", [])
    tracker.update("vscode", "w1", [err("boom")])
    assert tracker.counts().new_errors == 1


def test_version_changes_only_when_something_changes():
    tracker, _, _ = make()
    started(tracker)
    tracker.update("vscode", "w1", [err("a")])
    version = tracker.version
    tracker.update("vscode", "w1", [err("a")])
    assert tracker.version == version
