# -*- coding: utf-8 -*-
"""探测录像机上到底接了几路摄像头、分别是什么。

为什么要做这个
--------------------------------------------------------------------
录像机有两个容易混淆的「通道数」：

  1) **设备支持的最大通道数** —— DH-NVR4216 是 16 路。由 ChannelTitle 的条目数反映。
  2) **实际接入的摄像头数** —— 由 RemoteDevice 里 Enable=true 的条目数反映。

门店现场常见的情况是：设备满配 16 路，实际只接了 1~2 路，
其余通道**仍然保留着历史名称**，从 `--device-info` 看像是全接了，其实一个都没接。

归档脚本按**通道号**拉流，通道号必须对应真实存在的摄像头，
否则只会拿到空录像或异常状态码。所以部署新门店时应当先跑一次本模块。

读两个接口交叉判定
--------------------------------------------------------------------
    GET /cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice
        -> table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_<N>.Enable
           ...Address / ...DeviceType / ...SerialNo / ...ProtocolType …
    GET /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle
        -> table.ChannelTitle[<N>].Name

约定：**RemoteDevice 的索引 N 对应通道号 N+1**。
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import Config
from .deviceinfo import _DigestAuth, _http_get, _parse_kv
from .downloader import _md5, _recv_rtsp, _status

_PLACEHOLDER_RE = re.compile(r"^(通道|channel|ch|ipc|ipcamera|camera|cam)[-_ ]?\d*$", re.I)
_PLACEHOLDER_EXACT = {"ipc", "ipcamera", "camera", "cam", "channel", "未知"}

_PLACEHOLDER_ADDRS = {"", "0.0.0.0", "192.168.0.0", "127.0.0.1"}


@dataclass
class CameraInfo:
    """一路远程摄像头（RemoteDevice 条目）。"""
    address: str = ""
    device_type: str = ""
    serial: str = ""
    vendor: str = ""
    protocol: str = ""
    username: str = ""
    port: int = 0

    @property
    def is_placeholder(self) -> bool:
        return (self.address or "").strip() in _PLACEHOLDER_ADDRS


@dataclass
class ChannelInfo:
    """一个通道的完整画像。"""
    index: int                            # 通道号，从 1 开始
    title: str = ""                       # 通道名（ChannelTitle）
    enabled: bool = False                 # RemoteDevice 里是否启用
    camera: Optional[CameraInfo] = None   # 挂在该通道上的摄像头
    live_code: Optional[int] = None       # RTSP 实时流探测结果（None = 未探测/失败）

    @property
    def has_camera(self) -> bool:
        if not self.enabled or self.camera is None:
            return False
        return not self.camera.is_placeholder

    @property
    def streams(self) -> Optional[bool]:
        if self.live_code is None:
            return None
        return self.live_code == 200

    @property
    def title_is_placeholder(self) -> bool:
        raw = (self.title or "").strip()
        if not raw:
            return True
        return raw.lower() in _PLACEHOLDER_EXACT or bool(_PLACEHOLDER_RE.match(raw))

    @property
    def camera_ip(self) -> str:
        return (self.camera.address if self.camera else "") or ""


@dataclass
class ChannelMap:
    """整机通道清单。"""
    host: str = ""
    machine_name: str = ""
    model: str = ""
    serial: str = ""
    total: int = 0                                     # 设备支持的通道数
    channels: List[ChannelInfo] = field(default_factory=list)
    source: str = "device"                             # device | title-only | fallback
    probed: bool = False                               # 是否做了 RTSP 实时流探测
    error: str = ""

    @property
    def active(self) -> List[ChannelInfo]:
        return [c for c in self.channels if c.has_camera]

    @property
    def streaming(self) -> List[ChannelInfo]:
        return [c for c in self.channels if c.streams]

    @property
    def idle(self) -> List[ChannelInfo]:
        return [c for c in self.channels if not c.has_camera]

    def summary(self) -> str:
        return ("设备支持 %d 路，实际接入 %d 路%s"
                % (self.total, len(self.active),
                   "（RTSP 实测有流 %d 路）" % len(self.streaming) if self.probed else ""))


# --------------------------------------------------------------------------- #
#  从设备读取
# --------------------------------------------------------------------------- #
def _parse_remote_device(body: str) -> Tuple[Dict[int, CameraInfo], Dict[int, bool]]:
    out: Dict[int, CameraInfo] = {}
    enabled_flags: Dict[int, bool] = {}
    for key, val in _parse_kv(body).items():
        m = re.search(r"NETCAMERA_INFO_(\d+)\.(\w+)$", key)
        if not m:
            continue
        idx, attr, v = int(m.group(1)), m.group(2), (val or "").strip()
        cam = out.setdefault(idx, CameraInfo())
        if attr == "Enable":
            enabled_flags[idx] = v.lower() == "true"
        elif attr == "Address":
            cam.address = v
        elif attr == "DeviceType":
            cam.device_type = v
        elif attr == "SerialNo":
            cam.serial = v
        elif attr == "Vendor":
            cam.vendor = v
        elif attr == "ProtocolType":
            cam.protocol = v
        elif attr == "UserName":
            cam.username = v
        elif attr == "Port":
            try:
                cam.port = int(v)
            except ValueError:
                cam.port = 0
    return out, enabled_flags


def _parse_channel_titles(body: str) -> Dict[int, str]:
    titles: Dict[int, str] = {}
    for key, val in _parse_kv(body).items():
        m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name$", key)
        if m:
            titles[int(m.group(1)) + 1] = (val or "").strip()
    return titles


def fetch_channel_map(host: str, username: str, password: str, http_port: int = 80,
                      channel: int = 1, timeout: float = 6.0) -> ChannelMap:
    """读取整机通道清单。失败抛异常，由调用方决定回退。"""
    auth = _DigestAuth(username, password)
    cmap = ChannelMap(host=host)

    code, body = _http_get(
        host, http_port,
        "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle", auth, timeout)
    if code != 200:
        raise IOError("读取通道名失败：HTTP %s %s" % (code, body[:120]))
    titles = _parse_channel_titles(body)
    if not titles and channel:
        titles = {int(channel): ""}
    cmap.total = max(titles) if titles else 0

    cameras: Dict[int, CameraInfo] = {}
    enabled: Dict[int, bool] = {}
    code, body = _http_get(
        host, http_port,
        "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice", auth, timeout)
    if code == 200:
        cameras, enabled = _parse_remote_device(body)
        cmap.source = "device"
    else:
        cmap.source = "title-only"

    for ch in sorted(titles):
        cam = cameras.get(ch - 1)
        cmap.channels.append(ChannelInfo(
            index=ch,
            title=titles[ch],
            enabled=bool(enabled.get(ch - 1, False)),
            camera=cam))

    try:
        from .deviceinfo import fetch_identity
        ident = fetch_identity(host, username, password, http_port, channel, timeout)
        cmap.machine_name = ident.machine_name
        cmap.model = ident.model
        cmap.serial = ident.serial
    except Exception:      # noqa: BLE001
        pass
    return cmap


# --------------------------------------------------------------------------- #
#  RTSP 实时流探测（交叉验证）
# --------------------------------------------------------------------------- #
def probe_live_stream(host: str, port: int, username: str, password: str,
                      channel: int, subtype: int = 0, timeout: float = 6.0
                      ) -> Optional[int]:
    """请求某通道的 RTSP 实时流，返回状态码（200=有流，None=失败/超时）。"""
    path = "/cam/realmonitor?channel=%d&subtype=%d" % (channel, subtype)
    url = "rtsp://%s:%s@%s:%d%s" % (username, password, host, port, path)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    sock.settimeout(timeout)
    try:
        sock.sendall(("DESCRIBE %s RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n"
                      "User-Agent: nvr-puller/1.0\r\n\r\n" % url).encode())
        head, _ = _recv_rtsp(sock)
        code = _status(head)
        if code != 401:
            return code
        ch = re.search(r"WWW-Authenticate:\s*(.+?)(?:\r\n|$)", head, re.I)
        if not ch:
            return None
        realm = re.search(r'realm="([^"]*)"', ch.group(1))
        nonce = re.search(r'nonce="([^"]*)"', ch.group(1))
        if not realm or not nonce:
            return None
        ha1 = _md5("%s:%s:%s" % (username, realm.group(1), password))
        ha2 = _md5("DESCRIBE:%s" % path)
        response = _md5("%s:%s:%s" % (ha1, nonce.group(1), ha2))
        sock.sendall(("DESCRIBE %s RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
                      "User-Agent: nvr-puller/1.0\r\n"
                      'Authorization: Digest username="%s", realm="%s", nonce="%s", '
                      'uri="%s", response="%s"\r\n\r\n'
                      % (url, username, realm.group(1), nonce.group(1), path,
                         response)).encode())
        head2, _ = _recv_rtsp(sock)
        return _status(head2)
    except (OSError, ValueError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def probe_channels(cfg: Config, cmap: ChannelMap, logger=None,
                   only_active: bool = True) -> None:
    """就地补上每个通道的 RTSP 实测结果。"""
    targets = cmap.active if only_active else cmap.channels
    if not targets:
        targets = cmap.channels
    for ch in targets:
        code = probe_live_stream(cfg.nvr.host, cfg.nvr.rtsp_port,
                                 cfg.nvr.username, cfg.nvr.password,
                                 ch.index, cfg.nvr.subtype,
                                 timeout=max(3.0, cfg.device.timeout_seconds))
        ch.live_code = code
        if logger is not None:
            logger.debug("通道 %d 实时流探测 -> %s", ch.index, code)
    cmap.probed = True


# --------------------------------------------------------------------------- #
#  报告
# --------------------------------------------------------------------------- #
_CODE_TEXT = {200: "有流", 401: "认证失败", 403: "无权限",
              404: "无此通道", 500: "服务端拒绝", 503: "服务不可用"}


def _disp_width(s: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in (s or ""))


def _pad(s: str, width: int) -> str:
    s = s or ""
    return s + " " * max(0, width - _disp_width(s))


def describe_channels(cfg: Config, cmap: ChannelMap) -> str:
    """人类可读的通道清单（用于 --channels）。"""
    L: List[str] = []
    L.append("=" * 78)
    L.append("通道清单（录像机 %s）" % cmap.host)
    L.append("=" * 78)
    L.append("设备名称 : %s" % (cmap.machine_name or "(未取到)"))
    L.append("设备型号 : %s" % (cmap.model or "?"))
    L.append("序列号   : %s" % (cmap.serial or "?"))
    L.append("设备支持 : %d 路" % cmap.total)
    L.append("实际接入 : %d 路" % len(cmap.active))
    if cmap.source == "title-only":
        L.append("提示     : 本机 RemoteDevice 接口不可用，接入判定可能不准确，"
                 "建议加 --probe 用实时流复核")
    if cmap.error:
        L.append("错误     : %s" % cmap.error)
    L.append("")

    if cmap.active:
        L.append("【已接入摄像头】")
        head = ("  " + _pad("通道", 6) + _pad("通道名", 16) + _pad("摄像头 IP", 18)
                + _pad("型号", 20) + _pad("序列号", 17) + "实时流")
        L.append(head)
        L.append("  " + "-" * (_disp_width(head) - 2))
        for ch in cmap.active:
            cam = ch.camera
            live = ("未探测" if ch.live_code is None
                    else "%s %s" % (ch.live_code, _CODE_TEXT.get(ch.live_code, "")))
            row = ("  " + _pad(str(ch.index), 6) + _pad(ch.title or "(空)", 16)
                   + _pad(ch.camera_ip or "-", 18)
                   + _pad((cam.device_type if cam else "") or "?", 20)
                   + _pad((cam.serial if cam else "") or "?", 17) + live)
            mark = ""
            if ch.index == cfg.nvr.channel:
                mark = "   <== 本项目使用"
            if ch.title_is_placeholder:
                mark += "   [通道名仍是默认值]"
            L.append(row + mark)
        L.append("")
    else:
        L.append("【已接入摄像头】无")
        L.append("")

    if cmap.idle:
        L.append("【未接入通道 %d 路】" % len(cmap.idle))
        L.append("  这些通道在配置里保留了名称，但没有挂摄像头，拉流只会拿到空录像：")

        def _tag(c: ChannelInfo) -> str:
            if not c.title:
                return "无名称"
            return "默认名" if c.title_is_placeholder else "历史名称"

        items = ["%d %s(%s)" % (c.index, c.title or "-", _tag(c)) for c in cmap.idle]
        line = "  "
        for it in items:
            if _disp_width(line) + _disp_width(it) + 3 > 78:
                L.append(line.rstrip(" /"))
                line = "  "
            line += it + " / "
        if line.strip():
            L.append(line.rstrip(" /"))
        L.append("")
        L.append("  说明：未接入通道上没有摄像头，拉取会返回空录像或超时；")
        L.append("       归档脚本配置项 nvr.channel 必须指向【已接入摄像头】里的通道号。")
        L.append("")

    if cmap.probed and cmap.active and not cmap.streaming:
        L.append("【警告】配置显示有摄像头，但 RTSP 实测全部取不到流 —— ")
        L.append("       摄像头可能离线、IP 冲突，或账号缺少实时预览权限。")
        L.append("")
    elif cmap.probed:
        for ch in cmap.active:
            if ch.streams is False:
                L.append("【警告】通道 %d 配置有摄像头（%s），但 RTSP 实时流取不到 —— "
                         "可能离线或权限不足。" % (ch.index, ch.camera_ip))
        if any(c.streams is False for c in cmap.active):
            L.append("")

    L.append("-" * 78)
    L.append("当前配置使用通道 %d（%s）"
             % (cfg.nvr.channel,
                next((c.title for c in cmap.channels if c.index == cfg.nvr.channel),
                     "不存在")))
    if not any(c.index == cfg.nvr.channel and c.has_camera for c in cmap.active):
        L.append("【注意】通道 %d 不在【已接入摄像头】列表中 —— "
                 "归档脚本将拉不到有效录像，请核对 nvr.channel 配置。"
                 % cfg.nvr.channel)
    return "\n".join(L)


def describe_brief(cmap: ChannelMap) -> str:
    return cmap.summary()
