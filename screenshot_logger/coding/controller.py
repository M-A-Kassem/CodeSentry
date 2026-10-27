"""Monitoring controller: lifecycle, Auto Monitor, providers, history.

Lifecycle rules (all covered by tests):

- Nothing is monitored until :meth:`start` is called -- by the user's
  "Start Monitoring", or by Auto Monitor when it is enabled.
- With Auto Monitor on, monitoring starts by itself as soon as a known
  coding application is running (opened, or already open at launch).
- Clicking "Stop Monitoring" while Auto Monitor is on *pauses* Auto
  Monitor, so it does not immediately start again behind the user's back.
  It is re-armed by clicking Start, or by turning Auto Monitor off and on.
- Turning Auto Monitor off never stops a session that is running; it only
  stops future automatic starts.

The controller runs one light background loop (about once a second:
foreground window lookup, process list every few seconds). It does not
capture the screen or talk to editors itself -- providers do, and each
one throttles itself based on what is in the foreground.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .. import config as config_module
from .app_detector import EditorKind, ForegroundApp, get_foreground_app, running_editors
from .diagnostic import ERROR, WARNING
from .issue_tracker import Counts, IssueTracker, TrackedIssue
from .project import find_project_root, validate_project_root
from .providers.base import STATE_ERROR, Provider, ProviderStatus

_logger = logging.getLogger("screenshot_logger")

STARTED_MANUALLY = "manual"
STARTED_AUTOMATICALLY = "auto"


@dataclass(frozen=True)
class MonitorSnapshot:
    running: bool
    started_by: Optional[str]
    auto_monitor: bool
    auto_paused: bool
    counts: Counts
    providers: tuple[tuple[str, ProviderStatus], ...]
    foreground_editor: Optional[str]
    foreground_language: Optional[str]
    running_editors: tuple[str, ...]
    version: int
    project: Optional[str] = None
    project_mode: Optional[str] = None  # "selected" | "detected" | None
    analysis_partial: bool = False


ProviderFactory = Callable[[config_module.AppConfig], dict[str, Provider]]


def default_providers(config: config_module.AppConfig) -> dict[str, Provider]:
    """Build the providers enabled in ``config`` (imported lazily so a
    broken optional dependency only disables its own provider)."""
    providers: dict[str, Provider] = {}
    if config.project_analysis_enabled:
        from .providers.project_analysis import ProjectAnalysisProvider

        # project_fn is bound to the controller when the provider starts.
        providers["project"] = ProjectAnalysisProvider(
            project_fn=None,  # type: ignore[arg-type]
            overrides=config.analyzers,
            extra_excludes=tuple(config.project_exclude_dirs),
            max_files=config.project_max_files,
            poll_seconds=config.project_poll_seconds,
        )
    if config.vscode_bridge_enabled:
        from .providers.vscode_bridge import VSCodeBridgeProvider

        providers["vscode"] = VSCodeBridgeProvider(port=config.vscode_bridge_port)
    if config.visual_studio_enabled:
        from .providers.visual_studio import VisualStudioProvider

        providers["visualstudio"] = VisualStudioProvider(
            poll_seconds=config.visual_studio_poll_seconds,
            include_unsaved=config.project_analysis_enabled and config.analyze_unsaved_editor_text,
        )
    from .providers.build_tools import BuildToolsProvider

    providers["buildtools"] = BuildToolsProvider(config.build_tools)
    if config.screen_detection_enabled:
        from .providers.screen_detector import ScreenDetectionProvider

        providers["screen"] = ScreenDetectionProvider(
            poll_seconds=config.screen_poll_seconds, use_ocr=config.screen_ocr_enabled
        )
    return providers


class MonitorController:
    def __init__(
        self,
        config: config_module.AppConfig,
        incident_logger=None,
        provider_factory: ProviderFactory = default_providers,
        foreground_fn: Callable[[], Optional[ForegroundApp]] = get_foreground_app,
        running_fn: Callable[[], dict[str, EditorKind]] = running_editors,
        clock: Callable[[], float] = time.monotonic,
        tick_seconds: float = 1.0,
        process_scan_seconds: float = 5.0,
    ) -> None:
        self.config = config
        self.incident_logger = incident_logger
        self._provider_factory = provider_factory
        self._foreground_fn = foreground_fn
        self._running_fn = running_fn
        self._clock = clock
        self.tick_seconds = tick_seconds
        self.process_scan_seconds = process_scan_seconds

        self.tracker = IssueTracker(
            debounce_seconds=config.new_issue_debounce_seconds,
            min_confidence=config.screen_min_confidence_to_count,
            clock=clock,
            on_new_issue=self._on_new_issue,
        )
        self.providers: dict[str, Provider] = {}
        self.running = False
        self.started_by: Optional[str] = None
        self.auto_paused = False
        self._foreground: Optional[ForegroundApp] = None
        self._running_editors: dict[str, EditorKind] = {}
        self._last_process_scan: Optional[float] = None
        self.detected_project: Optional[str] = None
        self._last_active_path: Optional[str] = None
        self._lock = threading.RLock()
        self._loop_stop = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._history_queue: "queue.Queue[Optional[TrackedIssue]]" = queue.Queue()
        self._history_thread: Optional[threading.Thread] = None

    # -- background loop ---------------------------------------------------

    def launch(self) -> None:
        """Start the light background loop (not monitoring itself)."""
        if self._loop_thread is not None:
            return
        self._loop_stop.clear()
        self._loop_thread = threading.Thread(target=self._loop, name="monitor-controller", daemon=True)
        self._loop_thread.start()
        self._history_thread = threading.Thread(target=self._history_worker, name="coding-history", daemon=True)
        self._history_thread.start()

    def shutdown(self) -> None:
        self._loop_stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None
        self.stop(manual=False)
        self._history_queue.put(None)
        if self._history_thread is not None:
            self._history_thread.join(timeout=10)
            self._history_thread = None

    def _loop(self) -> None:
        while not self._loop_stop.is_set():
            try:
                self.tick()
            except Exception:
                _logger.warning("Monitor controller tick failed.", exc_info=True)
            self._loop_stop.wait(self.tick_seconds)

    def tick(self) -> None:
        """One iteration of the background loop (public for tests)."""
        now = self._clock()
        self._foreground = self._foreground_fn()
        if self._last_process_scan is None or now - self._last_process_scan >= self.process_scan_seconds:
            self._running_editors = self._running_fn()
            self._last_process_scan = now
        # The foreground editor is running by definition, even between scans.
        if self._foreground is not None and self._foreground.editor is not None:
            self._running_editors.setdefault(self._foreground.process_name.lower(), self._foreground.editor)

        if not self.running and self.config.auto_monitor_enabled and not self.auto_paused and self._running_editors:
            _logger.info("Auto Monitor: coding application detected (%s); starting.", ", ".join(self._running_editors))
            self.start(manual=False)

        if self.running:
            with self._lock:
                providers = list(self.providers.values())
            for provider in providers:
                if provider.status().state == STATE_ERROR and provider.tracker is None:
                    continue  # failed to start
                try:
                    provider.on_context(self._foreground, dict(self._running_editors))
                except Exception:
                    _logger.warning("Provider %s failed handling context.", provider.name, exc_info=True)
            self._detect_project(providers)
            self.tracker.tick()

    def _detect_project(self, providers: list[Provider]) -> None:
        """Project of the file open in Visual Studio / VS Code, if any."""
        for provider in providers:
            try:
                paths = provider.active_paths()
            except Exception:
                continue
            if paths:
                if paths[0] != self._last_active_path:  # only walk the disk on change
                    self._last_active_path = paths[0]
                    self.detected_project = find_project_root(paths[0])
                return

    # -- project selection ----------------------------------------------------------

    def unsaved_buffers(self) -> dict[str, str]:
        """Unsaved editor text from every provider that can read it."""
        with self._lock:
            providers = list(self.providers.values())
        buffers: dict[str, str] = {}
        for provider in providers:
            try:
                buffers.update(provider.unsaved_buffers())
            except Exception:
                _logger.debug("Provider %s failed to report unsaved text.", provider.name, exc_info=True)
        return buffers

    def effective_project(self) -> Optional[str]:
        """The user's chosen folder, else the detected one."""
        return self.config.project_path or self.detected_project

    def project_mode(self) -> Optional[str]:
        if self.config.project_path:
            return "selected"
        return "detected" if self.detected_project else None

    def set_project(self, path: Optional[str]) -> Optional[str]:
        """Choose a project folder (None = go back to detection). Raises
        :class:`UnsafeProjectRoot` for drive roots, system folders, etc.
        The running analysis switches on its next poll; the old project's
        issues leave the current count."""
        self.config.project_path = validate_project_root(path) if path else None
        provider = self.providers.get("project")
        if provider is not None:
            provider.wake()  # type: ignore[attr-defined]
        return self.config.project_path

    def project_provider(self):
        return self.providers.get("project")

    # -- lifecycle ------------------------------------------------------------

    def start(self, manual: bool = True) -> None:
        with self._lock:
            if manual:
                self.auto_paused = False
            if self.running:
                return
            self.tracker.start_session()
            self.providers = {}
            self.running = True
            self.started_by = STARTED_MANUALLY if manual else STARTED_AUTOMATICALLY
            self._start_providers(self._provider_factory(self.config))
        _logger.info("Coding monitor started (%s).", self.started_by)

    def stop(self, manual: bool = True) -> None:
        with self._lock:
            if manual and self.config.auto_monitor_enabled:
                self.auto_paused = True
            if not self.running:
                return
            for provider in self.providers.values():
                self._stop_provider(provider)
            self.running = False
            self.started_by = None
            # Keep the last results visible, but no longer "current".
            for name in self.providers:
                self.tracker.mark_stale(name)
        _logger.info("Coding monitor stopped.")

    def set_auto_monitor(self, enabled: bool) -> None:
        """Turning Auto Monitor on (again) re-arms it after a manual Stop."""
        self.config.auto_monitor_enabled = enabled
        if enabled:
            self.auto_paused = False

    def reconfigure(self) -> None:
        """Apply provider on/off settings to a running session without
        starting a new session (so session counts are kept)."""
        with self._lock:
            if not self.running:
                return
            desired = self._provider_factory(self.config)
            for name in list(self.providers):
                if name not in desired or name == "buildtools":
                    self._stop_provider(self.providers.pop(name))
                    self.tracker.clear_scope(name)
            self._start_providers({k: v for k, v in desired.items() if k not in self.providers})

    def _start_providers(self, providers: dict[str, Provider]) -> None:
        for name, provider in providers.items():
            self.providers[name] = provider
            if getattr(provider, "project_fn", False) is None:
                provider.project_fn = self.effective_project  # type: ignore[attr-defined]
                if self.config.analyze_unsaved_editor_text:
                    provider.buffers_fn = self.unsaved_buffers  # type: ignore[attr-defined]
            try:
                provider.start(self.tracker)
            except Exception as exc:
                # One provider failing (port in use, missing dependency)
                # must not prevent the others from working.
                _logger.warning("Provider %s failed to start.", name, exc_info=True)
                provider.tracker = None
                provider.set_status(STATE_ERROR, f"failed to start: {exc}")

    @staticmethod
    def _stop_provider(provider: Provider) -> None:
        try:
            provider.stop()
        except Exception:
            _logger.warning("Provider %s failed to stop cleanly.", provider.name, exc_info=True)

    # -- user actions -------------------------------------------------------------

    def reset_session_count(self) -> None:
        self.tracker.reset_session_count()

    def run_checks_now(self) -> bool:
        """Rerun every enabled analyzer and configured tool now."""
        if not self.running:
            return False
        ran = False
        for name in ("project", "buildtools"):
            provider = self.providers.get(name)
            if provider is not None and provider.tracker is not None:
                provider.request_run_all()  # type: ignore[attr-defined]
                ran = True
        return ran

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            providers = tuple((p.display_name, p.status()) for p in self.providers.values())
        foreground = self._foreground
        editor = foreground.editor if foreground is not None else None
        return MonitorSnapshot(
            running=self.running,
            started_by=self.started_by,
            auto_monitor=self.config.auto_monitor_enabled,
            auto_paused=self.auto_paused,
            counts=self.tracker.counts(),
            providers=providers,
            foreground_editor=editor.display_name if editor else None,
            foreground_language=foreground.language if (foreground and editor) else None,
            running_editors=tuple(sorted({kind.display_name for kind in self._running_editors.values()})),
            version=self.tracker.version,
            project=self.effective_project(),
            project_mode=self.project_mode(),
            analysis_partial=bool(self.providers.get("project") and self.providers["project"].is_partial()),  # type: ignore[attr-defined]
        )

    # -- history ------------------------------------------------------------------

    def _on_new_issue(self, issue: TrackedIssue) -> None:
        severity = issue.diagnostic.severity
        if (severity == ERROR and self.config.record_new_errors_to_history) or (
            severity == WARNING and self.config.record_new_warnings_to_history
        ):
            self._history_queue.put(issue)

    def _history_worker(self) -> None:
        while True:
            issue = self._history_queue.get()
            if issue is None:
                return
            try:
                self.record_history(issue)
            except Exception:
                _logger.warning("Could not record coding issue to history.", exc_info=True)

    def record_history(self, issue: TrackedIssue):
        if self.incident_logger is None:
            return None
        d = issue.diagnostic
        where = d.location_text() or "(location unknown)"
        description = "\n".join(
            [
                d.message,
                "",
                f"Location: {where}",
                f"Language: {d.language or 'unknown'}",
                f"Editor/tool: {d.application or 'unknown'}",
                f"Reported by: {issue.provider} ({d.source or 'n/a'})",
                f"Code: {d.code or 'n/a'}",
                f"Confidence: {d.confidence:.2f}",
                f"First seen: {issue.first_seen_at.isoformat(timespec='seconds')}",
            ]
        )
        title = f"{d.severity.capitalize()}: {d.message.splitlines()[0][:100]}"
        return self.incident_logger.log_manual(
            title=title,
            description=description,
            severity="ERROR" if d.severity == ERROR else "WARNING",
            source=f"Coding assistant ({issue.provider})",
            capture=self.config.screenshot_on_new_coding_error,
        )
