# -*- coding: utf-8 -*-
"""账密验收模块（nvrcore.acceptance）的离线断言测试。

不联网、不碰真机：把 HTTP / RTSP / ffmpeg 三处外部依赖全部替换成假实现，
只为验证"判定逻辑"本身。
真机验收请直接跑 `python nvr_puller.py --accept`。

用法：python tools/acceptance_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nvrcore import acceptance as acc
from nvrcore.channels import ChannelInfo, ChannelMap, CameraInfo
from nvrcore.config import Config, NvrConfig
from nvrcore.downloader import (DescribeResult, PullOutcome, describe_playback,
                                probe_range)

PASS = 0
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  [FAIL] %s" % msg)


def fresh_cfg(base_dir: str = ".") -> Config:
    cfg = Config(base_dir=base_dir)
    cfg.nvr = NvrConfig(host="192.0.2.9", rtsp_port=554, username="op", password="pw")
    return cfg


# --------------------------------------------------------------------------- #
def test_candidate_windows():
    print("[1] 候选验证时段")
    now = datetime(2026, 9, 16, 12, 0, 0)
    ws = acc.candidate_windows(now)
    check(len(ws) == 4, "应有 4 个候选窗口")
    check([w[0] for w in ws] == ["5 分钟前", "35 分钟前", "2 小时前", "昨天此刻"],
          "窗口标签顺序应由近及远")
    check(all((e - s) == timedelta(minutes=1) for _, s, e in ws),
          "每个窗口宽度应为 1 分钟")
    check(ws[0][2] < now, "最近一个窗口必须落在当前时刻之前")
    check(ws[3][2] <= now - timedelta(hours=23), "兜底窗口应在 24 小时前")
    check(acc.candidate_windows(now)[0][2] == now - timedelta(minutes=5),
          "首个窗口终点应为 5 分钟前")


# --------------------------------------------------------------------------- #
def test_rtsp_text():
    print("[2] RTSP 结果文案")
    check("200" in acc._rtsp_text(DescribeResult(200, "auth", "OK")), "200 应带状态码")
    check("有录像" in acc._rtsp_text(DescribeResult(200, "auth", "OK")), "200 应说明有录像")
    check("探测失败" in acc._rtsp_text(DescribeResult(None, "connect", "拒绝连接")),
          "None 应表述为探测失败")
    check("403" in acc._rtsp_text(DescribeResult(403, "auth", "Forbidden")), "403 应带状态码")


# --------------------------------------------------------------------------- #
def test_gate_login():
    print("[3] 门禁 1 · 能登录")
    cfg = fresh_cfg()

    def with_login(fake):
        acc.probe_login = fake
        return acc.gate_login(cfg)[0]

    cfg.nvr.password = ""
    g = with_login(lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该发起请求")))
    check(g.failed and "密码" in g.summary, "空密码应直接判失败且不发请求")

    cfg.nvr.password = "pw"

    g = with_login(lambda *a, **k: (0, "", "无法连接 192.0.2.9:80（timed out）"))
    check(g.failed, "连不上应判失败")
    check("80" in g.advice, "连不上时建议应提到端口")
    check(acc.gate_login(cfg)[1] == "", "登录失败不应带回设备名")

    g = with_login(lambda *a, **k: (401, "", ""))
    check(g.failed and "密码" in g.summary, "401 应判账号密码错")
    check("大小写" in g.advice, "401 的建议应提示核对大小写/空格")

    g = with_login(lambda *a, **k: (403, "", ""))
    check(g.failed, "403 应判失败")
    check("远程配置查询" in g.advice, "403 应指向配置查询权限/白名单")

    g = with_login(lambda *a, **k: (404, "", ""))
    check(g.failed and "404" in g.summary, "404 应判接口不存在")
    check("http_port" in g.advice, "404 应提示核对 Web 端口")

    g = with_login(lambda *a, **k: (500, "", ""))
    check(g.failed and "500" in g.summary, "未预期状态码应如实报出")

    acc.probe_login = lambda *a, **k: (200, "测试门店A", "")
    g = acc.gate_login(cfg)[0]
    check(g.passed, "200 应判通过")
    check("登录成功" in g.summary, "通过时应说明登录成功")
    check(any("测试门店A" in ln for ln in g.lines), "通过时应把设备名写进明细")

    acc.probe_login = lambda *a, **k: (200, "", "")
    g = acc.gate_login(cfg)[0]
    check(g.passed, "读不到 MachineName 不影响登录结论")
    check(any("读不到 MachineName" in ln for ln in g.lines), "应提示设备名为空")


# --------------------------------------------------------------------------- #
def _seq_describe(results):
    """按顺序返回预设结果的假 describe_playback。"""
    it = iter(results)

    def fake(*a, **k):
        try:
            return next(it)
        except StopIteration:
            return results[-1]
    return fake


def test_gate_playback():
    print("[4] 门禁 2 · 能回放")
    cfg = fresh_cfg()
    real = acc.describe_playback

    acc.describe_playback = _seq_describe([DescribeResult(200, "auth", "OK", True)])
    g, hit = acc.gate_playback(cfg)
    check(g.passed, "命中 200 应判通过")
    check(hit is not None and hit[0] == "5 分钟前", "应把命中的窗口带回去给门禁 3")
    check(len(g.lines) == 2, "命中就应停止探测")

    acc.describe_playback = _seq_describe([
        DescribeResult(404, "auth", "Not Found", True),
        DescribeResult(404, "auth", "Not Found", True),
        DescribeResult(200, "auth", "OK", True)])
    g, hit = acc.gate_playback(cfg)
    check(g.passed and hit[0] == "2 小时前", "应继续向后找直到找到有录像的时段")

    acc.describe_playback = _seq_describe([DescribeResult(404, "auth", "Not Found", True)] * 4)
    g, hit = acc.gate_playback(cfg)
    check(g.skipped, "认证通过但无录像样本应判『待复验』而不是通过")
    check(hit is None, "无命中不应把窗口传给门禁 3")
    check("--channels" in g.warning, "应提示核对通道号")

    acc.describe_playback = _seq_describe([DescribeResult(404, "done", "Not Found", False)] * 4)
    g, _h = acc.gate_playback(cfg)
    check(g.failed, "没触发认证挑战的 404 不能算认证通过")

    acc.describe_playback = _seq_describe([DescribeResult(401, "auth", "Unauthorized", True)])
    g, _h = acc.gate_playback(cfg)
    check(g.failed and "密码" in g.summary, "认证后 401 应判密码错")

    acc.describe_playback = _seq_describe([DescribeResult(403, "auth", "Forbidden", True)])
    g, _h = acc.gate_playback(cfg)
    check(g.failed and "远程回放" in g.summary, "403 应直指『远程回放』权限")
    check("用户管理" in g.advice, "403 的建议应说明去哪里开权限")

    acc.describe_playback = _seq_describe([DescribeResult(503, "done", "Service Unavailable", False)])
    g, _h = acc.gate_playback(cfg)
    check(g.failed and "503" in g.summary, "503 应判失败")
    check("并发" in g.advice, "503 应提示并发流已满")

    acc.describe_playback = _seq_describe([DescribeResult(None, "connect", "无法连接 192.0.2.9:554")])
    g, _h = acc.gate_playback(cfg)
    check(g.failed and "连不上" in g.summary, "端口不通应判失败")
    check("554" in g.advice, "应提示对应端口")

    acc.describe_playback = _seq_describe([DescribeResult(418, "done", "I am a teapot", False)])
    g, _h = acc.gate_playback(cfg)
    check(g.failed and "418" in g.summary, "未知状态码要如实报出")

    acc.describe_playback = real


# --------------------------------------------------------------------------- #
def test_pull_advice():
    print("[5] 实拉失败的处理建议")
    check("密码" in acc._pull_advice("拉取失败：401 Unauthorized"), "401 应指向密码")
    check("回放" in acc._pull_advice("拉取失败：403 Forbidden"), "403 应指向回放权限")
    check("时段" in acc._pull_advice("该时段无录像（404 Not Found）"), "404 应提示换时段")
    check("网络" in acc._pull_advice("拉流卡死被中断"), "卡死应指向网络稳定性")
    check(acc._pull_advice("莫名其妙").startswith("把上面"), "未知原因要有兜底建议")


# --------------------------------------------------------------------------- #
class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def test_gate_pull():
    print("[6] 门禁 3 · 拉得到")
    tmp = tempfile.mkdtemp(prefix="accept_test_")
    cfg = fresh_cfg(tmp)
    real_pull, real_probe, real_meta = acc.pull_segment, acc.probe_media, acc.probe_stream_meta

    g = acc.gate_pull(cfg, _Logger(), "ffmpeg", None)
    check(g.skipped, "拿不到有录像的时段应判待复验")
    check("--accept" in g.advice, "应提示在有录像时重跑验收")

    acc.pull_segment = lambda *a, **k: PullOutcome("error", detail="拉取失败：403 Forbidden")
    g = acc.gate_pull(cfg, _Logger(), "ffmpeg", ("5 分钟前", datetime.now(), datetime.now()))
    check(g.failed and "403" in g.summary, "实拉失败应判失败并保留原因")
    check("回放" in g.advice, "失败建议应由原因推导")

    acc.pull_segment = lambda *a, **k: PullOutcome(
        "ok", path=os.path.join(tmp, "x.mp4"), duration=29.0, size=900 * 1024)
    acc.probe_media = lambda *a, **k: (False, "invalid nal unit")
    acc.probe_stream_meta = lambda *a, **k: {}
    g = acc.gate_pull(cfg, _Logger(), "ffmpeg", ("5 分钟前", datetime.now(), datetime.now()))
    check(g.failed and "解不开" in g.summary, "文件解不开应判失败")

    acc.probe_media = lambda *a, **k: (True, "")
    acc.probe_stream_meta = lambda *a, **k: {"codec": "h264", "width": "1280",
                                             "height": "720", "fps": "25"}
    g = acc.gate_pull(cfg, _Logger(), "ffmpeg", ("5 分钟前", datetime.now(), datetime.now()))
    check(g.passed, "实拉成功 + 可解码应判通过")
    check(g.artifact.endswith(".mp4"), "应给出样片路径")
    check(any("1280x720" in ln for ln in g.lines), "报告应写出分辨率")
    check(any("kbps" in ln for ln in g.lines), "报告应写出码率")
    check(any("存储估算" in ln for ln in g.lines), "报告应给出存储估算")
    check(any("解码校验 : 通过" in ln for ln in g.lines), "应明确写出解码校验结论")
    check(g.warning == "", "时长正常时不应产生告警")

    acc.pull_segment = lambda *a, **k: PullOutcome(
        "ok", path=os.path.join(tmp, "y.mp4"), duration=8.0, size=300 * 1024)
    g = acc.gate_pull(cfg, _Logger(), "ffmpeg", ("5 分钟前", datetime.now(), datetime.now()))
    check(g.passed and g.warning, "拿到但时长严重不足时应通过但告警")
    check("不连续" in g.warning, "时长不足的告警应说明可能漏段")

    acc.pull_segment, acc.probe_media, acc.probe_stream_meta = real_pull, real_probe, real_meta
    _rmtree(tmp)


def _rmtree(path):
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:      # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
def _fake_map(active_channels=(1,), total=16, title="IPC"):
    cmap = ChannelMap(host="192.0.2.9", machine_name="某门店-总", model="DH-NVR4216",
                      serial="SN123", total=total)
    for i in range(1, total + 1):
        if i in active_channels:
            cmap.channels.append(ChannelInfo(
                index=i, title=title, enabled=True,
                camera=CameraInfo(address="192.0.2.67", device_type="IPC-HDW1025C",
                                  serial="S1")))
        else:
            cmap.channels.append(ChannelInfo(index=i, title="<摄像头2>3", enabled=False,
                                             camera=CameraInfo(address="192.168.0.0")))
    return cmap


def test_collect_notes():
    print("[7] 部署前核对")
    cfg = fresh_cfg()
    real_time, real_map = acc.probe_device_time, acc.fetch_channel_map

    acc.probe_device_time = lambda *a, **k: (
        datetime.now() + timedelta(seconds=389)).strftime("%Y-%m-%d %H:%M:%S")
    acc.fetch_channel_map = lambda *a, **k: _fake_map()
    notes, warns = acc.collect_notes(cfg)
    check(any("时钟" in n for n in notes), "应报出双方时钟")
    check(any("相差" in n for n in notes), "应报出偏差数值")
    drift_warn = [w for w in warns if "NTP" in w]
    check(len(drift_warn) == 1, "偏差 >120 秒应告警")
    check("safety_lag_seconds" in drift_warn[0], "时钟告警必须给出可执行的兜底动作")
    check("偏快" in drift_warn[0], "应指出偏差方向")

    acc.probe_device_time = lambda *a, **k: (
        datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    _n, warns = acc.collect_notes(cfg)
    check(not any("NTP" in w for w in warns), "偏差 30 秒不该告警")

    acc.probe_device_time = lambda *a, **k: None
    notes, warns = acc.collect_notes(cfg)
    check(any("读不到录像机时间" in n for n in notes), "读不到时钟应只提示")
    check(not any("NTP" in w for w in warns), "读不到时钟不该产生告警")

    cfg.nvr.channel = 7
    notes, warns = acc.collect_notes(cfg)
    check(any("没有摄像头" in w for w in warns), "通道号没摄像头必须告警")
    check("1" in [w for w in warns if "没有摄像头" in w][0], "应给出可用通道号")

    cfg.nvr.channel = 1
    _n, warns = acc.collect_notes(cfg)
    check(any("默认值" in w for w in warns), "默认通道名应告警")
    check("--refresh-device" in [w for w in warns if "默认值" in w][0],
          "改名后应提示刷新设备缓存")

    def boom(*a, **k):
        raise IOError("HTTP 403")
    acc.fetch_channel_map = boom
    notes, warns = acc.collect_notes(cfg)
    check(any("通道清单" in w for w in warns), "清单读不到应告警")
    check(any("时钟" in n for n in notes), "清单失败不应影响时钟部分")

    acc.probe_device_time, acc.fetch_channel_map = real_time, real_map


# --------------------------------------------------------------------------- #
def test_verdict():
    print("[8] 结论判定规则")
    ok = acc.GateResult("a", "t", acc.PASS)
    bad = acc.GateResult("b", "t", acc.FAIL)
    skip = acc.GateResult("c", "t", acc.SKIP)
    warn = acc.GateResult("d", "t", acc.PASS, warning="注意")

    check(acc._verdict([ok, ok], []) == acc.VERDICT_PASS, "全过无告警 -> 通过")
    check(acc._verdict([ok], ["一条告警"]) == acc.VERDICT_CONDITIONAL, "有告警 -> 有条件通过")
    check(acc._verdict([warn], []) == acc.VERDICT_CONDITIONAL, "门禁带告警 -> 有条件通过")
    check(acc._verdict([ok, skip], []) == acc.VERDICT_RETEST, "有门禁没验成 -> 待复验")
    check(acc._verdict([ok, bad, skip], []) == acc.VERDICT_FAIL, "失败优先于待复验")
    check(acc._verdict([bad], ["告警"]) == acc.VERDICT_FAIL, "失败优先于告警")
    check(acc._verdict([ok, skip], ["告警"]) == acc.VERDICT_RETEST, "待复验优先于有条件通过")


# --------------------------------------------------------------------------- #
def test_report_and_exit():
    print("[9] 报告渲染与退出码")
    cfg = fresh_cfg()
    rep = acc.AcceptReport(
        gates=[acc.GateResult("login", "门禁 1 · 能登录", acc.PASS, summary="登录成功",
                              lines=["  x"]),
               acc.GateResult("pull", "门禁 3 · 拉得到", acc.SKIP, summary="无样本",
                              advice="营业时段重跑")],
        notes=["设备名 : 某门店-总"],
        warnings=["通道 1 名称仍是默认值"],
        verdict=acc.VERDICT_RETEST,
        artifact=r"C:\tmp\a.mp4")
    text = acc.render_report(cfg, rep, r"C:\archive")

    check("账密验收" in text, "报告应有标题")
    check("门禁 1" in text and "门禁 3" in text, "报告应列出每道门禁")
    check("[待复验]" in text, "待复验应有对应标记")
    check("验收结论" in text, "报告应有结论行")
    check("待复验" in text.split("验收结论")[1], "结论行应反映真实判定")
    check("a.mp4" in text, "应打印样片路径")
    check("192.0.2.9" in text, "应写明目标设备")
    check("**" not in text, "终端报告不应残留 Markdown 粗体语法")

    import contextlib
    import io
    import nvr_puller
    real = nvr_puller.run_acceptance
    for verdict, want in ((acc.VERDICT_PASS, 0), (acc.VERDICT_CONDITIONAL, 0),
                          (acc.VERDICT_FAIL, 2), (acc.VERDICT_RETEST, 3)):
        nvr_puller.run_acceptance = lambda c, l, v=verdict: acc.AcceptReport(
            verdict=v, gates=[acc.GateResult("x", "t", acc.PASS)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = nvr_puller.do_accept(cfg, _Logger(), "C:\\archive")
        check(got == want, "结论 %s 应对应退出码 %s（实际 %s）" % (verdict, want, got))
    nvr_puller.run_acceptance = real


# --------------------------------------------------------------------------- #
def test_probe_range_wrapper():
    print("[10] probe_range 向后兼容")
    nvr = NvrConfig(host="127.0.0.1", rtsp_port=1, username="a", password="b")
    s = datetime(2026, 9, 16, 10, 0, 0)
    r = describe_playback(nvr, s, s + timedelta(minutes=1), timeout=2.0)
    check(r.code is None, "连不上的端口应返回 code=None")
    check(r.stage == "connect", "应停在 connect 阶段")
    check("无法连接" in r.detail, "应保留连接失败原因")
    check(probe_range(nvr, s, s + timedelta(minutes=1), timeout=2.0) is None,
          "probe_range 仍应返回 None")
    check(DescribeResult(200, "auth", "OK").ok, "ok 属性应对齐 200")
    check(not DescribeResult(404, "auth", "Not Found").ok, "404 不算 ok")


def main() -> int:
    test_candidate_windows()
    test_rtsp_text()
    test_gate_login()
    test_gate_playback()
    test_pull_advice()
    test_gate_pull()
    test_collect_notes()
    test_verdict()
    test_report_and_exit()
    test_probe_range_wrapper()
    print()
    print("=" * 60)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
