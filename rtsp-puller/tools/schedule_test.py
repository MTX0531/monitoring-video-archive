# -*- coding: utf-8 -*-
"""验证「拉取时段」功能：窗口计算的单元断言 + 真实拉取的两组场景。

覆盖点：
  1. 时段窗口的边界计算（含跨午夜）
  2. 游标落在夜间空档时直接跳过、不下载
  3. 分段在时段边界（22:30 类）被正确截断
  4. 到达 --to 终点后自动退出
  5. 追平实时后「等录满一段再拉」，不退化成碎片文件
  6. schedule.enabled=false 时不影响原有「只设终点」的用法
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, time, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TMP = os.path.join(BASE, "_sched_test")
FMT = "%Y-%m-%d %H:%M:%S"

sys.path.insert(0, BASE)
from nvrcore.config import (window_contains, advance_to_window, window_close,   # noqa: E402
                            windowed_seconds, load_config)

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s  得到=%r 期望=%r" % ("OK" if ok else "!!", name, got, want))


def unit_tests():
    print("=" * 74)
    print("一、时段窗口计算")
    print("=" * 74)
    ws, we = time(9, 30), time(22, 30)
    D = datetime(2026, 9, 14)
    for hh, mm, want in [(9, 29, False), (9, 30, True), (12, 0, True),
                         (22, 29, True), (22, 30, False), (23, 59, False), (3, 0, False)]:
        check("window_contains %02d:%02d" % (hh, mm),
              window_contains(D.replace(hour=hh, minute=mm), ws, we), want)

    check("advance_to_window 09:29 -> 09:30",
          advance_to_window(D.replace(hour=9, minute=29), ws, we), D.replace(hour=9, minute=30))
    check("advance_to_window 22:30 -> 次日 09:30",
          advance_to_window(D.replace(hour=22, minute=30), ws, we),
          (D + timedelta(days=1)).replace(hour=9, minute=30))
    check("advance_to_window 03:00 -> 当天 09:30",
          advance_to_window(D.replace(hour=3), ws, we), D.replace(hour=9, minute=30))
    check("advance_to_window 14:00 原地不动",
          advance_to_window(D.replace(hour=14), ws, we), D.replace(hour=14))

    check("window_close 14:00 -> 当天 22:30",
          window_close(D.replace(hour=14), ws, we), D.replace(hour=22, minute=30))

    got = windowed_seconds(datetime(2026, 9, 14, 14, 55), datetime(2026, 9, 15, 12, 0), ws, we)
    check("windowed_seconds 跨夜累计", round(got / 60), round((7 * 60 + 35) + (2 * 60 + 30)))
    check("windowed_seconds 夜间段为 0",
          windowed_seconds(datetime(2026, 9, 14, 23, 0), datetime(2026, 9, 15, 6, 0), ws, we), 0.0)

    nws, nwe = time(22, 0), time(6, 0)
    check("跨午夜 window_contains 23:00", window_contains(D.replace(hour=23), nws, nwe), True)
    check("跨午夜 window_contains 03:00", window_contains(D.replace(hour=3), nws, nwe), True)
    check("跨午夜 window_contains 12:00", window_contains(D.replace(hour=12), nws, nwe), False)


def write_cfg(name, schedule, archive, runtime_extra=None):
    os.makedirs(os.path.join(TMP, name), exist_ok=True)
    archive.setdefault("store_name", "测试门店-临时")
    archive.setdefault("channel_name", "通道1")
    cfg = {
        "nvr": {"host": "192.168.2.10", "rtsp_port": 554, "username": "admin",
                "password": "<NVR_PASSWORD>", "channel": 1, "stream": "main", "transport": "tcp"},
        "archive": archive,
        "schedule": schedule,
        "device": {"enabled": False},
        "retention": {"enabled": False, "keep_days": 7, "max_archive_gb": 0,
                      "min_free_gb": 0, "protect_recent_hours": 24,
                      "delete_mode": "permanent", "scan_interval_minutes": 30},
        "runtime": {"poll_interval_seconds": 5, "max_retries": 2, "retry_backoff_seconds": 5,
                    "stall_timeout_seconds": 120, "max_attempts_per_segment": 3,
                    "ffmpeg_path": "auto",
                    "log_dir": "_sched_test/%s/logs" % name,
                    "state_file": "_sched_test/%s/state.json" % name,
                    "log_keep_days": 30},
        "hooks": {"on_segment_saved": ""},
    }
    p = os.path.join(TMP, name, "config.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


def run(cfg_path, extra_args):
    cmd = [PY, "nvr_puller.py", "--config", cfg_path] + extra_args
    p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def show(title, rc, out):
    print("-" * 74)
    print(title)
    print("-" * 74)
    for line in out.splitlines():
        if any(k in line for k in ("准备拉取", "已保存", "跳过", "终点", "拉取时段",
                                   "不在拉取时段", "等录满", "空闲", "累计保存",
                                   "拉取进度", "起点", "最新录像只到")):
            print("   " + line.split("] ", 1)[-1])
    print("   退出码=%s" % rc)


def scenario_a():
    print()
    print("=" * 74)
    print("二、真实拉取：时段 09:00~23:00，拉 9/14 22:56 -> 23:20（2 分钟一段）")
    print("   预期：拉 22:56~22:58、22:58~23:00（在 23:00 边界截断）")
    print("=" * 74)
    cfg = write_cfg("a",
                    {"enabled": True, "start": "09:00", "end": "23:00", "mode": "footage"},
                    {"root_dir": "_sched_test/a/archive", "folder_name": "a",
                     "start_time": "2026-09-14 22:56:00", "end_time": "auto",
                     "segment_minutes": 2, "min_segment_seconds": 30,
                     "safety_lag_seconds": 180, "container": "mp4", "organize_by_date": True})
    rc, out = run(cfg, ["--from", "2026-09-14 22:56:00", "--to", "2026-09-14 23:20:00"])
    show("场景 A 输出", rc, out)
    files = []
    for dp, _dn, fn in os.walk(os.path.join(TMP, "a", "archive")):
        files += sorted(fn)
    print("   落盘文件：%s" % (files or "(无)"))
    check("A: 正常退出", rc, 0)
    check("A: 产出 2 个文件", len(files), 2)
    check("A: 出现夜间跳过与终点退出",
          ("跳过该时段" in out) and ("已到达归档终点" in out), True)
    starts = [f.split("_")[2] for f in files]
    print("   各文件起点：%s" % starts)
    check("A: 没有拉 23:00 之后的录像（按起点判断）",
          all(s < "20260914230000" for s in starts), True)


def scenario_b():
    print()
    print("=" * 74)
    print("三、真实拉取：追平实时时的行为（30 分钟一段，起点=1 分钟前）")
    print("   预期：不等出一堆小文件，直接判定「尚不足一段」并退出/等待")
    print("=" * 74)
    cfg = write_cfg("b",
                    {"enabled": True, "start": "00:00", "end": "23:59", "mode": "footage"},
                    {"root_dir": "_sched_test/b/archive", "folder_name": "b",
                     "start_time": "auto", "end_time": "auto",
                     "segment_minutes": 30, "min_segment_seconds": 60,
                     "safety_lag_seconds": 180, "container": "mp4", "organize_by_date": True})
    near = (datetime.now() - timedelta(minutes=1)).strftime(FMT)
    rc, out = run(cfg, ["--from", near, "--once"])
    show("场景 B 输出", rc, out)
    files = []
    for dp, _dn, fn in os.walk(os.path.join(TMP, "b", "archive")):
        files += sorted(fn)
    print("   落盘文件：%s" % (files or "(无)"))
    check("B: 未产出碎片文件", len(files), 0)
    check("B: 提示尚不足一段", "尚不足一段" in out, True)


def scenario_c():
    print()
    print("=" * 74)
    print("四、schedule.enabled=false（兼容旧的「只设终点」用法）")
    print("   预期：不套用时段限制，正常拉 1 分钟并退出")
    print("=" * 74)
    cfg = write_cfg("c",
                    {"enabled": False, "start": "09:30", "end": "22:30", "mode": "footage"},
                    {"root_dir": "_sched_test/c/archive", "folder_name": "c",
                     "start_time": "2026-09-14 15:00:00", "end_time": "auto",
                     "segment_minutes": 1, "min_segment_seconds": 20,
                     "safety_lag_seconds": 180, "container": "mp4", "organize_by_date": True})
    rc, out = run(cfg, ["--from", "2026-09-14 15:00:00", "--to", "2026-09-14 15:01:00"])
    show("场景 C 输出", rc, out)
    files = []
    for dp, _dn, fn in os.walk(os.path.join(TMP, "c", "archive")):
        files += sorted(fn)
    print("   落盘文件：%s" % (files or "(无)"))
    check("C: 正常退出", rc, 0)
    check("C: 产出 1 个文件", len(files), 1)
    check("C: 提示跟随最新录像（未套用时段）", "跟随最新录像" in out or "拉取时段" not in out, True)


def main():
    unit_tests()
    scenario_a()
    scenario_b()
    scenario_c()
    print()
    print("=" * 74)
    print("结果：通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   失败：" + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
