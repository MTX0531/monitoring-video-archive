# -*- coding: utf-8 -*-
"""补拉（重拉）指定的单个归档分片 —— 定点修复本地损坏/不完整的段落。

背景：某一段落片在录像机上确实有录像，但本地文件损坏或明显不完整。
此时不必用 `--day D --redo` 把整天几十段全部重拉一遍，
本工具只重拉调用者指定的那一个时间窗，而且**不读也不写 state 游标**，
所以可以在任何时候安全执行，不影响归档进度。

用法:
    # 体检：扫描全部归档，列出有问题的段落（只读，不改任何文件）
    python tools/repair_segment.py --scan

    # 定点修复：探测设备确有录像 -> 删旧文件 -> 重拉 -> 复验 -> 更新索引
    python tools/repair_segment.py "2026-09-15 10:00:00" "2026-09-15 10:30:00"

    # 强制重拉（即使现有文件看起来正常）
    python tools/repair_segment.py "..." "..." --force

退出码: 0=成功或无需修复  1=修复失败  2=参数/环境错误  4=已有实例在跑
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from nvrcore.config import load_config, pick_storage_root, ensure_dirs      # noqa: E402
from nvrcore.downloader import (find_ffmpeg, probe_range, probe_media,      # noqa: E402
                                RTSP_NO_RECORD)
from nvrcore.logutil import setup_logger, human_size, human_duration        # noqa: E402
from nvrcore.lockfile import SingleInstance, LockBusy, default_lock_path    # noqa: E402
from nvrcore import storage                                                 # noqa: E402

TIME_FMT = "%Y-%m-%d %H:%M:%S"
SHORT_RATIO = 0.5

DUR_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


# --------------------------------------------------------------------------- #
def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FMT)


def media_duration(ffmpeg: str, path: str, timeout: float = 120.0):
    """读容器声明的总时长（秒）。读不到返回 None（不代表文件损坏）。"""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stderr or b"").decode("utf-8", "replace")
    m = DUR_RE.search(text)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def assess(ffmpeg: str, path: str, expected_seconds: float, deep: bool = True):
    """判断一个分片是否需要重拉。返回 (need_repair, note, size, duration)。"""
    if not os.path.isfile(path):
        return True, "文件不存在", 0, None

    size = os.path.getsize(path)
    ok, detail = probe_media(ffmpeg, path, deep=deep)
    if not ok:
        return True, "无法解码播放：%s" % (detail or "未知原因"), size, None

    dur = media_duration(ffmpeg, path)
    if dur is not None and expected_seconds > 0 and dur < expected_seconds * SHORT_RATIO:
        return (True,
                "时长明显偏短：%s / 申请 %s（不足 %.0f%%）"
                % (human_duration(dur), human_duration(expected_seconds),
                   SHORT_RATIO * 100),
                size, dur)
    return False, "正常", size, dur


def segment_path(cfg, root: str, start: datetime, end: datetime) -> str:
    out_dir = storage.segment_dir(root, start, cfg.archive.organize_by_date)
    fname = storage.build_segment_name(cfg, start, end, cfg.archive.container)
    return os.path.join(out_dir, fname)


def index_record(cfg, path: str, start: datetime, end: datetime,
                 size: int, duration) -> dict:
    return {
        "file": path,
        "store_name": cfg.archive.store_name,
        "channel_name": cfg.archive.channel_name,
        "channel": cfg.nvr.channel,
        "stream": cfg.nvr.stream,
        "requested_start": start.strftime(TIME_FMT),
        "requested_end": end.strftime(TIME_FMT),
        "actual_duration": round(duration, 2) if duration else None,
        "size": size,
        "saved_at": datetime.now().strftime(TIME_FMT),
        "repaired": True,
    }


def _same_path(a: str, b: str) -> bool:
    """跨平台路径比较。"""
    na = os.path.normcase(os.path.normpath(os.path.abspath(a)))
    nb = os.path.normcase(os.path.normpath(os.path.abspath(b)))
    return na == nb


def replace_index_entry(cfg, path: str, record: dict):
    """把索引里该文件的旧记录换成新记录。整写用临时文件 + os.replace。"""
    index_file = cfg.index_file
    lines = []
    if os.path.isfile(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as exc:
            print("  [警告] 读取索引失败（%s），本次不改索引" % exc)
            return
    kept = []
    dropped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            old = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(old, dict) and old.get("file") and _same_path(old["file"], path):
            dropped += 1
            continue
        kept.append(line)
    kept.append(json.dumps(record, ensure_ascii=False))
    try:
        os.makedirs(os.path.dirname(index_file), exist_ok=True)
        tmp = index_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        os.replace(tmp, index_file)
        print("  [索引] 已替换旧记录 %d 条，并写入新记录" % dropped)
    except OSError as exc:
        print("  [警告] 索引写入失败：%s" % exc)


# --------------------------------------------------------------------------- #
def do_scan(cfg, ffmpeg: str, root: str) -> int:
    segs = storage.scan_segments(root)
    print("=" * 78)
    print("归档体检%s" % ("（快速抽查：每文件只解前 5 秒）"))
    print("=" * 78)
    print("归档目录：%s" % root)
    if not segs:
        print("目录里还没有符合命名规则的录像")
        return 0

    bad = []
    for seg in sorted(segs, key=lambda s: s.start):
        expected = (seg.end - seg.start).total_seconds()
        need, note, size, dur = assess(ffmpeg, seg.path, expected, deep=False)
        if need:
            bad.append((seg, note, size))
            print("[需修复] %s" % os.path.basename(seg.path))
            print("         %s" % note)
        else:
            print("[正常]   %s" % os.path.basename(seg.path))

    print("-" * 78)
    print("共 %d 个文件，%d 个需要修复" % (len(segs), len(bad)))
    if bad:
        print()
        print("修复命令（逐条执行）：")
        for seg, _note, _size in bad:
            print('  python tools/repair_segment.py "%s" "%s"'
                  % (seg.start.strftime(TIME_FMT), seg.end.strftime(TIME_FMT)))
    print("=" * 78)
    return 0


def do_repair(cfg, ffmpeg: str, root: str, start: datetime, end: datetime,
              force: bool, update_index: bool) -> int:
    if end <= start:
        print("[失败] 结束时间必须晚于开始时间")
        return 2
    expected = (end - start).total_seconds()
    path = segment_path(cfg, root, start, end)
    name = os.path.basename(path)

    print("=" * 78)
    print("定点补拉 %s ~ %s（申请 %s）" % (start.strftime(TIME_FMT),
                                           end.strftime(TIME_FMT),
                                           human_duration(expected)))
    print("=" * 78)
    print("目标文件：%s" % path)

    existed = os.path.isfile(path)
    need, note, size, dur = assess(ffmpeg, path, expected)
    if existed:
        print("现状    ：%s（%s）" % (note, human_size(size)))
    if not need and not force:
        print()
        print("该段无需修复。确实要重拉请加 --force")
        return 0

    print("探测设备：", end="", flush=True)
    code = probe_range(cfg.nvr, start, end)
    if code == RTSP_NO_RECORD:
        mid = start + (end - start) / 2
        if probe_range(cfg.nvr, mid, end) != 200:
            print("无录像")
            print()
            print("设备上该时段没有录像，补拉没有意义。")
            return 1
    elif code is None:
        print("探测失败（设备无响应或认证异常）")
        return 2
    else:
        print("有录像，可以补拉")

    if existed:
        if storage.purge_file(path):
            print("已删除  ：旧文件 %s（%s）" % (name, human_size(size)))
        else:
            print("[失败] 删除旧文件失败（已尝试覆盖销毁降级）：%s" % path)
            return 1

    from nvr_puller import pull_window                       # 延迟导入，避免循环依赖
    print("开始重拉…")
    outcome = pull_window(cfg, ffmpeg, start, end, root, logger=cfg._logger,
                          label="修复")
    if outcome is None:
        print()
        print("[失败] 多次重试仍未成功，该段目前缺文件。")
        print("       归档进度（游标）没有被改动，随时可以重跑本命令。")
        return 1
    if outcome.status != "ok":
        print()
        print("[失败] 拉取未成功：%s" % outcome.detail)
        return 1

    new_size = os.path.getsize(outcome.path)
    ok, detail = probe_media(ffmpeg, outcome.path)
    new_dur = media_duration(ffmpeg, outcome.path)
    print()
    print("新文件  ：%s（%s / 实际时长 %s）"
          % (os.path.basename(outcome.path), human_size(new_size),
             human_duration(outcome.duration)))
    if not ok:
        print("[失败] 新文件仍无法解码播放：%s" % detail)
        return 1
    if new_dur is not None and new_dur < expected * SHORT_RATIO:
        print("[注意] 新文件时长仍偏短（%s / 申请 %s）——"
              "很可能是设备上该时段录像本身就有断点。"
              % (human_duration(new_dur), human_duration(expected)))

    if update_index:
        replace_index_entry(cfg, outcome.path,
                            index_record(cfg, outcome.path, start, end,
                                         new_size, new_dur))

    print()
    print("修复完成：%s" % os.path.basename(outcome.path))
    print("=" * 78)
    return 0


# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="repair_segment.py",
        description="补拉（重拉）指定的单个归档分片",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("start", nargs="?", help='开始时间 "YYYY-MM-DD HH:MM:SS"')
    p.add_argument("end", nargs="?", help='结束时间 "YYYY-MM-DD HH:MM:SS"')
    p.add_argument("--scan", action="store_true",
                   help="只体检全部归档，列出需要修复的段落（只读）")
    p.add_argument("--force", action="store_true",
                   help="即使现有文件看起来正常也重拉")
    p.add_argument("--no-index", dest="no_index", action="store_true",
                   help="不改 state/index.jsonl")
    p.add_argument("--ignore-lock", dest="ignore_lock", action="store_true",
                   help="无视单实例锁强行执行")
    p.add_argument("--verbose", action="store_true", help="输出调试日志")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    start = end = None
    if not args.scan:
        if not args.start or not args.end:
            print(__doc__)
            return 2
        try:
            start = parse_time(args.start)
            end = parse_time(args.end)
        except ValueError:
            print('[失败] 时间格式应为 "YYYY-MM-DD HH:MM:SS"')
            return 2
        if end <= start:
            print("[失败] 结束时间必须晚于开始时间")
            return 2

    base_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(base_dir)
    cfg = load_config(None, base_dir=base_dir)
    logger = setup_logger(cfg.log_dir, cfg.runtime.log_keep_days,
                          level=10 if args.verbose else 20, console=False)
    cfg._logger = logger
    ffmpeg = find_ffmpeg(cfg)

    if cfg.names_need_device():
        from nvr_puller import resolve_names_into_config     # 延迟导入
        resolve_names_into_config(cfg, logger)
    root = ensure_dirs(pick_storage_root(cfg))

    if args.scan:
        return do_scan(cfg, ffmpeg, root)

    guard = SingleInstance(default_lock_path(cfg.state_file))
    try:
        guard.acquire(mode="定点补拉", ignore=args.ignore_lock, logger=logger)
        return do_repair(cfg, ffmpeg, root, start, end,
                         force=args.force, update_index=not args.no_index)
    except LockBusy as exc:
        print("[拒绝] %s" % exc)
        print("       已有归档实例在跑，此时补拉会和它抢录像机的并发路数。")
        print("       等它跑完再执行，或确实要并行才加 --ignore-lock。")
        return 4
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
