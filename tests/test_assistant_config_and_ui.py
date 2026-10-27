"""Config compatibility for the new settings, editor/title detection, and
the pure text/colour logic of the overlay and settings window. One smoke
test creates the real overlay window when a desktop session exists."""
from __future__ import annotations

import json
import tkinter as tk

import pytest

from screenshot_logger import config as config_module
from screenshot_logger.coding.app_detector import editor_for_process, parse_title
from screenshot_logger.coding.controller import MonitorSnapshot
from screenshot_logger.coding.issue_tracker import Counts
from screenshot_logger.overlay import GREEN, GREY, RED, ErrorOverlay, overlay_text
from screenshot_logger.tray_app import status_line
from screenshot_logger import win_display


def test_old_config_file_without_new_keys_still_loads(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"screenshot_enabled": False, "hotkey_enabled": True, "log_level": "DEBUG"}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", path)
    loaded = config_module.load_config()
    assert loaded.screenshot_enabled is False and loaded.hotkey_enabled is True
    assert loaded.auto_monitor_enabled is False  # privacy-preserving defaults
    assert loaded.screen_detection_enabled is False
    assert loaded.screenshot_on_new_coding_error is False
    assert loaded.build_tools == []


def test_new_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    config = config_module.AppConfig(auto_monitor_enabled=True, overlay_x=-1500, overlay_y=200,
                                     build_tools=[{"name": "ruff", "command": ["ruff"], "parser": "ruff-json"}])
    config_module.save_config(config)
    assert config_module.load_config() == config


def test_invalid_new_values_fall_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"build_tools": "not a list"}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", path)
    assert config_module.load_config() == config_module.AppConfig()


def test_editor_detection_and_title_parsing():
    assert editor_for_process("Code.exe").integration == "vscode"
    assert editor_for_process("devenv.exe").integration == "visualstudio"
    assert editor_for_process("pycharm64.exe").integration is None
    assert editor_for_process("chrome.exe") is None
    assert parse_title("● main.py - myproject - Visual Studio Code") == ("main.py", "Python")
    assert parse_title("App - Program.cs - Microsoft Visual Studio") == ("Program.cs", "C#")
    assert parse_title("Untitled - Notepad++") == (None, None)


def snapshot(running=True, **counts):
    return MonitorSnapshot(
        running=running, started_by="manual" if running else None, auto_monitor=False, auto_paused=False,
        counts=Counts(**counts), providers=(), foreground_editor=None, foreground_language=None,
        running_editors=(), version=0,
    )


def test_overlay_text_states():
    assert overlay_text(snapshot(running=False))[0] == "ERROR –"
    assert overlay_text(snapshot(running=False))[2] == GREY
    main, detail, colour = overlay_text(snapshot())
    assert (main, colour) == ("ERROR 0", GREEN)
    main, detail, colour = overlay_text(snapshot(errors=2, warnings=3, new_errors=1, possible=4, stale_scopes=1))
    assert main == "ERROR 2" and colour == RED
    assert detail == "W 3 · +1 new · ?4 · stale"


def test_status_line_shows_auto_monitor_state():
    paused = MonitorSnapshot(False, None, True, True, Counts(), (), None, None, (), 0)
    assert "paused" in status_line(paused)[0]
    waiting = MonitorSnapshot(False, None, True, False, Counts(), (), None, None, (), 0)
    assert "waiting" in status_line(waiting)[0]
    assert "ACTIVE" in status_line(snapshot())[0]


def test_clamp_keeps_overlay_inside_area():
    area = (-1920, 0, 0, 1080)  # a monitor left of the primary
    assert win_display.clamp_to_area(-50, 1070, 120, 30, area) == (-120, 1050)
    assert win_display.clamp_to_area(-5000, -10, 120, 30, area) == (-1920, 0)


def _tk_root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no desktop session")
    root.withdraw()
    return root


def test_overlay_window_does_not_take_focus_and_is_topmost_only_when_running():
    root = _tk_root()
    try:
        clicks = []
        overlay = ErrorOverlay(root, on_click=lambda: clicks.append(1), on_menu_action=lambda _a: None)
        overlay.deiconify()
        overlay.render(snapshot(errors=3))
        root.update()
        assert overlay.main.cget("text") == "ERROR 3"
        assert bool(overlay.attributes("-topmost"))
        import ctypes

        hwnd = win_display.toplevel_hwnd(overlay)
        style = ctypes.windll.user32.GetWindowLongW(hwnd, win_display.GWL_EXSTYLE)
        assert style & win_display.WS_EX_NOACTIVATE
        assert style & win_display.WS_EX_TOOLWINDOW
        overlay.render(snapshot(running=False))
        root.update()
        assert not bool(overlay.attributes("-topmost"))
        # A saved position on a monitor that no longer exists falls back on-screen.
        overlay._place(-99999, -99999)
        root.update()
        assert win_display.work_area_for_point(overlay.winfo_x() + 5, overlay.winfo_y() + 5) is not None
    finally:
        root.destroy()
