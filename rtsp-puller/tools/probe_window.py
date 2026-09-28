# -*- coding: utf-8 -*-
"""扫描指定时间窗内录像可用情况（利用 DESCRIBE 404 = 无录像）。

用法:
    python tools/probe_window.py "2026-09-14 14:50:00" "2026-09-14 17:10:00" [步长分钟]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from nvrcore.config import load_config                       # noqa: E402
from nvrcore.downloader import probe_range, RTSP_NO_RECORD   # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    s = datetime.strptime(sys.argv[1], FMT)
    e = datetime.strptime(sys.argv[2], FMT)
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    cfg = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)

    print("扫描 %s ~ %s，每 %d 分钟探测一次" % (s.strftime(FMT), e.strftime(FMT), step))
    print("-" * 62)
    cur = s
    has = []
    none = []
    while cur < e:
        probe_end = min(cur + timedelta(minutes=step), e)
        code = probe_range(cfg.nvr, cur, probe_end)
        label = {200: "有录像", 404: "无录像", None: "探测失败"}.get(code, str(code))
        print("%s ~ %s  ->  %s" % (cur.strftime(FMT), probe_end.strftime(FMT), label))
        (none if code == RTSP_NO_RECORD else has).append(cur)
        cur = probe_end
    print("-" * 62)
    print("有录像片段: %d / 共 %d" % (len(has), len(has) + len(none)))
    if has:
        print("最早有录像: %s" % has[0].strftime(FMT))
        print("最晚有录像: %s" % has[-1].strftime(FMT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
