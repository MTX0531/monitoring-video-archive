# -*- coding: utf-8 -*-
"""归档目录命名的验证脚本。

覆盖：
  1. 文件夹名清洗（保留可读的 '_'、非法字符、结尾点/空格、空值、超长）
  2. 文件夹名解析：<门店名>门店监控视频归档（含未解析到设备名时的兜底）
  3. 根目录选址：显式路径优先、auto 落到桌面时的拼接
  4. 旧归档目录探测（改名后防止录像被"落下"）
  5. 索引路径改写（迁移后 state/index.jsonl 必须同步，否则上传流程断链）

用法：python tools/folder_naming_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore.config import (AUTO, ArchiveConfig, Config, DEFAULT_ARCHIVE_FOLDER,
                            DEFAULT_ARCHIVE_FOLDER_SUFFIX, load_config,
                            looks_like_archive_folder, pick_storage_root,
                            resolve_folder_name, sanitize_folder_name)
from nvrcore.state import rewrite_index_paths
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


def make_cfg(store="测试门店A", folder_name=AUTO,
             folder_suffix=DEFAULT_ARCHIVE_FOLDER_SUFFIX, root_dir="auto"):
    cfg = Config(base_dir=tempfile.gettempdir())
    cfg.archive.store_name = store
    cfg.archive.channel_name = "IPC"
    cfg.archive.folder_name = folder_name
    cfg.archive.folder_suffix = folder_suffix
    cfg.archive.root_dir = root_dir
    return cfg


SEG = "测试门店_通道1_20260914145500_20260914152500_device.mp4"


def touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 16)


# --------------------------------------------------------------------------- #
print("=" * 74)
print("1) 文件夹名清洗")
print("=" * 74)
check("中文原样保留", sanitize_folder_name("测试门店A", "X"), "测试门店A")
check("下划线原样保留（与文件名规则不同）",
      sanitize_folder_name("A_B_C", "X"), "A_B_C")
check("非法字符替换", sanitize_folder_name('门店:1*2?3"4', "X"), "门店-1-2-3-4")
check("反斜杠替换", sanitize_folder_name("门店\\A/B", "X"), "门店-A-B")
check("多余空白折叠", sanitize_folder_name("  门店   一号  ", "X"), "门店 一号")
check("结尾的点剥除（Windows 不允许）", sanitize_folder_name("门店归档...", "X"), "门店归档")
check("结尾空格剥除", sanitize_folder_name("门店归档   ", "X"), "门店归档")
check("空值回退默认", sanitize_folder_name("", DEFAULT_ARCHIVE_FOLDER), DEFAULT_ARCHIVE_FOLDER)
check("纯空白回退默认", sanitize_folder_name("   ", DEFAULT_ARCHIVE_FOLDER),
      DEFAULT_ARCHIVE_FOLDER)
check("超长截断到120", len(sanitize_folder_name("门" * 300, "X")), 120)

print()
print("=" * 74)
print("2) 文件夹名解析（<门店名>门店监控视频归档）")
print("=" * 74)
check("默认后缀正确", DEFAULT_ARCHIVE_FOLDER_SUFFIX, "门店监控视频归档")
check("auto -> 门店名 + 后缀", resolve_folder_name(make_cfg()),
      "测试门店A门店监控视频归档")
check("门店名带后缀 -总 时原样拼接",
      resolve_folder_name(make_cfg(store="<门店7>")),
      "<门店7>门店监控视频归档")
check("门店名含非法字符时清洗",
      resolve_folder_name(make_cfg(store="门店/一号:2")),
      "门店-一号-2门店监控视频归档")
check("门店名未解析（auto）时用兜底名",
      resolve_folder_name(make_cfg(store=AUTO)), "NVR门店监控视频归档")
check("显式 folder_name 优于门店名",
      resolve_folder_name(make_cfg(folder_name="固定归档目录")), "固定归档目录")
check("自定义 folder_suffix 生效",
      resolve_folder_name(make_cfg(folder_suffix="监控归档")),
      "测试门店A监控归档")
check("folder_name 为空格串时按 auto 处理",
      resolve_folder_name(make_cfg(folder_name="   ")),
      "测试门店A门店监控视频归档")
check("默认配置就是 auto",
      ArchiveConfig().folder_name, AUTO)
check("默认配置后缀为门店监控视频归档",
      ArchiveConfig().folder_suffix, "门店监控视频归档")

print()
print("=" * 74)
print("3) 根目录选址")
print("=" * 74)
check("显式绝对路径优先且原样返回",
      pick_storage_root(make_cfg(root_dir=r"D:\录像归档")), r"D:\录像归档")


def _desktop_join(folder):
    from nvrcore.config import _desktop_path
    return os.path.join(_desktop_path(), folder)


from nvrcore.config import free_bytes, fixed_drives            # noqa: E402

_best = max(fixed_drives(), key=lambda d: free_bytes(d))
if _best.upper().startswith("C"):
    check("auto 落在 C 盘 -> 桌面下的门店归档目录",
          pick_storage_root(make_cfg()), _desktop_join("测试门店A门店监控视频归档"))
else:
    check("auto 落在非 C 盘 -> 盘根下的门店归档目录",
          pick_storage_root(make_cfg()),
          os.path.join(_best + os.sep, "测试门店A门店监控视频归档"))

print()
print("=" * 74)
print("4) 旧归档目录探测（改名后录像不能被落下）")
print("=" * 74)
check_true("旧名（监控视频归档）算归档目录", looks_like_archive_folder("监控视频归档"))
check_true("新名（XX门店监控视频归档）算归档目录",
           looks_like_archive_folder("测试门店A门店监控视频归档"))
check_true("无关目录不算", not looks_like_archive_folder("视频稽核本地上传"))

tmp = tempfile.mkdtemp(prefix="folder_test_")
try:
    new_root = os.path.join(tmp, "新店门店监控视频归档")
    old_root = os.path.join(tmp, "监控视频归档")
    touch(os.path.join(new_root, "2026-09-14", SEG))
    touch(os.path.join(old_root, "2026-09-14", SEG))

    stale = storage.find_stale_archive_dirs(new_root)
    check("能发现同级的旧归档目录", [os.path.basename(p) for p in stale], ["监控视频归档"])

    empty_old = os.path.join(tmp, "空店门店监控视频归档")
    os.makedirs(os.path.join(empty_old, "2026-09-14"), exist_ok=True)
    names = [os.path.basename(p) for p in storage.find_stale_archive_dirs(new_root)]
    check_true("空归档目录不报", "空店门店监控视频归档" not in names, str(names))

    other = os.path.join(tmp, "素材库")
    touch(os.path.join(other, "2026-09-14", SEG))
    names = [os.path.basename(p) for p in storage.find_stale_archive_dirs(new_root)]
    check_true("名字不像归档目录的不报", "素材库" not in names, str(names))

    nested = os.path.join(new_root, "backup", "旧门店监控视频归档")
    touch(os.path.join(nested, "2026-09-14", SEG))
    names = [os.path.basename(p) for p in storage.find_stale_archive_dirs(new_root)]
    check_true("新目录内部的嵌套目录不报",
               "旧门店监控视频归档" not in names, str(names))

    check_true("探测只读、不改动文件",
               os.path.isfile(os.path.join(old_root, "2026-09-14", SEG)))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 74)
print("5) 索引路径改写（迁移后 index.jsonl 同步）")
print("=" * 74)
tmp = tempfile.mkdtemp(prefix="idx_test_")
try:
    idx = os.path.join(tmp, "index.jsonl")
    old_p = r"<本机用户目录>\Desktop\监控视频归档\2026-09-14" + "\\" + SEG
    new_p = r"<本机用户目录>\Desktop\测试门店A门店监控视频归档\2026-09-14" + "\\" + SEG
    other_p = r"<本机用户目录>\Desktop\别的目录\x.mp4"
    with open(idx, "w", encoding="utf-8") as f:
        f.write(json.dumps({"file": old_p, "channel": 1}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"file": other_p, "channel": 1}, ensure_ascii=False) + "\n")
        f.write("这不是合法 JSON\n")

    def _flat(p: str) -> str:
        """JSON 里反斜杠是转义的（\\ 变 \\\\），比较前先拉平。"""
        return p.replace("\\\\", "\\")

    changed = rewrite_index_paths(idx, {os.path.normcase(old_p): new_p}, dry_run=True)
    check("试算模式统计到 1 条", changed, 1)
    with open(idx, "r", encoding="utf-8") as f:
        check_true("试算模式不落盘", _flat(old_p) in _flat(f.read()))

    changed = rewrite_index_paths(idx, {os.path.normcase(old_p): new_p})
    check("执行模式统计到 1 条", changed, 1)
    with open(idx, "r", encoding="utf-8") as f:
        text = _flat(f.read())
    check_true("路径已改写为新归档目录", _flat(new_p) in text)
    check_true("未涉及的记录保持原样", _flat(other_p) in text)
    check_true("坏行被保留而不是丢弃", "这不是合法 JSON" in text)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 74)
print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 74)
sys.exit(1 if FAIL else 0)
