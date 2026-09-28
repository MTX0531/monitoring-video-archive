# -*- coding: utf-8 -*-
"""列录像机硬盘上的原始录像文件（含 [R]/[M] 类型标记）。

用途：回答"设备上到底有没有这段录像""设备是怎么切文件的""我们有没有漏收"。
与 `probe_window.py` 互补——那个用 RTSP 探"某时刻有没有录像"（轻、单点），
这个用 `mediaFileFind` 列"某时段有哪些文件"（重、带类型与大小）。

用法：
    python tools/device_files.py                          # 今天 00:00 到现在
    python tools/device_files.py --start "2026-09-17 00:00:00" --end "2026-09-17 23:59:59"
    python tools/device_files.py --channel 2 --json       # 机器可读

文件名里的类型标记（大华手册）：
    R = 定时录像（regular）；M = 移动侦测（motion）；
    A = 外部报警（alarm）；I = 智能事件（intelligent）

⚠️ 低端机型 Web 服务很脆：连发十几次 CGI 会把它打到全线拒绝连接。
   本工具一次调用只开一个 find 会话，页间带间隔，请勿并发多开。
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from nvrcore.config import load_config                        # noqa: E402
from nvrcore.deviceinfo import _DigestAuth, _http_get         # noqa: E402

TIME_FMT = "%Y-%m-%d %H:%M:%S"
PAGE = 100                 # 每轮向设备要多少个
MAX_PAGES = 40             # 上限，防止失控翻页
PAGE_GAP = 0.8             # 页间隔（秒），别缩短
ITEM_RE = re.compile(r"items\[(\d+)\]\.(\w+)=(.*)")
NAME_TYPE_RE = re.compile(r"-(\d\d\.\d\d\.\d\d)\[([A-Z])\]")
NAME_RANGE_RE = re.compile(r"^(\d\d\.\d\d\.\d\d)-(\d\d\.\d\d\.\d\d)\[")
LOOSE_TYPE_RE = re.compile(r"\[([A-Z])\]")


def classify(filename):
    """从设备文件名里取录像类型标记；取不到返回 '?'。"""
    name = filename or ""
    m = NAME_TYPE_RE.search(name)
    if m:
        return m.group(2)
    m = LOOSE_TYPE_RE.search(name)
    return m.group(1) if m else "?"


def parse_items(body):
    """把 findNextFile 的响应体解析成记录列表（纯函数，便于离线测试）。"""
    grouped = {}
    for line in (body or "").splitlines():
        m = ITEM_RE.match(line.strip())
        if not m:
            continue
        grouped.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(3).strip()
    out = []
    for idx in sorted(grouped):
        it = grouped[idx]
        fp = it.get("FilePath", "")
        st, et = it.get("StartTime", ""), it.get("EndTime", "")
        try:
            length = int(it.get("Length") or 0)
        except ValueError:
            length = 0
        out.append({
            "file": fp,
            "name": os.path.basename(fp),
            "start": st,
            "end": et,
            "length": length,
            "type": classify(fp),
            "duration": _span_seconds(st, et),
        })
    return out


def _span_seconds(st, et):
    try:
        a = datetime.datetime.strptime(st, TIME_FMT)
        b = datetime.datetime.strptime(et, TIME_FMT)
        return int((b - a).total_seconds())
    except (ValueError, TypeError):
        return 0


def find_found(body):
    m = re.search(r"found=(\d+)", body or "")
    return int(m.group(1)) if m else None


def summarize(items):
    """按类型汇总（纯函数）。中位时长在偶数个时取偏大的那一个。"""
    agg = {}
    for it in items:
        t = it["type"]
        a = agg.setdefault(t, {"count": 0, "seconds": 0, "bytes": 0, "durs": []})
        a["count"] += 1
        a["seconds"] += it["duration"]
        a["bytes"] += it["length"]
        a["durs"].append(it["duration"])
    for a in agg.values():
        d = sorted(a.pop("durs"))
        a["median"] = d[len(d) // 2] if d else 0
        a["min"] = d[0] if d else 0
        a["max"] = d[-1] if d else 0
    return agg


def gaps(items, threshold=5):
    """相邻文件之间的断档（设备自己就没录的部分）。"""
    out = []
    for prev, cur in zip(items, items[1:]):
        try:
            a = datetime.datetime.strptime(prev["end"], TIME_FMT)
            b = datetime.datetime.strptime(cur["start"], TIME_FMT)
        except (ValueError, TypeError):
            continue
        d = (b - a).total_seconds()
        if d > threshold:
            out.append((prev["end"], cur["start"], d))
    return out


TYPE_LABEL = {"R": "定时录像", "M": "移动侦测", "A": "外部报警", "I": "智能事件"}


def call(host, auth, path, timeout=40.0):
    code, body = _http_get(host, 80, path, auth, timeout)
    return code, (body or "")


def list_files(nvr, start, end, channel=1, log=print):
    auth = _DigestAuth(nvr.username, nvr.password)
    host = nvr.host
    code, body = call(host, auth,
                      "/cgi-bin/mediaFileFind.cgi?action=factory.create")
    m = re.search(r"result=(\d+)", body)
    if not m:
        raise RuntimeError("factory.create 失败：%s %s" % (code, body[:120]))
    obj = m.group(1)
    try:
        q = ("%s&condition.Channel=%d"
             "&condition.StartTime=%s&condition.EndTime=%s"
             "&condition.Types[0]=dav"
             % (obj, channel, start.replace(" ", "%20"), end.replace(" ", "%20")))
        q = "/cgi-bin/mediaFileFind.cgi?action=findFile&object=" + q
        code, body = call(host, auth, q)
        if code != 200:
            raise RuntimeError("findFile 失败：%s %s" % (code, body[:120]))
        time.sleep(0.5)

        items = []
        for page in range(MAX_PAGES):
            code, body = call(host, auth,
                              "/cgi-bin/mediaFileFind.cgi?action=findNextFile"
                              "&object=%s&count=%d" % (obj, PAGE))
            if code != 200:
                raise RuntimeError("findNextFile 失败：%s" % code)
            got = parse_items(body)
            if not got:
                break
            items += got
            found = find_found(body)
            log("  第 %d 页：%d 个（found=%s）"
                % (page + 1, len(got), found if found is not None else "?"))
            if found is not None and len(got) < PAGE:
                break
            time.sleep(PAGE_GAP)
        return items
    finally:
        for act in ("close", "destroy"):
            try:
                call(host, auth,
                     "/cgi-bin/mediaFileFind.cgi?action=%s&object=%s" % (act, obj),
                     15.0)
                time.sleep(0.3)
            except Exception:                     # noqa: BLE001
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="列录像机硬盘上的原始录像文件")
    ap.add_argument("--start", help="起始时间 'YYYY-MM-DD HH:MM:SS'")
    ap.add_argument("--end", help="结束时间 'YYYY-MM-DD HH:MM:SS'")
    ap.add_argument("--channel", type=int, default=1, help="通道号（默认 1）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    now = datetime.datetime.now()
    start = args.start or now.strftime("%Y-%m-%d 00:00:00")
    end = args.end or now.strftime(TIME_FMT)

    cfg = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
    print("查询 %s ~ %s，通道 %d" % (start, end, args.channel))
    items = list_files(cfg.nvr, start, end, args.channel)

    if args.json:
        print(json.dumps({"items": items, "summary": summarize(items)},
                         ensure_ascii=False, indent=2))
        return 0

    print("\n共 %d 个文件：" % len(items))
    for it in items:
        print("  %-42s %s -> %s  %6d秒  %8.2fMB  [%s]"
              % (it["name"][:42], it["start"][11:], it["end"][11:],
                 it["duration"], it["length"] / 1048576.0,
                 TYPE_LABEL.get(it["type"], it["type"])))

    print("\n== 按类型汇总 ==")
    for t, a in sorted(summarize(items).items()):
        print("  [%s] %-8s %2d 个，合计 %.2f 小时 / %.1f MB，"
              "时长 中位 %d 秒（%d~%d）"
              % (t, TYPE_LABEL.get(t, t), a["count"], a["seconds"] / 3600.0,
                 a["bytes"] / 1048576.0, a["median"], a["min"], a["max"]))

    g = gaps(items)
    print("\n== 设备侧自身的断档（相邻文件之间 >5 秒）%d 处 ==" % len(g))
    for a, b, d in g:
        print("  %s -> %s  缺 %.0f 秒" % (a[11:], b[11:], d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
