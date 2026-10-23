"""Tkinter desktop GUI for Screenshot & Error Logger.

The window stays responsive during screenshot capture and file I/O by
running those operations on a background thread and marshalling the result
back to the main thread with ``Tk.after(0, ...)`` -- Tkinter widgets must
only ever be touched from the main thread.
"""
from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Optional

from . import config as config_module
from .event_monitor import EventLogMonitor
from .hotkey_service import HotkeyService
from .incident_logger import Incident, IncidentLogger, VALID_SEVERITIES

_logger = logging.getLogger("screenshot_logger")

APP_TITLE = config_module.APP_NAME


def _open_folder(path: Path) -> None:
    """Open a folder in Windows Explorer. Best-effort: never raises."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        _logger.warning("Could not open folder: %s", path, exc_info=True)
        messagebox.showerror(APP_TITLE, f"Could not open folder:\n{path}")


def _open_file(path: Path) -> None:
    """Open a file with its associated Windows application. Best-effort."""
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        _logger.warning("Could not open file: %s", path, exc_info=True)
        messagebox.showerror(APP_TITLE, f"Could not open file:\n{path}")


class ManualIncidentDialog(tk.Toplevel):
    """Dialog for recording a manual incident: title, description, severity."""

    def __init__(self, parent: "ScreenshotLoggerApp") -> None:
        super().__init__(parent)
        self.parent = parent
        self.title("Capture Manual Incident")
        self.resizable(False, False)
        self.transient(parent)

        padding = {"padx": 10, "pady": 5}

        tk.Label(self, text="Title:").grid(row=0, column=0, sticky="w", **padding)
        self.title_entry = tk.Entry(self, width=50)
        self.title_entry.grid(row=0, column=1, **padding)

        tk.Label(self, text="Description:").grid(row=1, column=0, sticky="nw", **padding)
        self.description_text = tk.Text(self, width=50, height=6)
        self.description_text.grid(row=1, column=1, **padding)

        tk.Label(self, text="Severity:").grid(row=2, column=0, sticky="w", **padding)
        self.severity_var = tk.StringVar(value="INFO")
        severity_menu = ttk.Combobox(
            self,
            textvariable=self.severity_var,
            values=list(VALID_SEVERITIES),
            state="readonly",
            width=15,
        )
        severity_menu.grid(row=2, column=1, sticky="w", **padding)

        button_frame = tk.Frame(self)
        button_frame.grid(row=3, column=0, columnspan=2, pady=10)
        self.submit_button = tk.Button(button_frame, text="Capture Incident", command=self._submit)
        self.submit_button.pack(side="left", padx=5)
        tk.Button(button_frame, text="Cancel", command=self.destroy).pack(side="left", padx=5)

        self.title_entry.focus_set()

    def _submit(self) -> None:
        title = self.title_entry.get().strip()
        description = self.description_text.get("1.0", "end").strip()
        severity = self.severity_var.get()

        if not title:
            messagebox.showwarning(APP_TITLE, "Please enter a title for the incident.")
            return

        self.submit_button.config(state="disabled", text="Capturing...")
        self.parent.set_status("capturing manual incident (taking a screenshot)...")

        def worker() -> None:
            incident = self.parent.logger.log_manual(
                title=title,
                description=description or "(no description provided)",
                severity=severity,
            )
            self.after(0, lambda: self._on_done(incident))

        threading.Thread(target=worker, daemon=True).start()

    def _on_done(self, incident: Incident) -> None:
        self.destroy()
        self.parent.refresh_incidents()
        self.parent.set_status(f"incident {incident.incident_id} recorded.")
        messagebox.showinfo(APP_TITLE, f"Incident recorded.\nIncident ID: {incident.incident_id}")


class IncidentDetailsDialog(tk.Toplevel):
    """Read-only dialog showing the full details of one incident record."""

    def __init__(self, parent: "ScreenshotLoggerApp", record: dict[str, Any]) -> None:
        super().__init__(parent)
        self.title(f"Incident Details - {record.get('incident_id', 'unknown')}")
        self.geometry("640x480")
        self.transient(parent)

        text = tk.Text(self, wrap="word")
        text.pack(fill="both", expand=True, padx=10, pady=10)

        lines = [
            f"Incident ID: {record.get('incident_id', 'N/A')}",
            f"Timestamp: {record.get('timestamp', 'N/A')}",
            f"Severity: {record.get('severity', 'N/A')}",
            f"Title: {record.get('title', 'N/A')}",
            f"Source: {record.get('source') or 'N/A'}",
            "",
            f"Message:\n{record.get('message', 'N/A')}",
            "",
            f"Exception type: {record.get('exception_type') or 'N/A'}",
            "",
            "Traceback:",
            record.get("traceback") or "(none)",
            "",
            f"Process ID: {record.get('process_id', 'N/A')}",
            f"Foreground window title: {record.get('foreground_window_title') or 'N/A'}",
            f"Foreground process name: {record.get('foreground_process_name') or 'N/A'}",
            "",
            f"Screenshot path: {record.get('screenshot_path') or '(no screenshot captured)'}",
        ]
        text.insert("1.0", "\n".join(lines))
        text.config(state="disabled")

        tk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 10))


class ScreenshotLoggerApp(tk.Tk):
    """Main application window."""

    def __init__(self, config: config_module.AppConfig) -> None:
        super().__init__()
        self.config_data = config
        self.logger = IncidentLogger(config=config)
        self._incident_records: list[dict[str, Any]] = []
        self.hotkey_service: Optional[HotkeyService] = None
        self.event_monitor: Optional[EventLogMonitor] = None

        self.title(APP_TITLE)
        self.geometry("800x540")
        self.minsize(640, 420)

        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.refresh_incidents()
        self._maybe_start_hotkey()
        self._maybe_apply_retention()
        self._maybe_start_event_monitor()
        self._schedule_auto_refresh()

    # -- UI construction ----------------------------------------------

    def _build_widgets(self) -> None:
        header = tk.Frame(self)
        header.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(header, text=APP_TITLE, font=("Segoe UI", 16, "bold")).pack(side="left")
        self.count_label = tk.Label(header, text="Total incidents: 0", font=("Segoe UI", 11))
        self.count_label.pack(side="right")

        columns = ("timestamp", "severity", "title")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("timestamp", text="Timestamp")
        self.tree.heading("severity", text="Severity")
        self.tree.heading("title", text="Title")
        self.tree.column("timestamp", width=170, anchor="w")
        self.tree.column("severity", width=90, anchor="center")
        self.tree.column("title", width=440, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)
        self.tree.bind("<Double-1>", lambda _event: self.view_selected_details())

        button_bar = tk.Frame(self)
        button_bar.pack(fill="x", padx=10, pady=(0, 5))
        tk.Button(button_bar, text="Capture Manual Incident", command=self.open_manual_capture).pack(
            side="left", padx=3
        )
        tk.Button(button_bar, text="Refresh", command=self.refresh_incidents).pack(side="left", padx=3)
        tk.Button(button_bar, text="View Details", command=self.view_selected_details).pack(
            side="left", padx=3
        )
        tk.Button(button_bar, text="Open Screenshot", command=self.open_selected_screenshot).pack(
            side="left", padx=3
        )
        tk.Button(
            button_bar, text="Open Screenshots Folder", command=self.open_screenshots_folder
        ).pack(side="left", padx=3)
        tk.Button(button_bar, text="Open Logs Folder", command=self.open_logs_folder).pack(
            side="left", padx=3
        )

        demo_bar = tk.Frame(self)
        demo_bar.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(demo_bar, text="Demo / test actions:").pack(side="left", padx=(0, 6))
        tk.Button(
            demo_bar, text="Division by Zero", command=lambda: self.run_demo("division")
        ).pack(side="left", padx=3)
        tk.Button(
            demo_bar, text="Missing File", command=lambda: self.run_demo("missing_file")
        ).pack(side="left", padx=3)
        tk.Button(
            demo_bar,
            text="Simulated Automation Failure",
            command=lambda: self.run_demo("automation"),
        ).pack(side="left", padx=3)

        monitor_bar = tk.Frame(self)
        monitor_bar.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(
            monitor_bar, text="Background monitoring (Windows Application/System error logs):"
        ).pack(side="left", padx=(0, 6))
        self.monitor_button = tk.Button(
            monitor_bar, text="Start Event Log Monitoring", command=self.toggle_event_monitor
        )
        self.monitor_button.pack(side="left", padx=3)

        self.status_var = tk.StringVar(value="Status: running normally.")
        status_bar = tk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken", bd=1)
        status_bar.pack(fill="x", side="bottom")

    # -- Status / incident list ----------------------------------------

    def set_status(self, message: str) -> None:
        self.status_var.set(f"Status: {message}")

    def refresh_incidents(self) -> None:
        self._incident_records = self.logger.list_recent()
        self.tree.delete(*self.tree.get_children())
        for record in self._incident_records:
            self.tree.insert(
                "",
                "end",
                iid=record.get("incident_id"),
                values=(
                    record.get("timestamp", "N/A"),
                    record.get("severity", "N/A"),
                    record.get("title", "N/A"),
                ),
            )
        self.count_label.config(text=f"Total incidents: {len(self._incident_records)}")
        self.set_status("running normally.")

    def _get_selected_record(self) -> Optional[dict[str, Any]]:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Please select an incident first.")
            return None
        incident_id = selection[0]
        for record in self._incident_records:
            if record.get("incident_id") == incident_id:
                return record
        messagebox.showerror(APP_TITLE, "Could not load the selected incident record.")
        return None

    # -- Button actions -------------------------------------------------

    def open_manual_capture(self) -> None:
        ManualIncidentDialog(self)

    def view_selected_details(self) -> None:
        record = self._get_selected_record()
        if record is not None:
            IncidentDetailsDialog(self, record)

    def open_selected_screenshot(self) -> None:
        record = self._get_selected_record()
        if record is None:
            return
        screenshot_path = record.get("screenshot_path")
        if not screenshot_path:
            messagebox.showinfo(APP_TITLE, "This incident has no screenshot.")
            return
        path = Path(screenshot_path)
        if not path.exists():
            messagebox.showwarning(APP_TITLE, f"Screenshot file is missing:\n{path}")
            return
        _open_file(path)

    def open_screenshots_folder(self) -> None:
        _open_folder(config_module.SCREENSHOTS_DIR)

    def open_logs_folder(self) -> None:
        _open_folder(config_module.LOGS_DIR)

    # -- Demo / test actions --------------------------------------------

    def run_demo(self, which: str) -> None:
        """Intentionally trigger a sample exception and report it, purely
        for demonstration purposes (clearly labelled in the UI as such)."""
        self.set_status("running demo action and capturing incident...")

        def worker() -> None:
            incident: Optional[Incident] = None
            try:
                if which == "division":
                    1 / 0
                elif which == "missing_file":
                    Path("this_file_does_not_exist_12345.txt").read_text(encoding="utf-8")
                elif which == "automation":
                    raise RuntimeError("Simulated automation failure: target element not found.")
            except Exception as exc:
                incident = self.logger.log_exception(
                    title=f"Demo: {which.replace('_', ' ')}",
                    exception=exc,
                    source="screenshot_logger.gui (demo action)",
                )
            self.after(0, lambda: self._on_demo_done(incident))

        threading.Thread(target=worker, daemon=True).start()

    def _on_demo_done(self, incident: Optional[Incident]) -> None:
        self.refresh_incidents()
        if incident is not None:
            messagebox.showinfo(APP_TITLE, f"Demo incident recorded.\nIncident ID: {incident.incident_id}")
        else:
            self.set_status("running normally.")

    # -- Hotkey -----------------------------------------------------------

    def _maybe_start_hotkey(self) -> None:
        if not self.config_data.hotkey_enabled:
            return
        self.hotkey_service = HotkeyService(self.config_data.hotkey_combo, self._on_hotkey_triggered)
        started = self.hotkey_service.start()
        if started:
            self.set_status(f"running normally. Global hotkey {self.config_data.hotkey_combo} enabled.")
        else:
            self.set_status("running normally. Global hotkey could not be enabled (see app.log).")

    def _on_hotkey_triggered(self) -> None:
        """Runs on the 'keyboard' library's own listener thread, not the Tk
        thread -- so it must not touch widgets directly. It deliberately
        skips the title/description dialog (which would pop up over
        whatever application is currently in the foreground and would be
        captured in the screenshot) and instead records a quick incident
        with a fixed title, then shows a brief, non-focus-stealing toast.
        """

        def worker() -> None:
            incident = self.logger.log_manual(
                title="Hotkey capture",
                description="Manually captured via global hotkey.",
                severity="INFO",
            )
            self.after(0, lambda: self._on_hotkey_done(incident))

        threading.Thread(target=worker, daemon=True).start()

    def _on_hotkey_done(self, incident: Incident) -> None:
        self.refresh_incidents()
        self.set_status(f"hotkey capture saved as incident {incident.incident_id}.")
        self._show_toast(f"Incident {incident.incident_id} captured.")

    def _show_toast(self, message: str) -> None:
        """Briefly show a small, non-focus-stealing notification in the
        corner of the screen, so a hotkey capture does not need to steal
        focus away from whatever application the user was using."""
        toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        tk.Label(toast, text=message, bg="#333333", fg="white", padx=12, pady=6).pack()
        self.update_idletasks()
        x = self.winfo_screenwidth() - 320
        y = self.winfo_screenheight() - 120
        toast.geometry(f"+{x}+{y}")
        toast.after(2500, toast.destroy)

    # -- Background event log monitoring --------------------------------

    def _maybe_start_event_monitor(self) -> None:
        """Resume monitoring on startup only if the user previously turned
        it on (persisted in config.json) -- it never starts on its own."""
        if self.config_data.event_monitoring_enabled:
            self._start_event_monitor(persist=False)
        else:
            self._update_monitor_button_label()

    def toggle_event_monitor(self) -> None:
        if self.event_monitor is not None and self.event_monitor.is_running:
            self._stop_event_monitor()
        else:
            self._start_event_monitor()

    def _start_event_monitor(self, persist: bool = True) -> None:
        if self.event_monitor is None:
            self.event_monitor = EventLogMonitor(
                self.logger, poll_seconds=self.config_data.event_monitoring_poll_seconds
            )
        self.event_monitor.start()
        self.config_data.event_monitoring_enabled = True
        if persist:
            config_module.save_config(self.config_data)
        self._update_monitor_button_label()
        self.set_status("background event log monitoring is ON.")

    def _stop_event_monitor(self, persist: bool = True) -> None:
        if self.event_monitor is not None:
            self.event_monitor.stop()
        self.config_data.event_monitoring_enabled = False
        if persist:
            config_module.save_config(self.config_data)
        self._update_monitor_button_label()
        self.set_status("background event log monitoring is OFF.")

    def _update_monitor_button_label(self) -> None:
        running = self.event_monitor is not None and self.event_monitor.is_running
        # "Event Log" in the label keeps it distinct from the tray app's
        # coding-error Start/Stop Monitoring control.
        self.monitor_button.config(
            text="Stop Event Log Monitoring" if running else "Start Event Log Monitoring"
        )

    def _schedule_auto_refresh(self) -> None:
        self.after(15000, self._auto_refresh_tick)

    def _auto_refresh_tick(self) -> None:
        if self.event_monitor is not None and self.event_monitor.is_running:
            self.refresh_incidents()
        self._schedule_auto_refresh()

    # -- Retention ----------------------------------------------------

    def _maybe_apply_retention(self) -> None:
        if self.config_data.retention_days <= 0:
            return

        def worker() -> None:
            deleted = self.logger.apply_retention()
            if deleted:
                self.after(0, self.refresh_incidents)

        threading.Thread(target=worker, daemon=True).start()

    # -- Shutdown -------------------------------------------------------

    def _on_close(self) -> None:
        if self.hotkey_service is not None:
            self.hotkey_service.stop()
        if self.event_monitor is not None:
            self.event_monitor.stop()
        self.destroy()


def run_app(config: Optional[config_module.AppConfig] = None) -> None:
    """Build and run the Tkinter application (blocking call)."""
    config = config or config_module.load_config()
    app = ScreenshotLoggerApp(config)
    app.mainloop()
