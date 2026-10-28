"""System-tray coding error assistant.

Builds on the existing Tkinter application rather than replacing it: the
original incident window (:class:`~screenshot_logger.gui.ScreenshotLoggerApp`)
becomes the hidden root and is shown as "Incident History", so manual
capture, the hotkey, Windows Event Log monitoring and retention all keep
working exactly as before. On top of it this adds:

- a tray icon (``pystray``) whose colour shows the monitoring state,
- a small settings window (left-click the tray icon),
- the floating ``ERROR n`` overlay,
- the detected-issues window.

Threading: Tk must only be touched on the main thread. Tray menu callbacks
arrive on pystray's thread and are queued; the main thread drains the
queue and refreshes the UI from controller snapshots every 400 ms.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable, Optional

from . import config as config_module
from .coding.controller import MonitorController, MonitorSnapshot
from .coding.project import UnsafeProjectRoot
from .coding.providers.base import STATE_ACTIVE, STATE_ERROR, STATE_IDLE
from .gui import APP_TITLE, IncidentDetailsDialog, ScreenshotLoggerApp
from .issues_window import IssuesWindow
from .overlay import ErrorOverlay

_logger = logging.getLogger("screenshot_logger")

UI_REFRESH_MS = 400
STATE_COLOURS = {STATE_ACTIVE: "#2e9d57", STATE_IDLE: "#7a7a7a", STATE_ERROR: "#c42b1c"}


def status_line(snapshot: MonitorSnapshot) -> tuple[str, str]:
    """Human-readable monitoring state and its colour."""
    if snapshot.running:
        how = "automatically" if snapshot.started_by == "auto" else "manually"
        return f"● Monitoring ACTIVE (started {how})", "#2e9d57"
    if snapshot.auto_monitor and snapshot.auto_paused:
        return "○ Monitoring OFF — Auto Monitor paused until you press Start", "#9d5d00"
    if snapshot.auto_monitor:
        return "○ Monitoring OFF — Auto Monitor waiting for a coding app", "#7a7a7a"
    return "○ Monitoring OFF", "#7a7a7a"


def coverage_text(controller: MonitorController) -> str:
    """One line per language: what was analyzed, and what wasn't."""
    provider = controller.project_provider()
    if provider is None:
        return "Project analysis is not running (start monitoring)."
    coverage = provider.coverage()
    if not coverage:
        return "Waiting for the first analysis…" if controller.effective_project() else "No project selected."
    marks = {"checked": "✔", "partial": "◐", "not checked": "✖", "no analyzer": "✖", "not code": "·"}
    lines = []
    for item in coverage:
        if item.status == "not code":
            continue
        line = f"{marks.get(item.status, '?')} {item.summary()}"
        if item.status != "checked":
            extra = item.notes[:1] + [f"optional: {s}" for s in item.suggestions[:1]]
            if extra:
                line += "  (" + "; ".join(extra) + ")"
        lines.append(line)
    profile = provider.profile
    if profile is not None and profile.truncated:
        lines.append(f"Only the first {provider.max_files} files were scanned.")
    return "\n".join(lines)


def tray_image(running: bool, errors: int):
    """64x64 tray icon: grey = off, green = on and clean, red = errors."""
    from PIL import Image, ImageDraw, ImageFont

    colour = "#c42b1c" if running and errors else ("#2e9d57" if running else "#7a7a7a")
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill=colour)
    text = "E" if not running else (str(errors) if errors < 100 else "99+")
    try:
        font = ImageFont.truetype("segoeuib.ttf", 34 if len(text) < 3 else 24)
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((64 - (box[2] - box[0])) / 2 - box[0], (64 - (box[3] - box[1])) / 2 - box[1]), text, fill="white", font=font)
    return image


def bring_to_front(window: tk.Misc) -> None:
    """Show a window above the editor. Windows refuses plain focus changes
    from background processes, so it is made topmost for a moment."""
    window.deiconify()
    window.lift()
    window.attributes("-topmost", True)
    window.after(300, lambda: window.winfo_exists() and window.attributes("-topmost", False))
    window.focus_force()


