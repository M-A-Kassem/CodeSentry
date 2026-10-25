"""Detect which coding application is running / in the foreground.

Uses only read-only, unprivileged Windows information: the foreground
window handle and title (``user32``) and process names (``psutil``). Nothing
is injected into other processes and no window contents are read here.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from .diagnostic import language_for_path

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None  # type: ignore[assignment]

_logger = logging.getLogger("screenshot_logger")

# Which dedicated integration (if any) can read diagnostics from an editor.
INTEGRATION_VSCODE = "vscode"
INTEGRATION_VISUAL_STUDIO = "visualstudio"


@dataclass(frozen=True)
class EditorKind:
    key: str
    display_name: str
    integration: Optional[str] = None  # None => only screen detection applies


# Lower-case process image names. VS Code forks run VS Code extensions, so
# the companion extension is expected to work there too (untested).
KNOWN_EDITORS: dict[str, EditorKind] = {
    "code.exe": EditorKind("vscode", "Visual Studio Code", INTEGRATION_VSCODE),
    "code - insiders.exe": EditorKind("vscode", "VS Code Insiders", INTEGRATION_VSCODE),
    "cursor.exe": EditorKind("vscode", "Cursor", INTEGRATION_VSCODE),
    "vscodium.exe": EditorKind("vscode", "VSCodium", INTEGRATION_VSCODE),
    "codium.exe": EditorKind("vscode", "VSCodium", INTEGRATION_VSCODE),
    "windsurf.exe": EditorKind("vscode", "Windsurf", INTEGRATION_VSCODE),
    "devenv.exe": EditorKind("visualstudio", "Visual Studio", INTEGRATION_VISUAL_STUDIO),
    "idea64.exe": EditorKind("jetbrains", "IntelliJ IDEA"),
    "pycharm64.exe": EditorKind("jetbrains", "PyCharm"),
    "rider64.exe": EditorKind("jetbrains", "Rider"),
    "webstorm64.exe": EditorKind("jetbrains", "WebStorm"),
    "clion64.exe": EditorKind("jetbrains", "CLion"),
    "goland64.exe": EditorKind("jetbrains", "GoLand"),
    "phpstorm64.exe": EditorKind("jetbrains", "PhpStorm"),
    "rubymine64.exe": EditorKind("jetbrains", "RubyMine"),
    "studio64.exe": EditorKind("jetbrains", "Android Studio"),
    "eclipse.exe": EditorKind("eclipse", "Eclipse"),
    "netbeans64.exe": EditorKind("netbeans", "NetBeans"),
    "sublime_text.exe": EditorKind("sublime", "Sublime Text"),
    "notepad++.exe": EditorKind("notepadpp", "Notepad++"),
    "zed.exe": EditorKind("zed", "Zed"),
}

# Editors join title parts with " - " (or an en/em dash).
_TITLE_SEPARATOR_RE = re.compile(r"\s[-–—]\s")


@dataclass(frozen=True)
class ForegroundApp:
    hwnd: int
    pid: int
    process_name: str
    title: str
    editor: Optional[EditorKind]
    file_hint: Optional[str] = None
    language: Optional[str] = None


def editor_for_process(process_name: Optional[str]) -> Optional[EditorKind]:
    if not process_name:
        return None
    return KNOWN_EDITORS.get(process_name.lower())


def parse_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """Guess the active file and its language from a window title such as
    ``"main.py - project - Visual Studio Code"``. Best effort only."""
    for part in _TITLE_SEPARATOR_RE.split(title or ""):
        candidate = part.strip().lstrip("●*• ").rstrip("*").strip()
        language = language_for_path(candidate)
        if language:
            return candidate, language
    return None, None


def get_foreground_app() -> Optional[ForegroundApp]:
    """Describe the foreground window, or ``None`` if unavailable."""
    if psutil is None:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None  # our own overlay/settings window
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        process_name = psutil.Process(pid.value).name()
    except Exception:
        _logger.debug("Could not read foreground app.", exc_info=True)
        return None
    title = buffer.value or ""
    file_hint, language = parse_title(title)
    return ForegroundApp(
        hwnd=int(hwnd),
        pid=int(pid.value),
        process_name=process_name,
        title=title,
        editor=editor_for_process(process_name),
        file_hint=file_hint,
        language=language,
    )


def running_editors() -> dict[str, EditorKind]:
    """Return ``{process_name: EditorKind}`` for every running known editor."""
    found: dict[str, EditorKind] = {}
    if psutil is None:
        return found
    try:
        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            kind = KNOWN_EDITORS.get(name)
            if kind is not None:
                found[name] = kind
    except Exception:
        _logger.debug("Could not enumerate processes.", exc_info=True)
    return found
