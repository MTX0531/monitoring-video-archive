# -*- coding: utf-8 -*-
"""大华 NVR RTSP 回放拉取。

要点（均来自实机验证）：
  * 回放地址: /cam/playback?channel=N&subtype=0|1&starttime=YYYY_MM_DD_HH_MM_SS&endtime=...
    时间使用录像机本地时间，不需要换算 UTC。
  * 认证: Digest，且该机型 nonce 与 TCP 连接绑定 —— 挑战与重发必须在同一条连接内完成。
    ffmpeg 的 RTSP 客户端天然满足该条件，Python 侧探测也按同连接方式实现。
  * 无录像时段: DESCRIBE 直接返回 404，可用于秒级判定空档。
  * 回放为 1 倍速推流，拉取 30 分钟录像需要约 30 分钟真实时间。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple
from urllib.parse import quote

from .config import Config, NvrConfig
from .logutil import human_size

RTSP_OK = 200
RTSP_NO_RECORD = 404

_GAP_PATTERNS = (
    "404 Not Found",
    "Server returned 404",
    "method DESCRIBE failed: 404",
)
_FATAL_PATTERNS = (
    "Connection refused",
    "Connection timed out",
    "No route to host",
    "Network is unreachable",
    "Connection reset by peer",
    "401 Unauthorized",
    "Server returned 401",
    "Server returned 403",
    "Server returned 5",
    "Invalid data found when processing input",
    "Cannot open connection",
    "Protocol not found",
    "Immediate exit requested",
)


# --------------------------------------------------------------------------- #
#  工具
# --------------------------------------------------------------------------- #
def find_ffmpeg(cfg: Config) -> str:
    raw = (cfg.runtime.ffmpeg_path or "auto").strip()
    candidates = []
    if raw and raw.lower() != "auto":
        candidates.append(raw)
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates.append(os.path.join(cfg.base_dir, "bin", exe))
    if os.name == "nt":
        candidates.append(os.path.join(cfg.base_dir, "bin", "ffmpeg.exe"))
    found_in_path = shutil.which("ffmpeg")
    if found_in_path:
        candidates.append(found_in_path)
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    raise FileNotFoundError(
        "未找到 ffmpeg。请把 ffmpeg.exe 放到 %s 目录下，"
        "或在 config.json 的 runtime.ffmpeg_path 指定完整路径。"
        % os.path.join(cfg.base_dir, "bin")
    )


def build_playback_url(nvr: NvrConfig, start: datetime, end: datetime) -> str:
    user = quote(nvr.username, safe="")
    pwd = quote(nvr.password, safe="")
    return (
        "rtsp://%s:%s@%s:%d/cam/playback?channel=%d&subtype=%d"
        "&starttime=%s&endtime=%s"
        % (user, pwd, nvr.host, nvr.rtsp_port, nvr.channel, nvr.subtype,
           start.strftime("%Y_%m_%d_%H_%M_%S"),
           end.strftime("%Y_%m_%d_%H_%M_%S"))
    )


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _recv_rtsp(sock: socket.socket) -> Tuple[str, str]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    text = head.decode("utf-8", "replace")
    m = re.search(r"Content-Length:\s*(\d+)", text, re.I)
    length = int(m.group(1)) if m else 0
    body = rest
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return text, body[:length].decode("utf-8", "replace")


@dataclass
class DescribeResult:
    """一次回放地址 DESCRIBE 的结果。

    probe_range 把各种失败都压成一个 None，验收场景下需要知道
    "到底卡在哪一步"（没连上 / 密码错 / 没权限 / 真没录像），所以单独保留细节。
    """
    code: Optional[int] = None      # 200/401/403/404/503…；None = 网络层没连上
    stage: str = "connect"          # connect | challenge | done | auth
    detail: str = ""                # 状态行里的原因短语，或异常说明
    had_challenge: bool = False     # 是否触发过 401 挑战（= Digest 流程被走到）

    @property
    def ok(self) -> bool:
        return self.code == RTSP_OK


def _status(head: str) -> int:
    try:
        return int(head.split(" ")[1])
    except (IndexError, ValueError):
        return -1


def _reason(head: str) -> str:
    """取出状态行里的原因短语，如 'Unauthorized' / 'Forbidden'。"""
    lines = (head or "").strip().splitlines()
    if not lines:
        return ""
    parts = lines[0].split(" ", 2)
    return parts[2].strip() if len(parts) > 2 else ""


def describe_playback(nvr: NvrConfig, start: datetime, end: datetime,
                      timeout: float = 8.0) -> DescribeResult:
    """对回放地址发 DESCRIBE，返回带分类的详细结果。"""
    url = build_playback_url(nvr, start, end)
    m = re.match(r"rtsp://[^@]*@([^/:]+):(\d+)(/.*)$", url)
    if not m:
        return DescribeResult(None, "connect", "回放地址解析失败")
    host, port, path = m.group(1), int(m.group(2)), m.group(3)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return DescribeResult(None, "connect", "无法连接 %s:%d（%s）" % (host, port, exc))
    sock.settimeout(timeout)
    try:
        sock.sendall(("DESCRIBE %s RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n"
                      "User-Agent: nvr-puller/1.0\r\n\r\n" % url).encode())
        head, _ = _recv_rtsp(sock)
        code = _status(head)
        if code != 401:
            return DescribeResult(code, "done", _reason(head), False)
        challenge = re.search(r"WWW-Authenticate:\s*(.+?)(?:\r\n|$)", head, re.I)
        if not challenge:
            return DescribeResult(code, "challenge",
                                  "响应 401 但未带 WWW-Authenticate 挑战头", False)
        realm = re.search(r'realm="([^"]*)"', challenge.group(1))
        nonce = re.search(r'nonce="([^"]*)"', challenge.group(1))
        if not realm or not nonce:
            return DescribeResult(code, "challenge", "挑战头缺少 realm 或 nonce", False)
        ha1 = _md5("%s:%s:%s" % (nvr.username, realm.group(1), nvr.password))
        ha2 = _md5("DESCRIBE:%s" % path)
        response = _md5("%s:%s:%s" % (ha1, nonce.group(1), ha2))
        sock.sendall(("DESCRIBE %s RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
                      "User-Agent: nvr-puller/1.0\r\n"
                      'Authorization: Digest username="%s", realm="%s", nonce="%s", '
                      'uri="%s", response="%s"\r\n\r\n'
                      % (url, nvr.username, realm.group(1), nonce.group(1), path, response)).encode())
        head2, _ = _recv_rtsp(sock)
        return DescribeResult(_status(head2), "auth", _reason(head2), True)
    except (OSError, ValueError) as exc:
        return DescribeResult(None, "challenge", "RTSP 交互异常：%s" % exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def probe_range(nvr: NvrConfig, start: datetime, end: datetime,
                timeout: float = 8.0) -> Optional[int]:
    """用 RTSP DESCRIBE 判定某时间段是否有录像。

    返回 200=有录像 / 404=无录像 / None=探测失败（网络或认证问题，应重试）。
    """
    return describe_playback(nvr, start, end, timeout).code


def check_connection(nvr: NvrConfig, timeout: float = 5.0) -> Tuple[bool, str]:
    """连通性快速自检，返回 (是否可达, 说明)。"""
    try:
        s = socket.create_connection((nvr.host, nvr.rtsp_port), timeout=timeout)
        s.close()
    except OSError as exc:
        return False, "无法连接 %s:%d (%s)" % (nvr.host, nvr.rtsp_port, exc)
    now = datetime.now()
    code = probe_range(nvr, now - timedelta(minutes=5), now - timedelta(minutes=4))
    if code == 200:
        return True, "设备可达，认证通过，近期录像可读"
    if code == 404:
        return True, "设备可达，认证通过（最近 5 分钟刚好没有录像）"
    if code is None:
        return False, "设备可达但 RTSP 认证/探测失败，请核对账号密码"
    return False, "RTSP DESCRIBE 返回异常状态码 %s" % code


# --------------------------------------------------------------------------- #
#  拉取
# --------------------------------------------------------------------------- #
@dataclass
class PullOutcome:
    status: str                 # ok | gap | error
    path: Optional[str] = None
    duration: float = 0.0
    size: int = 0
    detail: str = ""
    exit_code: Optional[int] = None
    elapsed: float = 0.0


def pull_segment(cfg: Config, ffmpeg: str, start: datetime, end: datetime,
                 out_path: str, logger,
                 on_progress: Optional[Callable[[float, float], None]] = None) -> PullOutcome:
    """把 [start, end) 这段回放录下来存成 out_path。"""
    expected = (end - start).total_seconds()
    url = build_playback_url(cfg.nvr, start, end)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".part_",
                                    suffix=os.path.splitext(out_path)[1])
    os.close(fd)
    # ffmpeg 的 stderr 日志只是诊断用的中间产物，放在**系统临时目录**。
    # 文件在 %TEMP%/nvrpuller_<pid>_<tid>.ffmpeg.err，由系统自行回收。
    err_path = os.path.join(
        tempfile.gettempdir(),
        "nvrpuller_%d_%d.ffmpeg.err" % (os.getpid(), threading.get_ident()))

    container = cfg.archive.container.lower()
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-rtsp_transport", cfg.nvr.transport,
        "-i", url,
        "-c", "copy",
        "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
    ]
    if not cfg.archive.keep_audio:
        cmd += ["-an"]
    if container == "mp4":
        cmd += ["-movflags", "+faststart", "-video_track_timescale", "90000"]
    cmd += ["-progress", "pipe:1", "-nostats", "-y", tmp_path]

    logger.debug("ffmpeg 命令: %s", " ".join(cmd))

    t0 = time.time()
    last_activity = [t0]
    last_logged = [0.0]
    duration_seen = [0.0]

    with open(err_path, "wb") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                stdin=subprocess.DEVNULL,
                                universal_newlines=False)

        def _reader():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            duration_seen[0] = int(line.split("=", 1)[1]) / 1_000_000.0
                            last_activity[0] = time.time()
                        except ValueError:
                            pass
                    elif line.startswith("progress="):
                        last_activity[0] = time.time()
            except Exception:
                pass
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        stalled = False
        hard_limit = expected + max(180.0, expected * 0.5)
        while proc.poll() is None:
            time.sleep(1.0)
            now = time.time()
            if now - last_activity[0] > cfg.runtime.stall_timeout_seconds:
                stalled = True
                logger.warning("拉流超过 %d 秒无数据，判定卡死，正在断开重连……",
                               cfg.runtime.stall_timeout_seconds)
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if now - t0 > hard_limit:
                logger.warning("拉流超时（%.0f 秒），强制结束本次拉取", now - t0)
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if on_progress and now - last_logged[0] >= 60:
                last_logged[0] = now
                on_progress(duration_seen[0], expected)

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        reader.join(timeout=5)

    elapsed = time.time() - t0
    try:
        with open(err_path, "r", encoding="utf-8", errors="replace") as f:
            stderr_text = f.read()
    except Exception:                       # pylint: disable=broad-except
        stderr_text = ""
    # 日志文件刻意不删：固定名、每次覆盖写，不会无限增长。

    size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
    duration = duration_seen[0]
    code = proc.returncode

    detail = ""
    for pat in _GAP_PATTERNS:
        if pat in stderr_text:
            detail = "该时段无录像（%s）" % pat
            break
    if not detail and stalled:
        detail = "拉流卡死被中断"
    if not detail and code not in (0, None):
        for pat in _FATAL_PATTERNS:
            if pat in stderr_text:
                detail = "拉取失败：%s" % pat
                break
    if not detail and code not in (0, None):
        tail = " / ".join(stderr_text.strip().splitlines()[-3:])[:300]
        detail = "ffmpeg 异常退出(%s) %s" % (code, tail)

    # ---- 结果判定 ----
    ok_threshold = max(3.0, min(float(cfg.archive.min_segment_seconds), expected * 0.5))
    if duration >= ok_threshold and size > 64 * 1024:
        try:
            os.replace(tmp_path, out_path)
        except Exception as exc:            # pylint: disable=broad-except
            return PullOutcome("error", detail="保存文件失败: %s" % exc, exit_code=code,
                               elapsed=elapsed)
        if expected - duration > 60:
            logger.warning("该区间录像不完整：请求 %.1f 分钟，实得 %.1f 分钟",
                           expected / 60.0, duration / 60.0)
        if cfg.archive.verify_playable:
            try:
                playable = ensure_playable(ffmpeg, out_path, logger)
            except Exception as exc:        # pylint: disable=broad-except
                logger.error("播放校验异常：%s", exc)
                playable = False
            if not playable:
                return PullOutcome("error",
                                   detail="文件已落盘但无法播放（自动修复失败）",
                                   duration=duration, size=size,
                                   exit_code=code, elapsed=elapsed)
            try:
                size = os.path.getsize(out_path)
            except OSError:
                pass
        return PullOutcome("ok", path=out_path, duration=duration, size=size,
                           detail="", exit_code=code, elapsed=elapsed)

    _safe_remove(tmp_path)
    if detail:
        status = "gap" if "无录像" in detail else "error"
        return PullOutcome(status, detail=detail, duration=duration, size=size,
                           exit_code=code, elapsed=elapsed)
    if size <= 64 * 1024:
        return PullOutcome("gap", detail="该时段无有效录像数据", duration=duration,
                           size=size, exit_code=code, elapsed=elapsed)
    return PullOutcome("error", detail="拉取结果异常（时长 %.1f 秒）" % duration,
                       duration=duration, size=size, exit_code=code, elapsed=elapsed)


def _safe_remove(path: str) -> None:
    """尽力永久删除，绝不抛异常。删除失败时走「覆盖销毁」降级（见 storage.purge_file）。"""
    try:
        if os.path.exists(path):
            from .storage import purge_file      # 延迟导入，避免拉取热路径多背一个模块
            purge_file(path)
    except Exception:                       # pylint: disable=broad-except
        pass


# --------------------------------------------------------------------------- #
#  本地播放兼容性：校验 + 自动修复
# --------------------------------------------------------------------------- #
_FATAL_DECODE_PATTERNS = (
    "invalid data found",
    "moov atom not found",
    "could not find codec parameters",
    "decoder not found",
    "no frame",
    "file ended prematurely",
    "error opening input",
    "error while decoding",
    "invalid nal unit",
    "is not supported",
)


def probe_media(ffmpeg: str, path: str, timeout: float = 900.0,
                deep: bool = True) -> Tuple[bool, str]:
    """把文件完整解码一遍，判断本地播放器能否正常打开。

    deep=False 时只解前 5 秒（快速抽查）。
    """
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        return False, "文件不存在或过小"
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-progress", "pipe:1", "-nostats"]
    if not deep:
        cmd += ["-t", "5"]
    cmd += ["-i", path, "-map", "0:v:0", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "解码校验超时"
    except OSError as exc:
        return False, "无法执行校验: %s" % exc

    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    frames = [int(m.group(1)) for m in re.finditer(r"^frame=(\d+)", out, re.M)]
    nframes = max(frames) if frames else 0

    if proc.returncode != 0:
        return False, "解码进程异常退出(%s)：%s" % (proc.returncode, _tail(err))
    low = err.lower()
    for pat in _FATAL_DECODE_PATTERNS:
        if pat in low:
            return False, _tail(err)
    if nframes <= 0:
        return False, "未能解出任何视频帧"
    if err:
        return True, ""
    return True, ""


def _tail(text: str, n: int = 3) -> str:
    return " / ".join((text or "").strip().splitlines()[-n:])[:300]


def probe_stream_meta(ffmpeg: str, path: str, timeout: float = 120.0) -> dict:
    """读出一个媒体文件的流参数（编码 / 分辨率 / 帧率 / 时长），用于验收报告。"""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    text = (proc.stderr or b"").decode("utf-8", "replace")
    out: dict = {}
    m = re.search(r"Video:\s*([A-Za-z0-9_\-]+)", text)
    if m:
        out["codec"] = m.group(1)
    m = re.search(r"(?<![\d])(\d{2,5})x(\d{2,5})(?![\d])", text)
    if m:
        out["width"], out["height"] = m.group(1), m.group(2)
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        out["fps"] = m.group(1)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return out


def _run_ffmpeg_quiet(ffmpeg: str, args: list, timeout: float) -> Tuple[bool, str]:
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y"] + args,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时"
    except OSError as exc:
        return False, str(exc)
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return proc.returncode == 0, err[:300]


def ensure_playable(ffmpeg: str, path: str, logger) -> bool:
    """确保落盘文件能被本地播放器正常打开。

    三级处理：先校验 -> 无损重封装（修索引/extradata，不掉画质）-> 转码兜底。
    """
    ok, detail = probe_media(ffmpeg, path)
    if ok:
        logger.debug("播放校验通过：%s", os.path.basename(path))
        return True

    name = os.path.basename(path)
    logger.warning("播放校验未通过（%s），尝试修复：%s", detail, name)

    base, ext = os.path.splitext(path)
    fixed = base + ".fixed" + ext

    # 1) 无损重封装
    ok2, err2 = _run_ffmpeg_quiet(
        ffmpeg, ["-i", path, "-c", "copy", "-movflags", "+faststart", fixed], timeout=1800)
    if ok2 and os.path.getsize(fixed) > 64 * 1024:
        ok3, detail3 = probe_media(ffmpeg, fixed)
        if ok3:
            os.replace(fixed, path)
            logger.info("已用无损重封装修复：%s", name)
            return True
        logger.warning("重封装后仍不可播放（%s），改用转码", detail3)
    else:
        logger.warning("重封装失败：%s", err2)
    _safe_remove(fixed)

    # 2) 转码兜底
    logger.info("正在转码修复（耗时约等于视频时长的一半以内）：%s", name)
    ok4, err4 = _run_ffmpeg_quiet(ffmpeg, [
        "-i", path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an",
        "-movflags", "+faststart", fixed], timeout=7200)
    if ok4 and os.path.getsize(fixed) > 64 * 1024:
        ok5, detail5 = probe_media(ffmpeg, fixed)
        if ok5:
            os.replace(fixed, path)
            logger.info("已转码修复为通用 H.264/MP4：%s（%s）",
                        name, human_size(os.path.getsize(path)))
            return True
        logger.warning("转码后仍不可播放：%s", detail5)
    else:
        logger.warning("转码失败：%s", err4)
    _safe_remove(fixed)

    logger.error("无法自动修复，已保留原始文件（个别播放器可能打不开）：%s", path)
    return False


def run_hook(command: str, file_path: str, logger) -> None:
    """执行 on_segment_saved 钩子（后续接 AI 稽核 / 上传用）。"""
    if not command or not command.strip():
        return
    cmd = command.replace("{file}", file_path)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0:
            logger.info("钩子执行成功: %s", cmd)
        else:
            logger.warning("钩子执行失败(退出码 %s): %s\n%s",
                           proc.returncode, cmd, (proc.stderr or "").strip()[:500])
    except Exception as exc:
        logger.warning("钩子执行异常: %s -> %s", cmd, exc)
