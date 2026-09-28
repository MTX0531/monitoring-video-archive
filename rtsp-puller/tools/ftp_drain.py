# -*- coding: utf-8 -*-
r"""把 ftp-inbox 里积压的文件补进入库流程。

用途：
  1. 接收端曾中断、收件箱里堆了文件（收到时的入库回调没跑到）时抢救；
  2. 事后补偿入库，而不必重传。

行为与接收端收到文件时完全一致：转封装成 mp4 → 按归档命名落到归档根
→ 写台账 → **永久删除收件箱原件**（成功的前提下）。

用法：
    python tools/ftp_drain.py                 # 处理全部积压
    python tools/ftp_drain.py --dry-run       # 只列出将要处理的文件
    python tools/ftp_drain.py --limit 20      # 只处理前 N 个

⚠️ 落盘位置一律由 `ftp_ingest.ingest()` 自己按 `archive` 段决定（= 与 RTSP 同一个
   归档根），**绝不能把收件箱当输出根传进去**。

另有安全约束：正在上传的文件不能动。设备是边转边传，落盘的 `.dav` 在传输结束前
就是半截文件；此时入库会把**残缺录像**当成品发布、再永久删掉原件 —— 数据就没了。
因此只处理 mtime 已经静默 `--min-age` 秒的文件。
"""
import argparse
import importlib.util
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from nvrcore.config import load_config, pick_storage_root   # noqa: E402
from nvrcore.downloader import find_ffmpeg                 # noqa: E402
from nvrcore import ftp_ingest                             # noqa: E402

TMP_PREFIXES = (".ingest-tmp_",)

# 上传中的文件至少静默这么久才认为传完了（设备 4 MB/s 左右，200MB 约 1 分钟）
DEFAULT_MIN_AGE = 180.0


class PrintLogger:
    def debug(self, msg, *a, **k):
        pass

    def _out(self, tag, msg, args):
        print("  [%s] %s" % (tag, (msg % args) if args else msg))

    def info(self, msg, *a, **k):
        self._out("INFO", msg, a)

    def warning(self, msg, *a, **k):
        self._out("WARN", msg, a)

    def error(self, msg, *a, **k):
        self._out("ERR ", msg, a)


def load_ftp_cfg(base):
    """读目标目录 config.json 的 ftp 段（与 ftp_receiver 同一套解析）。

    注意分工：**代码**永远从本脚本所在目录加载（换 base 不该换实现），
    **配置**才跟着 base 走 —— 测试用的临时目录里没有 tools/ 子目录，
    若两边都按 base 找，脚本会在测试环境里直接崩掉。
    """
    spec = importlib.util.spec_from_file_location(
        "ftp_receiver", os.path.join(HERE, "ftp_receiver.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.load_ftp_config(base)
    return mod, cfg


def collect_pending(root, min_age=DEFAULT_MIN_AGE, now=None):
    """列出可以安全处理的积压文件。

    排除两类：
      * 入库临时文件（`.ingest-tmp_*`）—— 是别的进程的中间产物；
      * mtime 距现在不足 `min_age` 秒的 —— 设备可能还在往里写。
    返回 (可处理 list, 还在写 list)。
    """
    now = time.time() if now is None else now
    ready, writing = [], []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            if f.startswith(TMP_PREFIXES):
                continue
            p = os.path.join(dp, f)
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                continue
            (writing if age < min_age else ready).append(p)
    ready.sort(key=lambda p: os.path.getmtime(p))
    writing.sort(key=lambda p: os.path.getmtime(p))
    return ready, writing


def main(argv=None, base=None):
    ap = argparse.ArgumentParser(description="补入队：处理 ftp-inbox 里积压的文件")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不处理")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个（0=全部）")
    ap.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE,
                    help="文件静默多少秒后才处理（默认 %g，防半截上传）"
                         % DEFAULT_MIN_AGE)
    args = ap.parse_args(argv)

    base = base or BASE
    mod, ftp_cfg = load_ftp_cfg(base)
    inbox = mod._abs(base, ftp_cfg["root"])

    proj = load_config(None, base_dir=base)
    ffmpeg = find_ffmpeg(proj)
    logger = PrintLogger()

    ftp_ingest.ensure_names(proj, logger)
    out_root = pick_storage_root(proj)

    pending, writing = collect_pending(inbox, args.min_age)

    print("收件箱  : %s" % inbox)
    print("输出根  : %s  ← 与 RTSP 拉取同一个归档根" % out_root)
    print("待处理  : %d 个文件" % len(pending))
    if writing:
        print("还在写  : %d 个文件（不足 %.0f 秒，本次跳过）"
              % (len(writing), args.min_age))
    if not pending:
        return 0
    if args.dry_run:
        for p in pending[:40]:
            print("  %s  (%.2f MB)"
                  % (os.path.relpath(p, inbox),
                     os.path.getsize(p) / 1048576.0))
        if len(pending) > 40:
            print("  ... 还有 %d 个" % (len(pending) - 40))
        print("\n--dry-run：未做任何改动")
        return 0

    limit = args.limit if args.limit > 0 else len(pending)
    stats = {"ok": 0, "skipped": 0, "failed": 0, "error": 0}
    print("")
    for i, p in enumerate(pending[:limit], 1):
        rel = os.path.relpath(p, inbox)
        print("[%d/%d] %s" % (i, min(limit, len(pending)), rel))
        try:
            res = ftp_ingest.ingest(proj, p, logger, ffmpeg=ffmpeg,
                                    name_hint=os.path.basename(p))
            st = res.get("status", "?")
            if st == "ok":
                stats["ok"] += 1
                print("     -> 已入库 %s (%.2f MB)"
                      % (os.path.basename(res["path"]),
                         res.get("size", 0) / 1048576.0))
            elif st == "skipped":
                stats["skipped"] += 1
                print("     -> 目标已存在，跳过")
            else:
                stats["failed"] += 1
                print("     -> 失败: %s" % res.get("detail", ""))
        except Exception as exc:                       # noqa: BLE001
            stats["error"] += 1
            print("     -> 异常: %r" % exc)

    left = []
    for dp, dn, fn in os.walk(inbox):
        for f in fn:
            if not f.startswith(TMP_PREFIXES):
                left.append(os.path.join(dp, f))
    print("")
    print("完成: 入库 %d / 跳过 %d / 失败 %d / 异常 %d"
          % (stats["ok"], stats["skipped"], stats["failed"], stats["error"]))
    if left:
        print("收件箱仍有 %d 个文件未处理（可能是还在上传或入库失败）：" % len(left))
        for p in left[:10]:
            print("   %s" % os.path.relpath(p, inbox))
    return 0 if (stats["failed"] == 0 and stats["error"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
