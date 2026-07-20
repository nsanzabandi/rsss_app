"""
utils/logger.py — Simple file + console logger for report jobs.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

_LOGS_DIR = Path(__file__).parent.parent / "logs"
_LOGS_DIR.mkdir(exist_ok=True)


class ReportLogger:
    """
    Thin wrapper around Python's logging module.
    Creates a named logger that writes to both console and a dated log file.
    """

    def __init__(self, name: str = "rsss"):
        self.name = name
        self._logger = logging.getLogger(f"rsss.{name}")

        if not self._logger.handlers:
            self._logger.setLevel(logging.DEBUG)

            fmt = logging.Formatter(
                "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            # Console
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            self._logger.addHandler(ch)

            # File (one per day)
            log_file = _LOGS_DIR / f"{datetime.today().strftime('%Y-%m-%d')}.log"
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            self._logger.addHandler(fh)

    # Proxy log methods
    def debug(self, msg: str, *a, **kw):    self._logger.debug(msg, *a, **kw)
    def info(self, msg: str, *a, **kw):     self._logger.info(msg, *a, **kw)
    def warning(self, msg: str, *a, **kw):  self._logger.warning(msg, *a, **kw)
    def error(self, msg: str, *a, **kw):    self._logger.error(msg, *a, **kw)
    def critical(self, msg: str, *a, **kw): self._logger.critical(msg, *a, **kw)

    def section(self, title: str) -> None:
        self.info("=" * 60)
        self.info(f"  {title}")
        self.info("=" * 60)
