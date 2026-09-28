# -*- coding: utf-8 -*-
"""配置加载与校验。"""
from __future__ import annotations

import ctypes
import json
import os
import re
import string
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 归档根目录的文件夹名。
#   默认 auto -> <门店名>门店监控视频归档，例：测试门店A门店监控视频归档
#   门店名与归档文件名同源（录像机 MachineName，读不到则用配置值 / 兜底值），
#   所以换一家门店时目录名会自动跟着变，不需要手工改配置。
DEFAULT_ARCHIVE_FOLDER_SUFFIX = "门店监控视频归档"
DEFAULT_ARCHIVE_FOLDER = "监控视频归档"        # 极端兜底（名字全部取不到时才会用到）

# 判断"这个目录看起来是归档目录"用的特征后缀 —— 用于发现改名后被落下的旧目录
ARCHIVE_FOLDER_SUFFIXES = (DEFAULT_ARCHIVE_FOLDER_SUFFIX, DEFAULT_ARCHIVE_FOLDER)

# 归档案命名的三段前缀
#   store_name / channel_name 默认 "auto" —— 启动时自动从录像机读取：
#     门店名  <- 通用配置 General.MachineName   （= 大华云联里的设备名称/分组名称）
#     通道名  <- ChannelTitle[通道-1].Name
#   门店名里的 "-总" / "-临时" 这类后缀因店而异，写死必然出错，所以默认自动读。
#   如需强制指定，把对应字段写成具体名称即可（非 auto 时优先用配置值）。
DEFAULT_NAME_SUFFIX = "device"
AUTO = "auto"

# 名字彻底取不到时的兜底（正常情况不会用到）
FALLBACK_STORE_NAME = "NVR"
FALLBACK_CHANNEL_NAME = "CH"


def is_auto(value: str) -> bool:
    """判断字段是否为"自动从设备读取"。空值也按自动处理。"""
    return (value or "").strip().lower() in ("", AUTO)

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_name(raw: str, fallback: str) -> str:
    """把门店名/通道名清洗成可安全用作文件名的一段。"""
    s = (raw or "").strip()
    s = _ILLEGAL_CHARS.sub("-", s)
    s = s.replace("_", "-")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip("-. ")
    s = s[:60].strip("-. ")
    return s or fallback


