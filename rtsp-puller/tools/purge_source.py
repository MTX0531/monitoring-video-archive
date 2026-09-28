# -*- coding: utf-8 -*-
"""按来源永久删除归档成品（默认只预览，`--apply` 才真删）。

来源由台账 `state/index.jsonl` 的 `source` 字段判定：
    source == "ftp"  -> 设备推送来的（FTP）
    没有 source 字段 -> RTSP 回放拉下来的

安全边界（任一不满足就跳过，不报错也不删）：
  1. 该文件必须在台账里登记过
  2. 文件必须落在归档根目录内（realpath 包含性校验）
  3. 文件名必须严格匹配归档命名规则 SEGMENT_RE
  4. 只删文件，不递归删目录；空掉的日期目录保留

删除走 `nvrcore.storage.purge_file`：先全文件写零再删条目。本机删除会被
回收站拦截，写零这一步保证即使被拦截，回收站里那份也只能是零字节壳，
画面不可恢复 —— 所以这是真"永久删除"，不是丢回收站。

用法:
    python tools/purge_source.py --source rtsp                  # 预览
    python tools/purge_source.py --source rtsp --day 2026-09-15 # 只预览某天
    python tools/purge_source.py --source rtsp --apply          # 永久删除
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from nvrcore.config import load_config, pick_storage_root    # noqa: E402
from nvrcore.storage import purge_file, SEGMENT_RE           # noqa: E402
from nvrcore import ftp_ingest                               # noqa: E402

SOURCE_RTSP = "rtsp"
SOURCE_FTP = "ftp"


def row_source(row):
    """台账行 -> 来源标签。"""
    return row.get("source") or SOURCE_RTSP


def load_rows(index_path):
    rows = []
    if not os.path.isfile(index_path):
        return rows
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def inside(root, path):
    """path 是否真的在 root 里。"""
    try:
        r = os.path.realpath(root)
        p = os.path.realpath(path)
        return os.path.commonpath([r, p]) == r
    except (ValueError, OSError):
        return False


def select(rows, root, source, day=None):
    """挑出待处理的行，并给出被拒行的原因。"""
    picked, rejected = [], []
    for row in rows:
        path = row.get("file", "")
        if not path:
            continue
        if source != "all" and row_source(row) != source:
            continue
        if day and not (row.get("requested_start", "").startswith(day)):
            continue
        name = os.path.basename(path)
        if not SEGMENT_RE.match(name):
            rejected.append((path, "文件名不符合归档命名规则"))
            continue
        if not inside(root, path):
            rejected.append((path, "不在归档根目录内"))
            continue
        if not os.path.isfile(path):
            rejected.append((path, "磁盘上已不存在（台账残留）"))
            continue
        picked.append((row, path, name, os.path.getsize(path)))
    return picked, rejected


def rewrite_ledger(index_path, drop_paths, stamp):
    """把已删的行从台账摘掉；原台账整份备份。返回 (摘除行数, 备份路径)。"""
    dst = index_path + ".bak-purge-" + stamp
    with open(index_path, "r", encoding="utf-8") as f:
        original = f.readlines()
    kept, dropped = [], 0
    for line in original:
        s = line.strip()
        if not s:
            continue
        try:
            path = json.loads(s).get("file", "")
        except ValueError:
            kept.append(line if line.endswith("\n") else line + "\n")
            continue
        if os.path.normcase(path) in drop_paths:
            dropped += 1
            continue
        kept.append(line if line.endswith("\n") else line + "\n")
    with open(dst, "w", encoding="utf-8") as f:
        f.writelines(original)
    with open(index_path, "w", encoding="utf-8") as f:
        f.writelines(kept)
    return dropped, dst


def plan(rows, root, source, day=None):
    """把待删清单算出来（纯计算，不碰磁盘）。"""
    picked, rejected = select(rows, root, source, day)
    picked.sort(key=lambda p: p[0].get("requested_start", ""))
    return picked, rejected


def run_purge(root, index_path, source, day=None, apply_now=False, logger=None,
              stamp=None):
    """执行体：给测试可注入的入口。返回 dict 汇总。"""
    logger = logger or PrintLogger()
    rows = load_rows(index_path)
    picked, rejected = plan(rows, root, source, day)

    out = {
        "picked": picked,
        "rejected": rejected,
        "deleted": 0,
        "failed": [],
        "freed": 0,
        "dropped": 0,
        "backup": None,
    }
    if not picked or not apply_now:
        return out

    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    drop = set()
    for row, path, name, size in picked:
        if purge_file(path, logger):
            drop.add(os.path.normcase(path))
            out["deleted"] += 1
            out["freed"] += size
        else:
            out["failed"].append(path)
    out["dropped"], out["backup"] = rewrite_ledger(index_path, drop, stamp)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="按来源永久删除归档成品")
    ap.add_argument("--source", default=SOURCE_RTSP,
                    choices=[SOURCE_RTSP, SOURCE_FTP, "all"],
                    help="要删的来源（默认 rtsp）")
    ap.add_argument("--day", default=None, help="只处理某天 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行永久删除（不加则只预览）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args(argv)

    cfg = load_config(args.config or os.path.join(BASE, "config.json"),
                      base_dir=BASE)
    index_path = os.path.join(os.path.dirname(cfg.state_file), "index.jsonl")

    logger = PrintLogger()
    ftp_ingest.ensure_names(cfg, logger)
    root = pick_storage_root(cfg)

    print("归档根目录: %s" % root)
    print("台账      : %s" % index_path)
    print("筛选      : source=%s%s"
          % (args.source, (" day=%s" % args.day) if args.day else ""))
    print("-" * 78)

    if not os.path.isdir(root):
        print("归档根目录不存在，退出。")
        return 2

    res = run_purge(root, index_path, args.source, args.day,
                    apply_now=args.apply, logger=logger)

    for path, why in res["rejected"]:
        print("  [跳过] %s —— %s" % (os.path.basename(path), why))
    if res["rejected"]:
        print("-" * 78)

    if not res["picked"]:
        print("没有匹配的文件，未做任何改动。")
        return 0

    total = sum(p[3] for p in res["picked"])
    print("待删除 %d 个文件，合计 %.2f GB：" % (len(res["picked"]), total / 1073741824.0))
    for row, path, name, size in res["picked"]:
        print("  %s  %8.2f MB  %s ~ %s"
              % (row.get("requested_start", "?")[:10], size / 1048576.0,
                 row.get("requested_start", "?")[11:19],
                 row.get("requested_end", "?")[11:19]))
        print("      %s" % path)
    print("-" * 78)

    if not args.apply:
        print("预览模式：以上文件一个都没删。确认后加 --apply 才会永久删除。")
        return 0

    print("已永久删除 %d 个，释放 %.2f GB；失败 %d 个"
          % (res["deleted"], res["freed"] / 1073741824.0, len(res["failed"])))
    print("台账摘除 %d 行，原台账备份于 %s" % (res["dropped"], res["backup"]))
    for p in res["failed"]:
        print("  [失败] %s" % p)
    return 0 if not res["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
