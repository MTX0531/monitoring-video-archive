# -*- coding: utf-8 -*-
"""日志初始化：控制台 + 按天滚动文件，并自动清理过期日志。"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta

_FMT = "%(asctime)s [%(levelname)-7s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _force_utf8_stream():
    """让 Windows 控制台也能正确显示中文。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logger(log_dir: str, keep_days: int = 30, level: int = logging.INFO,
                 console: bool = True) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    _force_utf8_stream()

    logger = logging.getLogger("nvr")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(_FMT, _DATEFMT)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        try:
            ch.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        logger.addHandler(ch)

    log_file = os.path.join(log_dir, "puller_%s.log" % datetime.now().strftime("%Y-%m-%d"))
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    _cleanup_old_logs(log_dir, keep_days, logger)
    return logger


def _cleanup_old_logs(log_dir: str, keep_days: int, logger: logging.Logger) -> None:
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        for name in os.listdir(log_dir):
            if not (name.startswith("puller_") and name.endswith(".log")):
                continue
            fp = os.path.join(log_dir, name)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    removed += 1
            except OSError:
                pass
    except OSError:
        return
    if removed:
        logger.info("已清理 %d 个过期日志文件", removed)


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return "%.2f %s" % (num_bytes, unit)
        num_bytes /= 1024.0
    return "%.2f PB" % num_bytes


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d小时%d分" % (h, m)
    if m:
        return "%d分%d秒" % (m, s)
    return "%d秒" % s
