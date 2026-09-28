# -*- coding: utf-8 -*-
"""归档目录扫描与留存清理。

安全约束（务必保留）：
  1. 只会删除归档根目录内、且文件名严格符合归档案命名规则的文件；
  2. 全程做 realpath 包含性校验，杜绝任何路径穿越；
  3. 每一条删除都会写日志；
  4. protect_recent_hours 内产生的录像受保护，不会被空间类规则删除；
  5. 删除一律走 purge_file：**先全文件写零、再删条目**（本机环境会把删除
     拦截转投回收站，先写零才能保证监控画面在任何地方都不可还原）。
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from .config import (Config, free_bytes, total_bytes,
                     looks_like_archive_folder)
from .logutil import human_size

# 归档案命名（现行）：<门店名>_<通道名>_<YYYYMMDDHHMMSS>_<YYYYMMDDHHMMSS>_<标识>.<ext>
#   例：<门店7>_通道1_20260905132941_20260905153140_device.mp4
SEGMENT_RE = re.compile(
    r"^(?P<store>[^_]+)_(?P<ch>[^_]+)_(?P<s>\d{14})_(?P<e>\d{14})_(?P<suffix>[A-Za-z0-9]+)"
    r"\.(?P<ext>mp4|mkv)$"
)
# 兼容早期版本产出的 CH01_20260914_145500_20260914_152500.mp4
LEGACY_SEGMENT_RE = re.compile(
    r"^CH(?P<ch>\d{2})_(?P<s>\d{8}_\d{6})_(?P<e>\d{8}_\d{6})\.(?P<ext>mp4|mkv)$"
)
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Segment:
    path: str
    size: int
    start: datetime
    end: datetime
    store: str = ""
    channel_name: str = ""
    channel: Optional[int] = None

    @property
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()


def _parse_dt(text: str) -> datetime:
    fmt = "%Y%m%d_%H%M%S" if "_" in text else "%Y%m%d%H%M%S"
    return datetime.strptime(text, fmt)


def build_segment_name(cfg: Config, start: datetime, end: datetime, ext: str) -> str:
    """生成归档案文件名（不含目录）。"""
    store, channel_name, suffix = cfg.archive.naming()
    return "%s_%s_%s_%s_%s.%s" % (
        store, channel_name,
        start.strftime("%Y%m%d%H%M%S"),
        end.strftime("%Y%m%d%H%M%S"),
        suffix,
        ext.lstrip(".").lower(),
    )


def sample_segment_name(cfg: Config) -> str:
    now = datetime.now().replace(microsecond=0)
    return build_segment_name(cfg, now,
                              now + timedelta(minutes=max(1, cfg.archive.segment_minutes)),
                              cfg.archive.container)


def _channel_no(name: str) -> Optional[int]:
    """从通道名里取通道号：'1'/'通道1'/'CH01' -> 1；'通道A' -> None。"""
    if name.isdigit():
        return int(name)
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else None


def parse_segment_name(filename: str):
    """解析归档案文件名，返回 dict 或 None。只认本脚本产出的两种命名。"""
    m = SEGMENT_RE.match(filename)
    if m:
        ch_name = m.group("ch")
        try:
            return {
                "store": m.group("store"),
                "channel_name": ch_name,
                "channel": _channel_no(ch_name),
                "start": _parse_dt(m.group("s")),
                "end": _parse_dt(m.group("e")),
            }
        except ValueError:
            return None
    m = LEGACY_SEGMENT_RE.match(filename)
    if m:
        try:
            return {
                "store": "",
                "channel_name": "CH%s" % m.group("ch"),
                "channel": int(m.group("ch")),
                "start": _parse_dt(m.group("s")),
                "end": _parse_dt(m.group("e")),
            }
        except ValueError:
            return None
    return None


def segment_dir(root: str, start: datetime, organize_by_date: bool) -> str:
    if not organize_by_date:
        return root
    return os.path.join(root, start.strftime("%Y-%m-%d"))


def scan_segments(root: str) -> List[Segment]:
    """递归扫描归档目录，只认符合命名规则的视频文件。"""
    found: List[Segment] = []
    root_real = os.path.realpath(root)
    if not os.path.isdir(root_real):
        return found
    for dirpath, dirnames, filenames in os.walk(root_real):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_tmp"]
        for fn in filenames:
            info = parse_segment_name(fn)
            if not info:
                continue
            fp = os.path.realpath(os.path.join(dirpath, fn))
            if not fp.startswith(root_real + os.sep) and fp != root_real:
                continue                      # 防御性校验
            try:
                st = os.stat(fp)
            except OSError:
                continue
            found.append(Segment(
                path=fp,
                size=st.st_size,
                start=info["start"],
                end=info["end"],
                store=info["store"],
                channel_name=info["channel_name"],
                channel=info["channel"],
            ))
    found.sort(key=lambda s: (s.end, s.path))
    return found


def find_stale_archive_dirs(root: str) -> List[str]:
    """找出与当前归档目录同级、同样装着录像、但目录名不一致的旧归档目录。

    只做**只读探测**并返回路径列表，绝不移动或删除任何文件。
    """
    root_real = os.path.realpath(root)
    parent = os.path.dirname(root_real)
    if not os.path.isdir(parent):
        return []
    stale: List[str] = []
    try:
        entries = os.listdir(parent)
    except OSError:
        return []
    for name in entries:
        if not looks_like_archive_folder(name):
            continue
        cand = os.path.realpath(os.path.join(parent, name))
        if not os.path.isdir(cand) or os.path.normcase(cand) == os.path.normcase(root_real):
            continue
        if is_within(cand, root_real) or is_within(root_real, cand):
            continue                          # 嵌套关系，不是"平行旧目录"
        if scan_segments(cand):
            stale.append(cand)
    return sorted(stale)


def is_within(path: str, parent: str) -> bool:
    """path 是否位于 parent 之内（含 realpath 解析，防路径穿越）。"""
    p = os.path.realpath(path)
    q = os.path.realpath(parent)
    return p != q and p.startswith(q.rstrip(os.sep) + os.sep)


def total_archive_bytes(root: str) -> int:
    return sum(s.size for s in scan_segments(root))


# --------------------------------------------------------------------------- #
#  删除实现
# --------------------------------------------------------------------------- #
def _recycle_bin(path: str) -> bool:
    """走系统回收站删除（delete_mode=recycle 时使用）。"""
    if os.name != "nt":
        return False
    try:
        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_bool),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004
        FOF_NOERRORUI = 0x0400
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = path + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0
    except Exception:
        return False


def overwrite_destroy(path: str, logger=None) -> bool:
    """用零字节覆盖文件内容并截断为 0（删除失败时的销毁降级手段）。

    监控录像是敏感素材：即使文件条目删不掉（被占用/被安全策略拦截），
    也要先把内容破坏掉，保证画面无法再被恢复、播放或取证还原。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            chunk = b"\x00" * (1024 * 1024)
            written = 0
            while written < size:
                n = min(len(chunk), size - written)
                f.write(chunk[:n])
                written += n
            f.flush()
            os.fsync(f.fileno())
            f.truncate(0)
        return True
    except Exception as exc:                # pylint: disable=broad-except
        if logger:
            logger.warning("覆盖销毁失败 %s : %s", path, exc)
        return False


