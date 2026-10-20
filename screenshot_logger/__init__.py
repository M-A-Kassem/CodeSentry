"""Screenshot & Error Logger -- local incident capture toolkit.

Public API for use from other Python automation scripts:

    from screenshot_logger import IncidentLogger

    logger = IncidentLogger()
    try:
        ...
    except Exception as exc:
        logger.log_exception(title="Something failed", exception=exc)
"""
from .config import AppConfig, load_config
from .incident_logger import Incident, IncidentLogger, VALID_SEVERITIES

__all__ = ["Incident", "IncidentLogger", "VALID_SEVERITIES", "AppConfig", "load_config"]