def sanitize_folder_name(raw: str, fallback: str) -> str:
    """把名称清洗成可安全用作文件夹名的一段（文件夹名保留 '_'，且不允许以空格/点结尾）。"""
    s = (raw or "").strip()
    s = _ILLEGAL_CHARS.sub("-", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    s = s[:120].strip(" .")
    return s or fallback


def resolve_folder_name(cfg) -> str:
    """归档根目录的文件夹名：默认 <门店名>门店监控视频归档。"""
    raw = (cfg.archive.folder_name or "").strip()
    if raw and raw.lower() != AUTO:
        return sanitize_folder_name(raw, DEFAULT_ARCHIVE_FOLDER)
    store, _channel, _suffix = cfg.archive.naming()
    suffix = sanitize_folder_name(cfg.archive.folder_suffix,
                                  DEFAULT_ARCHIVE_FOLDER_SUFFIX)
    return sanitize_folder_name(store + suffix, DEFAULT_ARCHIVE_FOLDER)


def looks_like_archive_folder(name: str) -> bool:
    """目录名是否像归档目录（改名后找旧目录用）。"""
    n = (name or "").strip()
    return any(n.endswith(suf) for suf in ARCHIVE_FOLDER_SUFFIXES)


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #
@dataclass
class NvrConfig:
    host: str = "192.168.2.10"
    rtsp_port: int = 554
    username: str = "admin"
    password: str = ""
    channel: int = 1
    stream: str = "main"          # main | sub
    transport: str = "tcp"        # tcp | udp

    @property
    def subtype(self) -> int:
        return 0 if self.stream.lower() == "main" else 1


@dataclass
class ArchiveConfig:
    root_dir: str = "auto"
    folder_name: str = AUTO
    folder_suffix: str = DEFAULT_ARCHIVE_FOLDER_SUFFIX
    start_time: str = ""
    end_time: str = "auto"
    segment_minutes: int = 30
    min_segment_seconds: int = 60
    safety_lag_seconds: int = 180
    container: str = "mp4"
    organize_by_date: bool = True

    store_name: str = AUTO        # "auto" = 读录像机的 MachineName（= 云联门店/分组名）
    channel_name: str = AUTO      # "auto" = 读录像机的 ChannelTitle
    name_suffix: str = DEFAULT_NAME_SUFFIX      # 末尾标识，云联导出为 device

    verify_playable: bool = True    # 落盘后校验能否正常解码，失败则自动转码修复
    keep_audio: bool = False        # 是否保留音轨

    def naming(self) -> Tuple[str, str, str]:
        """返回清洗后的 (门店名, 通道名, 末尾标识)。"""
        store = (FALLBACK_STORE_NAME if is_auto(self.store_name)
                 else sanitize_name(self.store_name, FALLBACK_STORE_NAME))
        channel = (FALLBACK_CHANNEL_NAME if is_auto(self.channel_name)
                   else sanitize_name(self.channel_name, FALLBACK_CHANNEL_NAME))
        return store, channel, sanitize_name(self.name_suffix, DEFAULT_NAME_SUFFIX)


@dataclass
class DeviceConfig:
    """从录像机读取真实名称（门店名 = MachineName，通道名 = ChannelTitle）。"""
    enabled: bool = True
    http_port: int = 80
    cache_file: str = "state/device.json"
    refresh_hours: int = 24          # 缓存有效期，超过则重新读取
    timeout_seconds: float = 6.0


@dataclass
class ScheduleConfig:
    """拉取时段：只在每天的这个时间段内动录像（门店 10:00~22:00 营业，留出前后半小时）。"""
    enabled: bool = True
    start: str = "09:30"          # HH:MM
    end: str = "22:30"            # HH:MM
    # footage = 只归档时段内的录像，夜间的空档直接把游标跳过去（推荐，能追上）
    # runtime = 时段外只暂停拉流，游标仍顺序推进（会连夜间录像一起拉，永远追不上）
    mode: str = "footage"


@dataclass
class RetentionConfig:
    enabled: bool = True
    keep_days: int = 7
    max_archive_gb: float = 0.0
    min_free_gb: float = 5.0
    protect_recent_hours: int = 24
    delete_mode: str = "permanent"      # permanent | recycle
    scan_interval_minutes: int = 30


@dataclass
class RuntimeConfig:
    poll_interval_seconds: int = 60
    max_retries: int = 3
    retry_backoff_seconds: int = 20
    stall_timeout_seconds: int = 120
    max_attempts_per_segment: int = 5
    parallel_streams: int = 6
    start_stagger_seconds: float = 1.5
    # 按日归档的自动补漏上限：游标落后于目标日时逐日补齐，但只回溯最近这么多天
    catchup_max_days: int = 7
    ffmpeg_path: str = "auto"
    log_dir: str = "logs"
    state_file: str = "state/state.json"
    log_keep_days: int = 30


@dataclass
class HooksConfig:
    on_segment_saved: str = ""


@dataclass
class Config:
    base_dir: str = "."
    nvr: NvrConfig = field(default_factory=NvrConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)

    # ---------- 派生路径 ----------
    @property
    def log_dir(self) -> str:
        return self._abs(self.runtime.log_dir)

    @property
    def state_file(self) -> str:
        return self._abs(self.runtime.state_file)

    @property
    def index_file(self) -> str:
        """归档索引，与状态文件同目录（自定义 state_file 的实例必须把索引一起带走）。"""
        return os.path.join(os.path.dirname(self.state_file), "index.jsonl")

    @property
    def device_cache_file(self) -> str:
        return self._abs(self.device.cache_file)

    def _abs(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.abspath(os.path.join(self.base_dir, p))

    # ---------- 命名 ----------
    def names_need_device(self) -> bool:
        return is_auto(self.archive.store_name) or is_auto(self.archive.channel_name)

    def apply_identity(self, identity) -> Tuple[str, str]:
        """把（读到的）设备身份落到命名字段上，返回最终 (门店名, 通道名)。"""
        store = ch_name = ""
        if identity is not None:
            store = getattr(identity, "machine_name", "") or ""
            try:
                ch_name = identity.channel_name(self.nvr.channel)
            except AttributeError:
                ch_name = ""

        if is_auto(self.archive.store_name):
            self.archive.store_name = sanitize_name(
                store, sanitize_name("%s-%s" % (FALLBACK_STORE_NAME, self.nvr.host),
                                     FALLBACK_STORE_NAME))
        if is_auto(self.archive.channel_name):
            self.archive.channel_name = sanitize_name(
                ch_name, sanitize_name("通道%d" % self.nvr.channel,
                                       "%s%d" % (FALLBACK_CHANNEL_NAME, self.nvr.channel)))
        return self.archive.store_name, self.archive.channel_name

    def resolve_start_time(self) -> datetime:
        raw = (self.archive.start_time or "").strip()
        if not raw or raw.lower() in ("auto", "now"):
            return datetime.now()
        return _parse_time(raw, "archive.start_time")

    def resolve_end_time(self):
        raw = (self.archive.end_time or "").strip()
        if not raw or raw.lower() in ("auto", "now", "none", "forever"):
            return None
        return _parse_time(raw, "archive.end_time")


_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d")


def _parse_time(raw: str, field: str) -> datetime:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("%s 格式无法识别: %r" % (field, raw))


# --------------------------------------------------------------------------- #
#  拉取时段（每日窗口，门店营业时间用）
# --------------------------------------------------------------------------- #
def parse_hhmm(raw: str, field: str) -> time:
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).time()
        except ValueError:
            continue
    raise ValueError("%s 需要 HH:MM 格式（如 09:30）: %r" % (field, raw))


def schedule_times(cfg) -> Tuple[time, time]:
    return (parse_hhmm(cfg.schedule.start, "schedule.start"),
            parse_hhmm(cfg.schedule.end, "schedule.end"))


def window_contains(dt: datetime, ws: time, we: time) -> bool:
    t = dt.time()
    if ws <= we:
        return ws <= t < we
    return t >= ws or t < we                       # 跨午夜


def advance_to_window(dt: datetime, ws: time, we: time) -> datetime:
    """返回第一个 >= dt 且落在拉取时段内的时刻（落在夜里就直接跳到次日窗口起点）。"""
    if window_contains(dt, ws, we):
        return dt
    cand = datetime.combine(dt.date(), ws)
    while cand < dt or not window_contains(cand, ws, we):
        cand += timedelta(days=1)
    return cand


def window_close(dt: datetime, ws: time, we: time) -> Optional[datetime]:
    """dt 所在那段拉取时段的结束时刻（dt 必须落在时段内）。用于把分段截断在时段边界。"""
    if not window_contains(dt, ws, we):
        return None
    if ws <= we:
        return datetime.combine(dt.date(), we)
    if dt.time() >= ws:
        return datetime.combine(dt.date(), we) + timedelta(days=1)
    return datetime.combine(dt.date(), we)


def windowed_seconds(a: datetime, b: datetime, ws: time, we: time) -> float:
    """[a, b) 区间里落在每日拉取时段内的总秒数（用于算真实待补时长）。"""
    if b <= a:
        return 0.0
    total = 0.0
    day = a.date()
    if ws > we:                                    # 跨午夜：可能落在前一天的窗口里
        day -= timedelta(days=1)
    while day <= b.date():
        w_start = datetime.combine(day, ws)
        w_end = datetime.combine(day, we)
        if w_end <= w_start:
            w_end += timedelta(days=1)
        lo = max(a, w_start)
        hi = min(b, w_end)
        if hi > lo:
            total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
#  加载
# --------------------------------------------------------------------------- #
def _deep_merge(defaults: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in defaults.items():
        if key in user and isinstance(val, dict) and isinstance(user[key], dict):
            out[key] = _deep_merge(val, user[key])
        elif key in user:
            out[key] = user[key]
        else:
            out[key] = val
    for key, val in user.items():
        if key not in out:
            out[key] = val
    return out


def _filter(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {f for f in cls.__dataclass_fields__}
    clean: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if k.startswith("_"):
            continue
        if k in allowed:
            clean[k] = v
    return clean


DEFAULT_CONFIG_NAME = "config.json"


def default_config_dict() -> Dict[str, Any]:
    return {
        "nvr": asdict(NvrConfig()),
        "archive": asdict(ArchiveConfig()),
        "schedule": asdict(ScheduleConfig()),
        "retention": asdict(RetentionConfig()),
        "runtime": asdict(RuntimeConfig()),
        "hooks": asdict(HooksConfig()),
        "device": asdict(DeviceConfig()),
    }


def load_config(path: str | None = None, base_dir: str | None = None) -> Config:
    base_dir = os.path.abspath(base_dir or os.path.dirname(os.path.abspath(__file__)) + os.sep + "..")
    if path is None:
        path = os.path.join(base_dir, DEFAULT_CONFIG_NAME)
    path = os.path.abspath(path)

    raw: Dict[str, Any] = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    merged = _deep_merge(default_config_dict(), raw)

    cfg = Config(
        base_dir=base_dir,
        nvr=NvrConfig(**_filter(NvrConfig, merged.get("nvr"))),
        archive=ArchiveConfig(**_filter(ArchiveConfig, merged.get("archive"))),
        schedule=ScheduleConfig(**_filter(ScheduleConfig, merged.get("schedule"))),
        retention=RetentionConfig(**_filter(RetentionConfig, merged.get("retention"))),
        runtime=RuntimeConfig(**_filter(RuntimeConfig, merged.get("runtime"))),
        hooks=HooksConfig(**_filter(HooksConfig, merged.get("hooks"))),
        device=DeviceConfig(**_filter(DeviceConfig, merged.get("device"))),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.nvr.stream.lower() not in ("main", "sub"):
        cfg.nvr.stream = "main"
    if cfg.nvr.transport.lower() not in ("tcp", "udp"):
        cfg.nvr.transport = "tcp"
    if cfg.archive.segment_minutes <= 0:
        raise ValueError("archive.segment_minutes 必须大于 0")
    if cfg.archive.segment_minutes > 30:
        raise ValueError("archive.segment_minutes 不能超过 30（需求限制单个视频不超过 30 分钟）")
    if cfg.archive.container.lower() not in ("mp4", "mkv"):
        cfg.archive.container = "mp4"
    if cfg.schedule.mode.lower() not in ("footage", "runtime"):
        cfg.schedule.mode = "footage"
    else:
        cfg.schedule.mode = cfg.schedule.mode.lower()
    if cfg.schedule.enabled:
        ws = parse_hhmm(cfg.schedule.start, "schedule.start")
        we = parse_hhmm(cfg.schedule.end, "schedule.end")
        if ws == we:
            raise ValueError("schedule.start 与 schedule.end 不能相同（否则拉取时段为空）")
    if cfg.retention.delete_mode.lower() not in ("permanent", "recycle"):
        cfg.retention.delete_mode = "permanent"
    if not (1 <= int(cfg.device.http_port) <= 65535):
        cfg.device.http_port = 80
    # 并发路数：设备单路回放恒为 1 倍速，提速只能靠并发（实测 8 路 = 8 倍速）。
    cfg.runtime.parallel_streams = max(1, min(16, int(cfg.runtime.parallel_streams or 1)))
    cfg.runtime.start_stagger_seconds = max(0.0, min(30.0,
                                                     float(cfg.runtime.start_stagger_seconds or 0.0)))
    cfg.runtime.catchup_max_days = max(1, min(31, int(cfg.runtime.catchup_max_days or 1)))
    cfg.device.refresh_hours = max(0, int(cfg.device.refresh_hours))
    cfg.device.timeout_seconds = float(cfg.device.timeout_seconds or 6.0)
    if not is_auto(cfg.archive.store_name):
        cfg.archive.store_name = sanitize_name(cfg.archive.store_name, FALLBACK_STORE_NAME)
    if not is_auto(cfg.archive.channel_name):
        cfg.archive.channel_name = sanitize_name(cfg.archive.channel_name, FALLBACK_CHANNEL_NAME)
    cfg.archive.name_suffix = sanitize_name(cfg.archive.name_suffix, DEFAULT_NAME_SUFFIX)
    cfg.archive.folder_suffix = sanitize_folder_name(cfg.archive.folder_suffix,
                                                     DEFAULT_ARCHIVE_FOLDER_SUFFIX)


# --------------------------------------------------------------------------- #
#  磁盘 / 目录选址
# --------------------------------------------------------------------------- #
def _desktop_path() -> str:
    """取真实桌面路径（兼容 OneDrive 重定向）。"""
    try:
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
            p = buf.value
            if p and os.path.isdir(p):
                return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def fixed_drives() -> List[str]:
    """返回本机固定磁盘盘符列表，如 ['C:', 'D:']。"""
    drives: List[str] = []
    if os.name != "nt":
        return [os.path.abspath(os.sep)]
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask & (1 << i)):
            continue
        root = "%s:\\" % letter
        # DRIVE_FIXED == 3
        if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:
            if os.path.exists(root):
                drives.append(root)
    return drives or ["C:\\"]


def free_bytes(path: str) -> int:
    try:
        import shutil
        return shutil.disk_usage(path).free
    except Exception:
        return 0


def total_bytes(path: str) -> int:
    try:
        import shutil
        return shutil.disk_usage(path).total
    except Exception:
        return 0


def pick_storage_root(cfg: Config) -> str:
    """决定录像归档根目录。

    - 配置了绝对路径 -> 直接用
    - auto -> 选剩余空间最大的固定磁盘；若该盘是 C 盘，则放到桌面下的独立文件夹
    """
    raw = (cfg.archive.root_dir or "auto").strip()
    if raw and raw.lower() != "auto":
        return os.path.abspath(os.path.expanduser(raw))

    folder = resolve_folder_name(cfg)
    drives = fixed_drives()
    best = max(drives, key=lambda d: free_bytes(d))
    if best.upper().startswith("C"):
        return os.path.join(_desktop_path(), folder)
    return os.path.join(best + os.sep, folder)


def ensure_dirs(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def describe_drives() -> str:
    parts = []
    for d in fixed_drives():
        parts.append("%s 总%.1fGB 剩余%.1fGB" % (d, total_bytes(d) / 1024 ** 3, free_bytes(d) / 1024 ** 3))
    return " | ".join(parts)
