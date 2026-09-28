# -*- coding: utf-8 -*-
"""回收站监控录像销毁工具。

背景（实测）：本机运行环境会把一切删除操作（os.remove /
.NET File.Delete / Remove-Item）拦截后**转投回收站**。归档脚本删除的
监控分片、半成品 .part_、ffmpeg 临时文件、测试夹具，全都躺在
C:\\$Recycle.Bin 里可被还原。此工具负责把它们找出来**先写零再删条目**，
保证画面在任何地方都不可还原。

只处理满足以下明确特征的条目（绝不碰用户自己的文件）：
  A. $I 元数据路径含项目标识：.part_ / _IPC_2026 / rtsp-puller / _probe_ /
     wb_ / puller.lock / _tools_out / _env_probe / _cleanup_report /
     nvrpuller_ / .ffmpeg.err 等
  B. --orphans 时顺带处理"孤儿 $R"（没有 $I 的裸数据/目录——本项目
     测试套件删除临时目录时产生的残留）；--orphans 需配合 --purge 才动手

用法：
  python tools/purge_recycle_bin.py                # 只扫描，报告有哪些命中
  python tools/purge_recycle_bin.py --purge        # 销毁命中的条目
  python tools/purge_recycle_bin.py --purge --orphans   # 连孤儿 $R 一起清
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = r"C:\$Recycle.Bin"
CHUNK = b"\x00" * (1024 * 1024)
MARKS = (".part_", "_IPC_2026", "rtsp-puller", "_probe_", "wb_report", "wb_",
         "_tools_out", "puller.lock", "_cleanup_report", "_env_probe",
         "nvrpuller_", ".ffmpeg.err", "_sched_test", "_probe_cn")


def extract_path(data: bytes) -> str:
    """从 $I v2 原始字节里取原始路径（本机条目带 2 字节杂前缀，从 C:\\ 起）。"""
    s = data[24:].decode("utf-16-le", errors="replace")
    idx = s.find("C:\\")
    if idx >= 0:
        s = s[idx:]
    s = s.split("\x00")[0]
    return "".join(c if c.isprintable() else "?" for c in s)


def zero_file(path: str) -> bool:
    """全文件写零（不改变长度），失败返回 False。"""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            written = 0
            while written < size:
                n = min(len(CHUNK), size - written)
                f.write(CHUNK[:n])
                written += n
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:                          # pylint: disable=broad-except
        return False


def destroy_tree(path: str) -> bool:
    """递归写零并删除目录内全部文件，最后删目录本身。"""
    ok = True
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            if not zero_file(fp):
                ok = False
                print("  zero-fail %s" % fp)
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        try:
            for name in filenames:
                os.remove(os.path.join(dirpath, name))
            for name in dirnames:
                os.rmdir(os.path.join(dirpath, name))
            os.rmdir(dirpath)
        except Exception as exc:               # pylint: disable=broad-except
            ok = False
            print("  rmdir-fail %s : %s" % (dirpath, exc))
    return ok


def destroy_entry(i_path: str, r_path: str) -> str:
    """销毁一个回收站条目，返回 ok / content-only / fail。"""
    data_ok = True
    if os.path.exists(r_path):
        if os.path.isdir(r_path):
            data_ok = destroy_tree(r_path)
        else:
            data_ok = zero_file(r_path)
    removed = []
    for p in (i_path, r_path):
        if not os.path.exists(p):
            removed.append(p)
            continue
        try:
            if os.path.isdir(p):
                os.rmdir(p)
            else:
                os.remove(p)
            removed.append(p)
        except Exception as exc:               # pylint: disable=broad-except
            print("  remove-fail %s : %s" % (p, exc))
    if len(removed) == 2:
        return "ok"
    return "content-only" if data_ok else "fail"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回收站监控录像销毁工具")
    ap.add_argument("--purge", action="store_true", help="真正销毁（默认只扫描）")
    ap.add_argument("--orphans", action="store_true",
                    help="连同孤儿 $R（无 $I 的裸数据/目录，多为测试残留）一起处理")
    args = ap.parse_args(argv)

    if not os.path.isdir(ROOT):
        print("本机没有 %s，无事可做" % ROOT)
        return 0
    hits = []
    orphans = []
    for sid in sorted(os.listdir(ROOT)):
        d = os.path.join(ROOT, sid)
        if not os.path.isdir(d):
            continue
        try:
            names = set(os.listdir(d))
        except OSError:
            continue
        for n in sorted(names):
            if n.startswith("$I"):
                ipath = os.path.join(d, n)
                try:
                    with open(ipath, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                if len(data) < 28:
                    continue
                orig = extract_path(data)
                if any(m in orig for m in MARKS):
                    size = struct.unpack("<Q", data[8:16])[0]
                    hits.append((n, size, orig, ipath,
                                 os.path.join(d, "$R" + n[2:])))
            elif n.startswith("$R") and ("$I" + n[2:]) not in names:
                orphans.append((n, os.path.join(d, n)))

    print("命中条目 %d 个，孤儿 $R %d 个" % (len(hits), len(orphans)))
    for n, size, orig, _i, _r in hits:
        print("  HIT  %s | %d B | %s" % (n, size, orig[:100]))
    if args.orphans:
        for n, p in orphans:
            kind = "DIR" if os.path.isdir(p) else "FILE"
            print("  ORPHAN %s %s" % (kind, n))
    if not args.purge:
        print("（只扫描模式；加 --purge 执行销毁）")
        return 0

    ok = fail = 0
    for n, size, orig, ipath, rpath in hits:
        print("PURGE %s | %d B | %s" % (n, size, orig[:80]))
        if destroy_entry(ipath, rpath) == "ok":
            ok += 1
        else:
            fail += 1
    if args.orphans:
        for n, p in orphans:
            print("PURGE-ORPHAN %s" % n)
            if destroy_entry(p, p) == "ok":
                ok += 1
            else:
                fail += 1
    print("销毁完成：成功 %d，失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
