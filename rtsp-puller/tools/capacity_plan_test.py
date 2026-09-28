# -*- coding: utf-8 -*-
"""容量测算工具验证。

覆盖：
  1. 时段解析；跨午夜时段自动 +24
  2. 素材总量 = 通道数 × 每日时长
  3. 存储账 = 总量 × 码率 × 留存天数（且按天线性）
  4. 并发分配：受 config 设置与「设备额度 ÷ 通道数」双重约束，取小
  5. 单通道时并发能拉满
  6. 15 通道时每通道只能 1 条流，且标记「额度已吃满」
  7. 通道数 > 额度时单批耗时 = 总素材 ÷ 额度（超出部分排队）
  8. 可行性判定：24 小时内能否跑完
  9. 带宽换算与占比
 10. 命令行：--json 字段齐全、非法参数返回码

全部离线运行，不连录像机。
用法：python tools/capacity_plan_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from capacity_plan import (plan, parse_hhmm, schedule_hours,   # noqa: E402
                           DEFAULT_SESSION_CAP, DEFAULT_BITRATE_KBPS)

PASS = 0
FAIL = 0
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望=%r\n         实得=%r" % (label, want, got))


def check_true(label, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s   %s" % (label, note))


def near(label, got, want, tol=0.05):
    global PASS, FAIL
    if abs(got - want) <= tol:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望≈%r (容差%r)\n         实得=%r"
              % (label, want, tol, got))


class _Sched:
    enabled = True
    start = "09:30"
    end = "22:30"


class _Cfg:
    schedule = _Sched()


def test_parse_hhmm():
    print("\n[1] 时段解析")
    check("'00:00' -> 0.0", parse_hhmm("00:00"), 0.0)
    check("'09:30' -> 9.5", parse_hhmm("09:30"), 9.5)
    check("'22:30' -> 22.5", parse_hhmm("22:30"), 22.5)
    check("'23:59' -> 23+59/60", round(parse_hhmm("23:59"), 4), round(23 + 59 / 60, 4))
    check("带空格也能解析", parse_hhmm(" 08:15 "), 8.25)


def test_schedule_hours():
    print("\n[2] 营业时段小时数")
    check("09:30~22:30 = 13 小时", schedule_hours(_Cfg()), 13.0)

    class _Cross:
        class schedule:
            enabled = True
            start = "22:00"
            end = "06:00"
    check("跨午夜 22:00~06:00 = 8 小时", schedule_hours(_Cross()), 8.0)

    class _Off:
        class schedule:
            enabled = False
            start = "09:30"
            end = "22:30"
    check("关闭时段限制 = 24 小时", schedule_hours(_Off()), 24.0)


def test_footage_total():
    print("\n[3] 素材总量")
    r1 = plan(1, 13.0, 153, 30, 7, 15, 6)
    r15 = plan(15, 13.0, 153, 30, 7, 15, 6)
    near("单通道 = 13 小时", r1["footage_h_total"], 13.0, 0.001)
    near("15 通道 = 195 小时", r15["footage_h_total"], 195.0, 0.001)
    check("单通道每天 26 段", int(r1["seg_count_total"]), 26)
    check("15 通道每天 390 段", int(r15["seg_count_total"]), 390)


def test_storage():
    print("\n[4] 存储账")
    r = plan(15, 13.0, 153, 30, 7, 15, 6)
    near("单通道每天 ≈ 0.83 GB", r["daily_bytes_per_ch"] / 1024 ** 3, 0.834, 0.01)
    near("15 通道每天 ≈ 12.5 GB", r["daily_bytes_total"] / 1024 ** 3, 12.51, 0.05)
    near("留存 7 天 ≈ 87.6 GB", r["retention_bytes"] / 1024 ** 3, 87.5, 0.5)
    near("留存按天数线性（1 天）",
         plan(15, 13.0, 153, 30, 1, 15, 6)["retention_bytes"],
         r["daily_bytes_total"], 1.0)
    r2 = plan(15, 13.0, 306, 30, 7, 15, 6)
    near("码率翻倍 -> 占用翻倍", r2["daily_bytes_total"], r["daily_bytes_total"] * 2, 1.0)


def test_concurrency_allocation():
    print("\n[5] 并发分配（config 设置与设备额度取小）")
    r1 = plan(1, 13.0, 153, 30, 7, 15, 6)
    check("单通道: 用 config 的 6 条流", r1["streams_per_ch"], 6)
    check("单通道: 有效并发 6", r1["effective_concurrency"], 6)
    near("单通道: 单批 ≈ 2.17 小时", r1["wall_hours"], 13.0 / 6, 0.01)

    r2 = plan(2, 13.0, 153, 30, 7, 15, 6)
    check("2 通道: 每通道 min(6, 15//2=7) = 6 条流", r2["streams_per_ch"], 6)
    check("2 通道: 但总额度只有 15 -> 有效并发 12", r2["effective_concurrency"], 12)

    r15 = plan(15, 13.0, 153, 30, 7, 15, 6)
    check("15 通道: 每通道被压到 1 条流", r15["streams_per_ch"], 1)
    check("15 通道: 有效并发 15", r15["effective_concurrency"], 15)
    near("15 通道: 单批 = 13 小时（正好等于一个营业日）",
         r15["wall_hours"], 13.0, 0.01)

    r4 = plan(4, 13.0, 153, 30, 7, 15, 6)
    check("4 通道: 每通道 min(6, 3) = 3 条流", r4["streams_per_ch"], 3)
    check("4 通道: 有效并发 12", r4["effective_concurrency"], 12)
    near("4 通道: 单批 ≈ 4.33 小时", r4["wall_hours"], 52.0 / 12, 0.01)


def test_headroom_and_beyond_cap():
    print("\n[6] 额度吃满与超出额度")
    r15 = plan(15, 13.0, 153, 30, 7, 15, 6)
    check_true("15 通道标记额度已吃满", r15["no_headroom"])
    r8 = plan(8, 13.0, 153, 30, 7, 15, 6)
    check_true("8 通道未吃满额度", not r8["no_headroom"])

    r20 = plan(20, 13.0, 153, 30, 7, 15, 6)
    check("20 通道: 每通道只能 1 条流", r20["streams_per_ch"], 1)
    check("20 通道: 有效并发仍被截到 15", r20["effective_concurrency"], 15)
    near("20 通道: 单批 = 260/15 小时（超出部分排队）",
         r20["wall_hours"], 260.0 / 15, 0.01)
    check_true("20 通道标记额度已吃满", r20["no_headroom"])


def test_feasibility_and_bandwidth():
    print("\n[7] 可行性判定与带宽")
    r15 = plan(15, 13.0, 153, 30, 7, 15, 6)
    check_true("15 通道 13 小时 < 24 小时 -> 可行", r15["fits_day"])
    near("15 通道余量约 45.8%", r15["margin_pct"], (24.0 - 13.0) / 24.0 * 100, 0.1)

    r30 = plan(30, 13.0, 153, 30, 7, 15, 6)
    check("30 通道: 单批 26 小时 > 24 小时", int(r30["wall_hours"]), 26)
    check_true("30 通道判定为跑不完", not r30["fits_day"])

    near("15 路 × 153kbps ≈ 2.30 Mbps", r15["net_mbps"], 2.295, 0.01)
    check_true("带宽占比远小于 100%", r15["net_pct"] < 5.0,
               "实测 %.1f%%" % r15["net_pct"])

    r_hi = plan(15, 13.0, 4096, 30, 7, 15, 6)
    check_true("码率提到 4Mbps 后带宽占比显著上升", r_hi["net_pct"] > 50.0,
               "实测 %.1f%%" % r_hi["net_pct"])


def test_cli():
    print("\n[8] 命令行")
    p = subprocess.run([PY, os.path.join(BASE, "tools", "capacity_plan.py"),
                        "--channels", "15", "--json"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=BASE)
    check_true("--json 退出码为 0 或 1", p.returncode in (0, 1),
               "rc=%s" % p.returncode)
    try:
        d = json.loads(p.stdout)
        check("JSON 含 channels=15", d["channels"], 15)
        check_true("JSON 含 wall_hours", "wall_hours" in d)
        check_true("JSON 含 retention_bytes", "retention_bytes" in d)
        check("默认额度为本机型实测值", d["cap"], DEFAULT_SESSION_CAP)
        check("默认码率为实测值", int(d["bitrate_kbps"]), DEFAULT_BITRATE_KBPS)
    except (ValueError, KeyError) as exc:
        check_true("JSON 可解析", False, "%s / %s" % (exc, p.stdout[:200]))

    p2 = subprocess.run([PY, os.path.join(BASE, "tools", "capacity_plan.py"),
                         "--channels", "0"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=BASE)
    check("--channels 0 返回码 2", p2.returncode, 2)

    p3 = subprocess.run([PY, os.path.join(BASE, "tools", "capacity_plan.py"),
                         "--channels", "1"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=BASE)
    check_true("单通道文本报告含时间账", "时间账" in p3.stdout)
    check_true("单通道文本报告含规模对照", "规模对照" in p3.stdout)


def main() -> int:
    print("=" * 70)
    print("容量测算工具验证（tools/capacity_plan.py）")
    print("=" * 70)
    test_parse_hhmm()
    test_schedule_hours()
    test_footage_total()
    test_storage()
    test_concurrency_allocation()
    test_headroom_and_beyond_cap()
    test_feasibility_and_bandwidth()
    test_cli()
    print("\n" + "=" * 70)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
