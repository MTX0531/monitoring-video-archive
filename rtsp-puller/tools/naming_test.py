# -*- coding: utf-8 -*-
"""归档命名规则的验证脚本。

覆盖：
  1. 名称清洗（中文、非法字符、下划线、空白、空值、超长）
  2. 文件名生成（对齐大华云联格式）
  3. 文件名解析（新格式 / 旧格式 / 各种畸形名不应被误认）
  4. scan_segments 只认本脚本产出的合规文件（留存删除的安全边界依赖它）

用法：python tools/naming_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore.config import sanitize_name, load_config, AUTO
from nvrcore import storage

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
        print("  [FAIL] %s  %s" % (label, note))


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("=" * 74)
print("1) 名称清洗")
print("=" * 74)
check("中文原样保留", sanitize_name("<门店7>", "X"), "<门店7>")
check("下划线替换为短横线", sanitize_name("通道A_1", "X"), "通道A-1")
check("非法字符替换", sanitize_name('门店:1*2?3"4', "X"), "门店-1-2-3-4")
check("反斜杠替换", sanitize_name("门店\\A/B", "X"), "门店-A-B")
check("多余空白折叠", sanitize_name("  门店   一号  ", "X"), "门店-一号")
check("连续短横线折叠", sanitize_name("门店---一号", "X"), "门店-一号")
check("首尾符号剥除", sanitize_name("-门店.", "X"), "门店")
check("空值回退默认", sanitize_name("", "测试门店-临时"), "测试门店-临时")
check("纯空白回退默认", sanitize_name("   ", "测试门店-临时"), "测试门店-临时")
check("超长截断到60", len(sanitize_name("门" * 200, "X")), 60)
check_true("清洗后不含下划线", "_" not in sanitize_name("青_丘_1", "X"))

print()
print("=" * 74)
print("2) 文件名生成（对齐大华云联）")
print("=" * 74)
cfg = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
cfg.archive.store_name = "<门店7>"
cfg.archive.channel_name = "通道1"
cfg.archive.name_suffix = "device"
s = datetime(2026, 9, 5, 13, 29, 41)
e = datetime(2026, 9, 5, 15, 31, 40)
name = storage.build_segment_name(cfg, s, e, "mp4")
check("对齐用户给的参考样例", name,
      "<门店7>_通道1_20260905132941_20260905153140_device.mp4")

cfg2 = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
if cfg2.names_need_device():
    try:
        from nvrcore.deviceinfo import resolve_identity
        _ident = resolve_identity(cfg2, force_refresh=True, quiet=True)
        cfg2.apply_identity(_ident)
        print("  （名称来源：%s）" % _ident.source)
    except Exception as _exc:                                # noqa: BLE001
        cfg2.apply_identity(None)
        print("  （读取设备失败，使用兜底名称：%s）" % _exc)
print("  当前配置生成示例：%s" % storage.sample_segment_name(cfg2))
print("  门店名=%s  通道名=%s  末段=%s"
      % (cfg2.archive.store_name, cfg2.archive.channel_name, cfg2.archive.name_suffix))
check_true("auto 解析后门店名不是占位符", cfg2.archive.store_name != AUTO,
           "实得 %r" % cfg2.archive.store_name)
check_true("auto 解析后通道名不是占位符", cfg2.archive.channel_name != AUTO,
           "实得 %r" % cfg2.archive.channel_name)
check_true("解析后的名称能生成可解析的文件名",
           storage.parse_segment_name(storage.build_segment_name(
               cfg2, s, e, "mp4")) is not None)

_unresolved = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
_fb = storage.build_segment_name(_unresolved, s, e, "mp4")
check_true("仍为 auto 时兜底命名合法（%s）" % _fb,
           storage.parse_segment_name(_fb) is not None, _fb)

print()
print("=" * 74)
print("3) 文件名解析")
print("=" * 74)
info = storage.parse_segment_name("<门店7>_通道1_20260905132941_20260905153140_device.mp4")
check_true("新格式可解析", info is not None)
check("  解析出门店名", info["store"], "<门店7>")
check("  解析出通道名", info["channel_name"], "通道1")
check("  解析出开始时间", info["start"], datetime(2026, 9, 5, 13, 29, 41))
check("  解析出结束时间", info["end"], datetime(2026, 9, 5, 15, 31, 40))
check("  通道名含数字时提取通道号", storage.parse_segment_name(
    "测试门店-临时_通道1_20260914145500_20260914152500_device.mp4")["channel"], 1)
check("  纯数字通道名", storage.parse_segment_name(
    "测试门店-临时_2_20260914145500_20260914152500_device.mp4")["channel"], 2)
check("  通道名无数字时为 None", storage.parse_segment_name(
    "测试门店-临时_通道A_20260914145500_20260914152500_device.mp4")["channel"], None)
check("  '通道1' 取到 1", info["channel"], 1)

legacy = storage.parse_segment_name("CH01_20260914_145500_20260914_152500.mp4")
check_true("旧格式（历史文件）仍可解析", legacy is not None)
check("  旧格式通道号", legacy["channel"], 1)
check("  旧格式开始时间", legacy["start"], datetime(2026, 9, 14, 14, 55))

print("  --- 以下畸形名必须一律拒绝（关系到留存删除安全）---")
for bad in [
    "readme.md",
    "门店_通道_20260905132941_20260905153140_device.txt",
    "门店_通道_202609051329_20260905153140_device.mp4",
    "门店_通道_20260905132941_20260905153140.mp4",
    "门店_通道_20260905132941_20260905153140_device_v2.mp4",
    "我的视频_20260905.mp4",
    "备份_通道1_20260905132941_20260905153140_device.mp4.bak",
    ".part_abcd.mp4",
]:
    check("拒绝 %s" % bad, storage.parse_segment_name(bad), None)

print()
print("=" * 74)
print("4) scan_segments 只认合规文件")
print("=" * 74)
tmp = tempfile.mkdtemp(prefix="naming_test_")
root = os.path.join(tmp, "archive")
os.makedirs(os.path.join(root, "2026-09-05"))
good = [
    "<门店7>_通道1_20260905132941_20260905153140_device.mp4",
    "CH01_20260914_145500_20260914_152500.mp4",
]
noise = [
    "readme.md",
    "门店名_通道名_20260905132941_20260905153140_device.txt",
    "备份.txt",
]
for f in good + noise:
    with open(os.path.join(root, "2026-09-05", f), "wb") as fh:
        fh.write(b"x" * 2048)

found = storage.scan_segments(root)
names = sorted(os.path.basename(s.path) for s in found)
check_true("只识别 2 个合规文件", len(found) == 2, "实得 %d 个: %s" % (len(found), names))
check_true("新命名被识别", good[0] in names)
check_true("旧命名被识别", good[1] in names)
check_true("readme.md 未被识别", "readme.md" not in names)
check_true("扩展名不对的未被识别", noise[1] not in names)
check_true("无关文件未被识别", "备份.txt" not in names)

os.makedirs(os.path.join(root, ".cache"))
with open(os.path.join(root, ".cache", good[0]), "wb") as fh:
    fh.write(b"x" * 2048)
check_true("隐藏目录内文件被忽略", len(storage.scan_segments(root)) == 2)

shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 74)
print("结果：%d 项通过，%d 项失败" % (PASS, FAIL))
print("=" * 74)
sys.exit(1 if FAIL else 0)
