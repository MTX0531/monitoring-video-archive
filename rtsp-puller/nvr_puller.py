# -*- coding: utf-8 -*-
"""门店监控录像本地归档脚本（大华 NVR / RTSP 回放）。

用法：
    python nvr_puller.py                 # 常驻运行
    python nvr_puller.py --once          # 只拉一段后退出
    python nvr_puller.py --status        # 查看归档与进度
    python nvr_puller.py --selftest      # 连通性 + 认证 + 试拉自检
    python nvr_puller.py --cleanup       # 只跑一次留存清理
    python nvr_puller.py --cleanup --dry-run   # 预览会删哪些文件（不真删）
    python nvr_puller.py --device-info         # 查看从录像机读到的门店名/通道名
    python nvr_puller.py --accept              # 新门店账密验收（登录/回放/实拉 三道门禁）
    python nvr_puller.py --from "2026-09-15 08:00:00"   # 重设拉取起点
    python nvr_puller.py --day yesterday                # 按日归档：拉取前一天，拉完即退出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nvrcore.config import (Config, load_config, pick_storage_root, ensure_dirs,
                            describe_drives, free_bytes, schedule_times,
                            window_contains, advance_to_window, window_close,
                            windowed_seconds, is_auto)
from nvrcore.acceptance import (run_acceptance, render_report,
                                VERDICT_PASS, VERDICT_CONDITIONAL,
                                VERDICT_RETEST, VERDICT_FAIL)
from nvrcore.deviceinfo import resolve_identity, fetch_identity, identity_report
from nvrcore.downloader import (find_ffmpeg, probe_range, pull_segment, check_connection,
                                run_hook, build_playback_url, PullOutcome, RTSP_NO_RECORD,
                                probe_media, ensure_playable)
from nvrcore.logutil import setup_logger, human_size, human_duration
from nvrcore.state import State
from nvrcore.lockfile import SingleInstance, LockBusy, default_lock_path
from nvrcore import storage

TIME_FMT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="大华 NVR 监控录像本地归档")
    p.add_argument("--config", default=None, help="配置文件路径")
    p.add_argument("--once", action="store_true", help="只拉取一段后退出")
    p.add_argument("--status", action="store_true", help="打印归档与进度状态")
    p.add_argument("--selftest", action="store_true", help="连通性/认证/试拉自检")
    p.add_argument("--cleanup", action="store_true", help="立刻执行一次留存清理")
    p.add_argument("--dry-run", action="store_true", help="配合 --cleanup，只预览不删除")
    p.add_argument("--verify", action="store_true",
                   help="校验归档文件能否被本地播放器正常打开")
    p.add_argument("--fix", action="store_true",
                   help="配合 --verify：对打不开的文件自动修复（重封装/转码）")
    p.add_argument("--from", dest="from_time", default=None, help="重设拉取起点时间")
    p.add_argument("--to", dest="to_time", default=None,
                   help="归档终点时间，到此为止就停止（用于补拉一段历史录像）")
    p.add_argument("--device-info", action="store_true",
                   help="显示从录像机读到的名称信息（门店名/通道名/型号/序列号）")
    p.add_argument("--refresh-device", action="store_true",
                   help="强制重新读取录像机名称（忽略本地缓存）")
    p.add_argument("--channels", action="store_true",
                   help="列出通道清单：设备支持几路、实际接了哪几路摄像头")
    p.add_argument("--probe", action="store_true",
                   help="配合 --channels：对已接入通道做 RTSP 实时流探测（较慢但更准）")
    p.add_argument("--accept", action="store_true",
                   help="新门店账密验收：登录 / 回放 / 实拉 三道门禁，全绿才部署")
    p.add_argument("--day", default=None, metavar="DAY",
                   help="按日归档：拉取 DAY 这一天的录像后退出。"
                        "可填 today / yesterday / 前天 / 2026-09-15。"
                        "『当天拉取前一天』写作 --day yesterday")
    p.add_argument("--redo", action="store_true",
                   help="配合 --day：忽略已有进度，从窗口开头重拉")
    p.add_argument("--ignore-lock", dest="ignore_lock", action="store_true",
                   help="无视单实例锁强行启动（仅用于运维手工介入；"
                        "正常情况发现已有实例在跑时会拒绝启动并以 4 退出）")
    p.add_argument("--no-sweep", dest="no_sweep", action="store_true",
                   help="跳过启动时对 .part_ 遗留分片的清理（交给人工或下一次运行处理）")
    p.add_argument("--parallel", type=int, default=None, metavar="N",
                   help="按日归档时的并发回放路数（覆盖配置里的 parallel_streams）")
    p.add_argument("--reset", action="store_true", help="清空进度状态（不删录像文件）")
    p.add_argument("--verbose", action="store_true", help="输出调试日志")
    return p.parse_args(argv)


def bootstrap(args, resolve_names: bool = True):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(args.config, base_dir=base_dir)
    logger = setup_logger(cfg.log_dir, cfg.runtime.log_keep_days,
                          level=10 if args.verbose else 20)
    if resolve_names and cfg.names_need_device():
        resolve_names_into_config(cfg, logger,
                                  force=bool(getattr(args, "refresh_device", False)))
    root = ensure_dirs(pick_storage_root(cfg))
    warn_archive_dir_issues(cfg, root, logger)
    return cfg, logger, root


def warn_archive_dir_issues(cfg: Config, root: str, logger) -> None:
    """归档目录相关的两项告警（都只读，不动任何文件）。"""
    if getattr(cfg, "name_source", "") == "fallback":
        logger.warning("没能读到录像机名称，本次归档目录暂用兜底名「%s」。",
                       os.path.basename(root))
    try:
        stale = storage.find_stale_archive_dirs(root)
    except OSError:
        return
    for old in stale:
        logger.warning("发现另一个归档目录 %s —— 它不在当前归档目录（%s）内，"
                       "里面的录像不会被留存清理扫到，也不会被上传流程看到。"
                       "确认要合并的话执行：python tools/move_archive_folder.py --from \"%s\"",
                       old, root, old)


NAME_SOURCE_TEXT = {"device": "读自录像机",
                    "cache": "读自录像机缓存（有效期内）",
                    "cache-offline": "设备未响应，使用本地缓存",
                    "fallback": "读取失败，使用兜底值",
                    "config": "配置指定"}


def resolve_names_into_config(cfg: Config, logger, force: bool = False):
    """把录像机的真实名称解析进配置（门店名 = MachineName，通道名 = ChannelTitle）。"""
    ident = resolve_identity(cfg, logger, force_refresh=force)
    cfg.apply_identity(ident)
    cfg.name_source = ident.source
    if ident.source == "device" and ident.is_placeholder_channel(cfg.nvr.channel):
        logger.warning("录像机上第 %d 通道的名称仍是默认值（%s）——建议登录录像机或大华云联"
                       "把它改成实际位置名，这样归档文件名更好辨认。",
                       cfg.nvr.channel, cfg.archive.channel_name)
    return ident


# --------------------------------------------------------------------------- #
def append_index(cfg: Config, record: dict) -> None:
    try:
        os.makedirs(os.path.dirname(cfg.index_file), exist_ok=True)
        with open(cfg.index_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ensure_space(cfg: Config, root: str, logger) -> bool:
    """磁盘空间不足时先清理；仍不足则返回 False（暂停拉取）。"""
    need = int(cfg.retention.min_free_gb * 1024 ** 3)
    if need <= 0:
        return True
    free = free_bytes(root)
    if free >= need:
        return True
    logger.warning("磁盘剩余 %s，低于阈值 %s，先执行清理……",
                   human_size(free), human_size(need))
    storage.cleanup(root, cfg, logger)
    free = free_bytes(root)
    if free >= need:
        logger.info("清理后剩余 %s，继续拉取", human_size(free))
        return True
    logger.error("清理后磁盘剩余仅 %s，仍低于阈值 %s。为避免写满磁盘，暂停拉取，"
                 "请调小 retention.keep_days / 更换存储盘后重启。",
                 human_size(free), human_size(need))
    return False


# --------------------------------------------------------------------------- #
def sweep_part_files(root: str, logger) -> None:
    """清理上次异常退出留下的半成品分片。"""
    cutoff = time.time() - 600
    removed = 0
    freed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.startswith(".part_"):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    sz = os.path.getsize(path)
                    if storage.purge_file(path, logger):
                        freed += sz
                        removed += 1
            except Exception:               # pylint: disable=broad-except
                pass
    if removed:
        logger.info("清理上次遗留的半成品分片：%d 个 / %s", removed, human_size(freed))


def sleep_until(target: datetime, cfg: Config, logger, reason: str) -> None:
    """分段休眠到目标时刻，期间定期输出剩余时间（可确认进程仍在运行）。"""
    while True:
        remain = (target - datetime.now()).total_seconds()
        if remain <= 0:
            return
        logger.info("%s，%s 后继续（目标 %s）", reason,
                    human_duration(remain), target.strftime(TIME_FMT))
        time.sleep(min(remain, max(30.0, cfg.runtime.poll_interval_seconds * 10.0)))


def _parse_saved_time(text: str):
    """解析状态文件里存的时间戳；无法解析时返回 None（当作没有记录）。"""
    try:
        return datetime.strptime(text, TIME_FMT)
    except (TypeError, ValueError):
        return None


def pull_window(cfg: Config, ffmpeg: str, start: datetime, end: datetime,
                root: str, logger, label: str = ""):
    """拉取 [start, end) 一段并落盘。

    与 pull_and_record 的区别：**不碰 state、不写索引**，因此可以多个线程
    同时调用（并发拉取就靠这个）。返回 PullOutcome（status = ok / gap）。
    """
    lead = ("[%s] " % label) if label else "  "
    outcome = None
    for attempt in range(1, cfg.runtime.max_attempts_per_segment + 1):
        probe = probe_range(cfg.nvr, start, end)
        if probe == RTSP_NO_RECORD:
            mid = start + (end - start) / 2
            if probe_range(cfg.nvr, mid, end) != 200:
                logger.info("%s探测结果 404，该时段确实没有录像，跳过", lead)
                outcome = PullOutcome("gap", detail="探测确认无录像")
                break
        elif probe is None:
            logger.warning("%s第 %d 次探测失败（设备无响应或认证异常）", lead, attempt)
            if attempt < cfg.runtime.max_attempts_per_segment:
                time.sleep(cfg.runtime.retry_backoff_seconds)
                continue

        out_dir = storage.segment_dir(root, start, cfg.archive.organize_by_date)
        fname = storage.build_segment_name(cfg, start, end, cfg.archive.container)
        out_path = os.path.join(out_dir, fname)

        def _progress(done: float, total: float, _lead=lead):
            logger.info("%s拉取进度 %s / %s（%.0f%%）", _lead,
                        human_duration(done), human_duration(total),
                        100.0 * done / total if total else 0)

        result = pull_segment(cfg, ffmpeg, start, end, out_path, logger,
                              on_progress=_progress)
        if result.status == "ok":
            outcome = result
            break
        if result.status == "gap":
            logger.info("%s该区间无录像：%s", lead, result.detail)
            outcome = result
            break
        logger.warning("%s第 %d 次拉取失败：%s", lead, attempt, result.detail)
        if attempt < cfg.runtime.max_attempts_per_segment:
            time.sleep(cfg.runtime.retry_backoff_seconds)

    return outcome


def record_outcome(cfg: Config, state, start: datetime, end: datetime,
                   outcome, logger, advance_cursor: bool = True) -> None:
    """把一段拉取结果写进状态与索引。"""
    if outcome.status == "ok":
        state.total_segments += 1
        state.total_bytes += outcome.size
        state.consecutive_errors = 0
        prev = _parse_saved_time(state.last_success_end)
        if prev is None or end > prev:
            state.last_success_end = end.strftime(TIME_FMT)
        logger.info("已保存 %s （%s / %s，实际时长 %s）",
                    os.path.basename(outcome.path), human_size(outcome.size),
                    human_duration(outcome.elapsed), human_duration(outcome.duration))
        append_index(cfg, {
            "file": outcome.path,
            "store_name": cfg.archive.store_name,
            "channel_name": cfg.archive.channel_name,
            "channel": cfg.nvr.channel,
            "stream": cfg.nvr.stream,
            "requested_start": start.strftime(TIME_FMT),
            "requested_end": end.strftime(TIME_FMT),
            "actual_duration": round(outcome.duration, 2),
            "size": outcome.size,
            "saved_at": datetime.now().strftime(TIME_FMT),
        })
        run_hook(cfg.hooks.on_segment_saved, outcome.path, logger)
    else:
        logger.info("跳过无录像区间：%s", outcome.detail)
        state.total_gap_segments += 1
        state.consecutive_errors = 0

    if advance_cursor:
        state.set_next_start(end)
        state.save(cfg.state_file)


def pull_and_record(cfg: Config, ffmpeg: str, state, root: str,
                    start: datetime, end: datetime, logger):
    """顺序模式：拉取 [start, end) 一段并落盘，同时更新进度与索引。"""
    outcome = pull_window(cfg, ffmpeg, start, end, root, logger)
    if outcome is None:
        state.consecutive_errors += 1
        state.last_error = "拉取失败（已达最大重试次数）"
        state.save(cfg.state_file)
        return None
    record_outcome(cfg, state, start, end, outcome, logger, advance_cursor=True)
    return outcome


def run_loop(cfg: Config, logger, root: str, once: bool = False,
             sweep: bool = True) -> int:
    ffmpeg = find_ffmpeg(cfg)
    logger.info("=" * 78)
    logger.info("监控录像本地归档启动")
    logger.info("录像机     : %s:%d  通道 %d  %s码流",
                cfg.nvr.host, cfg.nvr.rtsp_port, cfg.nvr.channel, cfg.nvr.stream)
    logger.info("归档目录   : %s", root)
    logger.info("磁盘情况   : %s", describe_drives())
    logger.info("单文件时长 : %d 分钟", cfg.archive.segment_minutes)
    logger.info("命名规则   : <门店名>_<通道名>_<开始时间>_<结束时间>_%s",
                cfg.archive.name_suffix)
    logger.info("门店名称   : %s（%s）", cfg.archive.store_name,
                NAME_SOURCE_TEXT.get(getattr(cfg, "name_source", "config"), "配置指定"))
    logger.info("通道名称   : %s（%s）", cfg.archive.channel_name,
                NAME_SOURCE_TEXT.get(getattr(cfg, "name_source", "config"), "配置指定"))
    logger.info("命名示例   : %s", storage.sample_segment_name(cfg))
    bound = cfg.resolve_end_time()
    logger.info("归档终点   : %s",
                bound.strftime(TIME_FMT) if bound else "跟随最新录像（持续运行）")

    sched_on = bool(cfg.schedule.enabled)
    ws = we = None
    if sched_on:
        ws, we = schedule_times(cfg)
        logger.info("拉取时段   : 每天 %s ~ %s，其余时间不拉取（模式：%s）",
                    cfg.schedule.start, cfg.schedule.end,
                    "只归档时段内录像" if cfg.schedule.mode == "footage"
                    else "仅暂停拉流，仍会顺序补夜间录像")

    logger.info("留存策略   : 保留 %d 天 / 磁盘至少留 %.1fGB / 最近 %d 小时受保护",
                cfg.retention.keep_days, cfg.retention.min_free_gb,
                cfg.retention.protect_recent_hours)
    logger.info("ffmpeg     : %s", ffmpeg)
    logger.info("=" * 78)

    if sweep:
        sweep_part_files(root, logger)
    else:
        logger.info("已按要求跳过启动清理（--no-sweep）：遗留 .part_ 分片保留在原处")

    state = State.load(cfg.state_file)
    if state.get_next_start() is None:
        state.set_next_start(cfg.resolve_start_time())
        state.started_at = datetime.now().strftime(TIME_FMT)
        logger.info("初始化拉取起点：%s", state.next_start)
    lag_delta = timedelta(seconds=cfg.archive.safety_lag_seconds)
    target_end = bound if bound else (datetime.now() - lag_delta)
    if sched_on and cfg.schedule.mode == "footage":
        need_seconds = windowed_seconds(state.get_next_start(), target_end, ws, we)
        if need_seconds > 0:
            daily = windowed_seconds(datetime.combine(datetime.now().date(), ws),
                                     datetime.combine(datetime.now().date(), we), ws, we)
            logger.info("待补齐的时段内录像约 %s（回放 1 倍速；每天可拉 %s，约需 %.1f 天）",
                        human_duration(need_seconds), human_duration(daily),
                        need_seconds / daily if daily else 0)
    else:
        need_seconds = (target_end - state.get_next_start()).total_seconds()
        if need_seconds > 0:
            logger.info("当前待补齐录像约 %s（回放为 1 倍速，预计需 %s 完成）",
                        human_duration(need_seconds), human_duration(need_seconds))

    next_cleanup_at = 0.0
    segment_delta = timedelta(minutes=cfg.archive.segment_minutes)
    footage_mode = sched_on and cfg.schedule.mode == "footage"
    stop = False

    while not stop:
        if cfg.retention.enabled and time.time() >= next_cleanup_at:
            storage.cleanup(root, cfg, logger)
            next_cleanup_at = time.time() + max(1, cfg.retention.scan_interval_minutes) * 60

        if not ensure_space(cfg, root, logger):
            time.sleep(600)
            next_cleanup_at = time.time() + 60
            continue

        now = datetime.now()
        live_edge = now - lag_delta

        if sched_on and not window_contains(now, ws, we):
            if once:
                logger.info("当前 %s 不在拉取时段 %s~%s，--once 模式不拉取，退出",
                            now.strftime(TIME_FMT), cfg.schedule.start, cfg.schedule.end)
                return 0
            sleep_until(advance_to_window(now, ws, we), cfg, logger,
                        "当前 %s 不在拉取时段 %s~%s，暂停拉取"
                        % (now.strftime(TIME_FMT), cfg.schedule.start, cfg.schedule.end))
            continue

        start = state.get_next_start()

        if footage_mode:
            nxt = advance_to_window(start, ws, we)
            if nxt > start:
                logger.info("起点 %s 不在拉取时段内，跳过该时段、不拉取，游标推进到 %s",
                            start.strftime(TIME_FMT), nxt.strftime(TIME_FMT))
                state.set_next_start(nxt)
                state.save(cfg.state_file)
                continue
            start = nxt

        if bound is not None and start >= bound:
            logger.info("=" * 78)
            logger.info("已到达归档终点 %s，本次归档任务完成", bound.strftime(TIME_FMT))
            logger.info("累计保存 %d 个文件 / %.2f GB，空档跳过 %d 段",
                        state.total_segments, state.total_bytes / 1024 ** 3,
                        state.total_gap_segments)
            logger.info("=" * 78)
            break

        cap = bound
        if footage_mode:
            win_cap = window_close(start, ws, we)
            if win_cap is not None:
                cap = win_cap if cap is None else min(cap, win_cap)
        end = start + segment_delta if cap is None else min(start + segment_delta, cap)

        if end > live_edge:
            if once:
                logger.info("最新录像只到 %s，尚不足一段，--once 模式不拉取，退出",
                            live_edge.strftime(TIME_FMT))
                return 0
            logger.info("最新录像只到 %s，距本段末尾 %s 还差 %s，等录满再拉",
                        live_edge.strftime(TIME_FMT), end.strftime(TIME_FMT),
                        human_duration((end - live_edge).total_seconds()))
            time.sleep(max(10, cfg.runtime.poll_interval_seconds))
            continue

        logger.info("-" * 78)
        logger.info("准备拉取 %s ~ %s（%s）",
                    start.strftime(TIME_FMT), end.strftime(TIME_FMT),
                    human_duration((end - start).total_seconds()))

        outcome = pull_and_record(cfg, ffmpeg, state, root, start, end, logger)

        if outcome is None:
            stale = (datetime.now() - end).total_seconds() > 3600
            if stale:
                logger.error("该区间位于历史时段且多次失败，判定设备上已无此录像，跳过 %s ~ %s",
                             start.strftime(TIME_FMT), end.strftime(TIME_FMT))
                state.set_next_start(end)
                state.total_gap_segments += 1
                state.consecutive_errors = 0
                state.save(cfg.state_file)
            else:
                logger.error("拉取持续失败，%d 秒后重试同一区间", cfg.runtime.poll_interval_seconds)
                time.sleep(max(10, cfg.runtime.poll_interval_seconds))
            if once:
                return 1
            continue

        if once:
            logger.info("--once 模式：本次任务完成")
            return 0

    return 0


# --------------------------------------------------------------------------- #
#  按日归档（当天拉取前一天）
# --------------------------------------------------------------------------- #
DAY_ALIASES = {
    "today": 0, "今天": 0, "now": 0,
    "yesterday": 1, "昨天": 1,
    "前天": 2, "the-day-before-yesterday": 2,
}


def parse_day_arg(raw: str) -> datetime:
    """解析 --day 取值 -> 目标日 00:00。"""
    s = (raw or "").strip()
    low = s.lower()
    if low in DAY_ALIASES:
        d = datetime.now().date() - timedelta(days=DAY_ALIASES[low])
        return datetime.combine(d, datetime.min.time())
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.combine(datetime.strptime(s, fmt).date(),
                                    datetime.min.time())
        except ValueError:
            continue
    raise ValueError("--day 取值无法识别：%r（可用 today / yesterday / 前天 / 2026-09-15）"
                     % raw)


def day_window(cfg: Config, day: datetime):
    """目标日的归档窗口 [起, 止)。"""
    d = day.date()
    if not cfg.schedule.enabled:
        s = datetime.combine(d, datetime.min.time())
        return s, s + timedelta(days=1)
    ws, we = schedule_times(cfg)
    s = datetime.combine(d, ws)
    e = datetime.combine(d, we)
    if e <= s:
        e += timedelta(days=1)
    return s, e


# --------------------------------------------------------------------------- #
#  并发拉取
# --------------------------------------------------------------------------- #
def day_plan_windows(cfg: Config, start: datetime, hard_end: datetime):
    """把 [start, hard_end) 按 segment_minutes 切段，与顺序模式逐段一致。"""
    seg = timedelta(minutes=cfg.archive.segment_minutes)
    out = []
    cur = start
    while cur < hard_end:
        end = min(cur + seg, hard_end)
        out.append((cur, end))
        cur = end
    return out


def segment_path(cfg: Config, root: str, start: datetime, end: datetime) -> str:
    """该窗口归档后的落盘路径（用来判断这一段是不是已经拉过了）。"""
    d = storage.segment_dir(root, start, cfg.archive.organize_by_date)
    return os.path.join(
        d, storage.build_segment_name(cfg, start, end, cfg.archive.container))


def split_batch(cfg: Config, root: str, windows, skip_existing: bool = True):
    """分成 (已完成数, 待拉窗口)。"""
    done, pending = 0, []
    for s, e in windows:
        if skip_existing and os.path.exists(segment_path(cfg, root, s, e)):
            done += 1
        else:
            pending.append((s, e))
    return done, pending


def _is_done(cfg: Config, root: str, window, heights) -> bool:
    st = heights.get(window)
    if st in ("ok", "gap"):
        return True
    s, e = window
    return os.path.exists(segment_path(cfg, root, s, e))


def _resolve_cursor(cfg: Config, root: str, plan, heights, batch_start: datetime):
    """游标 = 从批次起点开始「连续完成」的最后一段末尾。"""
    cur = batch_start
    for w in plan:
        if _is_done(cfg, root, w, heights):
            cur = w[1]
        else:
            break
    return cur


def _after_failure(cfg: Config, state, start: datetime, end: datetime,
                   logger, label: str) -> str:
    """一段重试用尽仍失败后的处置。返回 "gap"（视为无录像）或 "fail"。"""
    if (datetime.now() - end).total_seconds() > 3600:
        logger.error("%s该区间位于历史时段且多次失败，判定设备上已无此录像，跳过 %s ~ %s",
                     label, start.strftime(TIME_FMT), end.strftime(TIME_FMT))
        state.total_gap_segments += 1
        state.consecutive_errors = 0
        return "gap"
    logger.error("%s拉取持续失败，这一段留待下次重跑（%s ~ %s）",
                 label, start.strftime(TIME_FMT), end.strftime(TIME_FMT))
    state.consecutive_errors += 1
    state.last_error = "拉取失败（已达最大重试次数）"
    return "fail"


def run_batch(cfg: Config, ffmpeg: str, state, root: str, logger,
              plan, workers: int, skip_existing: bool = True):
    """拉取 plan 里尚未完成的窗口。顺序与并发走同一条记账路径。"""
    stats = {"saved": 0, "bytes": 0, "gaps": 0, "skipped": 0,
             "failed": [], "aborted": False}
    skipped, pending = split_batch(cfg, root, plan, skip_existing)
    stats["skipped"] = skipped
    if not pending:
        if plan and skipped:
            state.set_next_start(plan[-1][1])
            state.save(cfg.state_file)
        return stats

    batch_start = pending[0][0]
    heights = {}

    def commit_cursor():
        state.set_next_start(_resolve_cursor(cfg, root, plan, heights, batch_start))
        state.save(cfg.state_file)

    # ---------------- 顺序路径（并发度为 1） ----------------
    if workers <= 1:
        for s, e in pending:
            if not ensure_space(cfg, root, logger):
                logger.error("磁盘空间不足，中止本次归档（稍后重跑可从断点继续）")
                stats["aborted"] = True
                break
            logger.info("-" * 78)
            logger.info("准备拉取 %s ~ %s（%s）", s.strftime(TIME_FMT),
                        e.strftime(TIME_FMT), human_duration((e - s).total_seconds()))
            outcome = pull_window(cfg, ffmpeg, s, e, root, logger)
            if outcome is None:
                verdict = _after_failure(cfg, state, s, e, logger, "")
                heights[(s, e)] = verdict
                if verdict == "gap":
                    stats["gaps"] += 1
                else:
                    stats["failed"].append((s, e))
            else:
                record_outcome(cfg, state, s, e, outcome, logger, advance_cursor=False)
                heights[(s, e)] = outcome.status
                if outcome.status == "ok":
                    stats["saved"] += 1
                    stats["bytes"] += outcome.size
                else:
                    stats["gaps"] += 1
            commit_cursor()
        return stats

    # ---------------- 并发路径 ----------------
    lock = threading.Lock()
    sem = threading.Semaphore(workers)
    abort = threading.Event()
    total = len(pending)

    stagger = max(0.0, float(getattr(cfg.runtime, "start_stagger_seconds", 1.5) or 0.0))
    gate_lock = threading.Lock()
    next_slot = [0.0]

    def stagger_gate():
        if stagger <= 0:
            return
        with gate_lock:
            now = time.time()
            wait = max(0.0, next_slot[0] - now)
            next_slot[0] = max(now, next_slot[0]) + stagger
        if wait > 0:
            time.sleep(wait)

    def book(window, outcome, label):
        """记账（唯一会改动 state / 索引的地方，必须持锁）。"""
        s, e = window
        with lock:
            if outcome is None:
                verdict = _after_failure(cfg, state, s, e, logger, label + " ")
                heights[window] = verdict
                if verdict == "gap":
                    stats["gaps"] += 1
                else:
                    stats["failed"].append(window)
            else:
                record_outcome(cfg, state, s, e, outcome, logger, advance_cursor=False)
                heights[window] = outcome.status
                if outcome.status == "ok":
                    stats["saved"] += 1
                    stats["bytes"] += outcome.size
                else:
                    stats["gaps"] += 1
            commit_cursor()
            logger.info("批次进度：%d/%d 段已定论（%d 成功 / %d 无录像 / %d 失败）",
                        len(heights) + skipped, len(plan),
                        stats["saved"], stats["gaps"], len(stats["failed"]))
            if free_bytes(root) < cfg.retention.min_free_gb * 1024 ** 3:
                if not ensure_space(cfg, root, logger):
                    logger.error("磁盘空间不足，不再开启新的拉取流")
                    stats["aborted"] = True
                    abort.set()

    def worker(idx, window):
        label = "%d/%d %s" % (idx, total, window[0].strftime("%H:%M"))
        if abort.is_set():
            book(window, None, label)
            return
        with sem:
            if abort.is_set():
                book(window, None, label)
                return
            stagger_gate()
            logger.info("%s 开始拉取 %s ~ %s（%s）", label,
                        window[0].strftime(TIME_FMT), window[1].strftime(TIME_FMT),
                        human_duration((window[1] - window[0]).total_seconds()))
            outcome = pull_window(cfg, ffmpeg, window[0], window[1], root,
                                  logger, label)
        book(window, outcome, label)

    threads = []
    for i, window in enumerate(pending):
        t = threading.Thread(target=worker, args=(i + 1, window), daemon=True,
                             name="pull-%d" % (i + 1))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    with lock:
        commit_cursor()
    return stats


def _day_stats(rc: int, **over) -> dict:
    st = {"rc": rc, "saved": 0, "bytes": 0, "gaps": 0, "skipped": 0,
          "failed": [], "aborted": False, "elapsed": 0.0}
    st.update(over)
    return st


def _pending_days(cfg: Config, state, day: datetime, redo: bool):
    """本次要归档的日子（升序，最后一个必然是目标日）。"""
    target = day.date()
    if redo:
        return [day], None

    start = state.get_next_start()
    if start is None:
        return [day], None

    cap = max(1, int(getattr(cfg.runtime, "catchup_max_days", 7) or 7))
    floor = target - timedelta(days=cap - 1)

    dropped = None
    if start.date() < floor:
        dropped = (start.date(), floor - timedelta(days=1))

    todo = []
    d = max(start.date(), floor)
    while d <= target:
        dd = datetime.combine(d, datetime.min.time())
        _ws, we = day_window(cfg, dd)
        if we > start:
            todo.append(dd)
        d += timedelta(days=1)
    return todo, dropped


def _archive_day(cfg: Config, ffmpeg: str, state, root: str, logger,
                 day: datetime, workers: int, redo: bool) -> dict:
    """归档目标日窗口内的录像。返回 _day_stats() 结构。"""
    day_s = day.strftime("%Y-%m-%d")
    win_s, win_e = day_window(cfg, day)
    start = state.get_next_start()

    logger.info("-" * 78)
    logger.info("归档目标日 : %s    窗口 %s ~ %s", day_s,
                win_s.strftime(TIME_FMT), win_e.strftime(TIME_FMT))

    if redo:
        if start is not None and start >= win_s:
            logger.info("--redo：忽略已有进度 %s，从窗口开头重拉（同名文件会被覆盖）",
                        start.strftime(TIME_FMT))
        start = win_s
    elif start is None or start < win_s:
        if start is not None:
            logger.info("进度游标 %s 早于目标窗口，起点前移到窗口开头",
                        start.strftime(TIME_FMT))
        start = win_s

    if start >= win_e:
        logger.info("目标日 %s 已归档完成（游标 %s 已到窗口末尾 %s），无需重复拉取",
                    day_s, start.strftime(TIME_FMT), win_e.strftime(TIME_FMT))
        return _day_stats(0)

    lag = timedelta(seconds=cfg.archive.safety_lag_seconds)
    hard_end = min(win_e, datetime.now() - lag)
    if hard_end <= start:
        logger.info("目标日 %s 的录像尚未录到 %s（本机时间 %s），本次无事可做；稍后重跑即可补齐",
                    day_s, start.strftime(TIME_FMT), datetime.now().strftime(TIME_FMT))
        return _day_stats(3)

    state.set_next_start(start)
    state.save(cfg.state_file)

    span = (hard_end - start).total_seconds()
    plan = day_plan_windows(cfg, start, hard_end)
    already, pending = split_batch(cfg, root, plan, skip_existing=not redo)

    logger.info("待归档区间 : %s ~ %s（%s）",
                start.strftime(TIME_FMT), hard_end.strftime(TIME_FMT),
                human_duration(span))
    logger.info("分段       : 共 %d 段（每段 %d 分钟）%s",
                len(plan), cfg.archive.segment_minutes,
                ("，其中 %d 段已归档、%d 段待拉" % (already, len(pending)))
                if already else "")
    if workers > 1:
        logger.info("并发路数   : %d 路（设备单路回放恒为 1 倍速，实测并发可线性叠加）",
                    workers)
        logger.info("预计耗时   : 约 %s（单路约需 %s）",
                    human_duration(span / workers), human_duration(span))
    else:
        logger.info("并发路数   : 1 路（顺序拉取；把 runtime.parallel_streams 调大即可提速）")
        logger.info("预计耗时   : 约 %s（设备回放为 1 倍速推流，无法快进）",
                    human_duration(span))
    if hard_end < win_e:
        logger.info("说明       : 目标日只录到 %s，本次拉到此处；稍后重跑会从断点续拉",
                    hard_end.strftime(TIME_FMT))
    logger.info("=" * 78)

    t0 = time.time()
    stats = run_batch(cfg, ffmpeg, state, root, logger, plan, workers,
                      skip_existing=not redo)
    elapsed = time.time() - t0

    logger.info("=" * 78)
    logger.info("目标日 %s %s", day_s,
                "归档完成" if hard_end >= win_e else "部分完成（目标日尚未录完）")
    logger.info("归档窗口   : %s ~ %s",
                win_s.strftime(TIME_FMT), win_e.strftime(TIME_FMT))
    logger.info("本次新增   : %d 个文件 / %s", stats["saved"], human_size(stats["bytes"]))
    logger.info("无录像跳过 : %d 段", stats["gaps"])
    if stats["skipped"]:
        logger.info("此前已归档 : %d 段（直接复用，不重复拉取）", stats["skipped"])
    logger.info("耗时       : %s", human_duration(elapsed))

    rc = 0
    if stats["failed"]:
        logger.error("本次有 %d 段拉取失败，未计入完成：", len(stats["failed"]))
        for s, e in stats["failed"]:
            logger.error("    %s ~ %s", s.strftime(TIME_FMT), e.strftime(TIME_FMT))
        logger.error("重跑同一条命令即会只重试这些段（已完成的不会再拉）。")
        rc = 1
    elif stats["aborted"]:
        rc = 1
    return _day_stats(rc, saved=stats["saved"], bytes=stats["bytes"],
                      gaps=stats["gaps"], skipped=stats["skipped"],
                      failed=stats["failed"], aborted=stats["aborted"],
                      elapsed=elapsed)


def run_day(cfg: Config, logger, root: str, day: datetime,
            redo: bool = False, sweep: bool = True) -> int:
    """按日归档：把目标日窗口内的录像完整拉到本地，完成即退出。"""
    ffmpeg = find_ffmpeg(cfg)
    day_s = day.strftime("%Y-%m-%d")
    win_s, win_e = day_window(cfg, day)

    logger.info("=" * 78)
    logger.info("按日归档启动：目标日 %s", day_s)
    logger.info("录像机     : %s:%d  通道 %d  %s码流",
                cfg.nvr.host, cfg.nvr.rtsp_port, cfg.nvr.channel, cfg.nvr.stream)
    logger.info("归档目录   : %s", root)
    logger.info("归档窗口   : %s ~ %s",
                win_s.strftime(TIME_FMT), win_e.strftime(TIME_FMT))
    logger.info("命名示例   : %s", storage.sample_segment_name(cfg))
    logger.info("门店 / 通道: %s / %s",
                cfg.archive.store_name, cfg.archive.channel_name)
    logger.info("=" * 78)

    if sweep:
        sweep_part_files(root, logger)
    else:
        logger.info("已按要求跳过启动清理（--no-sweep）：遗留 .part_ 分片保留在原处")
    if cfg.retention.enabled:
        storage.cleanup(root, cfg, logger)

    state = State.load(cfg.state_file)
    workers = max(1, int(getattr(cfg.runtime, "parallel_streams", 1) or 1))

    todo, dropped = _pending_days(cfg, state, day, redo)
    if dropped:
        first, last = dropped
        logger.warning("回溯上限 %d 天：%s ~ %s（共 %d 天）已超出，本次不再补；"
                       "这些天如需留存请手工指定日期补拉",
                       int(getattr(cfg.runtime, "catchup_max_days", 7) or 7),
                       first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"),
                       (last - first).days + 1)

    if not todo:
        cur = state.get_next_start()
        logger.info("-" * 78)
        logger.info("目标日 %s 已归档完成（游标 %s 已到窗口末尾 %s），无需重复拉取",
                    day_s, cur.strftime(TIME_FMT) if cur else "(空)",
                    win_e.strftime(TIME_FMT))
        logger.info("=" * 78)
        return 0

    if len(todo) > 1:
        logger.info("本次需归档 %d 天：%s", len(todo),
                    "、".join(d.strftime("%m-%d") for d in todo))
        logger.info("说明       : 进度游标落后于目标日，先逐日补齐中间缺的日子")

    rc = 0
    target_rc = 0
    total = _day_stats(0)
    for i, d in enumerate(todo, 1):
        if len(todo) > 1:
            logger.info("")
            logger.info(">>> 第 %d/%d 天：%s", i, len(todo), d.strftime("%Y-%m-%d"))
        st = _archive_day(cfg, ffmpeg, state, root, logger, d, workers, redo)
        total["saved"] += st["saved"]
        total["bytes"] += st["bytes"]
        total["gaps"] += st["gaps"]
        total["skipped"] += st["skipped"]
        total["failed"].extend(st["failed"])
        total["aborted"] = total["aborted"] or st["aborted"]
        total["elapsed"] += st["elapsed"]
        if st["rc"] == 1:
            rc = 1
            logger.error("第 %s 天归档失败，本次到此为止；重跑同一条命令会从这天接着补",
                         d.strftime("%Y-%m-%d"))
            break
        target_rc = st["rc"]

    if len(todo) > 1:
        logger.info("=" * 78)
        logger.info("多日归档汇总：%d 天 / 新增 %d 个文件 / %s / 耗时 %s",
                    len(todo), total["saved"], human_size(total["bytes"]),
                    human_duration(total["elapsed"]))
    logger.info("=" * 78)
    if rc == 1:
        return 1
    if target_rc == 3:
        return 3
    return 0


# --------------------------------------------------------------------------- #
def do_status(cfg: Config, logger, root: str) -> int:
    print("=" * 78)
    print("监控录像归档状态")
    print("=" * 78)
    print("录像机       : %s:%d  通道 %d  %s码流"
          % (cfg.nvr.host, cfg.nvr.rtsp_port, cfg.nvr.channel, cfg.nvr.stream))
    print("归档命名     : %s_%s_<开始>_<结束>_%s.mp4（%s）"
          % (cfg.archive.store_name, cfg.archive.channel_name, cfg.archive.name_suffix,
             NAME_SOURCE_TEXT.get(getattr(cfg, "name_source", "config"), "配置指定")))
    print("磁盘情况     : %s" % describe_drives())
    try:
        print("ffmpeg       : %s" % find_ffmpeg(cfg))
    except FileNotFoundError as exc:
        print("ffmpeg       : 未找到 (%s)" % exc)
    print()
    print(storage.summarize(root, logger))
    print()
    print(State.load(cfg.state_file).describe())
    print("=" * 78)
    return 0


def do_verify(cfg: Config, logger, root: str, fix: bool = False) -> int:
    """逐个解码校验归档文件，确认本地播放器能正常打开。"""
    print("=" * 78)
    print("归档文件播放校验%s" % ("（发现问题将自动修复）" if fix else ""))
    print("=" * 78)
    try:
        ffmpeg = find_ffmpeg(cfg)
    except FileNotFoundError as exc:
        print("[失败] %s" % exc)
        return 2

    segs = storage.scan_segments(root)
    if not segs:
        print("归档目录里还没有符合命名规则的录像：%s" % root)
        return 0

    bad = 0
    fixed = 0
    for seg in segs:
        name = os.path.basename(seg.path)
        ok, detail = probe_media(ffmpeg, seg.path)
        if ok:
            print("[可播放] %s" % name)
            continue
        bad += 1
        print("[打不开] %s" % name)
        if detail:
            print("         %s" % detail)
        if fix:
            if ensure_playable(ffmpeg, seg.path, logger):
                print("         -> 已修复")
                fixed += 1
            else:
                print("         -> 修复失败，已保留原文件")

    print()
    if bad == 0:
        print("共 %d 个文件，全部可正常播放" % len(segs))
    else:
        print("共 %d 个文件，%d 个存在播放问题%s"
              % (len(segs), bad, "，已修复 %d 个" % fixed if fix else "（加 --fix 可自动修复）"))
    print("=" * 78)
    return 0


def do_selftest(cfg: Config, logger, root: str) -> int:
    print("=" * 78)
    print("自检")
    print("=" * 78)
    ok = True

    try:
        ffmpeg = find_ffmpeg(cfg)
        print("[通过] ffmpeg 可用: %s" % ffmpeg)
    except FileNotFoundError as exc:
        print("[失败] %s" % exc)
        return 2

    print("[信息] 磁盘情况: %s" % describe_drives())
    print("[信息] 归档目录: %s" % root)

    reachable, msg = check_connection(cfg.nvr)
    print("[%s] RTSP 连通/认证: %s" % ("通过" if reachable else "失败", msg))
    ok = ok and reachable
    if not reachable:
        return 2

    now = datetime.now()
    for label, s, e in [
        ("5 分钟前", now - timedelta(minutes=6), now - timedelta(minutes=5)),
        ("30 分钟前", now - timedelta(minutes=31), now - timedelta(minutes=30)),
        ("2 小时前", now - timedelta(hours=2, minutes=1), now - timedelta(hours=2)),
    ]:
        code = probe_range(cfg.nvr, s, e)
        print("[信息] 探测%-8s %s ~ %s -> %s"
              % (label, s.strftime(TIME_FMT), e.strftime(TIME_FMT),
                 {200: "有录像", 404: "无录像", None: "探测失败"}.get(code, code)))

    print("\n试拉 30 秒验证端到端……")
    test_dir = ensure_dirs(os.path.join(cfg.base_dir, "_test"))
    s = now - timedelta(minutes=31)
    e = s + timedelta(seconds=30)
    out = os.path.join(test_dir, "selftest_%s.mp4" % s.strftime("%Y%m%d_%H%M%S"))
    res = pull_segment(cfg, ffmpeg, s, e, out, logger)
    if res.status == "ok":
        print("[通过] 试拉成功: %s（%s，时长 %s）"
              % (res.path, human_size(res.size), human_duration(res.duration)))
        print("       平均码率约 %.0f kbps" % (res.size * 8 / max(res.duration, 1) / 1000))
        print("       该码率下 30 分钟约 %.0f MB，一天约 %.2f GB"
              % (res.size * 8 / max(res.duration, 1) * 1800 / 8 / 1024 / 1024,
                 res.size * 8 / max(res.duration, 1) * 86400 / 8 / 1024 ** 3))
    else:
        print("[失败] 试拉未成功: %s %s" % (res.status, res.detail))
        ok = False
    print("=" * 78)
    print("自检结果: %s" % ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 2


# --------------------------------------------------------------------------- #
def do_device_info(cfg: Config, logger) -> int:
    """显示录像机的身份信息，确认命名依据（--device-info）。"""
    ident = resolve_identity(cfg, logger, force_refresh=True, quiet=True)
    cfg.apply_identity(ident)
    print("=" * 78)
    print("录像机身份信息（归档案命名的依据）")
    print("=" * 78)
    print(identity_report(cfg, ident))
    print()
    print("-" * 78)
    print("归档案命名将使用：")
    print("    %s_%s_<开始时间>_<结束时间>_%s.mp4"
          % (cfg.archive.store_name, cfg.archive.channel_name, cfg.archive.name_suffix))
    print("    %s" % storage.sample_segment_name(cfg))
    print()
    print("门店名 = 大华云联『设备列表』里的设备名称（= 录像机 General.MachineName）")
    print("通道名 = 该通道在录像机上的名称（= ChannelTitle）")
    print("如需强制指定，把 config.json 里 archive.store_name / channel_name 改为具体名称即可。")
    print("-" * 78)
    return 0


def do_channels(cfg: Config, logger, probe: bool = False) -> int:
    """列出录像机的通道清单，确认实际接了几路摄像头（--channels）。"""
    from nvrcore.channels import (fetch_channel_map, probe_channels,
                                  describe_channels)
    try:
        cmap = fetch_channel_map(cfg.nvr.host, cfg.nvr.username, cfg.nvr.password,
                                 cfg.device.http_port, cfg.nvr.channel,
                                 cfg.device.timeout_seconds)
    except (OSError, IOError, ValueError) as exc:
        print("[失败] 读取通道清单失败：%s" % exc)
        print("       请确认录像机可达、账号密码正确，且账号有『远程配置查询』权限。")
        return 2

    if probe:
        n = len(cmap.active) or len(cmap.channels)
        print("正在对通道做 RTSP 实时流探测（最多 %d 路，每路超时 %.0f 秒）……"
              % (n, cfg.device.timeout_seconds))
        probe_channels(cfg, cmap, logger)

    print(describe_channels(cfg, cmap))
    return 0


def do_accept(cfg: Config, logger, root: str) -> int:
    """新门店部署前的账密验收（--accept）。

    三道门禁：能登录 / 能回放 / 拉得到。全绿才建议部署。
    退出码：0=通过(含条件通过) 2=不通过 3=待复验
    """
    report = run_acceptance(cfg, logger)
    print(render_report(cfg, report, root))
    return {VERDICT_PASS: 0, VERDICT_CONDITIONAL: 0,
            VERDICT_RETEST: 3, VERDICT_FAIL: 2}.get(report.verdict, 2)


def main(argv=None) -> int:
    args = parse_args(argv)

    day_target = None
    if args.day:
        if args.from_time or args.to_time or args.once:
            print("--day 不能与 --from / --to / --once 同时使用")
            return 2
        try:
            day_target = parse_day_arg(args.day)
        except ValueError as exc:
            print("%s" % exc)
            return 2
        print("按日归档：目标日 %s（拉完即退出）" % day_target.strftime("%Y-%m-%d"))

    cfg, logger, root = bootstrap(args)

    if args.parallel is not None:
        cfg.runtime.parallel_streams = max(1, min(16, int(args.parallel)))
        logger.info("并发路数由命令行指定为 %d 路", cfg.runtime.parallel_streams)

    if args.device_info:
        return do_device_info(cfg, logger)

    if args.channels:
        return do_channels(cfg, logger, probe=args.probe)

    if args.accept:
        return do_accept(cfg, logger, root)

    if args.from_time:
        state = State.load(cfg.state_file)
        try:
            dt = datetime.strptime(args.from_time, TIME_FMT)
        except ValueError:
            print("时间格式应为 'YYYY-MM-DD HH:MM:SS'")
            return 2
        state.set_next_start(dt)
        state.save(cfg.state_file)
        print("已把拉取起点重设为 %s" % dt.strftime(TIME_FMT))

    if args.reset:
        state = State()
        state.save(cfg.state_file)
        print("进度状态已清空（录像文件未删除）")

    if args.to_time:
        try:
            datetime.strptime(args.to_time, TIME_FMT)
        except ValueError:
            print("--to 时间格式应为 'YYYY-MM-DD HH:MM:SS'")
            return 2
        cfg.archive.end_time = args.to_time
        print("本次归档终点设为 %s" % args.to_time)

    guard = SingleInstance(default_lock_path(cfg.state_file))
    try:
        if day_target is not None:
            guard.acquire(mode="按日归档", ignore=args.ignore_lock, logger=logger)
            return run_day(cfg, logger, root, day_target, redo=args.redo,
                           sweep=not args.no_sweep)
        if args.status:
            return do_status(cfg, logger, root)
        if args.selftest:
            guard.acquire(mode="连接自检", ignore=args.ignore_lock, logger=logger)
            return do_selftest(cfg, logger, root)
        if args.cleanup:
            stats = storage.cleanup(root, cfg, logger, dry_run=args.dry_run)
            print("\n" + storage.summarize(root, logger))
            return 0
        if args.verify:
            return do_verify(cfg, logger, root, fix=args.fix)
        guard.acquire(mode="常驻归档", ignore=args.ignore_lock, logger=logger)
        return run_loop(cfg, logger, root, once=args.once, sweep=not args.no_sweep)
    except LockBusy as exc:
        logger.error("%s", exc)
        logger.error("拒绝并行启动第二份归档任务：并发路数会翻倍（可能打满门店录像机、"
                     "影响实时预览），而且两批会写同一个文件名互相覆盖、游标互相踩。")
        logger.error("确认另一份确实已经退出后重跑即可；确实要强行并行才用 --ignore-lock。")
        return 4
    except KeyboardInterrupt:
        logger.info("收到中断信号，已安全退出（进度已保存，下次可继续）")
        return 0
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:      # noqa: BLE001
        logger.exception("运行异常: %s", exc)
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
