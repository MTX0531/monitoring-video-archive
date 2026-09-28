# -*- coding: utf-8 -*-
"""把归档目录里的录像批量改成当前配置的命名规则。

什么时候用：
  * 调整了 config.json 里的 store_name / channel_name / name_suffix，想让已有文件跟着改名
  * 早期版本产出的 CH01_YYYYMMDD_HHMMSS_... 旧命名，需要统一成云联风格

只处理归档根目录内、能被识别为归档案的文件（安全边界与留存清理一致），
不会碰任何其它文件。目标名已存在时跳过，不覆盖。

用法：
    python tools/rename_archive.py --dry-run     # 只预览，不改动任何文件
    python tools/rename_archive.py               # 执行改名
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore.config import load_config, pick_storage_root, ensure_dirs, is_auto
from nvrcore.deviceinfo import resolve_identity
from nvrcore.state import rewrite_index_paths as sync_index
from nvrcore import storage


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    config_path = None
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            config_path = argv[i + 1]

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(config_path, base_dir=base_dir)
    root = ensure_dirs(pick_storage_root(cfg))

    source_note = "配置指定"
    if cfg.names_need_device():
        try:
            ident = resolve_identity(cfg, force_refresh=True, quiet=True)
            cfg.apply_identity(ident)
            source_note = {"device": "读自录像机",
                           "cache": "读自录像机缓存（有效期内）",
                           "cache-offline": "设备未响应，使用本地缓存",
                           "fallback": "读取失败，使用兜底值"}.get(ident.source, ident.source)
        except Exception as exc:                      # noqa: BLE001
            cfg.apply_identity(None)
            source_note = "读取失败(%s)，使用兜底值" % exc

    store, channel, suffix = cfg.archive.naming()
    print("=" * 78)
    print("归档文件批量改名%s" % ("（预览，不改动）" if dry_run else ""))
    print("=" * 78)
    print("归档目录 : %s" % root)
    print("命名规则 : <门店名>_<通道名>_<开始时间>_<结束时间>_%s" % suffix)
    print("门店名   : %s   （%s）" % (store, source_note))
    print("通道名   : %s   （%s）" % (channel, source_note))
    print("示例     : %s" % storage.sample_segment_name(cfg))
    print()

    segs = storage.scan_segments(root)
    if not segs:
        print("目录里没有可识别的录像文件。")
        return 0

    renamed = 0
    skipped = 0
    mapping = {}
    for seg in segs:
        ext = os.path.splitext(seg.path)[1].lstrip(".")
        new_name = storage.build_segment_name(cfg, seg.start, seg.end, ext)
        cur_dir = os.path.dirname(seg.path)
        new_path = os.path.join(cur_dir, new_name)

        if os.path.normcase(new_path) == os.path.normcase(seg.path):
            skipped += 1
            continue
        if os.path.exists(new_path):
            print("[跳过] 目标已存在：%s" % new_name)
            skipped += 1
            continue

        print("%s  %s\n     -> %s"
              % ("[预览]" if dry_run else "[改名]", os.path.basename(seg.path), new_name))
        mapping[os.path.normcase(seg.path)] = new_path
        if not dry_run:
            try:
                os.rename(seg.path, new_path)
                renamed += 1
            except OSError as exc:
                print("     !! 改名失败：%s" % exc)
                mapping.pop(os.path.normcase(seg.path), None)
                skipped += 1

    idx_changed = sync_index(cfg.index_file, mapping, dry_run)

    print()
    if idx_changed:
        print("索引同步：state/index.jsonl 中 %d 条记录的路径%s。"
              % (idx_changed, "将更新" if dry_run else "已更新"))
    if dry_run:
        print("预览完成：%d 个待改名，%d 个无需改动。确认无误后去掉 --dry-run 再跑一次即可改名。"
              % (len(segs) - skipped, skipped))
    else:
        print("完成：改名 %d 个，跳过 %d 个。" % (renamed, skipped))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
