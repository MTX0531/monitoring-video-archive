#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑齐离线回归测试。

用法：
    python tools/run_all_tests.py              # 跑全部离线测试（跳过真机测试）
    python tools/run_all_tests.py --all         # 连真机测试（会向录像机加流）一起跑
    python tools/run_all_tests.py --list        # 只列出会跑哪些测试
    python tools/run_all_tests.py daily lock    # 只跑名字里含 daily / lock 的

设计说明
--------
- 自动发现 tools/ 下所有 `*_test.py`，无需手工维护清单。
- **默认跳过真机测试**（标记见 NETWORK_TESTS）：这些测试会真的去连录像机拉流，
  在归档任务正在跑的时候再跑会给设备叠加并发路数，可能挤掉正常归档。
  要跑必须显式 `--all`。
- 退出码：0 全过 / 1 有用例失败或某个测试文件非 0 退出。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 会真连录像机/真拉流的测试文件（默认不跑）
NETWORK_TESTS = {"schedule_test.py"}

PASS_RES = (
    re.compile(r"(\d+)\s*项?\s*通过"),
    re.compile(r"通过\s*[:：]?\s*(\d+)"),
)
FAIL_RES = (
    re.compile(r"(\d+)\s*项?\s*失败"),
    re.compile(r"失败\s*[:：]?\s*(\d+)"),
)


def discover() -> list:
    files = []
    tdir = os.path.join(BASE_DIR, "tools")
    for name in sorted(os.listdir(tdir)):
        if name.endswith("_test.py") and name != "run_all_tests.py":
            files.append(name)
    return files


def parse_counts(output: str):
    """从输出尾部抓最后一行统计，尽量还原 (通过, 失败)。"""
    passed = failed = None
    for line in reversed(output.splitlines()):
        if ("通过" in line or "失败" in line) and any(c.isdigit() for c in line):
            for pat in PASS_RES:
                m = pat.search(line)
                if m:
                    passed = int(m.group(1))
                    break
            for pat in FAIL_RES:
                m = pat.search(line)
                if m:
                    failed = int(m.group(1))
                    break
            if passed is not None or failed is not None:
                break
    return passed, failed


def tail(output: str, n: int = 4) -> str:
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    return " / ".join(lines[-n:]) if lines else "(无输出)"


def run_one(name: str, quiet: bool):
    path = os.path.join(BASE_DIR, "tools", name)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run([sys.executable, path], cwd=BASE_DIR, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", errors="replace")
    passed, failed = parse_counts(out)
    ok = (proc.returncode == 0) and (failed in (None, 0))
    if quiet and ok:
        detail = ""
    else:
        detail = tail(out)
    return ok, proc.returncode, passed, failed, detail


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="跑齐 tools/ 下的离线回归测试")
    p.add_argument("--all", action="store_true",
                   help="连真机测试一起跑（会向录像机加流，归档任务在跑时别用）")
    p.add_argument("--list", action="store_true", help="只列出将运行的测试")
    p.add_argument("filters", nargs="*", help="只跑名字里含这些关键字的测试")
    args = p.parse_args(argv)

    files = discover()
    if not args.all:
        files = [f for f in files if f not in NETWORK_TESTS]
    if args.filters:
        files = [f for f in files if any(k in f for k in args.filters)]

    if args.list:
        for f in files:
            tag = "  [真机]" if f in NETWORK_TESTS else ""
            print("%-26s%s" % (f, tag))
        return 0

    if not files:
        print("没有匹配的测试文件")
        return 0

    results = []
    for f in files:
        ok, rc, passed, failed, detail = run_one(f, quiet=True)
        results.append((f, ok, rc, passed, failed, detail))
        status = "OK  " if ok else "FAIL"
        cnt = ""
        if passed is not None:
            cnt = "通过 %d 项" % passed
            if failed:
                cnt += "，失败 %d 项" % failed
        print("[%s] %-26s %s" % (status, f, cnt))
        if not ok:
            print("        退出码 %d | %s" % (rc, detail))

    total_pass = sum(r[3] or 0 for r in results)
    total_fail = sum(r[4] or 0 for r in results)
    bad = [r[0] for r in results if not r[1]]

    print("=" * 66)
    print("文件 %d 个 | 用例通过 %d 项 | 用例失败 %d 项 | 异常文件 %d 个"
          % (len(results), total_pass, total_fail, len(bad)))
    if bad:
        print("失败文件：" + "、".join(bad))
    print("=" * 66)
    return 1 if bad or total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
