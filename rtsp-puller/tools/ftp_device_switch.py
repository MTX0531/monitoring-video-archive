# -*- coding: utf-8 -*-
"""开关录像机的『录像 FTP 上传』。

为什么要单独一个工具
--------------------------------------------------------------------
接收端（tools/ftp_receiver.py）在**收工**时会自己关掉设备端开关，但它
**不负责开**——开机是"点火"动作，属于调度层的事。定时任务先调本工具把
设备端打开，再起接收端；接收端空闲收工后自己关掉。这样：

    定时点火 → 设备开始推 → 接收端收 + 转码 → 连续空闲 → 收工并关设备

与 curl 拼 URL 的区别：这件工具会**回读确认**。大华这类 CGI 返回 200
但配置没生效的情况并不罕见，只看状态码会让人以为"已经开了"。

用法：
    python tools/ftp_device_switch.py --status     # 只看当前开关（只读）
    python tools/ftp_device_switch.py --on         # 打开（并回读确认）
    python tools/ftp_device_switch.py --off        # 关闭（并回读确认）
    python tools/ftp_device_switch.py --on --no-verify   # 跳过回读（不建议）

退出码：0 成功（含回读一致）| 1 失败（未生效 / 设备不可达）| 2 参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import importlib.util                                        # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ftp_receiver", os.path.join(BASE_DIR, "tools", "ftp_receiver.py"))
ftp_receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ftp_receiver)

from nvrcore.config import load_config                        # noqa: E402


def load_device_creds(path=None, base_dir=BASE_DIR):
    """读 nvr 段（host / 账号 / 密码 / 设备 HTTP 端口）。"""
    return load_config(path, base_dir=base_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(description="开关录像机的录像 FTP 上传")
    ap.add_argument("--on", action="store_true", help="打开设备端上传")
    ap.add_argument("--off", action="store_true", help="关闭设备端上传")
    ap.add_argument("--status", action="store_true", help="只读：打印当前开关状态")
    ap.add_argument("--no-verify", action="store_true",
                    help="不做回读确认（只发命令，不看结果）")
    ap.add_argument("--config", default=None, help="配置文件路径，默认 config.json")
    ap.add_argument("--base-dir", default=BASE_DIR)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    args = ap.parse_args(argv)

    if sum([bool(args.on), bool(args.off), bool(args.status)]) != 1:
        print("请且仅请指定 --status / --on / --off 之一")
        return 2

    base = os.path.abspath(args.base_dir)
    proj = load_device_creds(args.config, base)
    dev = proj.nvr

    if args.status:
        info = ftp_receiver.read_nas_switch(proj)
        if args.json:
            print(json.dumps({"enable": info.get("enable"),
                              "code": info.get("code"),
                              "error": info.get("error")},
                             ensure_ascii=False))
        else:
            print("设备        : %s" % dev.host)
            if info.get("enable") is None:
                print("FTP 上传开关 : 未读到（%s）" % (info.get("error") or "无响应"))
            else:
                print("FTP 上传开关 : %s" % ("已打开 (Enable=true)"
                                            if info["enable"] else
                                            "已关闭 (Enable=false)"))
        return 0 if info.get("enable") is not None else 1

    want = bool(args.on)
    if args.no_verify:
        from nvrcore.deviceinfo import _DigestAuth, _http_get
        auth = _DigestAuth(dev.username, dev.password)
        path = ("/cgi-bin/configManager.cgi?action=setConfig&NAS[0].Enable="
                + ("true" if want else "false"))
        code, _body = _http_get(dev.host, proj.device.http_port, path, auth,
                                proj.device.timeout_seconds)
        print("已发送 setConfig（HTTP %s），未回读确认" % code)
        return 0 if code == 200 else 1

    res = ftp_receiver.set_nas_switch(proj, want)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print("设备        : %s" % dev.host)
        print("目标状态    : %s" % ("打开" if want else "关闭"))
        if res["ok"]:
            print("结果        : 已生效（回读 Enable=%s）"
                  % ("true" if res["readback"] else "false"))
        else:
            print("结果        : 未生效 —— %s（回读=%s）"
                  % (res.get("detail") or "未知", res.get("readback")))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
