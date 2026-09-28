# -*- coding: utf-8 -*-
"""诊断工具：定位录像机上最早可用的录像时刻，并可按范围扫描空档。

用法：
    python tools/find_recording_start.py                     # 从早期逐点往前找最早录像
    python tools/find_recording_start.py 2026-09-16 00:00 2026-09-16 12:00 30
                                                            # 扫描区间，步进 30 分钟
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from nvrcore.config import load_config          # noqa: E402
from nvrcore.downloader import probe_range      # noqa: E402

LABEL = {200: "有录像", 404: "无录像", None: "探测失败"}


def probe(nvr, start: datetime, seconds: int = 20) -> int:
    return probe_range(nvr, start, start + timedelta(seconds=seconds))


def scan(nvr, start: datetime, end: datetime, step_minutes: int) -> None:
    print("扫描 %s ~ %s，步进 %d 分钟" % (start, end, step_minutes))
    print("-" * 78)
    t = start
    while t <= end:
        code = probe(nvr, t)
        mark = "  " if code == 200 else ("空" if code == 404 else "!!")
        print("  %s %s  %s" % (mark, t.strftime("%Y-%m-%d %H:%M:%S"), LABEL.get(code, code)))
        t += timedelta(minutes=step_minutes)


def find_earliest(nvr, hint: datetime, window_hours: int = 12, step_minutes: int = 5) -> None:
    """从 hint 往前后二分，定位最早的录像时刻。"""
    print("在 %s 前后 %d 小时内定位最早录像……" % (hint.strftime("%Y-%m-%d %H:%M"), window_hours))
    print("-" * 78)
    lo = hint - timedelta(hours=window_hours)   # 应无录像
    hi = hint + timedelta(hours=window_hours)   # 应有录像
    if probe(nvr, hi) != 200:
        print("  起点 %s 处探测不到录像，请先确认监控是否在录。" % hi.strftime("%Y-%m-%d %H:%M"))
        return
    if probe(nvr, lo) == 200:
        print("  %s 处已有录像，需要把窗口往前扩。" % lo.strftime("%Y-%m-%d %H:%M"))
        return
    while (hi - lo).total_seconds() > step_minutes * 60:
        mid = lo + (hi - lo) / 2
        code = probe(nvr, mid)
        print("  二分 %s -> %s" % (mid.strftime("%Y-%m-%d %H:%M:%S"), LABEL.get(code, code)))
        if code == 200:
            hi = mid
        else:
            lo = mid
    print("-" * 78)
    print("最早可用录像时刻约为：%s" % hi.strftime("%Y-%m-%d %H:%M:%S"))


def main() -> int:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cfg = load_config(None, base_dir=base_dir)

    if len(sys.argv) >= 4:
        start = datetime.strptime(sys.argv[1], "%Y-%m-%d %H:%M")
        end = datetime.strptime(sys.argv[2], "%Y-%m-%d %H:%M")
        step = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        scan(cfg.nvr, start, end, step)
    else:
        find_earliest(cfg.nvr, datetime(2026, 9, 14, 12, 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
