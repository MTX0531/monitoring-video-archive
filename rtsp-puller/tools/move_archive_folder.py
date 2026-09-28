# -*- coding: utf-8 -*-
"""把录像归档整体迁到当前配置的归档目录（目录改名 / 换盘）。

什么时候用：
  * 归档目录名改成了 <门店名>门店监控视频归档，要把旧目录里的录像并过去
  * 一开始落在了 C 盘，后来接了大容量数据盘，想把归档整体搬过去

安全约束：
  * 只移动，不做任何删除；目标已存在同名文件则跳过，不覆盖
  * 唯一会移除的是搬空之后残留的空目录
  * state/index.jsonl 里的路径会同步改写，保证后续上传流程不断链
  * 默认只预览（dry-run），必须显式加 --apply 才真正移动

用法：
    python tools/move_archive_folder.py                       # 预览：自动找同级旧归档目录
    python tools/move_archive_folder.py --from "C:\\...\\监控视频归档"
    python tools/move_archive_folder.py --apply               # 真正执行
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore.config import load_config, pick_storage_root, ensure_dirs
from nvrcore.deviceinfo import resolve_identity
from nvrcore.logutil import human_size
from nvrcore.state import rewrite_index_paths
from nvrcore import storage


# --------------------------------------------------------------------------- #
def collect_moves(src: str, dst: str):
    """列出 (旧路径, 新路径)，并保持目录结构。"""
    moves = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames]
        rel = os.path.relpath(dirpath, src)
        for fn in filenames:
            old = os.path.join(dirpath, fn)
            new = os.path.join(dst, fn) if rel == "." else os.path.join(dst, rel, fn)
            moves.append((old, new))
    moves.sort()
    return moves


def dir_size(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def remove_empty_tree(src: str, dry_run: bool) -> int:
    """自底向上删掉已经搬空的空目录（只删空目录）。"""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(src, topdown=False):
        if os.path.realpath(dirpath) == os.path.realpath(src):
            continue
        if filenames:
            continue
        try:
            if not os.listdir(dirpath):
                if not dry_run:
                    os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass
    try:
        if not dry_run and not os.listdir(src):
            os.rmdir(src)
    except OSError:
        pass
    return removed


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply_now = "--apply" in argv
    config_path = None
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            config_path = argv[i + 1]
    src_arg = None
    if "--from" in argv:
        i = argv.index("--from")
        if i + 1 < len(argv):
            src_arg = argv[i + 1]

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(config_path, base_dir=base_dir)

    if cfg.names_need_device():
        try:
            ident = resolve_identity(cfg, force_refresh=True, quiet=True)
            cfg.apply_identity(ident)
        except Exception:                              # noqa: BLE001
            cfg.apply_identity(None)

    dst = pick_storage_root(cfg)

    print("=" * 78)
    print("归档目录迁移%s" % ("（实际执行）" if apply_now else "（预览，不移动任何文件）"))
    print("=" * 78)
    print("目标目录 : %s" % dst)

    if src_arg:
        candidates = [os.path.abspath(os.path.expanduser(src_arg))]
    else:
        candidates = storage.find_stale_archive_dirs(dst)

    if not candidates:
        print("没找到需要迁移的旧归档目录 —— 同级目录下没有其它装着录像的归档目录。")
        print("=" * 78)
        return 0
    if len(candidates) > 1 and not src_arg:
        print("同级目录下发现多个候选，请用 --from 明确指定要迁移哪一个：")
        for c in candidates:
            print("  %s" % c)
        print("=" * 78)
        return 2

    src = os.path.realpath(candidates[0])
    dst_real = os.path.realpath(dst)

    if not os.path.isdir(src):
        print("[中止] 来源目录不存在：%s" % src)
        return 2
    if os.path.normcase(src) == os.path.normcase(dst_real):
        print("[中止] 来源与目标相同，无需迁移。")
        return 0
    if storage.is_within(src, dst_real) or storage.is_within(dst_real, src):
        print("[中止] 来源与目标存在嵌套关系，拒绝执行（避免把目录搬进自己里面）。")
        return 2

    moves = collect_moves(src, dst_real)
    total = dir_size([m[0] for m in moves])
    conflicts = [m for m in moves if os.path.exists(m[1])]

    print("来源目录 : %s" % src)
    print("待移动   : %d 个文件 / %s" % (len(moves), human_size(total)))
    if conflicts:
        print("同名已存在: %d 个（这些会跳过，不覆盖）" % len(conflicts))
    print("-" * 78)
    for old, new in moves[:20]:
        print("  %s" % os.path.relpath(old, src))
    if len(moves) > 20:
        print("  …… 另有 %d 个文件" % (len(moves) - 20))
    print("-" * 78)

    if not apply_now:
        print("以上仅为预览。确认无误后加 --apply 执行：")
        print('  python tools/move_archive_folder.py --apply')
        print("=" * 78)
        return 0

    ensure_dirs(dst_real)
    mapping = {}
    moved = 0
    skipped = 0
    for old, new in moves:
        if os.path.exists(new):
            skipped += 1
            continue
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.move(old, new)
            mapping[os.path.normcase(old)] = new
            moved += 1
        except OSError as exc:
            print("  !! 移动失败：%s（%s）" % (os.path.basename(old), exc))
            skipped += 1

    empty_removed = remove_empty_tree(src, dry_run=False)
    idx_changed = rewrite_index_paths(cfg.index_file, mapping)

    print("完成：移动 %d 个 / 跳过 %d 个；清理空目录 %d 个。" % (moved, skipped, empty_removed))
    if idx_changed:
        print("索引同步：state/index.jsonl 中 %d 条记录的路径已更新。" % idx_changed)
    rest = os.path.isdir(src) and os.listdir(src)
    if rest:
        print("注意：来源目录仍保留 %d 项未移动（可能同名或占用中）：" % len(rest))
        for r in rest[:10]:
            print("  %s" % r)
    elif not os.path.isdir(src):
        print("来源目录已搬空并移除。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