class SettingsWindow(tk.Toplevel):
    def __init__(self, app: "AssistantApp") -> None:
        super().__init__(app)
        self.withdraw()  # shown from the tray icon
        self.app = app
        self.title(f"{APP_TITLE} — Coding Monitor")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        config = app.config_data
        pad = {"padx": 12}

        self.state_label = tk.Label(self, font=("Segoe UI Semibold", 11), anchor="w")
        self.state_label.pack(fill="x", pady=(12, 4), **pad)

        row = tk.Frame(self)
        row.pack(fill="x", pady=4, **pad)
        self.start_button = tk.Button(row, width=18, command=app.toggle_monitoring)
        self.start_button.pack(side="left")
        tk.Button(row, text="Show Issues", command=app.open_issues).pack(side="left", padx=6)
        tk.Button(row, text="Reset Session Count", command=app.reset_session).pack(side="left")

        project_frame = tk.LabelFrame(self, text="Project")
        project_frame.pack(fill="x", pady=(6, 4), **pad)
        self.project_label = tk.Label(project_frame, anchor="w", justify="left", wraplength=500)
        self.project_label.pack(fill="x", padx=6, pady=(2, 0))
        project_buttons = tk.Frame(project_frame)
        project_buttons.pack(fill="x", padx=6, pady=4)
        tk.Button(project_buttons, text="Choose Folder…", command=app.choose_project).pack(side="left")
        tk.Button(project_buttons, text="Use Detected Project", command=app.use_detected_project).pack(side="left", padx=6)
        self.coverage_label = tk.Label(project_frame, anchor="w", justify="left", wraplength=500, fg="#444444")
        self.coverage_label.pack(fill="x", padx=6, pady=(0, 4))

        self.auto_var = tk.BooleanVar(value=config.auto_monitor_enabled)
        tk.Checkbutton(
            self, variable=self.auto_var, command=self._toggle_auto,
            text="Auto Monitor — start automatically when a supported coding app is open",
        ).pack(anchor="w", **pad)
        self.overlay_var = tk.BooleanVar(value=config.overlay_enabled)
        tk.Checkbutton(self, text="Show floating error counter", variable=self.overlay_var, command=self._toggle_overlay).pack(anchor="w", **pad)
        self.screen_var = tk.BooleanVar(value=config.screen_detection_enabled)
        tk.Checkbutton(
            self, variable=self.screen_var, command=self._toggle_screen,
            text="Screen detection for editors without an integration (best effort, local only)",
        ).pack(anchor="w", **pad)

        self.counts_label = tk.Label(self, anchor="w", justify="left")
        self.counts_label.pack(fill="x", pady=(8, 0), **pad)
        self.context_label = tk.Label(self, anchor="w", justify="left", fg="#555555")
        self.context_label.pack(fill="x", **pad)

        frame = tk.LabelFrame(self, text="Detection providers")
        frame.pack(fill="x", pady=8, **pad)
        self.providers_frame = frame
        self.provider_rows: list[tuple[tk.Label, tk.Label]] = []

        bar = tk.Frame(self)
        bar.pack(fill="x", pady=(0, 6), **pad)
        tk.Button(bar, text="Run Checks Now", command=app.run_checks_now).pack(side="left")
        tk.Button(bar, text="Incident History", command=app.show_history).pack(side="left", padx=6)
        tk.Button(bar, text="Open config.json", command=app.open_config).pack(side="left")
        tk.Label(
            self, fg="#777777", anchor="w", justify="left", wraplength=520,
            text="Closing this window keeps the app running in the tray. Use the tray menu → Exit to quit. "
                 "Starting at Windows login is not configured — see README.",
        ).pack(fill="x", pady=(0, 12), **pad)

    def _toggle_auto(self) -> None:
        self.app.set_auto_monitor(self.auto_var.get())

    def _toggle_overlay(self) -> None:
        self.app.set_overlay_visible(self.overlay_var.get())

    def _toggle_screen(self) -> None:
        self.app.set_screen_detection(self.screen_var.get())

    def render(self, snapshot: MonitorSnapshot) -> None:
        text, colour = status_line(snapshot)
        self.state_label.configure(text=text, fg=colour)
        self.start_button.configure(text="Stop Monitoring" if snapshot.running else "Start Monitoring")
        self.auto_var.set(snapshot.auto_monitor)
        self.overlay_var.set(self.app.config_data.overlay_enabled)
        c = snapshot.counts
        self.counts_label.configure(
            text=f"Current: {c.errors} error(s), {c.warnings} warning(s)    "
                 f"New this session: {c.new_errors} error(s), {c.new_warnings} warning(s)    "
                 f"Possible: {c.possible}"
        )
        editor = snapshot.foreground_editor or "no coding app in front"
        if snapshot.foreground_language:
            editor += f" ({snapshot.foreground_language})"
        running = ", ".join(snapshot.running_editors) or "none detected"
        self.context_label.configure(text=f"Foreground: {editor}\nRunning editors: {running}")
        if snapshot.project:
            how = "chosen by you" if snapshot.project_mode == "selected" else "detected from the file open in your editor"
            self.project_label.configure(text=f"{snapshot.project}\n({how})")
        else:
            self.project_label.configure(text="No project yet. Choose a folder, or open a file in Visual Studio / VS Code "
                                              "while monitoring to detect it.")
        self.coverage_label.configure(text=coverage_text(self.app.controller))

        providers = snapshot.providers or (("(start monitoring to activate providers)", None),)
        while len(self.provider_rows) < len(providers):
            name = tk.Label(self.providers_frame, anchor="w", font=("Segoe UI Semibold", 9))
            detail = tk.Label(self.providers_frame, anchor="w", justify="left", wraplength=360)
            index = len(self.provider_rows)
            name.grid(row=index, column=0, sticky="nw", padx=6, pady=1)
            detail.grid(row=index, column=1, sticky="w", padx=6, pady=1)
            self.provider_rows.append((name, detail))
        for index, (name_label, detail_label) in enumerate(self.provider_rows):
            if index >= len(providers):
                name_label.configure(text="")
                detail_label.configure(text="")
                continue
            name, status = providers[index]
            name_label.configure(text=name)
            if status is None:
                detail_label.configure(text="", fg="#555555")
            else:
                detail_label.configure(
                    text=f"{status.state}: {status.detail}" if status.detail else status.state,
                    fg=STATE_COLOURS.get(status.state, "#555555"),
                )


