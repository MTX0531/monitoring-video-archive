# -*- coding: utf-8 -*-
"""按来源永久删除（tools/purge_source.py）验证。

这是个**破坏性**工具，所以测试的重点不是"它删得对不对"，而是
"它在什么情况下**坚决不删**"。核心契约：

  1. 不在台账里的文件，即使躺在归档目录里也绝不碰
  2. 台账里有、但文件在归档根之外的，绝不碰（realpath 包含性校验；
     注意 `归档` 与 `归档_x` 这种前缀相近的兄弟目录必须判为外部）
  3. 文件名不符合归档命名规则的行，绝不碰
  4. 台账有、磁盘没有的行：只报告，不算待删
  5. 不加 --apply 时一个字节都不许动
  6. --apply 之后：文件消失、台账对应行摘除、原台账整份备份可查

用临时目录造档案与台账，不碰真实归档、不连录像机。
用法：python tools/purge_source_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore.storage import SEGMENT_RE                     # noqa: E402

PASS = 0
FAIL = 0


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "purge_source", os.path.join(BASE, "tools", "purge_source.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PS = _load_mod()


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


# --------------------------------------------------------------------------- #
def seg_name(hhmm):
    return "测试门店_通道1_20260915%s00_20260915%s00_device.mp4" % (hhmm, hhmm)


def make_file(path, size=4096, fill=b"\xa5"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(fill * size)
    return path


def row_of(path, start, source=None):
    r = {
        "file": path,
        "store_name": "测试门店",
        "channel_name": "通道1",
        "requested_start": start,
        "requested_end": start[:11] + "30:00",
        "size": os.path.getsize(path) if os.path.isfile(path) else 0,
    }
    if source:
        r["source"] = source
    return r


class Sandbox:
    """一套临时归档 + 台账。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="purge_test_")
        self.root = os.path.join(self.tmp, "测试门店门店监控视频归档")
        self.outside = os.path.join(self.tmp, "外部目录")
        self.sibling = self.root + "_x"
        self.index = os.path.join(self.tmp, "index.jsonl")
        self.rows = []

    def add(self, rel, source=None, start=None, on_disk=True, real_name=None):
        """登记一行；real_name 可造出"台账名与磁盘名不一致"的情形。"""
        day = "2026-09-15"
        disk_name = real_name or os.path.basename(rel)
        path = os.path.join(self.root, day, disk_name)
        if on_disk:
            make_file(path)
        row = row_of(path, start or ("2026-09-15 " + disk_name[16:18] + ":00:00"),
                     source)
        self.rows.append(row)
        return path, row

    def add_raw(self, path, source=None, start="2026-09-15 10:00:00",
                on_disk=True):
        if on_disk:
            make_file(path)
        row = row_of(path, start, source)
        self.rows.append(row)
        return path, row

    def write_index(self, rows=None):
        with open(self.index, "w", encoding="utf-8") as f:
            for r in (rows if rows is not None else self.rows):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


print("=" * 74)
print("0) 前置：构造的档案名必须真的符合归档命名规则")
print("=" * 74)
check_true("测试用文件名能通过 SEGMENT_RE 校验",
           bool(SEGMENT_RE.match(seg_name("0930"))),
           seg_name("0930"))

print()
print("=" * 74)
print("1) 筛选：只挑指定来源，且各种越界情形一律跳过")
print("=" * 74)
sb = Sandbox()
rtsp_a, _ = sb.add("2026-09-15/" + seg_name("0930"))
rtsp_b, _ = sb.add("2026-09-15/" + seg_name("1000"))
ftp_a, _ = sb.add("2026-09-15/" + seg_name("1030"), source="ftp")
out_side, _ = sb.add_raw(os.path.join(sb.outside, seg_name("1100")))
sib_side, _ = sb.add_raw(os.path.join(sb.sibling, "2026-09-15", seg_name("1130")))
bad_name, _ = sb.add("2026-09-15/" + seg_name("1200"), real_name="随便起的名.mp4")
missing, _ = sb.add("2026-09-15/" + seg_name("1230"), on_disk=False)
not_in_ledger = make_file(
    os.path.join(sb.root, "2026-09-15", seg_name("1300")))
sb.write_index()

picked, rejected = PS.plan(sb.rows, sb.root, "rtsp")
paths = [p[1] for p in picked]
check("待删恰好 2 个（两条 rtsp）", len(picked), 2)
check_true("挑中的是两条 rtsp", set(paths) == {rtsp_a, rtsp_b})
check_true("source=ftp 的那条不在其中", ftp_a not in paths)
check_true("不在台账的文件不在其中", not_in_ledger not in paths)

reasons = {os.path.basename(p): why for p, why in rejected}
check("被拒条目数", len(rejected), 4)
check_true("归档根之外 -> 拒绝", "不在归档根目录内" in reasons.get(
    os.path.basename(out_side), ""), str(reasons))
check_true("前缀相近的兄弟目录 -> 拒绝", "不在归档根目录内" in reasons.get(
    os.path.basename(sib_side), ""), str(reasons))
