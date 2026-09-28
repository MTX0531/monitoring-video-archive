# -*- coding: utf-8 -*-
"""FTP 入库（nvrcore/ftp_ingest.py）验证。

核心契约有三条：
  1. 产出的文件名必须能被 `storage.parse_segment_name()` 解析
  2. 先临时文件、校验通过才 rename 成正式名
  3. 目标已存在就跳过，天然幂等

用真实 ffmpeg 生成短视频跑端到端，不连录像机。
用法：python tools/ftp_ingest_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore import ftp_ingest                                 # noqa: E402
from nvrcore.config import load_config                         # noqa: E402
from nvrcore.downloader import find_ffmpeg                     # noqa: E402
from nvrcore.storage import parse_segment_name, SEGMENT_RE     # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


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


class NullLogger:
    def __init__(self):
        self.lines = []

    def _rec(self, kind, msg, args):
        self.lines.append("%s %s" % (kind, (msg % args) if args else msg))

    def debug(self, msg, *a, **k):
        pass

    def info(self, msg, *a, **k):
        self._rec("INFO", msg, a)

    def warning(self, msg, *a, **k):
        self._rec("WARN", msg, a)

    def error(self, msg, *a, **k):
        self._rec("ERROR", msg, a)

    def text(self):
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- #
def test_parse_name():
    print("\n[1] 设备推来的文件名解析")
    p = ftp_ingest.parse_pushed_name

    r = p("20260917093000.dav")
    check("纯 14 位时间戳 -> 起始", r.get("start"),
          datetime(2026, 9, 17, 9, 30, 0))
    check("纯时间戳不臆造结束时间", "end" in r, False)

    r = p("ch1_20260917093000_20260917095959.dav")
    check("通道_起_止 -> 起始", r.get("start"), datetime(2026, 9, 17, 9, 30, 0))
    check("通道_起_止 -> 结束", r.get("end"), datetime(2026, 9, 17, 9, 59, 59))
    check("通道_起_止 -> 通道号", r.get("channel"), 1)

    r = p("01_20260917093000.dav")
    check("01_ 前缀 -> 通道号", r.get("channel"), 1)

    r = p("20260917_093000.dav")
    check("下划线日期时间 -> 起始", r.get("start"),
          datetime(2026, 9, 17, 9, 30, 0))

    r = p("20260917-093000.mp4")
    check("短横线日期时间 -> 起始", r.get("start"),
          datetime(2026, 9, 17, 9, 30, 0))

    r = p("channel12_20260917093000_20260917095959.dav")
    check("channelNN -> 通道号", r.get("channel"), 12)

    check("认不出来的名字返回空", p("garbage.dav"), {})
    check("空名字不炸", p(""), {})

    r = p("20260917093000_20260917090000.dav")
    check("结束早于起始时丢弃 end", r.get("end"), None)


# --------------------------------------------------------------------------- #
def test_tmp_prefix():
    print("\n[2] 临时文件前缀的安全约束")
    pref = ftp_ingest.INGEST_TMP_PREFIX
    check_true("前缀以点开头（不会被当成归档案）", pref.startswith("."))
    check_true("前缀**不是** .part_", not pref.startswith(".part_"))


# --------------------------------------------------------------------------- #
def make_config(tmp, root):
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "nvr": {"host": "127.0.0.1", "channel": 1, "stream": "main",
                    "transport": "tcp"},
            "device": {"enabled": False},
            "archive": {
                "root_dir": root,
                "store_name": "测试门店",
                "channel_name": "通道1",
                "segment_minutes": 30,
                "container": "mp4",
                "organize_by_date": True,
                "verify_playable": True,
            },
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


def test_ingest_end_to_end(ffmpeg):
    print("\n[3] 端到端：推来的文件 -> 归档目录（与 RTSP 同命名）")
    tmp = tempfile.mkdtemp(prefix="wb_ingest_")
    try:
        root = os.path.join(tmp, "archive")
        os.makedirs(root)
        cfg = make_config(tmp, root)

        inbox = os.path.join(tmp, "ftp-inbox")
        os.makedirs(inbox)
        src = os.path.join(inbox, "ch1_20260917093000_20260917095959.dav")
        if not make_video(ffmpeg, src):
            skip("端到端入库", "本机 ffmpeg 无法生成测试视频")
            return

        log = NullLogger()
        res = ftp_ingest.ingest(cfg, src, log, ffmpeg=ffmpeg)
        check("入库状态为 ok", res.get("status"), "ok")

        final = res.get("path", "")
        check_true("落在归档根的日期目录下",
                   os.path.dirname(final) == os.path.join(root, "2026-09-17"),
                   final)
        expect = "测试门店_通道1_20260917093000_20260917095959_device.mp4"
        check("文件名与 RTSP 命名规则一致", os.path.basename(final), expect)
        check_true("产出文件存在", os.path.isfile(final))
        check_true("产出文件非空",
                   os.path.isfile(final) and os.path.getsize(final) > 1024)

        parsed = parse_segment_name(os.path.basename(final))
        check_true("能被 storage.parse_segment_name 解析", parsed is not None)
        if parsed:
            check("解析出的门店名", parsed["store"], "测试门店")
            check("解析出的通道名", parsed["channel_name"], "通道1")
        check_true("能被 SEGMENT_RE 直接匹配",
                   SEGMENT_RE.match(os.path.basename(final)) is not None)

        check_true("收件箱原件已清理", not os.path.isfile(src))

        idx = cfg.index_file
        check_true("归档台账已生成", os.path.isfile(idx))
        if os.path.isfile(idx):
            rows = [json.loads(l) for l in
                    open(idx, encoding="utf-8").read().strip().split("\n") if l.strip()]
            check("台账 1 条", len(rows), 1)
            check("台账标记来源为 ftp", rows[0].get("source"), "ftp")
            check("台账记录最终路径", rows[0].get("file"), final)

        leftovers = [n for n in os.listdir(os.path.dirname(final))
                     if n.startswith(".")]
        check("不留临时文件", leftovers, [])

        src2 = os.path.join(inbox, "ch1_20260917093000_20260917095959.dav")
        if make_video(ffmpeg, src2):
            with open(final, "rb") as f:
                before = f.read()
            res2 = ftp_ingest.ingest(cfg, src2, log, ffmpeg=ffmpeg)
            check("重推同一段 -> skipped", res2.get("status"), "skipped")
            with open(final, "rb") as f:
                check("已有归档未被覆盖", f.read(), before)
            check_true("重推的原件也被清掉", not os.path.isfile(src2))

        src3 = os.path.join(inbox, "unknown_name.dav")
        if make_video(ffmpeg, src3):
            res3 = ftp_ingest.ingest(cfg, src3, log, ffmpeg=ffmpeg)
            check("无时间戳也能入库", res3.get("status"), "ok")
            base3 = os.path.basename(res3.get("path", ""))
            check_true("无时间戳产出的名字仍合规",
                       SEGMENT_RE.match(base3) is not None, base3)
            check_true("日志说明了退回 mtime", "没有时间戳" in log.text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_reject_garbage(ffmpeg):
    print("\n[4] 非视频文件必须被拒绝，不能污染归档目录")
    tmp = tempfile.mkdtemp(prefix="wb_ingestbad_")
    try:
        root = os.path.join(tmp, "archive")
        os.makedirs(root)
        cfg = make_config(tmp, root)
        inbox = os.path.join(tmp, "ftp-inbox")
        os.makedirs(inbox)

        src = os.path.join(inbox, "junk_20260917093000.dav")
        with open(src, "wb") as f:
            f.write(b"this is definitely not a video" * 200)

        log = NullLogger()
        res = ftp_ingest.ingest(cfg, src, log, ffmpeg=ffmpeg)
        check("垃圾文件 -> failed", res.get("status"), "failed")
        check_true("隔离目录里有原件", os.path.isfile(res.get("quarantined", "")))

        date_dir = os.path.join(root, "2026-09-17")
        left = os.listdir(date_dir) if os.path.isdir(date_dir) else []
        check_true("归档目录里没有留下任何成品", left == [], str(left))

        idx = cfg.index_file
        rows = ([l for l in open(idx, encoding="utf-8").read().splitlines()
                 if l.strip()] if os.path.isfile(idx) else [])
        check("失败不入台账", len(rows), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def main():
    print("=" * 68)
    print("FTP 入库回归")
    print("=" * 68)

    test_parse_name()
    test_tmp_prefix()

    try:
        ffmpeg = find_ffmpeg(load_config(None, base_dir=BASE))
    except Exception as exc:                       # noqa: BLE001
        ffmpeg = None
        print("\n找不到 ffmpeg：%r" % exc)

    if not ffmpeg:
        skip("端到端入库", "本机没有可用的 ffmpeg")
        skip("拒绝垃圾文件", "同上")
    else:
        print("\n使用 ffmpeg：%s" % ffmpeg)
        test_ingest_end_to_end(ffmpeg)
        test_reject_garbage(ffmpeg)

    print("\n" + "=" * 68)
    print("通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
