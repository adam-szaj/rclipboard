from __future__ import annotations

import logging
import os


TRACE_LEVEL_NUM = 5
if not hasattr(logging, "TRACE"):
    logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self: logging.Logger, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    # Optionally set level from env if not configured
    level = os.environ.get("RCLIPBOARD_PY_LOG_LEVEL")
    if level and not logger.handlers:
        logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))
    return logger