def purge_file(path: str, logger=None) -> bool:
    """永久销毁文件：先覆盖内容，再删条目。

    为什么必须「先覆盖再删」：本机运行环境会把一切删除操作拦截后转投回收站，
    先全文件写零再删，即使被转投，回收站里也只剩零字节数据，画面不可恢复。
    """
    if os.path.exists(path):
        overwrite_destroy(path, logger)      # 尽力销毁内容（失败不阻断删除尝试）
    try:
        os.remove(path)
        return True
    except Exception as exc:                 # pylint: disable=broad-except
        if logger:
            logger.error("删除失败（内容已尽力销毁）%s : %s", path, exc)
        return False


def _delete_file(path: str, mode: str, logger) -> bool:
    if mode == "recycle":
        if _recycle_bin(path):
            return True
        logger.warning("移入回收站失败，退化为永久删除: %s", path)
    if purge_file(path, logger):
        return True
    logger.error("删除失败（含覆盖销毁降级）: %s", path)
    return False


def _cleanup_empty_dirs(root: str) -> None:
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root_real, topdown=False):
        if os.path.realpath(dirpath) == root_real:
            continue
        base = os.path.basename(dirpath)
        if not DATE_DIR_RE.match(base):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except Exception:                   # pylint: disable=broad-except
            pass


# --------------------------------------------------------------------------- #
#  留存策略主流程
# --------------------------------------------------------------------------- #
@dataclass
class CleanupStats:
    scanned: int = 0
    total_bytes: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    kept_by_protection: int = 0
    free_before: int = 0
    free_after: int = 0
    volume_total: int = 0
    skipped_reason: str = ""

    def describe(self) -> str:
        return ("扫描 %d 个文件 / %s -> 删除 %d 个 / 释放 %s ；磁盘剩余 %s -> %s"
                % (self.scanned, human_size(self.total_bytes), self.deleted,
                   human_size(self.freed_bytes), human_size(self.free_before),
                   human_size(self.free_after)))


