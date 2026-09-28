# -*- coding: utf-8 -*-
"""设备端『录像 FTP 上传』开关工具（tools/ftp_device_switch.py）验证。

为什么值得单独验：这是**会改设备配置**的操作，而且它被定时任务在无人值守
时调用。写错了的后果不是"少收几个文件"，而是"把录像机的上传开关搞成
一个谁也不知道的状态"。所以：

  1. 参数必须互斥且必选（--status / --on / --off 只能给一个）
  2. --status 必须**只读**，绝不能顺手改设备配置
  3. 目标状态要正确映射成 Enable=true/false
  4. 设备不可达时明确失败（退出码 1），不能假装成功
  5. 回读不一致时必须判为失败

全部用假设备（本地关闭端口 / 桩函数），不碰真录像机。
用法：python tools/ftp_device_switch_test.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_spec = importlib.util.spec_from_file_location(
    "ftp_device_switch", os.path.join(BASE, "tools", "ftp_device_switch.py"))
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望=%r\n         实得=%r" % (label, want, got))


def check_true(label, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (label, ("  (%s)" % note) if note else ""))


class _FakeProj:
    """冒充 nvrcore.config.Config：指向一个必然连不上的端口。"""

    class nvr:
        host = "127.0.0.1"
        username = "admin"
        password = "x"

    class device:
        http_port = 9                      # discard 端口，连接必失败
        timeout_seconds = 0.4


def _run(argv, proj=None):
    """跑一次 main()，返回 (退出码, stdout 文本)。"""
    old = sw.load_device_creds
    if proj is not None:
        sw.load_device_creds = lambda *_a, **_k: proj
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = sw.main(argv)
    finally:
        sw.load_device_creds = old
    return rc, buf.getvalue()


def test_arg_validation():
    print("\n[1] 参数校验")
    rc, out = _run(["--on", "--off"])
    check("同时给 --on 和 --off 时退出码 2", rc, 2)
    check_true("提示了要选一个", "之一" in out)

    rc, out = _run(["--status", "--off"])
    check("同时给 --status 和 --off 时退出码 2", rc, 2)

    rc, out = _run([])
    check("什么都不给时退出码 2", rc, 2)


def test_status_is_read_only():
    print("\n[2] --status 必须只读")
    rc, out = _run(["--status"], proj=_FakeProj)
    check("设备不可达时 --status 退出码 1", rc, 1)
    check_true("文案说明未读到开关", "未读到" in out)

    rc2, out2 = _run(["--status", "--json"], proj=_FakeProj)
    check("--json 模式同样退出码 1", rc2, 1)
    check_true("--json 输出可被解析", out2.strip().startswith("{"))


def test_switch_target_mapping():
    print("\n[3] 目标状态映射与失败判定")
    seen = {}

    def fake_set(proj, enable, timeout=6.0):
        seen["enable"] = enable
        return {"ok": True, "want": bool(enable), "readback": bool(enable),
                "detail": ""}

    old = sw.ftp_receiver.set_nas_switch
    sw.ftp_receiver.set_nas_switch = fake_set
    try:
        rc, _ = _run(["--on"], proj=_FakeProj)
        check("--on 调用时 enable=True", seen.get("enable"), True)
        check("--on 生效时退出码 0", rc, 0)

        rc, _ = _run(["--off"], proj=_FakeProj)
        check("--off 调用时 enable=False", seen.get("enable"), False)
        check("--off 生效时退出码 0", rc, 0)

        def fake_set_bad(proj, enable, timeout=6.0):
            return {"ok": False, "want": bool(enable), "readback": not enable,
                    "detail": "回读值未生效"}
        sw.ftp_receiver.set_nas_switch = fake_set_bad
        rc, out = _run(["--on"], proj=_FakeProj)
        check("回读不一致时退出码 1", rc, 1)
        check_true("文案点明未生效", "未生效" in out)
    finally:
        sw.ftp_receiver.set_nas_switch = old


def test_unreachable_device_never_reports_success():
    print("\n[4] 设备不可达时绝不报成功")
    try:
        rc, _out = _run(["--on"], proj=_FakeProj)
        check("不可达时 --on 退出码 1", rc, 1)
    except Exception as e:                                    # noqa: BLE001
        check_true("不可达时 --on 不抛异常", False, repr(e))

    try:
        rc, _out = _run(["--off"], proj=_FakeProj)
        check("不可达时 --off 退出码 1", rc, 1)
    except Exception as e:                                    # noqa: BLE001
        check_true("不可达时 --off 不抛异常", False, repr(e))


def main():
    print("=" * 68)
    print("设备端 FTP 上传开关工具回归")
    print("=" * 68)
    test_arg_validation()
    test_status_is_read_only()
    test_switch_target_mapping()
    test_unreachable_device_never_reports_success()
    print("\n" + "=" * 68)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
