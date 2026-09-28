# -*- coding: utf-8 -*-
"""验证「按录像机实际情况命名」这块功能。

覆盖：
  A. auto 判定与名称清洗
  B. 身份解析（真机读取）
  C. apply_identity 的回退链（设备 > 缓存 > 兜底），且绝不覆盖显式配置
  D. 缓存读写与版本/损坏处理
  E. 设备不可达时的降级（不阻断拉取）
  F. 命名 → 文件名 → 反向解析 的闭环
  G. --device-info 命令

用法：python tools/device_naming_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nvrcore import storage
from nvrcore.config import (AUTO, Config, ArchiveConfig, DeviceConfig, NvrConfig,
                            load_config, sanitize_name, is_auto)
from nvrcore.deviceinfo import (DeviceIdentity, load_cache, save_cache,
                                fetch_identity, resolve_identity)

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望=%r\n         实得=%r" % (label, want, got))


def check_true(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s   %s" % (label, detail))


# =========================================================================== #
print("=" * 76)
print("A. auto 判定与名称清洗")
print("=" * 76)
for raw in ("", "auto", "AUTO", "  auto  ", "Auto"):
    check("is_auto(%r) 视为自动读取" % raw, is_auto(raw), True)
for raw in ("IPC", "测试门店A", "通道1", "0", "auto-x"):
    check("is_auto(%r) 视为显式指定" % raw, is_auto(raw), False)

check("下划线被替换（字段分隔符不可出现在名称里）",
      sanitize_name("测试_门店", "x"), "测试-门店")
check("非法字符被替换",
      sanitize_name('A/B\\C:D*E?F"G<H>I|J', "x"), "A-B-C-D-E-F-G-H-I-J")
check("空白折叠", sanitize_name("测试   门店", "x"), "测试-门店")
check("纯非法字符时用兜底值", sanitize_name("///", "FALLBACK"), "FALLBACK")
check("空值用兜底值", sanitize_name("", "FALLBACK"), "FALLBACK")

# =========================================================================== #
print()
print("=" * 76)
print("B. 身份解析（真机读取）")
print("=" * 76)
cfg = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
live = None
try:
    live = fetch_identity(cfg.nvr.host, cfg.nvr.username, cfg.nvr.password,
                          cfg.device.http_port, cfg.nvr.channel, timeout=6.0)
    print("  真机返回：设备名=%r  型号=%r  序列号=%r" % (live.machine_name, live.model, live.serial))
    check_true("读到了设备名称(MachineName)", bool(live.machine_name))
    check_true("读到了至少 1 个通道名称", len(live.channel_titles) >= 1)
except Exception as exc:                                     # noqa: BLE001
    print("  [SKIP] 真机不可达（%s），跳过设备读取断言" % exc)

# ---- 通道名默认值识别 ----
print()
id1 = DeviceIdentity(machine_name="X", channel_titles={1: "IPC", 2: "通道1", 3: "", 4: "通道7"})
check("IPC 被识别为默认名", id1.is_placeholder_channel(1), True)
check("通道1 不认为是默认名", id1.is_placeholder_channel(2), False)
check("空通道名视为未命名", id1.is_placeholder_channel(3), True)
check("通道7 视为默认名", id1.is_placeholder_channel(4), True)
check("不存在的通道视为未命名", id1.is_placeholder_channel(99), True)

# =========================================================================== #
print()
print("=" * 76)
print("C. apply_identity 回退链")
print("=" * 76)


def fresh_cfg(**archive_kw):
    c = Config(base_dir=BASE, nvr=NvrConfig(host="192.168.2.10", channel=1))
    for k, v in archive_kw.items():
        setattr(c.archive, k, v)
    return c


# C1: auto + 真机身份
c = fresh_cfg(store_name=AUTO, channel_name=AUTO)
ident = DeviceIdentity(machine_name="测试门店A", channel_titles={1: "通道1"})
store, ch = c.apply_identity(ident)
check("门店名取自 MachineName", store, "测试门店A")
check("通道名取自 ChannelTitle[通道]", ch, "通道1")

# C2: 门店名含非法字符/下划线
c = fresh_cfg(store_name=AUTO, channel_name=AUTO)
store, ch = c.apply_identity(DeviceIdentity(machine_name="<门店名>_总", channel_titles={1: "通道A_1"}))
check("门店名里的下划线被替换", store, "<门店名>-总")
check("通道名里的下划线被替换", ch, "通道A-1")

# C3: auto + 身份为 None -> 兜底且文件名合法
c = fresh_cfg(store_name=AUTO, channel_name=AUTO)
store, ch = c.apply_identity(None)
check_true("门店名兜底非空", bool(store), "实得 %r" % store)
check("通道名兜底为『通道1』", ch, "通道1")
name = storage.build_segment_name(c, __import__("datetime").datetime(2026, 9, 14, 14, 55),
                                  __import__("datetime").datetime(2026, 9, 14, 15, 25), "mp4")
check_true("兜底命名仍是合法文件名", storage.parse_segment_name(name) is not None, name)

# C4: 显式配置不被设备值覆盖
c = fresh_cfg(store_name="<门店7>", channel_name="通道1")
store, ch = c.apply_identity(DeviceIdentity(machine_name="测试门店A", channel_titles={1: "IPC"}))
check("显式门店名不被覆盖", store, "<门店7>")
check("显式通道名不被覆盖", ch, "通道1")

# C5: 只 auto 一个字段
c = fresh_cfg(store_name="<门店7>", channel_name=AUTO)
store, ch = c.apply_identity(DeviceIdentity(machine_name="X", channel_titles={1: "通道1"}))
check("门店名保留配置值", store, "<门店7>")
check("通道名自动读取", ch, "通道1")

# C6: names_need_device
check("两个都 auto -> 需要读设备", fresh_cfg(store_name=AUTO, channel_name=AUTO).names_need_device(), True)
check("都显式 -> 不需要读设备", fresh_cfg(store_name="A", channel_name="B").names_need_device(), False)

# =========================================================================== #
print()
print("=" * 76)
print("D. 缓存读写")
print("=" * 76)
tmp = tempfile.mkdtemp(prefix="devname_")
cache_path = os.path.join(tmp, "state", "device.json")
try:
    src = DeviceIdentity(machine_name="测试门店A", channel_titles={1: "IPC", 2: "<摄像头2>3"},
                         model="DH-NVR4216-HDS2", serial="<序列号2>",
                         firmware="4.0", fetched_at="2026-09-16 11:00:00", source="device")
    save_cache(cache_path, src)
    check_true("缓存文件已写出", os.path.isfile(cache_path))
    back = load_cache(cache_path)
    check_true("缓存可读回", back is not None)
    check("机器名一致", back.machine_name, "测试门店A")
    check("通道表一致", back.channel_titles, {1: "IPC", 2: "<摄像头2>3"})
    check("来源标记为 cache", back.source, "cache")

    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    check("缓存带版本号", data.get("_cache_version"), 1)
    check("通道键以字符串存储", sorted(data["channel_titles"]), ["1", "2"])

    data["_cache_version"] = 999
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    check("版本不符时拒绝使用缓存", load_cache(cache_path), None)

    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("{ 这不是合法 json")
    check("缓存损坏时返回 None（不抛异常）", load_cache(cache_path), None)
    check("缓存不存在时返回 None", load_cache(os.path.join(tmp, "none.json")), None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# =========================================================================== #
print()
print("=" * 76)
print("E. 设备不可达 / 缓存 的回退链")
print("=" * 76)
tmp = tempfile.mkdtemp(prefix="devfall_")
try:
    missing_cache = os.path.join(tmp, "no_such_cache.json")

    # E1: 设备不可达 + 无缓存 -> 兜底（且绝不抛异常）
    c = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
    c.nvr.host = "192.0.2.1"
    c.device.timeout_seconds = 1.0
    c.device.refresh_hours = 0
    c.device.cache_file = missing_cache
    c.archive.store_name = AUTO
    c.archive.channel_name = AUTO
    try:
        ident = resolve_identity(c, force_refresh=True, quiet=True)
        check_true("不可达时返回兜底身份而非抛异常", ident is not None)
        check("来源标记为 fallback", ident.source, "fallback")
        store, ch = c.apply_identity(ident)
        check_true("门店名降级为非空值", bool(store), "实得 %r" % store)
        check("通道名降级为『通道1』", ch, "通道1")
    except Exception as exc:                                 # noqa: BLE001
        check_true("不可达时不抛异常", False, "抛出：%s" % exc)

    # E2: 设备不可达但本地有缓存 -> 用缓存
    import time as _time
    from datetime import datetime, timedelta
    _fetched = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    cache2 = os.path.join(tmp, "device.json")
    save_cache(cache2, DeviceIdentity(machine_name="测试门店A",
                                      channel_titles={1: "IPC"},
                                      fetched_at=_fetched, source="device"))
    c = load_config(os.path.join(BASE, "config.json"), base_dir=BASE)
    c.nvr.host = "192.0.2.1"
    c.device.timeout_seconds = 1.0
    c.device.refresh_hours = 24
    c.device.cache_file = cache2
    c.archive.store_name = AUTO
    c.archive.channel_name = AUTO
    t0 = _time.time()
    ident = resolve_identity(c, quiet=True)
    elapsed = _time.time() - t0
    check("缓存有效时直接使用缓存", ident.source, "cache")
    check_true("缓存有效时不发起网络请求（耗时 %.2fs < 1s）" % elapsed, elapsed < 1.0)
    store, ch = c.apply_identity(ident)
    check("门店名取自缓存", store, "测试门店A")
    check("通道名取自缓存", ch, "IPC")

    c.device.refresh_hours = 0
    ident = resolve_identity(c, force_refresh=True, quiet=True)
    check("缓存过期且设备不可达时退回缓存", ident.source, "cache-offline")
    check("退回缓存时名称仍可用", c.apply_identity(ident), ("测试门店A", "IPC"))

    # E3: device.enabled=false -> 不读设备，显式配置保留
    c2 = fresh_cfg(store_name="<门店7>", channel_name="通道1")
    c2.device = DeviceConfig(enabled=False, cache_file=missing_cache)
    i2 = resolve_identity(c2, force_refresh=True, quiet=True)
    check("device.enabled=false 且无缓存时来源为 fallback", i2.source, "fallback")
    store, ch = c2.apply_identity(i2)
    check("关闭设备读取时保留配置名称", (store, ch), ("<门店7>", "通道1"))

    # E4: device.enabled=false + 有缓存 + auto -> 用缓存
    c3 = fresh_cfg(store_name=AUTO, channel_name=AUTO)
    c3.device = DeviceConfig(enabled=False, cache_file=cache2)
    i3 = resolve_identity(c3, force_refresh=True, quiet=True)
    check("关闭设备读取但有缓存时来源为 cache", i3.source, "cache")
    store, ch = c3.apply_identity(i3)
    check("关闭设备读取时用缓存命名", (store, ch), ("测试门店A", "IPC"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# =========================================================================== #
print()
print("=" * 76)
print("F. 命名 → 文件名 → 反向解析 闭环（留存清理依赖）")
print("=" * 76)
from datetime import datetime as _dt

c = fresh_cfg(store_name=AUTO, channel_name=AUTO)
c.apply_identity(DeviceIdentity(machine_name="测试门店A", channel_titles={1: "IPC"}))
start, end = _dt(2026, 9, 14, 14, 55, 0), _dt(2026, 9, 14, 15, 25, 0)
fname = storage.build_segment_name(c, start, end, "mp4")
check("文件名符合云联格式",
      fname, "测试门店A_IPC_20260914145500_20260914152500_device.mp4")
info = storage.parse_segment_name(fname)
check_true("能被反向解析", info is not None)
check("解析出的门店名", info["store"], "测试门店A")
check("解析出的通道名", info["channel_name"], "IPC")
check("解析出的起点", info["start"], start)
check("解析出的终点", info["end"], end)

# 门店名带 '-总' 后缀
c = fresh_cfg(store_name=AUTO, channel_name=AUTO)
c.apply_identity(DeviceIdentity(machine_name="<门店名>-总", channel_titles={1: "通道1"}))
f2 = storage.build_segment_name(c, start, end, "mp4")
check("带 -总 后缀的门店名", f2, "<门店名>-总_通道1_20260914145500_20260914152500_device.mp4")
info2 = storage.parse_segment_name(f2)
check_true("带 -总 的文件名仍可解析", info2 is not None, f2)
check("解析出的门店名保留 -总", info2["store"], "<门店名>-总")

# 扫描目录时能被识别
tmp = tempfile.mkdtemp(prefix="devscan_")
try:
    d = os.path.join(tmp, "2026-09-14")
    os.makedirs(d)
    for n in (fname, f2, "测试门店-临时_通道1_20260914145500_20260914152500_device.mp4",
              "readme.md", "随手拍.mp4"):
        with open(os.path.join(d, n), "wb") as fh:
            fh.write(b"x" * 4096)
    found = sorted(os.path.basename(s.path) for s in storage.scan_segments(tmp))
    check_true("三种命名的归档案都被识别", len(found) == 3, str(found))
    check_true("readme.md 未被识别", "readme.md" not in found)
    check_true("无关的 mp4 未被识别", "随手拍.mp4" not in found)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# =========================================================================== #
print()
print("=" * 76)
print("G. --device-info 命令")
print("=" * 76)
proc = subprocess.run([sys.executable, os.path.join(BASE, "nvr_puller.py"), "--device-info"],
                      cwd=BASE, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=120)
out = (proc.stdout or "") + (proc.stderr or "")
check("命令退出码为 0", proc.returncode, 0)
check_true("输出中包含命名示例行", "归档案命名将使用" in out, out[-400:])

# =========================================================================== #
print()
print("=" * 76)
print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 76)
sys.exit(1 if FAIL else 0)
