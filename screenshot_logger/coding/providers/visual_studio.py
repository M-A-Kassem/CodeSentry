"""Visual Studio (devenv.exe) Error List provider.

Visual Studio does not run VS Code extensions, so this uses Visual Studio's
own documented automation model instead: EnvDTE's
``DTE.ToolWindows.ErrorList.ErrorItems``, reached out-of-process through
the COM Running Object Table. No Visual Studio extension (VSIX) needs to
be installed and nothing is injected into Visual Studio.

What it reports is exactly what the Error List window shows, so:

- Build errors appear after a build; live IntelliSense/Roslyn errors appear
  only while the Error List filter is "Build + IntelliSense" (the default).
- The Error List's own scope filter ("Current Document", "Open Documents",
  "Entire Solution") limits what is visible to EnvDTE as well.
- A Visual Studio instance running as Administrator cannot be read from a
  non-elevated process (Windows blocks it); it shows up as unreachable.

The COM work runs in one long-lived PowerShell helper
(``vs_errorlist.ps1``) driven over stdin/stdout, so there is no new Python
dependency and no per-poll process start-up cost.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ..diagnostic import ERROR, INFO, WARNING, Diagnostic
from .base import STATE_ACTIVE, STATE_ERROR, STATE_IDLE, Provider

_logger = logging.getLogger("screenshot_logger")

SCRIPT_PATH = Path(__file__).with_name("vs_errorlist.ps1")

# EnvDTE80.vsBuildErrorLevel, verified against envdte80.dll (VS 2022):
# Low = 1, Medium = 2, High = 4.
_LEVEL_TO_SEVERITY = {4: ERROR, 2: WARNING, 1: INFO}

FAILURES_BEFORE_STALE = 3


class Poller(Protocol):
    def poll(self) -> str: ...
    def close(self) -> None: ...


@dataclass
class DteInstance:
    moniker: str
    ok: bool
    solution: str = ""
    active_document: Optional[str] = None
    error: str = ""
    diagnostics: list[Diagnostic] = field(default_factory=list)
    unsaved: dict[str, str] = field(default_factory=dict)  # path -> unsaved editor text


def parse_dte_output(text: str) -> list[DteInstance]:
    """Parse one JSON line from ``vs_errorlist.ps1``.

    Raises ``ValueError`` if the helper itself failed or the output is not
    valid. Tolerates PowerShell 5.1's habit of emitting a bare object
    instead of a one-element array.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("unexpected Visual Studio helper output")
    if not data.get("ok", False):
        raise ValueError(str(data.get("error") or "Visual Studio helper failed"))

    raw_instances = data.get("instances") or []
    if isinstance(raw_instances, dict):
        raw_instances = [raw_instances]

    instances: list[DteInstance] = []
    for raw in raw_instances:
        if not isinstance(raw, dict) or not raw.get("moniker"):
            continue
        instance = DteInstance(
            moniker=str(raw["moniker"]),
            ok=bool(raw.get("ok")),
            solution=str(raw.get("solution") or ""),
            active_document=raw.get("activeDocument") or None,
            error=str(raw.get("error") or ""),
        )
        unsaved = raw.get("unsaved") or []
        if isinstance(unsaved, dict):
            unsaved = [unsaved]
        for doc in unsaved:
            if isinstance(doc, dict) and doc.get("path") and isinstance(doc.get("text"), str):
                instance.unsaved[str(doc["path"])] = doc["text"]
        items = raw.get("items") or []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if not isinstance(item, dict) or not item.get("description"):
                continue
            line = int(item.get("line") or 0)
            column = int(item.get("column") or 0)
            instance.diagnostics.append(
                Diagnostic(
                    severity=_LEVEL_TO_SEVERITY.get(int(item.get("level") or 4), ERROR),
                    message=str(item["description"]),
                    file=str(item.get("file") or "") or None,
                    line=line if line > 0 else None,
                    column=column if column > 0 else None,
                    # EnvDTE's ErrorItem exposes no error code (e.g. CS0103).
                    source=str(item.get("project") or "") or "Visual Studio",
                    application="visualstudio",
                )
            )
        instances.append(instance)
    return instances


