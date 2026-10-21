"""DEMO / TEST SCRIPT -- not part of the application itself.

Shows how an external Python automation script integrates with
Screenshot & Error Logger. Run it directly to see sample incidents get
created:

    python examples/demo_errors.py

It intentionally triggers three sample failures (division by zero, a
missing file, and a simulated automation failure) and reports each one
through IncidentLogger, the same way a real automation script would.
This never runs automatically -- it only runs when you execute this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running this script directly without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screenshot_logger import IncidentLogger  # noqa: E402


def demo_division_by_zero(logger: IncidentLogger) -> None:
    try:
        10 / 0
    except Exception as exc:
        incident = logger.log_exception(
            title="Calculation failed", exception=exc, source="examples.demo_errors"
        )
        print(f"[division by zero] incident id: {incident.incident_id}")


def demo_missing_file(logger: IncidentLogger) -> None:
    try:
        Path("this_file_does_not_exist_12345.txt").read_text(encoding="utf-8")
    except Exception as exc:
        incident = logger.log_exception(
            title="Expected input file missing", exception=exc, source="examples.demo_errors"
        )
        print(f"[missing file] incident id: {incident.incident_id}")


def demo_simulated_automation_failure(logger: IncidentLogger) -> None:
    try:
        raise RuntimeError("Simulated automation failure: target UI element not found.")
    except Exception as exc:
        incident = logger.log_exception(
            title="Automation step failed", exception=exc, source="examples.demo_errors"
        )
        print(f"[automation failure] incident id: {incident.incident_id}")


def main() -> None:
    print("Running Screenshot & Error Logger demo/test scenarios...")
    logger = IncidentLogger()
    demo_division_by_zero(logger)
    demo_missing_file(logger)
    demo_simulated_automation_failure(logger)
    print(f"Done. Total incidents recorded so far: {logger.count()}")


if __name__ == "__main__":
    main()
