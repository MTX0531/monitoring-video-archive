# -*- coding: utf-8 -*-
"""从录像机读取它自身的真实身份，用于归档案命名。

为什么必须读设备而不是写死配置
--------------------------------------------------------------------
大华云联里看到的「设备名称 / 分组名称」，就是录像机通用配置里的 MachineName；
列表里的通道名，就是录像机的 ChannelTitle。门店名后缀（-总 / -临时 / 总 …）
因店而异且会变，写死在配置里迟早对不上。

本模块直接问录像机要这两个值：
    GET /cgi-bin/configManager.cgi?action=getConfig&name=General      -> MachineName
    GET /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle -> 各通道名称

注：HTTP 侧的 Digest 没有 RTSP 那种「nonce 绑定 TCP 连接」的坑，普通实现即可。

命名回退链（任一环失效都不会中断拉取）
--------------------------------------------------------------------
    配置里显式写的值  >  录像机实时读取  >  本地缓存  >  内置兜底
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from .config import AUTO, Config, sanitize_name

_CACHE_VERSION = 1


# --------------------------------------------------------------------------- #
#  HTTP Digest
# --------------------------------------------------------------------------- #
class _DigestAuth:
    def __init__(self, user: str, pwd: str):
        self.user = user
        self.pwd = pwd
        self.nc = 0

    @staticmethod
    def parse_challenge(header: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key in ("realm", "nonce", "qop", "opaque", "algorithm"):
            m = re.search(key + r'=(?:"([^"]*)"|([^,\s]+))', header or "", re.I)
            if m:
                out[key] = m.group(1) if m.group(1) is not None else m.group(2)
        return out

    def header(self, method: str, uri: str, ch: Dict[str, str]) -> str:
        realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
        qop, opaque = ch.get("qop", ""), ch.get("opaque", "")
        algo = ch.get("algorithm", "MD5")

        def H(text: str) -> str:
            data = text.encode("utf-8")
            return (hashlib.sha256(data) if algo.upper().startswith("SHA-256")
                    else hashlib.md5(data)).hexdigest()

        ha1 = H("%s:%s:%s" % (self.user, realm, self.pwd))
        ha2 = H("%s:%s" % (method, uri))
        parts = ['username="%s"' % self.user, 'realm="%s"' % realm,
                 'nonce="%s"' % nonce, 'uri="%s"' % uri]
        if qop:
            self.nc += 1
            nc = "%08x" % self.nc
            cnonce = base64.b16encode(os.urandom(8)).decode()
            parts += ['qop=%s' % qop, 'nc=%s' % nc, 'cnonce="%s"' % cnonce,
                      'response="%s"' % H("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2))]
        else:
            parts.append('response="%s"' % H("%s:%s:%s" % (ha1, nonce, ha2)))
        if opaque:
            parts.append('opaque="%s"' % opaque)
        if algo:
            parts.append("algorithm=%s" % algo)
        return "Digest " + ", ".join(parts)


def _http_get(host: str, port: int, path: str, auth: _DigestAuth,
              timeout: float) -> Tuple[int, str]:
    url = "http://%s:%d%s" % (host, port, path)
    req = urllib.request.Request(url, headers={"User-Agent": "nvr-puller"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return exc.code, exc.read().decode("utf-8", "replace")
        challenge = _DigestAuth.parse_challenge(exc.headers.get("WWW-Authenticate", ""))
        if not challenge:
            return exc.code, "响应 401 但未提供 WWW-Authenticate"
    req2 = urllib.request.Request(
        url, headers={"User-Agent": "nvr-puller",
                      "Authorization": auth.header("GET", path, challenge)})
    try:
        with urllib.request.urlopen(req2, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def probe_login(host: str, username: str, password: str, http_port: int = 80,
                timeout: float = 6.0) -> Tuple[int, str, str]:
    """只验证 HTTP 侧登录是否成立，返回 (状态码, 设备名, 失败说明)。

    状态码含义（用于新门店部署前的账密验收）：
      200 = 账号密码正确，且账号有「远程配置查询」权限（设备名一并带回）
      401 = 账号或密码错误
      403 = 账号无该接口权限，或本机 IP 不在设备的访问白名单里
      0   = 网络层就没连上（说明里带原因）
    """
    path = "/cgi-bin/configManager.cgi?action=getConfig&name=General"
    auth = _DigestAuth(username, password)
    try:
        code, body = _http_get(host, http_port, path, auth, timeout)
    except (socket.timeout, OSError) as exc:
        return 0, "", "无法连接 %s:%d（%s）" % (host, http_port, exc)
    if code != 200:
        return code, "", ""
    return code, (_parse_kv(body).get("table.General.MachineName") or "").strip(), ""


def probe_device_time(host: str, username: str, password: str, http_port: int = 80,
                      timeout: float = 6.0) -> Optional[str]:
    """读取录像机的当前时间，返回 'YYYY-MM-DD HH:MM:SS'，接口不可用时返回 None。"""
    auth = _DigestAuth(username, password)
    try:
        code, body = _http_get(host, http_port,
                               "/cgi-bin/global.cgi?action=getCurrentTime", auth, timeout)
    except (socket.timeout, OSError):
        return None
    if code != 200:
        return None
    m = re.search(r"result=([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2})", body)
    return m.group(1).replace("T", " ") if m else None


# --------------------------------------------------------------------------- #
#  身份数据
# --------------------------------------------------------------------------- #
_PLACEHOLDER_TITLES = {"ipc", "ipcamera", "camera", "cam", "channel", "通道", "未知"}


@dataclass
class DeviceIdentity:
    machine_name: str = ""                 # 通用配置里的设备名称 = 云联里的门店/分组名
    channel_titles: Dict[int, str] = field(default_factory=dict)
    model: str = ""
    serial: str = ""
    firmware: str = ""
    fetched_at: str = ""
    source: str = "device"                 # device | cache | fallback

    # ---------- 取值 ----------
    def channel_name(self, channel: int) -> str:
        return (self.channel_titles.get(int(channel)) or "").strip()

    def is_placeholder_channel(self, channel: int) -> bool:
        raw = self.channel_name(channel)
        if not raw:
            return True
        low = raw.strip().lower()
        if low in _PLACEHOLDER_TITLES:
            return True
        return bool(re.match(r"^(通道|channel|ch|ipc)[-_ ]?\d*$", low))

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["channel_titles"] = {str(k): v for k, v in self.channel_titles.items()}
        d["_cache_version"] = _CACHE_VERSION
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceIdentity":
        titles = {}
        for k, v in (d.get("channel_titles") or {}).items():
            try:
                titles[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        return cls(
            machine_name=str(d.get("machine_name") or ""),
            channel_titles=titles,
            model=str(d.get("model") or ""),
            serial=str(d.get("serial") or ""),
            firmware=str(d.get("firmware") or ""),
            fetched_at=str(d.get("fetched_at") or ""),
            source=str(d.get("source") or "cache"),
        )

    def describe(self) -> str:
        return "%s / %s / %s" % (self.machine_name or "(未取到设备名)",
                                 self.model or "?", self.serial or "?")


# --------------------------------------------------------------------------- #
#  从设备读取
# --------------------------------------------------------------------------- #
def fetch_identity(host: str, username: str, password: str, http_port: int = 80,
                   channel: int = 1, timeout: float = 6.0) -> DeviceIdentity:
    """向录像机查询真实身份。失败抛异常，由调用方决定回退。"""
    auth = _DigestAuth(username, password)
    ident = DeviceIdentity(fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           source="device")

    code, body = _http_get(host, http_port,
                           "/cgi-bin/configManager.cgi?action=getConfig&name=General",
                           auth, timeout)
    if code != 200:
        raise IOError("读取设备名称失败：HTTP %s %s" % (code, body[:120]))
    kv = _parse_kv(body)
    ident.machine_name = (kv.get("table.General.MachineName") or "").strip()

    code, body = _http_get(host, http_port,
                           "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
                           auth, timeout)
    if code == 200:
        for key, val in _parse_kv(body).items():
            m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name$", key)
            if m:
                ident.channel_titles[int(m.group(1)) + 1] = val.strip()
    if not ident.channel_titles and channel:
        ident.channel_titles = {int(channel): ""}

    for path, attr in (("/cgi-bin/magicBox.cgi?action=getDeviceType", "model"),
                       ("/cgi-bin/magicBox.cgi?action=getSerialNo", "serial"),
                       ("/cgi-bin/magicBox.cgi?action=getSoftwareVersion", "firmware")):
        try:
            c2, b2 = _http_get(host, http_port, path, auth, timeout)
            if c2 == 200:
                kv2 = _parse_kv(b2)
                val = (kv2.get("type") or kv2.get("sn") or kv2.get("version") or "").strip()
                if val:
                    setattr(ident, attr, val)
        except (socket.timeout, OSError):
            break
    return ident


# --------------------------------------------------------------------------- #
#  缓存
# --------------------------------------------------------------------------- #
def load_cache(path: str) -> Optional[DeviceIdentity]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if int(data.get("_cache_version") or 0) != _CACHE_VERSION:
            return None
        ident = DeviceIdentity.from_dict(data)
        ident.source = "cache"
        return ident
    except (OSError, ValueError, TypeError):
        return None


def save_cache(path: str, ident: DeviceIdentity) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ident.to_dict(), f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _cache_age_hours(cache: DeviceIdentity) -> float:
    try:
        t = datetime.strptime(cache.fetched_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 1e9
    return (datetime.now() - t).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
#  对外主入口
# --------------------------------------------------------------------------- #
def resolve_identity(cfg: Config, logger=None, force_refresh: bool = False,
                     quiet: bool = False) -> DeviceIdentity:
    """取回录像机身份，并按配置裁剪成命名需要的形态。

    顺序：设备实时读取 → 本地缓存 → 兜底（空名字，由 Config.apply_identity 补）。
    任何一步失败都不会中断拉取。
    """
    dev = cfg.device
    cache_path = cfg.device_cache_file
    cache = load_cache(cache_path)

    def log(level, msg, *a):
        if logger is not None:
            getattr(logger, level)(msg, *a)

    need = (not dev.enabled) or force_refresh
    if not dev.enabled:
        if cache:
            if not quiet:
                log("info", "设备名称读取已关闭，使用缓存：%s（%s）",
                    cache.machine_name, cache.fetched_at)
            return cache
        if not quiet:
            log("warning", "设备名称读取已关闭，且无本地缓存 —— 将使用配置里的静态名称")
        return DeviceIdentity(source="fallback")

    if cache and not force_refresh and _cache_age_hours(cache) < max(0, dev.refresh_hours):
        if not quiet:
            log("info", "设备名称：%s（来自缓存 %s，通道%s = %s）",
                cache.machine_name, cache.fetched_at, cfg.nvr.channel,
                cache.channel_name(cfg.nvr.channel) or "未命名")
        return cache

    try:
        ident = fetch_identity(cfg.nvr.host, cfg.nvr.username, cfg.nvr.password,
                               dev.http_port, cfg.nvr.channel, dev.timeout_seconds)
        if not ident.machine_name:
            raise IOError("设备返回的 MachineName 为空")
        save_cache(cache_path, ident)
        if not quiet:
            log("info", "设备名称：%s  型号：%s  序列号：%s（已写入缓存）",
                ident.machine_name, ident.model or "?", ident.serial or "?")
        return ident
    except (socket.timeout, OSError, IOError, ValueError) as exc:
        if cache:
            if not quiet:
                log("warning", "读取设备名称失败（%s），改用本地缓存：%s（%s）",
                    exc, cache.machine_name, cache.fetched_at)
            cache.source = "cache-offline"
            return cache
        if not quiet:
            log("warning", "读取设备名称失败（%s），且无缓存，将用配置里的静态名称", exc)
        return DeviceIdentity(source="fallback")


def identity_report(cfg: Config, ident: DeviceIdentity) -> str:
    """人类可读的身份报告（用于 --device-info）。"""
    lines = []
    lines.append("设备地址 : %s:%d" % (cfg.nvr.host, cfg.device.http_port))
    lines.append("设备名称 : %s      <- 对应大华云联里的『门店/分组名称』" % (ident.machine_name or "(未取到)"))
    lines.append("设备型号 : %s" % (ident.model or "?"))
    lines.append("序列号   : %s" % (ident.serial or "?"))
    lines.append("固件版本 : %s" % (ident.firmware or "?"))
    lines.append("数据来源 : %s" % {"device": "设备实时读取",
                                    "cache": "本地缓存（有效期内）",
                                    "cache-offline": "设备未响应，使用本地缓存",
                                    "fallback": "兜底（读取失败且无缓存）"
                                    }.get(ident.source, ident.source))
    lines.append("读取时间 : %s" % (ident.fetched_at or "-"))
    lines.append("")
    lines.append("通道名称（本脚本使用第 %d 通道）：" % cfg.nvr.channel)
    for ch in sorted(ident.channel_titles):
        mark = "  <== 本项目使用" if ch == cfg.nvr.channel else ""
        flag = "   [疑似未命名]" if ident.is_placeholder_channel(ch) else ""
        lines.append("  通道%-3d %s%s%s" % (ch, ident.channel_titles[ch] or "(空)", flag, mark))
    return "\n".join(lines)
