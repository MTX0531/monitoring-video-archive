# -*- coding: utf-8 -*-
"""永久销毁（purge_file / overwrite_destroy）验证。

为什么值得单独验：监控录像是敏感素材，而本机环境会把一切删除操作
（os.remove / .NET Delete / Remove-Item）拦截后**转投回收站**——
"删除成功"的文件实际躺在 C:\\$Recycle.Bin 里可被还原。因此删除语义必须钉死：
  1. purge_file 必须**先全文件写零、再删条目**：即使被转投回收站，
     副本也只剩零字节数据，画面不可还原；
  2. 删除被拦截（抛异常）时返回 False，内容也已被销毁，且绝不抛异常
     拖垮归档主流程；
  3. overwrite_destroy 必须把内容全部写零并截断（含跨 1MB 块的大文件）；
  4. 目录、不存在路径等非法输入不得误伤。

全部离线运行，不连录像机。
用法：python tools/purge_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore import storage                                     # noqa: E402

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


def make_file(tmp, name, data):
    p = os.path.join(tmp, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *a):
        self.lines.append("W: " + (fmt % a if a else fmt))

    def error(self, fmt, *a):
        self.lines.append("E: " + (fmt % a if a else fmt))

    def info(self, fmt, *a):
        self.lines.append("I: " + (fmt % a if a else fmt))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="purge_test_")
    try:
        p = make_file(tmp, "normal.mp4", b"x" * 4096)
        check("常规文件删除成功", storage.purge_file(p), True)
        check_true("常规删除后文件不存在（永久，不进回收站）", not os.path.exists(p))

        check("不存在的路径返回 False", storage.purge_file(os.path.join(tmp, "nope.mp4")), False)

        d = os.path.join(tmp, "2026-09-17")
        os.makedirs(d, exist_ok=True)
        check("目录路径返回 False", storage.purge_file(d), False)
        check_true("目录未被误删", os.path.isdir(d))

        p = make_file(tmp, "wipe.mp4", os.urandom(256 * 1024))
        check("覆盖销毁返回 True", storage.overwrite_destroy(p), True)
        check("覆盖销毁后大小为 0", os.path.getsize(p), 0)
        with open(p, "rb") as f:
            check("覆盖销毁后内容为空", f.read(), b"")

        p = make_file(tmp, "big.mp4", os.urandom(int(2.5 * 1024 * 1024)))
        check("大文件(2.5MB)覆盖销毁返回 True", storage.overwrite_destroy(p), True)
        check("大文件覆盖销毁后大小为 0", os.path.getsize(p), 0)

        p = make_file(tmp, "empty.mp4", b"")
        check("空文件覆盖销毁返回 True", storage.overwrite_destroy(p), True)

        p = make_file(tmp, "stuck.mp4", os.urandom(64 * 1024))
        log = _Log()
        real_remove = os.remove

        def boom(path):
            raise PermissionError("simulated: delete intercepted")

        os.remove = boom
        try:
            check("remove 被拦截时 purge 返回 False", storage.purge_file(p, log), False)
        finally:
            os.remove = real_remove
        check("remove 被拦截时内容已被销毁", os.path.getsize(p), 0)
        check_true("失败时记录了日志", len(log.lines) >= 1)
        check("环境恢复后重删成功", storage.purge_file(p), True)

        # 模拟「删除被静默转投回收站」：先复制再删
        p = make_file(tmp, "trapped.mp4", os.urandom(128 * 1024))
        fakebin = os.path.join(tmp, "fakebin")
        os.makedirs(fakebin, exist_ok=True)
        calls = {"n": 0}

        def trap_remove(path):
            calls["n"] += 1
            dst = os.path.join(fakebin, os.path.basename(path))
            with open(path, "rb") as src, open(dst, "wb") as out:
                out.write(src.read())
            real_remove(path)

        os.remove = trap_remove
        try:
            check("转投回收站场景下 purge 返回 True", storage.purge_file(p), True)
            check("os.remove 恰好被调用一次", calls["n"], 1)
        finally:
            os.remove = real_remove
        bin_copy = os.path.join(fakebin, "trapped.mp4")
        check_true("回收站副本存在（转投确实发生）", os.path.exists(bin_copy))
        with open(bin_copy, "rb") as f:
            body = f.read()
        check("回收站副本为 0 字节（写零+截断先于转投）", len(body), 0)

        p = make_file(tmp, "readonly.mp4", os.urandom(16 * 1024))
        os.chmod(p, 0o444)
        log = _Log()

        def boom2(path):
            raise PermissionError("simulated: delete intercepted")

        os.remove = boom2
        try:
            got = storage.purge_file(p, log)
            if os.name == "nt":
                check("只读+拦截 → purge 返回 False", got, False)
            else:
                check_true("只读+拦截 → 不抛异常", isinstance(got, bool))
        finally:
            os.remove = real_remove
            os.chmod(p, 0o644)
            if os.path.exists(p):
                os.remove(p)

        p = make_file(tmp, "retention.mp4", b"y" * 2048)
        log = _Log()
        check("_delete_file permanent 成功", storage._delete_file(p, "permanent", log), True)
        check_true("_delete_file permanent 后文件不存在", not os.path.exists(p))

        p = make_file(tmp, "nolog.mp4", b"z" * 512)
        check("logger=None 常规删除成功", storage.purge_file(p), True)

        arch = os.path.join(tmp, "arch")
        os.makedirs(arch, exist_ok=True)
        old = time.time() - 3600
        stale = make_file(arch, ".part_stale.mp4", b"q" * 2048)
        os.utime(stale, (old, old))
        fresh = make_file(arch, ".part_live.mp4", b"r" * 128)
        log = _Log()
        check_true("过期分片可被 purge 清除", storage.purge_file(stale, log))
        check_true("未过期分片不受影响", os.path.exists(fresh))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 78)
    print("结果：%d 项通过，%d 项失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
