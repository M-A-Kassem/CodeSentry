"""Entry point for the Screenshot & Error Logger desktop application.

Run with:

    python main.py                 # tray app: coding error monitor + overlay (default)
    python main.py --classic       # the original incident window only, no tray
    python main.py --monitor       # run the Windows Event Log monitor only, no GUI
    python main.py --stop-monitor  # ask a running --monitor process to stop

See README.md for full setup and usage instructions, including how to
start monitoring automatically at Windows logon.
"""
from __future__ import annotations

import argparse
import os
import time

from screenshot_logger.config import (
    DATA_DIR,
    configure_logging,
    ensure_data_dirs,
    load_config,
)
from screenshot_logger.event_monitor import EventLogMonitor
from screenshot_logger.gui import run_app
from screenshot_logger.incident_logger import IncidentLogger
from screenshot_logger import win_display

MONITOR_PID_FILE = DATA_DIR / "logs" / "monitor.pid"


def _run_monitor() -> None:
    """Run the event log monitor with no GUI, blocking until stopped.

    Writes its own process ID to a small PID file. Stop it either with
    Ctrl+C in this console, or from another window with
    ``python main.py --stop-monitor`` -- which deletes the PID file to
    cooperatively ask this loop to shut down; it is checked once a second.
    """
    ensure_data_dirs()
    config = load_config()
    logger = configure_logging(config.log_level)

    MONITOR_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    incident_logger = IncidentLogger(config=config)
    monitor = EventLogMonitor(incident_logger, poll_seconds=config.event_monitoring_poll_seconds)

    print(f"Event log monitor running (PID {os.getpid()}).")
    print("Press Ctrl+C to stop, or run 'python main.py --stop-monitor' from another window.")
    logger.info("Starting headless event log monitor (PID %s).", os.getpid())

    monitor.start()
    try:
        while MONITOR_PID_FILE.exists():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        MONITOR_PID_FILE.unlink(missing_ok=True)
        logger.info("Headless event log monitor stopped.")
        print("Event log monitor stopped.")


def _stop_monitor() -> None:
    """Ask a running ``--monitor`` process to stop.

    Removing the PID file is the stop signal the running process polls
    for; this is a cooperative, graceful shutdown rather than a forced
    kill, so no extra dependency or OS-specific signalling is needed.
    """
    if not MONITOR_PID_FILE.exists():
        print("No running monitor process was found (no PID file).")
        return

    pid_text = MONITOR_PID_FILE.read_text(encoding="utf-8").strip()
    MONITOR_PID_FILE.unlink(missing_ok=True)

    try:
        import psutil

        if not psutil.pid_exists(int(pid_text)):
            print("Found a leftover PID file from a previous run (process was not running). Removed it.")
            return
    except (ValueError, ImportError):
        pass

    print(f"Stop requested for monitor process (PID {pid_text}). It will exit within a few seconds.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Screenshot & Error Logger")
    parser.add_argument(
        "--monitor", action="store_true", help="Run the Windows Event Log monitor without the GUI."
    )
    parser.add_argument(
        "--stop-monitor", action="store_true", help="Stop a monitor process started with --monitor."
    )
    parser.add_argument(
        "--classic", action="store_true", help="Open the original incident window without the tray app."
    )
    args = parser.parse_args()

    if args.stop_monitor:
        _stop_monitor()
        return
    if args.monitor:
        _run_monitor()
        return

    ensure_data_dirs()
    config = load_config()
    logger = configure_logging(config.log_level)
    win_display.enable_dpi_awareness()  # must happen before any window exists
    if args.classic:
        run_app(config)
        return

    if not win_display.acquire_single_instance():
        print("The tray app is already running (look for its icon in the notification area).")
        return
    try:
        from screenshot_logger.tray_app import run_tray_app
    except ImportError:
        logger.warning("Tray app unavailable (missing dependency); opening the classic window.", exc_info=True)
        run_app(config)
        return
    run_tray_app(config)


if __name__ == "__main__":
    main()