class PowerShellDtePoller:
    """Owns the long-lived PowerShell helper process."""

    def __init__(self, script_path: Path = SCRIPT_PATH, first_timeout: float = 45.0, timeout: float = 20.0) -> None:
        self.script_path = script_path
        self.first_timeout = first_timeout
        self.timeout = timeout
        self._process: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._answered_once = False

    def _ensure_process(self) -> subprocess.Popen:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._lines = queue.Queue()
        self._answered_once = False
        self._process = subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(self.script_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read_lines, args=(self._process, self._lines), daemon=True).start()
        return self._process

    @staticmethod
    def _read_lines(process: subprocess.Popen, lines: "queue.Queue[Optional[str]]") -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            text = raw.decode("utf-8", errors="replace").strip().lstrip("﻿")
            if text:
                lines.put(text)
        lines.put(None)

    def poll(self, include_unsaved: bool = False) -> str:
        process = self._ensure_process()
        assert process.stdin is not None
        try:
            process.stdin.write(b"poll+unsaved\n" if include_unsaved else b"poll\n")
            process.stdin.flush()
        except OSError as exc:
            self.close()
            raise RuntimeError("Visual Studio helper exited") from exc
        timeout = self.timeout if self._answered_once else self.first_timeout
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError("Visual Studio helper did not answer in time")
        if line is None:
            self.close()
            raise RuntimeError("Visual Studio helper exited")
        self._answered_once = True
        return line

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=3)
        except Exception:
            process.kill()


class VisualStudioProvider(Provider):
    name = "visualstudio"
    display_name = "Visual Studio (Error List)"

    def __init__(self, poll_seconds: float = 5.0, poller: Optional[Poller] = None, include_unsaved: bool = False) -> None:
        super().__init__()
        # Also read the text of documents with unsaved changes, so the
        # project analysis can count errors before the file is saved.
        self.include_unsaved = include_unsaved
        self._unsaved: dict[str, str] = {}
        self.poll_seconds = max(1.0, poll_seconds)
        self.poller: Poller = poller or PowerShellDtePoller()
        self._devenv_running = False
        self._foreground = False
        self._failures = 0
        self._active_paths: list[str] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, tracker) -> None:
        super().start(tracker)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="visual-studio-provider", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.poller.close()
        super().stop()

    def on_context(self, foreground, running) -> None:
        was_running = self._devenv_running
        self._devenv_running = "devenv.exe" in running
        foreground_now = foreground is not None and foreground.process_name.lower() == "devenv.exe"
        if (foreground_now and not self._foreground) or (self._devenv_running and not was_running):
            self._wake.set()  # poll promptly when the user switches to VS
        self._foreground = foreground_now

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._devenv_running:
                self.poll_once()
            else:
                self._on_not_running()
            # Poll at full rate while VS is in front, slower behind.
            interval = self.poll_seconds if self._foreground else self.poll_seconds * 3
            self._wake.wait(interval)
            self._wake.clear()

    def active_paths(self) -> list[str]:
        return list(self._active_paths) if self._devenv_running else []

    def unsaved_buffers(self) -> dict[str, str]:
        return dict(self._unsaved) if self._devenv_running else {}

    def _on_not_running(self) -> None:
        if self.tracker is not None and self.tracker.scopes(self.name):
            self.tracker.clear_scope(self.name)  # VS closed: its issues are gone
        self._unsaved = {}
        self.poller.close()
        self.set_status(STATE_IDLE, "Visual Studio is not running")

    def poll_once(self) -> None:
        tracker = self.tracker
        if tracker is None:
            return
        try:
            raw = self.poller.poll(include_unsaved=True) if self.include_unsaved else self.poller.poll()
            instances = parse_dte_output(raw)
        except Exception as exc:
            self._failures += 1
            _logger.debug("Visual Studio poll failed (%s in a row).", self._failures, exc_info=True)
            if self._failures >= FAILURES_BEFORE_STALE:
                tracker.mark_stale(self.name)
                self.set_status(STATE_ERROR, f"cannot read the Error List: {exc}")
            return
        self._failures = 0

        reported: set[str] = set()
        reachable: list[str] = []
        self._active_paths = [p for i in instances if i.ok for p in (i.active_document, i.solution) if p]
        # A busy instance keeps its last-known unsaved text rather than flipping.
        if any(i.ok for i in instances) or not instances:
            self._unsaved = {path: text for i in instances if i.ok for path, text in i.unsaved.items()}
        for instance in instances:
            reported.add(instance.moniker)
            if instance.ok:
                tracker.update(self.name, instance.moniker, instance.diagnostics)
                reachable.append(Path(instance.solution).name or "(no solution)")
            else:
                # Busy (e.g. mid-build): keep last-known issues, flagged stale.
                tracker.mark_stale(self.name, instance.moniker)
        for scope in set(tracker.scopes(self.name)) - reported:
            tracker.clear_scope(self.name, scope)

        if reachable:
            self.set_status(STATE_ACTIVE, f"{len(reachable)} instance(s): {', '.join(reachable)}")
        elif instances:
            self.set_status(STATE_ERROR, "Visual Studio is busy or not responding")
        else:
            self.set_status(
                STATE_ERROR,
                "Visual Studio is running but not reachable (is it running as Administrator?)",
            )