check_true("文件名不合规 -> 拒绝", "文件名不符合归档命名规则" in reasons.get(
    os.path.basename(bad_name), ""), str(reasons))
check_true("磁盘上已不存在 -> 拒绝", "已不存在" in reasons.get(
    os.path.basename(missing), ""), str(reasons))

check_true("inside(根, 根/日子/文件) = True", PS.inside(sb.root, rtsp_a))
check_true("inside(根, 前缀相近兄弟目录) = False", not PS.inside(sb.root, sib_side))
check_true("inside(根, 完全无关目录) = False", not PS.inside(sb.root, out_side))

print()
print("=" * 74)
print("2) 只看某一天")
print("=" * 74)
d16 = os.path.join(sb.root, "2026-09-16",
                   "测试门店_通道1_20260916093000_20260916093000_device.mp4")
make_file(d16)
rows2 = list(sb.rows) + [row_of(d16, "2026-09-16 09:30:00")]
p16, _ = PS.plan(rows2, sb.root, "rtsp", day="2026-09-16")
check("--day 2026-09-16 只挑 1 个", len(p16), 1)
check_true("挑中的正是 9/16 那个", bool(p16) and p16[0][1] == d16)

print()
print("=" * 74)
print("3) 不加 --apply：一个字节都不许动")
print("=" * 74)
res = PS.run_purge(sb.root, sb.index, "rtsp", apply_now=False,
                   logger=PS.QuietLogger())
check("预览不算删除", res["deleted"], 0)
check("预览不写备份", res["backup"], None)
check_true("预览后文件都还在", all(os.path.isfile(p)
                                   for p in (rtsp_a, rtsp_b, ftp_a)))
check_true("预览后台账原样未动",
           len(PS.load_rows(sb.index)) == len(sb.rows))

print()
print("=" * 74)
print("4) --apply：删对的文件、留住不该删的、台账同步")
print("=" * 74)
before = len(PS.load_rows(sb.index))
res = PS.run_purge(sb.root, sb.index, "rtsp", apply_now=True,
                   logger=PS.QuietLogger(), stamp="teststamp")
check("删除数", res["deleted"], 2)
check("失败数", len(res["failed"]), 0)
check_true("释放字节数 > 0", res["freed"] > 0, "%s" % res["freed"])
check_true("两个 rtsp 文件已消失",
           not os.path.exists(rtsp_a) and not os.path.exists(rtsp_b))
check_true("ftp 文件安然无恙", os.path.isfile(ftp_a))
check_true("归档根之外的文件安然无恙", os.path.isfile(out_side))
check_true("兄弟目录的文件安然无恙", os.path.isfile(sib_side))
check_true("名字不合规的文件安然无恙", os.path.isfile(bad_name))
check_true("不在台账的文件安然无恙", os.path.isfile(not_in_ledger))

after = PS.load_rows(sb.index)
check("台账摘除 2 行", before - len(after), 2)
check_true("台账里不再有已删路径",
           all(r["file"] not in (rtsp_a, rtsp_b) for r in after))
check_true("台账里 ftp 行仍在", any(r["file"] == ftp_a for r in after))
check("返回的摘除行数", res["dropped"], 2)
check_true("备份文件存在", os.path.isfile(res["backup"]),
           str(res["backup"]))
check_true("备份文件名带 stamp", res["backup"].endswith(".bak-purge-teststamp"))
if res["backup"] and os.path.isfile(res["backup"]):
    backup_rows = [l for l in open(res["backup"], encoding="utf-8") if l.strip()]
    check("备份 == 删除前的完整台账行数", len(backup_rows), before)
    parsed = []
    for line in backup_rows:
        try:
            parsed.append(json.loads(line)["file"])
        except (ValueError, KeyError, TypeError):
            pass
    check_true("备份里能查到已删的路径", rtsp_a in parsed,
               "解析出 %d 条路径" % len(parsed))

print()
print("=" * 74)
print("5) 幂等 + 空匹配：重跑不再删、也不产出备份")
print("=" * 74)
res2 = PS.run_purge(sb.root, sb.index, "rtsp", apply_now=True,
                    logger=PS.QuietLogger(), stamp="second")
check("重跑待删为 0", len(res2["picked"]), 0)
check("重跑删除数 0", res2["deleted"], 0)
check("重跑不写备份", res2["backup"], None)

print()
print("=" * 74)
print("6) 按来源删 ftp")
print("=" * 74)
res3 = PS.run_purge(sb.root, sb.index, "ftp", apply_now=True,
                    logger=PS.QuietLogger(), stamp="ftpstamp")
check("ftp 待删 1 个", len(res3["picked"]), 1)
check_true("ftp 文件已消失", not os.path.exists(ftp_a))

sb.cleanup()

print()
print("=" * 74)
print("结果: 通过 %d，失败 %d" % (PASS, FAIL))
print("=" * 74)
sys.exit(0 if FAIL == 0 else 1)
