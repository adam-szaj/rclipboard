from __future__ import annotations

import logging
import os

TRACE_LEVEL_NUM = 5
if not hasattr(logging, "TRACE"):
    logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self: logging.Logger, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, *args, **kws)


def backtrace(self: logging.Logger, e: Exception, *args, **kws):
    tb = e.__traceback__
    while tb:
        self._log(3, f"{tb.tb_frame}:{tb.tb_lineno}", *args, **kws)
        tb = tb.tb_next


logging.Logger.trace = trace  # type: ignore[attr-defined]
logging.Logger.backtrace = backtrace

FORMAT = (
    "%(levelname)s * [%(taskName)s] %(filename)s:%(lineno)d:%(funcName)s: %(message)s"
)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    # Optionally set level from env if not configured
    level = os.environ.get("RCLIPBOARD_PY_LOG_LEVEL")
    if level and not logger.handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO), format=FORMAT
        )
    return logger
