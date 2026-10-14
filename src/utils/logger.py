"""
统一日志：写到 %APPDATA%/AgentMonitor/logs/
"""
from __future__ import annotations
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOGGER_INITIALIZED = False


def setup_logger(log_dir: Path) -> logging.Logger:
    global _LOGGER_INITIALIZED
    logger = logging.getLogger("agent_monitor")
    if _LOGGER_INITIALIZED:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台（开发用，发布版改为 WARNING）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件（保留 5MB × 3 个）
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        log_dir / "monitor.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    _LOGGER_INITIALIZED = True
    return logger