class TrayIcon:
    """Thin wrapper over pystray; every action is queued to the Tk thread."""

    def __init__(self, post: Callable[[str], None], snapshot: Callable[[], MonitorSnapshot]) -> None:
        import pystray

        self._post = post
        self._snapshot = snapshot
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Open Settings", lambda: post("settings"), default=True),
            item(lambda _i: "Stop Monitoring" if snapshot().running else "Start Monitoring", lambda: post("toggle")),
            item("Auto Monitor", lambda: post("auto"), checked=lambda _i: snapshot().auto_monitor),
            pystray.Menu.SEPARATOR,
            item("Show Issues", lambda: post("issues")),
            item("Reset Session Count", lambda: post("reset")),
            item("Show / Hide Overlay", lambda: post("overlay")),
            item("Incident History", lambda: post("history")),
            pystray.Menu.SEPARATOR,
            item("Exit", lambda: post("exit")),
        )
        self._state: Optional[tuple[bool, int]] = None
        self.icon = pystray.Icon("ScreenshotErrorLogger", tray_image(False, 0), APP_TITLE, menu)
        self._thread = threading.Thread(target=self.icon.run, name="tray-icon", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def render(self, snapshot: MonitorSnapshot) -> None:
        state = (snapshot.running, snapshot.counts.errors)
        if state == self._state:
            return
        self._state = state
        self.icon.icon = tray_image(*state)
        c = snapshot.counts
        self.icon.title = (
            f"{APP_TITLE}: monitoring, {c.errors} error(s), {c.warnings} warning(s)"
            if snapshot.running else f"{APP_TITLE}: monitoring off"
        )[:127]
        self.icon.update_menu()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            _logger.debug("Could not stop tray icon.", exc_info=True)


class AssistantApp(ScreenshotLoggerApp):
    def __init__(self, config: config_module.AppConfig) -> None:
        super().__init__(config)
        self.title(f"{APP_TITLE} — Incident History")
        self.withdraw()  # the incident window is opened on demand
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

        self._closing = False
        self.controller = MonitorController(config, incident_logger=self.logger)
        self._actions: "queue.Queue[str]" = queue.Queue()
        self.issues_window: Optional[IssuesWindow] = None
        self.settings_window = SettingsWindow(self)
        self.overlay = ErrorOverlay(
            self,
            on_click=self.open_issues,
            on_menu_action=self._overlay_action,
            position=(config.overlay_x, config.overlay_y),
            on_moved=self._overlay_moved,
        )

        try:
            self.tray: Optional[TrayIcon] = TrayIcon(self._actions.put, self.controller.snapshot)
            self.tray.start()
        except Exception:
            # Without a tray icon the settings window is the app's main
            # window: closing it exits.
            _logger.warning("System tray icon unavailable; running without it.", exc_info=True)
            self.tray = None
            self.settings_window.protocol("WM_DELETE_WINDOW", self.quit_app)
            self.settings_window.deiconify()

        if config.overlay_enabled:
            self.overlay.deiconify()
        self.controller.launch()
        # Show the settings window on launch so the app is visibly running;
        # closing it leaves the app in the tray.
        self.open_settings()
        self.after(UI_REFRESH_MS, self._ui_tick)

    # -- UI loop -------------------------------------------------------------

    def _ui_tick(self) -> None:
        try:
            while not self._closing:
                self._handle_action(self._actions.get_nowait())
        except queue.Empty:
            pass
        if self._closing:
            return
        snapshot = self.controller.snapshot()
        self.overlay.render(snapshot)
        if self.settings_window.winfo_viewable():
            self.settings_window.render(snapshot)
        if self.issues_window is not None and self.issues_window.winfo_exists():
            self.issues_window.refresh()
        if self.tray is not None:
            self.tray.render(snapshot)
        self.after(UI_REFRESH_MS, self._ui_tick)

    def _handle_action(self, action: str) -> None:
        handlers = {
            "settings": self.open_settings,
            "toggle": self.toggle_monitoring,
            "auto": lambda: self.set_auto_monitor(not self.config_data.auto_monitor_enabled),
            "issues": self.open_issues,
            "reset": self.reset_session,
            "overlay": lambda: self.set_overlay_visible(not self.config_data.overlay_enabled),
            "history": self.show_history,
            "exit": self.quit_app,
        }
        handler = handlers.get(action)
        if handler is not None:
            handler()

    def _overlay_action(self, action: str) -> None:
        mapping = {"issues": "issues", "reset": "reset", "toggle": "toggle", "settings": "settings"}
        if action == "hide":
            self.set_overlay_visible(False)
        elif action in mapping:
            self._handle_action(mapping[action])

    # -- actions -------------------------------------------------------------------

    def _save_config(self) -> None:
        try:
            config_module.save_config(self.config_data)
        except OSError:
            _logger.warning("Could not save config.json.", exc_info=True)

    def toggle_monitoring(self) -> None:
        if self.controller.running:
            self.controller.stop(manual=True)
        else:
            self.controller.start(manual=True)
        self.settings_window.render(self.controller.snapshot())

    def set_auto_monitor(self, enabled: bool) -> None:
        self.controller.set_auto_monitor(enabled)
        self._save_config()

    def set_overlay_visible(self, visible: bool) -> None:
        self.config_data.overlay_enabled = visible
        self._save_config()
        if visible:
            self.overlay.deiconify()
            self.overlay.keep_on_screen()
        else:
            self.overlay.withdraw()

    def set_screen_detection(self, enabled: bool) -> None:
        self.config_data.screen_detection_enabled = enabled
        self._save_config()
        self.controller.reconfigure()

    def _overlay_moved(self, x: int, y: int) -> None:
        self.config_data.overlay_x, self.config_data.overlay_y = x, y
        self._save_config()

    def open_settings(self) -> None:
        self.settings_window.render(self.controller.snapshot())
        bring_to_front(self.settings_window)

    def open_issues(self) -> None:
        if self.issues_window is None or not self.issues_window.winfo_exists():
            self.issues_window = IssuesWindow(
                self,
                self.controller.tracker,
                load_history=self.logger.list_recent,
                on_reset=self.controller.reset_session_count,
                on_open_history_record=lambda record: IncidentDetailsDialog(self, record),
                project_root_fn=self.controller.effective_project,
                coverage_fn=lambda: coverage_text(self.controller).replace("\n", "   "),
                buffers_fn=self.controller.unsaved_buffers,
            )
        else:
            self.issues_window.refresh(force=True)
        bring_to_front(self.issues_window)

    def choose_project(self) -> None:
        folder = filedialog.askdirectory(parent=self.settings_window, title="Choose the project folder to analyze",
                                         initialdir=self.controller.effective_project() or None)
        if not folder:
            return
        try:
            self.controller.set_project(folder)
        except UnsafeProjectRoot as exc:
            messagebox.showerror(APP_TITLE, f"That folder can't be analyzed:\n{exc}", parent=self.settings_window)
            return
        self._save_config()
        self.settings_window.render(self.controller.snapshot())

    def use_detected_project(self) -> None:
        self.controller.set_project(None)
        self._save_config()
        self.settings_window.render(self.controller.snapshot())

    def reset_session(self) -> None:
        self.controller.reset_session_count()

    def run_checks_now(self) -> None:
        if not self.controller.run_checks_now():
            messagebox.showinfo(
                APP_TITLE,
                "Start monitoring first. Then all available analyzers for the project run again "
                "(including opt-in ones you enabled in config.json).",
                parent=self.settings_window,
            )

    def show_history(self) -> None:
        self.refresh_incidents()
        bring_to_front(self)

    def open_config(self) -> None:
        try:
            os.startfile(str(config_module.CONFIG_FILE))  # type: ignore[attr-defined]
        except OSError:
            messagebox.showerror(APP_TITLE, f"Could not open {config_module.CONFIG_FILE}", parent=self.settings_window)

    def quit_app(self) -> None:
        self._closing = True
        if self.tray is not None:
            self.tray.stop()
        self.controller.shutdown()
        self._on_close()  # existing cleanup: hotkey, event log monitor, destroy


def run_tray_app(config: Optional[config_module.AppConfig] = None) -> None:
    config = config or config_module.load_config()
    app = AssistantApp(config)
    app.mainloop()
