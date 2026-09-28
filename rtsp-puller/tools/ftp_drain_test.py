# -*- coding: utf-8 -*-
"""补入队脚本（tools/ftp_drain.py）验证。

核心契约两条：
  1. 输出必须落在 `archive` 段决定的归档根（与 RTSP 同一个），
     **绝不能出现在收件箱里**；
  2. 正在上传的文件不能动（设备边转边传，半截文件入库 = 发不出去的成品 + 原件被删）。

用真实 ffmpeg 造短视频端到端跑，不连录像机、不占端口。
用法：python tools/ftp_drain_test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore.config import load_config                     # noqa: E402
from nvrcore.downloader import find_ffmpeg                 # noqa: E402
from nvrcore.storage import parse_segment_name             # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def _load_drain():
    spec = importlib.util.spec_from_file_location(
        "ftp_drain", os.path.join(BASE, "tools", "ftp_drain.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def skip(label, why):
    global SKIP
    SKIP += 1
    print("  [SKIP] %s  (%s)" % (label, why))


# --------------------------------------------------------------------------- #
#  1) 待处理筛选：不许碰正在上传的文件
# --------------------------------------------------------------------------- #
def test_collect_pending(drain):
    print("\n[1] 待处理筛选（正在上传的文件必须跳过）")
    tmp = tempfile.mkdtemp(prefix="wb_drainpick_")
    try:
        inbox = os.path.join(tmp, "ftp-inbox", "192.168.2.10", "2026-09-17")
        os.makedirs(inbox)
        now = time.time()

        old = os.path.join(inbox, "old_20260917093000.dav")
        fresh = os.path.join(inbox, "fresh_20260917100000.dav")
        tmpp = os.path.join(inbox, ".ingest-tmp_deadbeef.mp4")
        for p in (old, fresh, tmpp):
            with open(p, "wb") as f:
                f.write(b"x" * 64)
        os.utime(old, (now - 600, now - 600))
        os.utime(tmpp, (now - 600, now - 600))

        ready, writing = drain.collect_pending(os.path.join(tmp, "ftp-inbox"),
                                               min_age=180.0, now=now)
        names = [os.path.basename(p) for p in ready]
        wnames = [os.path.basename(p) for p in writing]
        check("静默够久的文件 -> 可处理", names, ["old_20260917093000.dav"])
        check("刚落盘的文件 -> 视为还在上传", wnames, ["fresh_20260917100000.dav"])
        check_true("入库临时文件不出现",
                   "tmp" not in " ".join(names + wnames))
        check("默认静默阈值是正数", drain.DEFAULT_MIN_AGE > 0, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  2) 端到端：积压文件必须落到归档根，不能落在收件箱
# --------------------------------------------------------------------------- #
def make_config(tmp, root, ffmpeg=None):
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "nvr": {"host": "127.0.0.1", "channel": 1, "stream": "main",
                    "transport": "tcp"},
            "device": {"enabled": False},
            "runtime": {"ffmpeg_path": ffmpeg} if ffmpeg else {},
            "archive": {
                "root_dir": root,
                "store_name": "测试门店",
                "channel_name": "通道1",
                "segment_minutes": 30,
                "container": "mp4",
                "organize_by_date": True,
                "verify_playable": True,
            },
            "ftp": {"root": "ftp-inbox", "ingest": True},
        }, f, ensure_ascii=False)
    return load_config(None, base_dir=tmp)


def make_video(ffmpeg, path, seconds=3):
    tmp_out = path + ".gen.mp4"
    proc = subprocess.run([
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", tmp_out,
    ], capture_output=True, timeout=180)
    if not (os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024):
        return False
    os.replace(tmp_out, path)
    return True


def _index_rows(tmp):
    p = os.path.join(tmp, "state", "index.jsonl")
    if not os.path.isfile(p):
        return []
    return [json.loads(l) for l in
            open(p, encoding="utf-8").read().splitlines() if l.strip()]


def _files_under(path):
    return [os.path.join(dp, f) for dp, dn, fn in os.walk(path) for f in fn]


def test_drain_end_to_end(drain, ffmpeg):
    print("\n[2] 端到端：积压 -> 归档根（不是收件箱）")
    tmp = tempfile.mkdtemp(prefix="wb_drain_")
    try:
        root = os.path.join(tmp, "archive")
        os.makedirs(root)
        make_config(tmp, root, ffmpeg)

        dev_dir = os.path.join(tmp, "ftp-inbox", "192.168.2.10", "2026-09-17")
        os.makedirs(dev_dir)
        src = os.path.join(dev_dir, "测试店_ch1_main_20260917093000_20260917095959.dav")
        if not make_video(ffmpeg, src):
            skip("端到端补入队", "本机 ffmpeg 无法生成测试视频")
            return
        old = time.time() - 900
        os.utime(src, (old, old))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            drain.main(["--dry-run"], base=tmp)
        check_true("dry-run 不搬文件", os.path.isfile(src))
        check_true("dry-run 提示了输出根",
                   "输出根" in buf.getvalue(), buf.getvalue()[:120])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = drain.main([], base=tmp)
        out = buf.getvalue()
        check("补入队退出码 0", rc, 0)

        expect_dir = os.path.join(root, "2026-09-17")
        expect = os.path.join(
            expect_dir, "测试门店_通道1_20260917093000_20260917095959_device.mp4")
        check_true("成品落在归档根的日期目录下", os.path.isfile(expect), expect)
        check_true("归档目录里只有这一个成品",
                   sorted(os.listdir(expect_dir)) == [os.path.basename(expect)],
                   str(os.listdir(expect_dir)))

        inbox = os.path.join(tmp, "ftp-inbox")
        left = _files_under(inbox)
        check("收件箱已清空", left, [])
        check_true("输出根不是收件箱",
                   not os.path.abspath(expect).startswith(os.path.abspath(inbox)),
                   expect)

        rows = _index_rows(tmp)
        check("台账恰好 1 条", len(rows), 1)
        if rows:
            check("台账来源标记 ftp", rows[0].get("source"), "ftp")
            check("台账记的是归档根路径",
                  os.path.abspath(rows[0].get("file", "")), os.path.abspath(expect))
        check_true("产出名字能被下游解析规则认出",
                   parse_segment_name(os.path.basename(expect)) is not None)
        check_true("控制台打出了输出根",
                   "输出根" in out and root in out)

        with contextlib.redirect_stdout(io.StringIO()):
            drain.main([], base=tmp)
        check("重复补入队不产生重复台账", len(_index_rows(tmp)), 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  3) 还在上传的文件必须原地不动
# --------------------------------------------------------------------------- #
def test_skip_uploading(drain, ffmpeg):
    print("\n[3] 还在上传的文件必须原地不动（防半截入库）")
    tmp = tempfile.mkdtemp(prefix="wb_drainup_")
    try:
        root = os.path.join(tmp, "archive")
        os.makedirs(root)
        make_config(tmp, root, ffmpeg)
        dev_dir = os.path.join(tmp, "ftp-inbox", "192.168.2.10", "2026-09-17")
        os.makedirs(dev_dir)
        src = os.path.join(dev_dir, "ch1_20260917110000_20260917112959.dav")
        if not make_video(ffmpeg, src):
            skip("跳过上传中文件", "本机 ffmpeg 无法生成测试视频")
            return
        now = time.time()
        os.utime(src, (now, now))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            drain.main([], base=tmp)
        check_true("文件仍留在收件箱", os.path.isfile(src))
        check("没有产生台账", _index_rows(tmp), [])
        check_true("控制台明说有几个还在写",
                   "还在写" in buf.getvalue(), buf.getvalue()[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #

def main():
    print("=" * 68)
    print("FTP 补入队（ftp_drain）回归")
    print("=" * 68)

    drain = _load_drain()
    test_collect_pending(drain)

    try:
        ffmpeg = find_ffmpeg(load_config(None, base_dir=BASE))
    except Exception as exc:                       # noqa: BLE001
        ffmpeg = None
        print("\n找不到 ffmpeg：%r" % exc)

    if not ffmpeg:
        skip("端到端补入队", "本机没有可用的 ffmpeg")
        skip("跳过上传中文件", "同上")
    else:
        print("\n使用 ffmpeg：%s" % ffmpeg)
        test_drain_end_to_end(drain, ffmpeg)
        test_skip_uploading(drain, ffmpeg)

    print("\n" + "=" * 68)
    print("通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
