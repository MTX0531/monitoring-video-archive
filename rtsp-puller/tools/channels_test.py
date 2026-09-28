# -*- coding: utf-8 -*-
"""通道清单模块（nvrcore.channels）的离线断言测试。

不联网：用真机抓下来的 RemoteDevice / ChannelTitle 响应片段做输入，
验证解析、判定、报告生成三部分（内容已脱敏为占位符）。
真机探测请直接跑 `python nvr_puller.py --channels --probe`。

用法：python tools/channels_test.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nvrcore.channels import (ChannelInfo, ChannelMap, CameraInfo,
                              _disp_width, _pad, _parse_channel_titles,
                              _parse_remote_device, describe_channels)
from nvrcore.config import Config

PASS = 0
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  [FAIL] %s" % msg)


# --------------------------------------------------------------------------- #
#  真机抓取的真实响应（本机 DH-NVR4216-HDS2，通道 1 接了一路 IPC）
#  含门店、摄像头名等真实信息，已脱敏为占位符。
# --------------------------------------------------------------------------- #
REMOTE_DEVICE_BODY = """table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Name=<摄像头1>
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Address=192.168.2.13
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Mac=08:ed:ed:28:8f:97
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Port=37777
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.HttpPort=80
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.UserName=admin
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Vendor=Private
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.ProtocolType=Dahua2
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.DeviceType=IPC-HDW1025C
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.SerialNo=<序列号1>
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Name=
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Enable=false
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Address=192.168.0.0
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.DeviceType=DH-IPC-HDW1235C-A-V7
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Enable=false
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Address=192.168.0.0
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_15.Enable=false
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_15.Address=192.168.0.0
"""

TITLES_BODY = """table.ChannelTitle[0].Name=IPC
table.ChannelTitle[1].Name=<摄像头2>3
table.ChannelTitle[2].Name=IPC
table.ChannelTitle[3].Name=通道7
table.ChannelTitle[4].Name=<场所A>
table.ChannelTitle[5].Name=<场所B>2
table.ChannelTitle[6].Name=<场所C>2
table.ChannelTitle[7].Name=前台
table.ChannelTitle[8].Name=<场所D>1
table.ChannelTitle[9].Name=<摄像头2>5
table.ChannelTitle[10].Name=<场所E>3
table.ChannelTitle[11].Name=<场所F>入口
table.ChannelTitle[12].Name=IPC
table.ChannelTitle[13].Name=<场所E>2
table.ChannelTitle[14].Name=<场所B>1
table.ChannelTitle[15].Name=<场所F>入口
"""


def test_parse_remote_device() -> None:
    print("解析 RemoteDevice")
    cams, enabled = _parse_remote_device(REMOTE_DEVICE_BODY)
    check(len(cams) == 4, "应解析出 4 个条目，实际 %d" % len(cams))
    check(enabled.get(0) is True, "条目 0 应为 Enable=true")
    check(enabled.get(1) is False, "条目 1 应为 Enable=false")
    check(enabled.get(2) is False, "条目 2 应为 Enable=false")

    c0 = cams[0]
    check(c0.address == "192.168.2.13", "条目 0 地址，实际 %r" % c0.address)
    check(c0.device_type == "IPC-HDW1025C", "条目 0 型号，实际 %r" % c0.device_type)
    check(c0.serial == "<序列号1>", "条目 0 序列号，实际 %r" % c0.serial)
    check(c0.protocol == "Dahua2", "条目 0 协议，实际 %r" % c0.protocol)
    check(c0.port == 37777, "条目 0 端口，实际 %r" % c0.port)
    check(not c0.is_placeholder, "条目 0 不应被判为占位地址")

    check(cams[1].is_placeholder, "条目 1 地址 192.168.0.0 应判为占位")
    check(cams[1].device_type == "DH-IPC-HDW1235C-A-V7", "条目 1 仍应解析出型号")


def test_parse_titles() -> None:
    print("解析 ChannelTitle")
    titles = _parse_channel_titles(TITLES_BODY)
    check(len(titles) == 16, "应有 16 个通道，实际 %d" % len(titles))
    check(titles.get(1) == "IPC", "通道 1 名称，实际 %r" % titles.get(1))
    check(titles.get(2) == "<摄像头2>3", "通道 2 名称，实际 %r" % titles.get(2))
    check(titles.get(16) == "<场所F>入口", "通道 16 名称，实际 %r" % titles.get(16))
    check(16 in titles, "索引 15 应映射为通道 16（1-based）")


def build_map(probed: bool = False) -> ChannelMap:
    cams, enabled = _parse_remote_device(REMOTE_DEVICE_BODY)
    titles = _parse_channel_titles(TITLES_BODY)
    cmap = ChannelMap(host="192.168.2.10", machine_name="测试门店A",
                      model="DH-NVR4216-HDS2", serial="<序列号2>",
                      total=max(titles), source="device", probed=probed)
    for ch in sorted(titles):
        cmap.channels.append(ChannelInfo(
            index=ch, title=titles[ch],
            enabled=bool(enabled.get(ch - 1, False)),
            camera=cams.get(ch - 1)))
    if probed:
        for c in cmap.channels:
            c.live_code = 200 if c.index == 1 else None
    return cmap


def test_has_camera() -> None:
    print("接入判定")
    cmap = build_map()
    check(len(cmap.active) == 1, "应只有 1 路接入，实际 %d" % len(cmap.active))
    check(cmap.active[0].index == 1, "接入的应是通道 1")
    check(len(cmap.idle) == 15, "应有 15 路未接入，实际 %d" % len(cmap.idle))

    fake = ChannelInfo(index=9, title="前台", enabled=True,
                       camera=CameraInfo(address="192.168.0.0"))
    check(not fake.has_camera, "Enable=true 但地址占位时不应算作已接入")
    fake2 = ChannelInfo(index=9, title="前台", enabled=True,
                        camera=CameraInfo(address="192.168.2.14"))
    check(fake2.has_camera, "Enable=true 且地址真实时应算作已接入")
    check(not ChannelInfo(index=9, enabled=False,
                          camera=CameraInfo(address="192.168.2.14")).has_camera,
          "Enable=false 时不应算作已接入")
    check(not ChannelInfo(index=9, enabled=True).has_camera,
          "无摄像头信息时不应算作已接入")


def test_placeholder_title() -> None:
    print("通道名占位判定")
    for name, expect in [("IPC", True), ("ipc", True), ("通道7", True),
                         ("CH3", True), ("IPC1", True), ("", True),
                         ("<摄像头2>3", False), ("前台", False),
                         ("<场所F>入口", False), ("<场所D>1", False),
                         ("<场所F>入口", False)]:
        check(ChannelInfo(index=1, title=name).title_is_placeholder == expect,
              "通道名 %r 占位判定应为 %s" % (name, expect))


def test_streams_and_summary() -> None:
    print("实时流判定与摘要")
    cmap = build_map(probed=False)
    check(cmap.channels[0].streams is None, "未探测时 streams 应为 None")

    cmap2 = build_map(probed=True)
    check(len(cmap2.streaming) == 1, "应有 1 路实测有流，实际 %d" % len(cmap2.streaming))
    check(cmap2.channels[0].streams is True, "通道 1 实测应有流")
    check(cmap2.channels[1].streams is None, "未探测的通道 streams 应为 None")
    check("RTSP 实测有流 1 路" in cmap2.summary(), "摘要应包含实测路数")


def test_display_width() -> None:
    print("显示宽度对齐")
    check(_disp_width("abc") == 3, "ASCII 宽度")
    check(_disp_width("通道") == 4, "中文宽度应为 4")
    check(_disp_width("") == 0, "空串宽度 0")
    check(_disp_width("通道1") == 5, "中英混排宽度")
    check(len(_pad("通道", 6)) == 4 and _pad("通道", 6).endswith("  "),
          "中文补空格后总显示宽度应为 6")
    check(_disp_width(_pad("IPC", 6)) == 6, "ASCII 补空格")
    check(_disp_width(_pad("<摄像头2>3", 6)) == 7, "超宽时不补空格")
    check(_pad("很长的一个名字", 4) == "很长的一个名字", "超宽时不截断")


def test_report() -> None:
    print("报告生成")
    cfg = Config()
    cfg.nvr.channel = 1
    text = describe_channels(cfg, build_map(probed=True))
    for frag in ["通道清单（录像机 192.168.2.10）",
                 "设备型号 : DH-NVR4216-HDS2",
                 "设备支持 : 16 路",
                 "实际接入 : 1 路",
                 "【已接入摄像头】",
                 "IPC-HDW1025C",
                 "192.168.2.13",
                 "200 有流",
                 "<== 本项目使用",
                 "[通道名仍是默认值]",
                 "【未接入通道 15 路】",
                 "<摄像头2>3",
                 "<场所F>入口",
                 "当前配置使用通道 1"]:
        check(frag in text, "报告应包含 %r" % frag)
    check("**" not in text, "报告不应残留 Markdown 粗体语法")
    check("`" not in text, "报告不应残留 Markdown 反引号")

    cfg2 = Config()
    cfg2.nvr.channel = 5
    text2 = describe_channels(cfg2, build_map(probed=True))
    check("【注意】通道 5 不在【已接入摄像头】列表中" in text2,
          "配置指向未接入通道时必须告警")
    check("<== 本项目使用" not in text2, "未接入通道不应被标记为使用中")

    cmap3 = build_map(probed=True)
    cmap3.channels[0].live_code = 401
    text3 = describe_channels(cfg, cmap3)
    check("【警告】" in text3 and "取不到流" in text3, "实测无流时应告警")

    cmap4 = build_map()
    cmap4.source = "title-only"
    check("RemoteDevice 接口不可用" in describe_channels(cfg, cmap4),
          "title-only 来源时应有提示")


def test_edge_cases() -> None:
    print("边界情况")
    check(_parse_remote_device("")[0] == {}, "空响应应返回空字典")
    check(_parse_channel_titles("Error\nBad Request!") == {}, "错误响应应返回空字典")
    cams, enabled = _parse_remote_device(
        "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Enable=TRUE\n"
        "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Address=192.0.2.5\n")
    check(enabled.get(0) is True, "Enable 大小写不敏感")
    check(cams[0].address == "192.0.2.5", "非 192.168 地址也应解析")

    empty = ChannelMap(host="1.2.3.4", total=0)
    check(empty.active == [] and empty.idle == [], "空清单不应抛异常")
    check("实际接入 : 0 路" in describe_channels(Config(), empty), "空清单报告可生成")


def main() -> int:
    print("=" * 70)
    print("通道清单模块测试（离线，不联网）")
    print("=" * 70)
    test_parse_remote_device()
    test_parse_titles()
    test_has_camera()
    test_placeholder_title()
    test_streams_and_summary()
    test_display_width()
    test_report()
    test_edge_cases()
    print("=" * 70)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
