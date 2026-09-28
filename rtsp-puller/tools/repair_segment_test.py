# -*- coding: utf-8 -*-
"""定点补拉工具验证（tools/repair_segment.py）。

覆盖：
  1. 参数校验：漏参数 / 时间格式错 / 结束不晚于开始 —— 全部在连设备之前退出
  2. 路径归一比较：大小写、分隔符、相对段不同的同一文件应判为同一路径
  3. 分片路径推导：按日期分层 / 不分层两种配置
  4. 现状判据：不存在 / 解不开 / 时长偏短 / 正常 / 恰好 50% 边界 / 读不到时长
  5. 索引替换：同文件只留一条、无关行与坏行原样保留、幂等、目录自动创建
  6. 体检输出：无合规文件、正常与需修复混排时计数与修复命令
  7. 命令行错误码

全部离线运行，不连录像机、不读真实归档目录。
用法：python tools/repair_segment_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import repair_segment as rs                                    # noqa: E402
from nvrcore.config import load_config                         # noqa: E402

PASS = 0
FAIL = 0
PY = sys.executable
TOOL = os.path.join(BASE, "tools", "repair_segment.py")

FMT = "%Y-%m-%d %H:%M:%S"


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
        print("  [FAIL] %s   %s" % (label, note))


def contains(label, text, needle):
    check_true(label, needle in text, "输出中找不到 %r" % needle)


# --------------------------------------------------------------------------- #
def _patch_probe(ok, detail=""):
    rs.probe_media = lambda ff, p, deep=True: (ok, detail)


def _patch_duration(sec):
    rs.media_duration = lambda ff, p, timeout=120.0: sec


def _touch(path, size=4096):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


class _Arch:
    def __init__(self, organize=True):
        self.organize_by_date = organize
        self.container = "mp4"
        self.store_name = "测试店"
        self.channel_name = "通道1"
        self.name_suffix = "device"

    def naming(self):
        return ("测试店", "通道1", "device")


class _Nvr:
    channel = 1
    stream = "main"


class _Cfg:
    def __init__(self, index_file, organize=True):
        self.archive = _Arch(organize)
        self.nvr = _Nvr()
        self.index_file = index_file


# --------------------------------------------------------------------------- #
def test_arg_validation():
    print("\n[1] 参数校验（必须在连设备之前退出）")
    p = subprocess.run([PY, TOOL], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=BASE)
    check("不给参数返回码 2", p.returncode, 2)

    p = subprocess.run([PY, TOOL, "2026-09-15 10:00:00"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", cwd=BASE)
    check("只给一个时间返回码 2", p.returncode, 2)

    p = subprocess.run([PY, TOOL, "2026/09/15 10:00", "2026/09/15 10:30"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=BASE)
    check("时间格式错返回码 2", p.returncode, 2)

    p = subprocess.run([PY, TOOL, "2026-09-15 10:30:00", "2026-09-15 10:00:00"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=BASE)
    check("结束早于开始返回码 2", p.returncode, 2)

    p = subprocess.run([PY, TOOL, "2026-09-15 10:00:00", "2026-09-15 10:00:00"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=BASE)
    check("起止相同返回码 2", p.returncode, 2)

    p = subprocess.run([PY, TOOL, "--no-such-flag"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=BASE)
    check("未知参数返回码 2", p.returncode, 2)


def test_parse_time():
    print("\n[2] 时间解析")
    dt = rs.parse_time("2026-09-15 10:00:00")
    check("解析出年份", dt.year, 2026)
    check("解析出分钟", dt.minute, 0)
    check("解析出小时", dt.hour, 10)
    raised = False
    try:
        rs.parse_time("2026/09/15 10:00")
    except ValueError:
        raised = True
    check_true("非法格式抛 ValueError", raised)


def test_same_path():
    print("\n[3] 路径归一比较")
    a = r"C:\data\店A\2026-09-15\x.mp4"
    check_true("完全相同的路径", rs._same_path(a, a))
    check_true("大小写不同视为同一文件",
               rs._same_path(a, r"c:\DATA\店A\2026-09-15\X.MP4"))
    if os.name == "nt":
        check_true("分隔符不同视为同一文件",
                   rs._same_path(a, "C:/data/店A/2026-09-15/x.mp4"))
    check_true("含 .. 的等价路径",
               rs._same_path(r"C:\data\a\..\店A\x.mp4", r"C:\data\店A\x.mp4"))
    check_true("不同文件不相等", not rs._same_path(a, r"C:\data\店A\2026-09-15\y.mp4"))
    check_true("不同目录不相等", not rs._same_path(a, r"C:\data\店B\2026-09-15\x.mp4"))


def test_segment_path():
    print("\n[4] 分片路径推导")
    cfg = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
    root = r"C:\arch"
    s = datetime(2026, 9, 15, 10, 0, 0)
    e = datetime(2026, 9, 15, 10, 30, 0)

    cfg.archive.organize_by_date = True
    p = rs.segment_path(cfg, root, s, e)
    check_true("按日期分层", p.endswith(os.path.join("2026-09-15",
                                                    os.path.basename(p))))
    check_true("文件名以开始时间起头",
               "_20260915100000_" in os.path.basename(p))
    check_true("扩展名为 mp4", p.endswith(".mp4"))

    cfg.archive.organize_by_date = False
    p2 = rs.segment_path(cfg, root, s, e)
    check("不分层时直接放根目录", os.path.dirname(p2), root)
    check("两种配置文件名一致", os.path.basename(p2), os.path.basename(p))


def test_assess(tmp):
    print("\n[5] 现状判据")
    ff = "ffmpeg"
    s = datetime(2026, 9, 15, 10, 0, 0)
    e = datetime(2026, 9, 15, 10, 30, 0)
    expected = 1800.0
    path = os.path.join(tmp, "seg.mp4")

    if os.path.exists(path):
        os.remove(path)
    need, note, size, dur = rs.assess(ff, path, expected)
    check_true("不存在 -> 需要修复", need)
    check("不存在的说明", note, "文件不存在")
    check("不存在时体积为 0", size, 0)

    _touch(path, 8192)
    _patch_probe(False, "Invalid data found when processing input")
    _patch_duration(None)
    need, note, size, dur = rs.assess(ff, path, expected)
    check_true("解不开 -> 需要修复", need)
    check("解不开时体积照报", size, 8192)

    _patch_probe(True, "")
    _patch_duration(880.0)
    need, note, size, dur = rs.assess(ff, path, expected)
    check_true("时长偏短 -> 需要修复", need)
    check("偏短时返回读到的时长", dur, 880.0)

    _patch_duration(expected * rs.SHORT_RATIO)
    need, _note, _size, dur = rs.assess(ff, path, expected)
    check_true("恰好 50%% 不判为需修复", not need)

    _patch_duration(expected * rs.SHORT_RATIO - 1)
    need, _note, _size, _dur = rs.assess(ff, path, expected)
    check_true("低于 50%% 判为需修复", need)

    _patch_duration(1799.7)
    need, note, size, dur = rs.assess(ff, path, expected)
    check_true("完整段落不需要修复", not need)
    check("正常时说明为『正常』", note, "正常")

    _patch_duration(None)
    need, note, _size, dur = rs.assess(ff, path, expected)
    check_true("读不到时长不判为坏文件", not need)

    got = {}

    def _spy(ff_, p_, deep=True):
        got["deep"] = deep
        return True, ""

    rs.probe_media = _spy
    _patch_duration(1799.0)
    rs.assess(ff, path, expected, deep=False)
    check("deep=False 透传给底层校验", got.get("deep"), False)
    rs.assess(ff, path, expected, deep=True)
    check("deep=True 透传给底层校验", got.get("deep"), True)


def test_replace_index(tmp):
    print("\n[6] 索引替换（同一文件只能留一条）")
    index = os.path.join(tmp, "idx", "index.jsonl")
    cfg = _Cfg(index)
    s = datetime(2026, 9, 15, 10, 0, 0)
    e = datetime(2026, 9, 15, 10, 30, 0)
    target = r"C:\arch\2026-09-15\店_通道_20260915100000_20260915103000_device.mp4"
    other = r"C:\arch\2026-09-15\店_通道_20260915093000_20260915100000_device.mp4"

    os.makedirs(os.path.dirname(index), exist_ok=True)
    with open(index, "w", encoding="utf-8") as f:
        f.write(json.dumps({"file": other, "size": 111}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"file": target, "size": 14942256,
                            "saved_at": "2026-09-16 14:14:35"},
                           ensure_ascii=False) + "\n")
        f.write("这条不是 json\n")
        f.write("\n")

    rec = rs.index_record(cfg, target, s, e, 40000000, 1799.7)
    check("新记录带 repaired 标记", rec.get("repaired"), True)
    check("新记录时长四舍五入两位", rec.get("actual_duration"), 1799.7)

    rs.replace_index_entry(cfg, target, rec)
    lines = [ln for ln in open(index, encoding="utf-8").read().splitlines() if ln.strip()]
    check("替换后总行数不变（2 条记录 + 1 条坏行）", len(lines), 3)
    objs = []
    for ln in lines:
        try:
            objs.append(json.loads(ln))
        except ValueError:
            objs.append(None)
    kept_targets = [o for o in objs if isinstance(o, dict)
                    and rs._same_path(o["file"], target)]
    check("目标文件只剩一条记录", len(kept_targets), 1)
    check("留下的是新记录", kept_targets[0].get("size"), 40000000)
    others = [o for o in objs if isinstance(o, dict)
              and rs._same_path(o["file"], other)]
    check("无关记录原样保留", len(others), 1)
    check("无关记录内容未变", others[0].get("size"), 111)
    check_true("坏行原样保留", any(o is None for o in objs))

    upper = target.upper()
    rs.replace_index_entry(cfg, upper, rs.index_record(cfg, upper, s, e, 41000000, 1799.0))
    lines2 = [ln for ln in open(index, encoding="utf-8").read().splitlines() if ln.strip()]
    count_same = 0
    for ln in lines2:
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("file") and rs._same_path(o["file"], target):
            count_same += 1
    check("大小写不同的同一文件仍只有一条", count_same, 1)

    n_before = len(lines2)
    rs.replace_index_entry(cfg, target, rec)
    lines3 = [ln for ln in open(index, encoding="utf-8").read().splitlines() if ln.strip()]
    check("重复替换不增行", len(lines3), n_before)

    fresh = os.path.join(tmp, "idx2", "index.jsonl")
    cfg2 = _Cfg(fresh)
    rs.replace_index_entry(cfg2, target, rec)
    got = [ln for ln in open(fresh, encoding="utf-8").read().splitlines() if ln.strip()]
    check("索引目录不存在时自动创建并写入", len(got), 1)


def test_do_scan(tmp):
    print("\n[7] 体检输出")
    root = os.path.join(tmp, "archive")
    os.makedirs(root, exist_ok=True)
    cfg = _Cfg(os.path.join(tmp, "scanidx", "index.jsonl"))

    check("空目录返回码 0", rs.do_scan(cfg, "ffmpeg", root), 0)

    day = os.path.join(root, "2026-09-15")
    good = os.path.join(day, "测试店_通道1_20260915093000_20260915100000_device.mp4")
    bad = os.path.join(day, "测试店_通道1_20260915100000_20260915103000_device.mp4")
    junk = os.path.join(day, "readme.txt")
    _touch(good, 4096)
    _touch(bad, 4096)
    _touch(junk, 64)

    calls = []

    def _probe(ff, p, deep=True):
        calls.append((os.path.basename(p), deep))
        if os.path.basename(p) == os.path.basename(bad):
            return False, "Invalid data found"
        return True, ""

    rs.probe_media = _probe
    _patch_duration(1799.0)

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = rs.do_scan(cfg, "ffmpeg", root)
    text = buf.getvalue()

    check("有文件时返回码 0", rc, 0)
    contains("统计只算合规文件", text, "共 2 个文件，1 个需要修复")
    contains("标出需修复的文件", text, "[需修复]")
    contains("标出正常的文件", text, "[正常]")
    check("体检用浅校验", all(deep is False for _n, deep in calls), True)
    check("体检只看了 2 个合规文件", len(calls), 2)
    check_true("体检未把 readme.txt 当录像", "readme.txt" not in text)


def main() -> int:
    print("=" * 70)
    print("定点补拉工具验证（tools/repair_segment.py）")
    print("=" * 70)
    tmp = tempfile.mkdtemp(prefix="repair_test_")
    try:
        test_arg_validation()
        test_parse_time()
        test_same_path()
        test_segment_path()
        test_assess(tmp)
        test_replace_index(tmp)
        test_do_scan(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 70)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
