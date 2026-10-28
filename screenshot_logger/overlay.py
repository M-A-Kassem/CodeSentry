"""Floating error-counter overlay.

A small borderless window showing ``ERROR n``. It never takes keyboard
focus (``WS_EX_NOACTIVATE``), has no taskbar button, is topmost only while
monitoring is active, and can be dragged anywhere -- including other
monitors. Its position is remembered; if the saved monitor is gone, it
falls back to the primary screen's bottom-right corner.

- Left click: open the issue list.
- Drag: move it.
- Right click: menu (issues, reset count, compact view, hide, settings).
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from . import win_display
from .coding.controller import MonitorSnapshot

BG = "#1e1e1e"
FG_DIM = "#9a9a9a"
RED = "#f14c4c"
GREEN = "#4ec97a"
AMBER = "#e0b341"
GREY = "#6f6f6f"
DRAG_THRESHOLD = 4


def overlay_text(snapshot: MonitorSnapshot) -> tuple[str, str, str]:
    """``(main text, detail text, main colour)`` for a snapshot."""
    counts = snapshot.counts
    if not snapshot.running:
        return "ERROR –", "monitor off", GREY
    detail = [f"W {counts.warnings}"]
    if counts.new_errors or counts.new_warnings:
        detail.append(f"+{counts.new_errors + counts.new_warnings} new")
    if counts.possible:
        detail.append(f"?{counts.possible}")
    if counts.stale_scopes:
        detail.append("stale")
    return f"ERROR {counts.errors}", " · ".join(detail), RED if counts.errors else GREEN


class ErrorOverlay(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        on_click: Callable[[], None],
        on_menu_action: Callable[[str], None],
        position: tuple[Optional[int], Optional[int]] = (None, None),
        on_moved: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg=BG, highlightthickness=1, highlightbackground="#3c3c3c")
        self.attributes("-alpha", 0.92)
        self._on_click = on_click
        self._on_menu_action = on_menu_action
        self._on_moved = on_moved
        self._press: Optional[tuple[int, int, int, int]] = None
        self._dragged = False
        self.compact = False
        self._last_render: Optional[tuple] = None

        self.dot = tk.Label(self, text="●", bg=BG, fg=GREY, font=("Segoe UI", 9))
        self.main = tk.Label(self, text="ERROR –", bg=BG, fg=GREY, font=("Segoe UI Semibold", 11))
        self.detail = tk.Label(self, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 8))
        self.dot.pack(side="left", padx=(8, 2), pady=4)
        self.main.pack(side="left", padx=(0, 6), pady=4)
        self.detail.pack(side="left", padx=(0, 8), pady=4)

        self.menu = tk.Menu(self, tearoff=False)
        for label, action in (
            ("Show issues", "issues"),
            ("Reset session count", "reset"),
            ("Compact view", "compact"),
            (None, None),
            ("Start / stop monitoring", "toggle"),
            ("Settings…", "settings"),
            ("Hide overlay", "hide"),
        ):
            if label is None:
                self.menu.add_separator()
            else:
                self.menu.add_command(label=label, command=lambda a=action: self._menu(a))

        for widget in (self, self.dot, self.main, self.detail):
            widget.bind("<ButtonPress-1>", self._on_press)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<ButtonRelease-1>", self._on_release)
            widget.bind("<Button-3>", self._on_right_click)
            widget.configure(cursor="hand2")

        self.update_idletasks()
        win_display.make_non_activating_tool_window(self)
        self._place(*position)

    # -- placement ----------------------------------------------------------

    def _place(self, x: Optional[int], y: Optional[int]) -> None:
        self.update_idletasks()
        width, height = max(self.winfo_reqwidth(), 120), self.winfo_reqheight()
        area = win_display.work_area_for_point(x, y) if x is not None and y is not None else None
        if area is None:  # first run, or the saved monitor is disconnected
            area = win_display.primary_work_area()
            x, y = area[2] - width - 24, area[3] - height - 24
        x, y = win_display.clamp_to_area(int(x), int(y), width, height, area)
        self.geometry(f"+{x}+{y}")

    def keep_on_screen(self) -> None:
        """Re-clamp after the content (and so the width) changed."""
        self._place(self.winfo_x(), self.winfo_y())

    # -- rendering -------------------------------------------------------------

    def render(self, snapshot: MonitorSnapshot) -> None:
        main, detail, colour = overlay_text(snapshot)
        if self.compact:
            main = main.replace("ERROR ", "E ")
            detail = ""
        dot = AMBER if snapshot.running and snapshot.counts.stale_scopes else (GREEN if snapshot.running else GREY)
        render_key = (main, detail, colour, dot, snapshot.running)
        if render_key == self._last_render:
            return
        self._last_render = render_key
        self.main.configure(text=main, fg=colour)
        self.dot.configure(fg=dot)
        self.detail.configure(text=detail)
        if detail:
            self.detail.pack(side="left", padx=(0, 8), pady=4)
        else:
            self.detail.pack_forget()
        # Topmost only while monitoring, so a stopped monitor never sits
        # over the editor.
        self.attributes("-topmost", snapshot.running)
        self.keep_on_screen()

    # -- mouse -----------------------------------------------------------------------

    def _on_press(self, event: tk.Event) -> None:
        self._press = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())
        self._dragged = False

    def _on_drag(self, event: tk.Event) -> None:
        if self._press is None:
            return
        start_x, start_y, win_x, win_y = self._press
        dx, dy = event.x_root - start_x, event.y_root - start_y
        if not self._dragged and abs(dx) + abs(dy) < DRAG_THRESHOLD:
            return
        self._dragged = True
        self.geometry(f"+{win_x + dx}+{win_y + dy}")

    def _on_release(self, _event: tk.Event) -> None:
        was_drag, self._press = self._dragged, None
        if was_drag:
            self.keep_on_screen()
            if self._on_moved is not None:
                self._on_moved(self.winfo_x(), self.winfo_y())
        else:
            self._on_click()

    def _on_right_click(self, event: tk.Event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _menu(self, action: str) -> None:
        if action == "compact":
            self.compact = not self.compact
            self._last_render = None
        self._on_menu_action(action)
