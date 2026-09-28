# -*- coding: utf-8 -*-
"""tools/device_files.py 的离线回归（不连设备，只测解析与汇总）。"""
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

spec = importlib.util.spec_from_file_location(
    "device_files", os.path.join(BASE, "tools", "device_files.py"))
DF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DF)

N = 0
FAIL = 0


def check(name, got, want):
    global N, FAIL
    N += 1
    if got == want:
        print("  [ok] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  得到=%r 期望=%r" % (name, got, want))


def check_true(name, cond, extra=""):
    global N, FAIL
    N += 1
    if cond:
        print("  [ok] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, extra))


# ---------- 1) 类型标记分类 ----------
print("\n[1] 文件名类型标记")
check("定时录像 [R]",
      DF.classify("08.30.01-08.44.03[R][0@0][0].dav"), "R")
check("移动侦测 [M]",
      DF.classify("08.44.03-08.44.22[M][0@0][0].dav"), "M")
check("外部报警 [A]",
      DF.classify("10.00.00-10.00.30[A][0@0][0].dav"), "A")
check("智能事件 [I]",
      DF.classify("10.00.00-10.00.30[I][0@0][0].dav"), "I")
check("FTP 上传后的名字（无标记）-> ?",
      DF.classify("测试门店A_ch1_main_20260917084403_20260917084422.dav"), "?")
check("空串 -> ?", DF.classify(""), "?")
check("None -> ?", DF.classify(None), "?")
check("普通名字里的方括号不误判",
      DF.classify("录像[0@0][0].dav"), "?")

# ---------- 2) findNextFile 响应解析 ----------
print("\n[2] findNextFile 响应解析")
BODY = """found=3
items[0].Channel=1
items[0].StartTime=2026-09-17 00:00:00
items[0].EndTime=2026-09-17 04:15:00
items[0].FilePath=/mnt/dvr/2026-09-17/1/dav/00.00.00-04.15.00[R][0@0][0].dav
items[0].Length=261046272
items[1].Channel=1
items[1].StartTime=2026-09-17 08:44:03
items[1].EndTime=2026-09-17 08:44:22
items[1].FilePath=/mnt/dvr/2026-09-17/1/dav/08.44.03-08.44.22[M][0@0][0].dav
items[1].Length=2746368
items[2].Channel=1
items[2].StartTime=2026-09-17 09:07:27
items[2].EndTime=2026-09-17 09:08:05
items[2].FilePath=/mnt/dvr/2026-09-17/1/dav/09.07.27-09.08.05[R][0@0][0].dav
items[2].Length=3670016
"""
items = DF.parse_items(BODY)
check("解析出 3 条", len(items), 3)
check("按索引顺序", [i["type"] for i in items], ["R", "M", "R"])
check("取到文件名", items[0]["name"], "00.00.00-04.15.00[R][0@0][0].dav")
check("时长 = 首尾之差（4h15m）", items[0]["duration"], 15300)
check("短片段时长（19 秒）", items[1]["duration"], 19)
check("大小解析", items[1]["length"], 2746368)
check("found 解析", DF.find_found(BODY), 3)
check("无 found 字段 -> None", DF.find_found("items[0].Channel=1"), None)
check("空 body -> 空列表", DF.parse_items(""), [])
check("乱码行被忽略", len(DF.parse_items("hello\nworld=1")), 0)

BROKEN = ("items[0].StartTime=2026-09-17 08:44:03\n"
          "items[0].EndTime=\n"
          "items[0].FilePath=/mnt/dvr/x/f[M][0@0][0].dav\n")
bi = DF.parse_items(BROKEN)
check("时间不完整时长记 0", bi[0]["duration"], 0)
check("时间不完整仍能拿到类型", bi[0]["type"], "M")

BADLEN = ("items[0].StartTime=2026-09-17 08:44:03\n"
          "items[0].EndTime=2026-09-17 08:44:22\n"
          "items[0].Length=abc\n"
          "items[0].FilePath=/mnt/dvr/x/a[R][0@0][0].dav\n")
check("Length 非数字按 0", DF.parse_items(BADLEN)[0]["length"], 0)

# ---------- 3) 汇总 ----------
print("\n[3] 按类型汇总")
s = DF.summarize(items)
check("[R] 计数", s["R"]["count"], 2)
check("[M] 计数", s["M"]["count"], 1)
check("[R] 合计秒数", s["R"]["seconds"], 15300 + 38)
check("[M] 合计秒数", s["M"]["seconds"], 19)
check("[R] 中位时长（偶数个取偏大）", s["R"]["median"], 15300)
check("[R] 最短/最长", (s["R"]["min"], s["R"]["max"]), (38, 15300))
check_true("[R] 合计字节", s["R"]["bytes"] == 261046272 + 3670016)
check("空列表汇总为空", DF.summarize([]), {})

# ---------- 4) 断档 ----------
print("\n[4] 设备侧断档")
g = DF.gaps(items)
check("本例断档 2 处（04:15->08:30、08:44->09:07）", len(g), 2)
check("最大断档 16143 秒", int(g[0][2]), 16143)
check("阈值放大到 20000 秒后无断档", len(DF.gaps(items, threshold=20000)), 0)
check_true("空列表无断档", DF.gaps([]) == [])
check_true("坏时间不炸", DF.gaps(DF.parse_items(BROKEN)) == [])

# ---------- 5) 类型标签 ----------
print("\n[5] 类型标签")
check("R 标签", DF.TYPE_LABEL["R"], "定时录像")
check("M 标签", DF.TYPE_LABEL["M"], "移动侦测")
check_true("未知类型不在标签表里", "Z" not in DF.TYPE_LABEL)

print("\n%s" % ("=" * 60))
print("结果：通过 %d 项，失败 %d 项" % (N - FAIL, FAIL))
sys.exit(1 if FAIL else 0)