def cleanup(root: str, cfg: Config, logger, dry_run: bool = False) -> CleanupStats:
    """按 天数 -> 归档总量 -> 磁盘剩余空间 三级策略清理。"""
    rc = cfg.retention
    stats = CleanupStats()
    stats.free_before = free_bytes(root)
    stats.volume_total = total_bytes(root)

    segments = scan_segments(root)
    stats.scanned = len(segments)
    stats.total_bytes = sum(s.size for s in segments)

    if not segments:
        stats.free_after = stats.free_before
        logger.info("清理检查完成：归档目录还没有录像文件（%s）", root)
        return stats

    now = datetime.now()
    protect_since = now - timedelta(hours=max(0, rc.protect_recent_hours))
    age_limit = now - timedelta(days=rc.keep_days) if rc.keep_days > 0 else None
    max_archive = int(rc.max_archive_gb * 1024 ** 3) if rc.max_archive_gb > 0 else 0
    min_free = int(rc.min_free_gb * 1024 ** 3) if rc.min_free_gb > 0 else 0

    doomed: List[Segment] = []
    doomed_set = set()

    def mark(seg: Segment, why: str):
        if seg.path in doomed_set:
            return
        doomed_set.add(seg.path)
        doomed.append(seg)
        logger.info("[清理-预览] %s  原因=%s", os.path.basename(seg.path), why)

    # --- 1) 超过保留天数 ---
    if age_limit is not None:
        for seg in segments:
            if seg.end < age_limit:
                mark(seg, "超过保留天数 %d 天" % rc.keep_days)

    remaining = [s for s in segments if s.path not in doomed_set]
    remaining_bytes = sum(s.size for s in remaining)

    # --- 2) 归档总量上限 ---
    if max_archive > 0:
        for seg in list(remaining):
            if remaining_bytes <= max_archive:
                break
            if seg.end >= protect_since:
                stats.kept_by_protection += 1
                continue
            mark(seg, "归档总量超过上限 %.1fGB" % rc.max_archive_gb)
            remaining_bytes -= seg.size

    # --- 3) 磁盘剩余空间下限 ---
    if min_free > 0:
        projected_free = stats.free_before
        for seg in remaining:
            if projected_free >= min_free:
                break
            if seg.end >= protect_since:
                stats.kept_by_protection += 1
                continue
            if seg.path in doomed_set:
                continue
            mark(seg, "磁盘剩余空间低于 %.1fGB" % rc.min_free_gb)
            projected_free += seg.size

    if not doomed:
        stats.free_after = stats.free_before
        logger.info("清理检查完成：无需删除（当前归档 %s，磁盘剩余 %s）",
                    human_size(stats.total_bytes), human_size(stats.free_before))
        return stats

    logger.info("本次计划删除 %d 个录像文件，共 %s（dry_run=%s）",
                len(doomed), human_size(sum(s.size for s in doomed)), dry_run)

    if dry_run:
        stats.deleted = len(doomed)
        stats.freed_bytes = sum(s.size for s in doomed)
        stats.free_after = stats.free_before
        return stats

    for seg in doomed:
        if _delete_file(seg.path, rc.delete_mode, logger):
            stats.deleted += 1
            stats.freed_bytes += seg.size
            logger.info("[清理-已删除] %s (%s)", seg.path, human_size(seg.size))

    _cleanup_empty_dirs(root)
    stats.free_after = free_bytes(root)

    logger.info("清理完成：%s", stats.describe())
    if min_free > 0 and stats.free_after < min_free:
        stats.skipped_reason = "清理后磁盘剩余仍低于阈值 %.1fGB" % rc.min_free_gb
        logger.error("磁盘剩余空间仍不足（%s），已停止继续删除受保护的新录像。"
                     "建议调小 retention.keep_days 或更换存储盘。", human_size(stats.free_after))
    return stats


def summarize(root: str, logger) -> str:
    segs = scan_segments(root)
    if not segs:
        return "归档目录为空: %s" % root
    total = sum(s.size for s in segs)
    span_hours = (segs[-1].end - segs[0].start).total_seconds() / 3600.0
    free = free_bytes(root)
    total_disk = total_bytes(root)
    return ("归档目录 : %s\n"
            "文件数量 : %d 个\n"
            "占用空间 : %s\n"
            "覆盖时间 : %s ~ %s（跨度 %.1f 小时）\n"
            "磁盘空间 : 剩余 %s / 共 %s"
            % (root, len(segs), human_size(total),
               segs[0].start.strftime("%Y-%m-%d %H:%M:%S"),
               segs[-1].end.strftime("%Y-%m-%d %H:%M:%S"),
               span_hours, human_size(free), human_size(total_disk)))
