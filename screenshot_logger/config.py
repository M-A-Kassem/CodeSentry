"""Application configuration for Screenshot & Error Logger.

All paths are computed relative to the project root (the folder that
contains ``main.py``), so the application works the same way regardless of
Windows username or machine -- nothing is hard-coded to a specific path.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
INCIDENTS_DIR = DATA_DIR / "incidents"
LOGS_DIR = DATA_DIR / "logs"
APP_LOG_FILE = LOGS_DIR / "app.log"
CONFIG_FILE = PROJECT_ROOT / "config.json"

APP_NAME = "Screenshot & Error Logger"

# Per-user (not per-project) folder for the VS Code bridge discovery file.
# It lives under %LOCALAPPDATA% so the companion extension can always find
# it without knowing where this project was checked out.
USER_STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ScreenshotErrorLogger"


@dataclass
class AppConfig:
    """Runtime configuration, loaded from (or saved to) ``config.json``."""

    screenshot_enabled: bool = True
    retention_days: int = 0
    hotkey_enabled: bool = False
    hotkey_combo: str = "ctrl+shift+f9"
    log_level: str = "INFO"
    event_monitoring_enabled: bool = False
    event_monitoring_poll_seconds: int = 30

    # -- Coding error assistant (tray app) ------------------------------
    # Auto Monitor is off by default: monitoring only starts when the user
    # clicks Start Monitoring, unless they explicitly turn this on.
    auto_monitor_enabled: bool = False
    overlay_enabled: bool = True
    overlay_x: Optional[int] = None
    overlay_y: Optional[int] = None
    # Seconds a new diagnostic must persist before it counts as a "new
    # issue this session" (filters out errors that flash up mid-typing).
    new_issue_debounce_seconds: float = 3.0
    record_new_errors_to_history: bool = True
    record_new_warnings_to_history: bool = False
    # Off by default: a screenshot per coding error would capture code.
    screenshot_on_new_coding_error: bool = False
    vscode_bridge_enabled: bool = True
    vscode_bridge_port: int = 0  # 0 = pick a free port (published in the discovery file)
    visual_studio_enabled: bool = True
    visual_studio_poll_seconds: int = 5
    # Screen detection is opt-in: it captures the foreground editor window.
    screen_detection_enabled: bool = False
    screen_poll_seconds: int = 5
    screen_ocr_enabled: bool = True
    screen_min_confidence_to_count: float = 0.8
    # Project source analysis (the primary detector). project_path is the
    # folder the user chose; when unset, the project of the file open in
    # Visual Studio / VS Code is detected and used.
    project_analysis_enabled: bool = True
    project_path: Optional[str] = None
    project_exclude_dirs: list = field(default_factory=list)
    project_poll_seconds: float = 3.0
    project_max_files: int = 20000
    # Analyze the unsaved text of files open in Visual Studio (read through
    # its automation API), so errors count before you save.
    analyze_unsaved_editor_text: bool = True
    # Turn analyzers on/off by name, e.g. {"dotnet-build": true, "ruff": false}.
    analyzers: dict = field(default_factory=dict)
    # Explicitly configured compilers/linters; never run unless listed here
    # with "enabled": true. See README "Build and static-analysis tools".
    build_tools: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CONFIG: dict[str, Any] = AppConfig().to_dict()


def ensure_data_dirs() -> None:
    """Create the data directory tree (screenshots/incidents/logs) if needed."""
    for directory in (SCREENSHOTS_DIR, INCIDENTS_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    """Load configuration from ``config.json``, creating defaults if missing.

    Any read or parse failure falls back to defaults rather than raising, so
    a corrupted config file never prevents the application from starting.
    """
    if not CONFIG_FILE.exists():
        default = AppConfig()
        save_config(default)
        return default

    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.getLogger(__name__).warning(
            "Could not read config.json; using default configuration."
        )
        return AppConfig()

    if not isinstance(raw, dict):
        raw = {}
    merged = {**DEFAULT_CONFIG, **raw}
    try:
        return AppConfig(**{f.name: _coerce(f.name, merged[f.name]) for f in fields(AppConfig)})
    except (KeyError, ValueError, TypeError):
        logging.getLogger(__name__).warning(
            "config.json has invalid values; using default configuration."
        )
        return AppConfig()


def _coerce(name: str, value: Any) -> Any:
    """Coerce a raw JSON value to the type of the matching default."""
    default = DEFAULT_CONFIG[name]
    if name in ("overlay_x", "overlay_y"):
        return None if value is None else int(value)
    if name == "project_path":
        return None if not value else str(value)
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise TypeError(f"{name} must be an object")
        return value
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value)
    if isinstance(default, list):
        if not isinstance(value, list):
            raise TypeError(f"{name} must be a list")
        return value
    return value


def save_config(config: AppConfig) -> None:
    """Persist configuration to ``config.json`` (UTF-8)."""
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def configure_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure (once) the shared operational logger -> data/logs/app.log."""
    ensure_data_dirs()
    logger = logging.getLogger("screenshot_logger")
    if logger.handlers:
        return logger  # already configured by an earlier call

    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.FileHandler(APP_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger
