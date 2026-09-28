# -*- coding: utf-8 -*-
"""把录像机经由 FTP **推**来的原始录像，落到与 RTSP 拉取**完全相同**的归档位置。

为什么需要这一层：
  大华 Record FTP 推来的是录像机自己的文件——`.dav` 容器、名字也是它自己起的。
  而归档目录里 RTSP 拉来的是 `<门店>_<通道>_<起>_<止>_device.mp4`。
  两套东西混在同一个目录里，下游（AI 稽核 / 播放 / 上传）就得写两套解析逻辑。
  所以这里把推来的文件**转封装成 mp4、并按同一命名规则改名**，
  让归档目录对下游呈现为同一种东西。

安全约束（务必保留）：
  1. 先写临时文件，**校验通过后才 rename 成正式名**。
  2. 目标文件已存在则跳过 —— 设备重推同一段不会覆盖已归档的内容，天然幂等。
  3. 校验不通过就**不发布**：原件留到 `_failed/` 供人工查。
  4. 临时文件前缀用 `.ingest-tmp_`，**不能用 `.part_`** ——
     `nvr_puller.sweep_part_files()` 会扫归档目录里的 `.part_*` 并永久删除。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta

from .config import Config, pick_storage_root
from .deviceinfo import resolve_identity
from .downloader import (ensure_playable, find_ffmpeg, probe_stream_meta,
                         run_hook, _run_ffmpeg_quiet)
from .logutil import human_size
from .storage import build_segment_name, segment_dir

TIME_FMT = "%Y-%m-%d %H:%M:%S"

INGEST_TMP_PREFIX = ".ingest-tmp_"

_TS14 = re.compile(r"(\d{14})")
_TS86 = re.compile(r"(\d{8})[_\-]?(\d{6})")
_RANGE_RE = re.compile(r"(\d{14})[_\-.](\d{14})")
_CH_RE = re.compile(r"(?:^|[^0-9A-Za-z])(?:ch(?:annel)?[_\-]?)?(\d{1,2})(?=[^0-9]|$)",
                    re.IGNORECASE)


def parse_pushed_name(name: str) -> dict:
    """从设备推来的文件名里尽力抠出时间与通道号。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    out: dict = {}

    m = _RANGE_RE.search(stem)
    if m:
        try:
            s = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            e = datetime.strptime(m.group(2), "%Y%m%d%H%M%S")
            if e > s:
                out["start"], out["end"] = s, e
        except ValueError:
            pass

    if "start" not in out:
        m = _TS14.search(stem)
        if m:
            try:
                out["start"] = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            except ValueError:
                pass

    if "start" not in out:
        m = _TS86.search(stem)
        if m:
            try:
                out["start"] = datetime.strptime(m.group(1) + m.group(2),
                                                 "%Y%m%d%H%M%S")
            except ValueError:
                pass

    m = _CH_RE.search(stem)
    if m:
        out["channel"] = int(m.group(1))
    return out


def ensure_names(cfg: Config, logger) -> None:
    """确保门店名/通道名已解析（否则文件名会退化成兜底值）。"""
    if not cfg.names_need_device():
        return
    try:
        ident = resolve_identity(cfg, logger)
        cfg.apply_identity(ident)
        cfg.name_source = ident.source
    except Exception as exc:                 # noqa: BLE001
        logger.warning("解析设备名称失败（%r），将使用兜底名称", exc)


def _quarantine(src_path: str, failed_dir: str, logger) -> str:
    try:
        os.makedirs(failed_dir, exist_ok=True)
        base = os.path.basename(src_path)
        dst = os.path.join(failed_dir, base)
        if os.path.exists(dst):
            dst = os.path.join(failed_dir,
                               "%s.%s" % (base, uuid.uuid4().hex[:8]))
        shutil.move(src_path, dst)
        return dst
    except Exception as exc:                 # noqa: BLE001
        logger.warning("隔离失败原件失败（%r），文件留在原地：%s", exc, src_path)
        return src_path


def _remove_quiet(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:                        # noqa: BLE001
        pass


def _discard(path: str, logger) -> None:
    """丢弃一个含监控内容的中间件/原件。必须走 purge_file：先全文件写零、再删条目。"""
    if not path or not os.path.isfile(path):
        return
    try:
        from .storage import purge_file
        if purge_file(path, logger):
            return
    except Exception:                        # noqa: BLE001
        pass
    _remove_quiet(path)


def ingest(cfg: Config, src_path: str, logger, *, ffmpeg: str = None,
           root: str = None, name_hint: str = None,
           failed_dir: str = None, index: bool = True) -> dict:
    """把一个推来的文件转成归档案。返回 dict：
      status = ok / skipped / failed
    """
    name_hint = name_hint or os.path.basename(src_path)
    ffmpeg = ffmpeg or find_ffmpeg(cfg)
    ensure_names(cfg, logger)
    root = root or pick_storage_root(cfg)

    info = parse_pushed_name(name_hint)
    start = info.get("start")
    if start is None:
        try:
            start = datetime.fromtimestamp(os.path.getmtime(src_path))
        except OSError:
            start = datetime.now()
        logger.info("推来的文件名里没有时间戳（%s），按文件修改时间归档：%s",
                    name_hint, start.strftime(TIME_FMT))

    parsed_ch = info.get("channel")
    if parsed_ch is not None and parsed_ch != cfg.nvr.channel:
        logger.warning("文件名里的通道号（%s）与配置的通道（%s）不一致——"
                       "仍按配置的通道名归档，请确认设备是否只推了本通道。",
                       parsed_ch, cfg.nvr.channel)

    date_dir = segment_dir(root, start, cfg.archive.organize_by_date)
    try:
        os.makedirs(date_dir, exist_ok=True)
    except OSError as exc:
        logger.error("无法创建归档目录 %s：%s", date_dir, exc)
        return {"status": "failed", "detail": "无法创建归档目录"}

    tmp = os.path.join(date_dir,
                       "%s%s.mp4" % (INGEST_TMP_PREFIX, uuid.uuid4().hex[:12]))

    # 1) 转封装（无损，只换容器）：大华 .dav -> mp4
    args = ["-i", src_path, "-c", "copy",
            "-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
    if not cfg.archive.keep_audio:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-video_track_timescale", "90000", tmp]
    ok, err = _run_ffmpeg_quiet(ffmpeg, args, timeout=3600)

    if not ok:
        # 2) 转码兜底
        logger.warning("无损转封装失败（%s），改用转码：%s", err[:160], name_hint)
        ok2, err2 = _run_ffmpeg_quiet(ffmpeg, [
            "-i", src_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an",
            "-movflags", "+faststart", "-video_track_timescale", "90000",
            tmp], timeout=7200)
        if not ok2:
            _discard(tmp, logger)
            dst = _quarantine(src_path, failed_dir or os.path.join(root, "_failed"),
                              logger)
            logger.error("转码也失败（%s），拒绝入库：%s -> %s",
                         err2[:160], name_hint, dst)
            return {"status": "failed", "detail": "转封装与转码均失败",
                    "quarantined": dst}

    # 3) 定时间
    dur = 0.0
    try:
        dur = float((probe_stream_meta(ffmpeg, tmp) or {}).get("duration") or 0)
    except Exception:                        # noqa: BLE001
        pass
    end = info.get("end")
    if not end or (end - start).total_seconds() <= 0:
        end = start + timedelta(
            seconds=dur if dur > 0
            else max(1, cfg.archive.segment_minutes) * 60)

    final = os.path.join(date_dir,
                         build_segment_name(cfg, start, end, "mp4"))

    # 4) 幂等
    if os.path.isfile(final):
        _discard(tmp, logger)
        _discard(src_path, logger)
        logger.info("目标已存在，跳过（不覆盖）：%s", os.path.basename(final))
        return {"status": "skipped", "path": final}

    # 5) 可播放性校验
    if cfg.archive.verify_playable and not ensure_playable(ffmpeg, tmp, logger):
        _discard(tmp, logger)
        dst = _quarantine(src_path, failed_dir or os.path.join(root, "_failed"),
                          logger)
        logger.error("落盘校验未通过，拒绝发布（原件已隔离）：%s -> %s",
                     name_hint, dst)
        return {"status": "failed", "detail": "落盘后校验不可播放",
                "quarantined": dst}

    size = os.path.getsize(tmp)
    os.replace(tmp, final)
    store, channel, _suffix = cfg.archive.naming()
    logger.info("已入库 %s （%s）", os.path.basename(final), human_size(size))

    if index:
        append_index(cfg, {
            "file": final,
            "source": "ftp",
            "store_name": store,
            "channel_name": channel,
            "channel": cfg.nvr.channel,
            "stream": cfg.nvr.stream,
            "requested_start": start.strftime(TIME_FMT),
            "requested_end": end.strftime(TIME_FMT),
            "actual_duration": round(dur, 2),
            "size": size,
            "saved_at": datetime.now().strftime(TIME_FMT),
            "ftp_name": name_hint,
        })
        run_hook(cfg.hooks.on_segment_saved, final, logger)

    _discard(src_path, logger)
    return {"status": "ok", "path": final, "size": size,
            "start": start, "end": end, "duration": dur}


def append_index(cfg: Config, record: dict) -> None:
    """与 nvr_puller.append_index 同构：追加一行到 state/index.jsonl。"""
    try:
        os.makedirs(os.path.dirname(cfg.index_file), exist_ok=True)
        with open(cfg.index_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
