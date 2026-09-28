# -*- coding: utf-8 -*-
"""探测录像机自身记录的名称信息。

思路：大华录像机在本地配置里就保存了
  - 设备名称（通用配置 General.MachineName）—— 大华云联里的"录像机名称/分组名称"
  - 通道名称（ChannelTitle[].Name）—— 每个摄像头在录像机里设定的名字
这些才是命名的权威来源，不该由人填。

用法：python tools/device_identity_probe.py [ip] [user] [pwd]
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import sys
import urllib.request
from urllib.error import HTTPError

DEFAULT_IP = "192.168.2.10"
DEFAULT_USER = "admin"
DEFAULT_PWD = "<NVR_PASSWORD>"


# --------------------------------------------------------------------------- #
class DigestAuth:
    def __init__(self, user, pwd):
        self.user = user
        self.pwd = pwd
        self.nc = 0

    @staticmethod
    def parse_challenge(header: str) -> dict:
        out = {}
        for key in ("realm", "nonce", "qop", "opaque", "algorithm"):
            m = re.search(key + r'=(?:"([^"]*)"|([^,\s]+))', header, re.I)
            if m:
                out[key] = m.group(1) if m.group(1) is not None else m.group(2)
        return out

    def make_header(self, method: str, uri: str, ch: dict) -> str:
        realm = ch.get("realm", "")
        nonce = ch.get("nonce", "")
        qop = ch.get("qop", "")
        opaque = ch.get("opaque", "")
        algo = ch.get("algorithm", "MD5")

        def H(text):
            data = text.encode("utf-8")
            if algo.upper().startswith("SHA-256"):
                return hashlib.sha256(data).hexdigest()
            return hashlib.md5(data).hexdigest()

        ha1 = H("%s:%s:%s" % (self.user, realm, self.pwd))
        ha2 = H("%s:%s" % (method, uri))
        parts = ['username="%s"' % self.user, 'realm="%s"' % realm,
                 'nonce="%s"' % nonce, 'uri="%s"' % uri]
        if qop:
            self.nc += 1
            nc = "%08x" % self.nc
            cnonce = base64.b16encode(os.urandom(8)).decode()
            resp = H("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2))
            parts += ['qop=%s' % qop, "nc=%s" % nc, 'cnonce="%s"' % cnonce,
                      'response="%s"' % resp]
        else:
            parts.append('response="%s"' % H("%s:%s:%s" % (ha1, nonce, ha2)))
        if opaque:
            parts.append('opaque="%s"' % opaque)
        if algo:
            parts.append("algorithm=%s" % algo)
        return "Digest " + ", ".join(parts)


def http_get(ip: str, path: str, auth: DigestAuth, timeout: float = 8.0):
    """带 Digest 认证的 GET。先请求一次拿 401 挑战，再带 Authorization 重发。"""
    url = "http://%s%s" % (ip, path)
    req = urllib.request.Request(url, headers={"User-Agent": "nvr-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        if e.code != 401:
            return e.code, e.read().decode("utf-8", "replace")
        ch = DigestAuth.parse_challenge(e.headers.get("WWW-Authenticate", ""))
        if not ch:
            return e.code, "no WWW-Authenticate header"
    req2 = urllib.request.Request(
        url, headers={"User-Agent": "nvr-probe",
                      "Authorization": auth.make_header("GET", path, ch)})
    try:
        with urllib.request.urlopen(req2, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
def parse_kv_table(text: str) -> dict:
    """解析 configManager 返回的 table.xxx.yyy=zzz 形式。"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IP
    user = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_USER
    pwd = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PWD
    auth = DigestAuth(user, pwd)

    print("=" * 76)
    print("录像机身份探测  %s" % ip)
    print("=" * 76)

    print("\n[1] 设备信息（magicBox / deviceType）")
    for path in ("/cgi-bin/magicBox.cgi?action=getDeviceType",
                 "/cgi-bin/magicBox.cgi?action=getSystemInfo",
                 "/cgi-bin/magicBox.cgi?action=getSerialNo",
                 "/cgi-bin/magicBox.cgi?action=getHardwareVersion",
                 "/cgi-bin/magicBox.cgi?action=getSoftwareVersion"):
        try:
            code, body = http_get(ip, path, auth)
        except (socket.timeout, OSError) as exc:
            print("  %-56s 连接失败: %s" % (path, exc))
            continue
        body = body.strip().replace("\r\n", " | ")
        print("  %-56s [%s] %s" % (path.split("action=")[-1], code, body[:160]))

    print("\n[2] 设备名称 —— 这是命名要用的『门店/录像机名称』")
    for name in ("General", "general"):
        try:
            code, body = http_get(
                ip, "/cgi-bin/configManager.cgi?action=getConfig&name=%s" % name, auth)
        except (socket.timeout, OSError) as exc:
            print("  [%s] 连接失败: %s" % (name, exc))
            continue
        kv = parse_kv_table(body)
        hits = {k: v for k, v in kv.items()
                if any(s in k.lower() for s in ("machinename", "devicename",
                                                "name", "locate", "machinegroup"))}
        print("  name=%-8s [%s] 共 %d 项" % (name, code, len(kv)))
        for k, v in sorted(hits.items()):
            print("      %s = %s" % (k, v))

    print("\n[3] 通道名称 —— 这是命名要用的『通道名称』")
    for name in ("ChannelTitle", "ChannelTitle[0]", "Camera"):
        try:
            code, body = http_get(
                ip, "/cgi-bin/configManager.cgi?action=getConfig&name=%s" % name, auth)
        except (socket.timeout, OSError) as exc:
            print("  [%s] 连接失败: %s" % (name, exc))
            continue
        kv = parse_kv_table(body)
        titles = {k: v for k, v in kv.items() if re.search(r"\.Name$", k, re.I)}
        print("  name=%-16s [%s] 共 %d 项，其中名称 %d 项" % (name, code, len(kv), len(titles)))
        for k, v in sorted(titles.items()):
            print("      %s = %s" % (k, v))

    print("\n[4] 备用通道：RPC2 取 General 配置")
    try:
        code, body = http_get(ip, "/RPC2_Login", auth)
        print("  RPC2_Login [%s] %s" % (code, body.strip()[:200]))
    except (socket.timeout, OSError) as exc:
        print("  RPC2_Login 连接失败: %s" % exc)

    print("\n" + "=" * 76)


if __name__ == "__main__":
    main()
