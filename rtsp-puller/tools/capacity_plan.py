# -*- coding: utf-8 -*-
"""按门店的摄像头数量，测算「拉不拉得完、存不存得下」。

为什么需要这个工具
--------------------------------------------------------------------
只有 1 路摄像头时，唯一要关心的是「拉多快」。门店变成 15 路之后，
问题从「速度」变成三本账，而且**都是硬约束**：

  1. **素材总量** = 通道数 × 每日时长。
  2. **设备并发上限**。提速只能靠同时开多条回放流，而录像机的并发流条数是
     有限的（本机型实测 15 路）。
  3. **存储**。容量 = 通道数 × 时长 × 码率 × 留存天数。

本工具把这三本账一次算清，部署前先跑一遍。

实测依据（测试门店A 店）
--------------------------------------------------------------------
  设备    DH-NVR4216-HDS2（16 路，转发带宽规格 64~128 Mbps，千兆网口）
  码率    153 kbps / 1280x720 / 25fps（主码流配置上限 1024 kbps VBR，实际只跑到 15%）
  体积    30 分钟片段约 32.8 MB，即 1.09 MB/分钟
  并发    阶梯法实测：叠加到 6+9 = 15 路同时传流全部正常，第 16 路拿不到流

用法
--------------------------------------------------------------------
    python tools/capacity_plan.py --channels 15
    python tools/capacity_plan.py --channels 15 --bitrate 1024 --hours 12 --keep-days 7
    python tools/capacity_plan.py --channels 15 --cap 15 --json

退出码：0 = 可行；1 = 不可行（存储或时间不够），便于部署脚本判定。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nvrcore.config import free_bytes, load_config          # noqa: E402
from nvrcore.logutil import human_size                       # noqa: E402

# 本机型（DH-NVR4216-HDS2）实测的并发回放流上限。换机型请用 --cap 覆盖并重新实测。
DEFAULT_SESSION_CAP = 15
# 实测单路码率（kbps）。--bitrate 可覆盖。
DEFAULT_BITRATE_KBPS = 153
# 设备转发带宽规格（Mbps），用于判断瓶颈在不在网上。
DEVICE_FORWARD_MBPS = (64, 128)


def parse_hhmm(text: str) -> float:
    """'09:30' -> 9.5（小时）。"""
    hh, mm = text.strip().split(":")
    return int(hh) + int(mm) / 60.0


def schedule_hours(cfg) -> float:
    """营业时段的小时数；跨午夜自动加 24。"""
    if not cfg.schedule.enabled:
        return 24.0
    a = parse_hhmm(cfg.schedule.start)
    b = parse_hhmm(cfg.schedule.end)
    return (b - a) if b >= a else (b + 24.0 - a)


def archive_root(cfg) -> str:
    """归档根目录（用于报告可用空间）。只做解析，不建目录、不发网络请求。"""
    try:
        from nvrcore.deviceinfo import load_cache
        from nvrcore.config import is_auto
        cache = load_cache(cfg.device_cache_file)
        if cache and cache.machine_name and is_auto(cfg.archive.store_name):
            cfg.archive.store_name = cache.machine_name
    except Exception:                      # noqa: BLE001
        pass
    try:
        from nvrcore.config import pick_storage_root
        return pick_storage_root(cfg)
    except Exception:                      # noqa: BLE001
        return ""


def plan(channels: int, hours: float, bitrate_kbps: float, segment_minutes: int,
         keep_days: int, cap: int, parallel_cfg: int = 1) -> dict:
    """核心测算，返回结构化结果（便于测试与 --json）。"""
    footage_h_per_ch = hours
    footage_h_total = channels * hours

    bytes_per_sec = bitrate_kbps * 1000.0 / 8.0
    daily_bytes_per_ch = footage_h_per_ch * 3600.0 * bytes_per_sec
    daily_bytes_total = footage_h_total * 3600.0 * bytes_per_sec
    retention_bytes = daily_bytes_total * keep_days
    seg_per_day_per_ch = (hours * 60.0) / max(1, segment_minutes)
    seg_count_total = seg_per_day_per_ch * channels

    streams_per_ch = max(1, min(parallel_cfg, cap // channels)) if channels <= cap else 1
    sessions_wanted = channels * streams_per_ch
    effective = min(cap, sessions_wanted)
    wall_hours = footage_h_total / effective if effective else float("inf")

    net_mbps = effective * bitrate_kbps / 1000.0
    net_pct = net_mbps / DEVICE_FORWARD_MBPS[0] * 100.0

    return {
        "channels": channels,
        "hours": hours,
        "bitrate_kbps": bitrate_kbps,
        "segment_minutes": segment_minutes,
        "keep_days": keep_days,
        "cap": cap,
        "parallel_cfg": parallel_cfg,
        "footage_h_total": footage_h_total,
        "daily_bytes_per_ch": daily_bytes_per_ch,
        "daily_bytes_total": daily_bytes_total,
        "retention_bytes": retention_bytes,
        "seg_count_total": seg_count_total,
        "streams_per_ch": streams_per_ch,
        "sessions_wanted": sessions_wanted,
        "effective_concurrency": effective,
        "wall_hours": wall_hours,
        "net_mbps": net_mbps,
        "net_pct": net_pct,
        "fits_day": wall_hours <= 24.0,
        "margin_pct": (24.0 - wall_hours) / 24.0 * 100.0,
        "no_headroom": channels >= cap,
    }


def render(r: dict, free: int, root: str, cfg) -> str:
    L = []
    bar = "=" * 78
    L.append(bar)
    L.append("门店容量测算（按摄像头数量）")
    L.append(bar)
    L.append("测算参数")
    L.append("  摄像头数     : %d 路" % r["channels"])
    L.append("  每日营业时段 : %s ~ %s（%.1f 小时）"
             % (cfg.schedule.start, cfg.schedule.end, r["hours"]))
    L.append("  单路码率     : %d kbps（实测值；主码流配置上限另计）" % r["bitrate_kbps"])
    L.append("  分段时长     : %d 分钟（每天每通道 %.0f 段）"
             % (r["segment_minutes"], r["seg_count_total"] / max(1, r["channels"])))
    L.append("  留存天数     : %d 天" % r["keep_days"])
    L.append("  设备并发上限 : %d 路（本机型实测；换机型须重新实测）" % r["cap"])
    L.append("")

    L.append("一、素材总量")
    L.append("  单通道每日   : %.1f 小时" % r["hours"])
    L.append("  全店每日     : %.1f 小时（%.0f 段）"
             % (r["footage_h_total"], r["seg_count_total"]))
    L.append("")

    L.append("二、存储账")
    L.append("  单通道每日   : %s" % human_size(r["daily_bytes_per_ch"]))
    L.append("  全店每日     : %s" % human_size(r["daily_bytes_total"]))
    L.append("  留存 %-2d 天占用: %s" % (r["keep_days"], human_size(r["retention_bytes"])))
    if free > 0:
        L.append("  归档盘可用   : %s%s" % (human_size(free), ("  (%s)" % root) if root else ""))
        if r["retention_bytes"] <= free:
            L.append("  >> 存储：够用（占可用空间 %.0f%%）"
                     % (r["retention_bytes"] / free * 100.0))
        else:
            L.append("  >> 存储：**不够**，缺口 %s"
                     % human_size(r["retention_bytes"] - free))
            need = r["retention_bytes"] - free
            days = free / r["daily_bytes_total"] if r["daily_bytes_total"] else 0
            L.append("     按当前速度，这块盘只放得下约 %.1f 天的全店素材，"
                     "而留存策略要求保留 %d 天。" % (days, r["keep_days"]))
            L.append("     可选：①加/换归档盘  ②降低留存天数  ③只归档部分通道或时段"
                     "  ④提高码率前先扩盘")
    L.append("")

    L.append("三、时间账（回放恒 1 倍速，并发是唯一提速手段）")
    L.append("  总素材       : %.1f 小时" % r["footage_h_total"])
    L.append("  每通道分到   : %d 条流（config 设 %d 条，设备额度 %d 路 ÷ %d 通道取小）"
             % (r["streams_per_ch"], r["parallel_cfg"], r["cap"], r["channels"]))
    L.append("  有效并发     : %d 路" % r["effective_concurrency"])
    L.append("  单批耗时     : 约 %.1f 小时" % r["wall_hours"])
    if r["no_headroom"]:
        L.append("  >> 注意：通道数已吃满设备额度（%d 路），每通道只能分到 1 条流 ——"
                 % r["cap"])
        L.append("     想给某个通道多开一条流也开不出来。")
    if r["fits_day"]:
        L.append("  >> 时间：可行（一天 24 小时，余量 %.0f%%）" % r["margin_pct"])
    else:
        L.append("  >> 时间：**跑不完**，单批 %.1f 小时 > 24 小时，"
                 "逐日积压会越来越多。" % r["wall_hours"])
    L.append("")

    L.append("四、网络带宽")
    L.append("  %d 路并发合计 : %.1f Mbps" % (r["effective_concurrency"], r["net_mbps"]))
    L.append("  设备转发能力 : %d~%d Mbps（DH-NVR4216-HDS2 规格）"
             % DEVICE_FORWARD_MBPS)
    L.append("  >> 带宽占用约 %.0f%%，**不是瓶颈**；瓶颈在并发条数与存储。"
             % r["net_pct"])
    L.append("     但若把码率提到 2~4 Mbps（为 AI 稽核提画质），"
             "%d 路就要 %.0f~%.0f Mbps，" % (r["effective_concurrency"],
                                             r["effective_concurrency"] * 2.0,
                                             r["effective_concurrency"] * 4.0))
    L.append("     届时必须重新核算带宽与存储。")
    L.append("")

    L.append("五、规模对照（同样时段 %.0f 小时、同样码率、同样并发设置）" % r["hours"])
    L.append("  通道数   素材/天    全店/天      留存 %.0f 天     单批耗时"
             % r["keep_days"])
    for ch in (1, 2, 4, 6, 8, 12, 15, 16, 20):
        p = plan(ch, r["hours"], r["bitrate_kbps"], r["segment_minutes"],
                 r["keep_days"], r["cap"], r["parallel_cfg"])
        L.append("  %-8d %-10s %-12s %-14s %.1f 小时%s"
                 % (ch,
                    "%.0f 小时" % p["footage_h_total"],
                    human_size(p["daily_bytes_total"]),
                    human_size(p["retention_bytes"]),
                    p["wall_hours"],
                    "   <== 本店" if ch == r["channels"] else
                    ("   (超出设备额度)" if ch > r["cap"] else "")))
    L.append("")
    L.append("六、部署前必须在现场实测的三件事（本工具用的是本机型实测值，别照搬）")
    L.append("  1) 设备并发上限。默认 %d 是 DH-NVR4216-HDS2 的实测值，换型号/换固件"
             % r["cap"])
    L.append("     都要重测。方法：阶梯法逐条加回放会话。")
    L.append("  2) 并发能否长时间保持。实测只验证了保持 15 秒不掉线，"
             "连续跑满一个营业日（%.0f 小时）尚未验证。" % r["hours"])
    L.append("  3) 门店实时预览是否占用同一额度。预览流与回放流是否共用会话池未验证。")
    if not r["no_headroom"]:
        L.append("  附：本店通道数 %d < 额度 %d，仍有余量，可适当多开几条流提速。"
                 % (r["channels"], r["cap"]))
    else:
        L.append("  附：本店通道数 %d 已达额度，不存在「多开几条流提速」的空间，"
                 % r["channels"])
        L.append("     只能靠减少通道数或缩短归档时段来减压。")
    L.append("")
    L.append(bar)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="按门店摄像头数量测算归档可行性与资源需求")
    ap.add_argument("--channels", type=int, default=1,
                    help="门店摄像头数（通道数），默认 1")
    ap.add_argument("--hours", type=float, default=None,
                    help="每日归档时段的小时数；默认取 config.json 的 schedule")
    ap.add_argument("--bitrate", type=float, default=DEFAULT_BITRATE_KBPS,
                    help="单路码率 kbps，默认 %d（实测值）" % DEFAULT_BITRATE_KBPS)
    ap.add_argument("--segment-minutes", type=int, default=None,
                    help="分段时长，默认取 config.json 的 archive.segment_minutes")
    ap.add_argument("--keep-days", type=int, default=None,
                    help="留存天数，默认取 config.json 的 retention.keep_days")
    ap.add_argument("--cap", type=int, default=DEFAULT_SESSION_CAP,
                    help="设备并发回放流上限，默认 %d（本机型实测）"
                         % DEFAULT_SESSION_CAP)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(None, base_dir=base)
    hours = args.hours if args.hours is not None else schedule_hours(cfg)
    seg = args.segment_minutes if args.segment_minutes is not None \
        else cfg.archive.segment_minutes
    keep = args.keep_days if args.keep_days is not None else cfg.retention.keep_days

    if args.channels < 1:
        print("--channels 必须 >= 1")
        return 2
    if args.cap < 1:
        print("--cap 必须 >= 1")
        return 2

    r = plan(args.channels, hours, args.bitrate, seg, keep, args.cap,
             parallel_cfg=cfg.runtime.parallel_streams)
    root = archive_root(cfg)
    free = free_bytes(root) if root else 0
    if not free and root:
        drive = os.path.splitdrive(root)[0] + os.sep
        free = free_bytes(drive)

    if args.json:
        out = dict(r)
        out["free_bytes"] = free
        out["archive_root"] = root
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(r, free, root, cfg))

    ok = r["fits_day"] and (free <= 0 or r["retention_bytes"] <= free)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
