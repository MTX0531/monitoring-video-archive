# -*- coding: utf-8 -*-
"""新门店部署前的账密验收（--accept）。

场景
--------------------------------------------------------------------
门店现场会提供录像机的账号密码。约定：**先验收、再部署**。
本模块把验收固化成三道门禁，一次跑完并给出一张结论表：

    门禁 1  能登录   HTTP Digest 打 configManager.cgi —— 账号密码对不对、有没有配置查询权限
    门禁 2  能回放   RTSP Digest 对回放地址 DESCRIBE —— 有没有「远程回放」权限
    门禁 3  拉得到   ffmpeg 实拉 30 秒并完整解码一遍 —— 落地文件能不能播

判定原则：宁可判「待复验」，也不把「验不了」当成「通过」
--------------------------------------------------------------------
    * 任一门禁 失败          -> 不通过，不部署
    * 门禁 2 拿不到录像样本  -> 待复验（认证结论可用，但回放权限未证实）
    * 门禁 3 无法实拉        -> 待复验
    * 全过但有告警          -> 有条件通过
    * 全过且无告警          -> 通过
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .channels import fetch_channel_map
from .config import Config, ensure_dirs, schedule_times
from .deviceinfo import probe_device_time, probe_login
from .downloader import (RTSP_NO_RECORD, describe_playback, find_ffmpeg,
                         probe_media, probe_stream_meta, pull_segment)
from .logutil import human_duration, human_size

PULL_SECONDS = 30

PASS, FAIL, SKIP = "pass", "fail", "skip"

VERDICT_PASS = "pass"
VERDICT_CONDITIONAL = "conditional"
VERDICT_RETEST = "retest"
VERDICT_FAIL = "fail"

VERDICT_TEXT = {
    VERDICT_PASS: "通过 —— 可以进入部署",
    VERDICT_CONDITIONAL: "有条件通过 —— 可以部署，但请先处理下面的告警",
    VERDICT_RETEST: "待复验 —— 有门禁没能验证，先不要部署",
    VERDICT_FAIL: "不通过 —— 不要部署，先解决卡住的那道门禁",
}

_GATE_MARK = {PASS: "[通过]", FAIL: "[失败]", SKIP: "[待复验]"}

_HTTP_TEXT = {200: "OK", 400: "请求错误", 401: "Unauthorized", 403: "Forbidden",
              404: "Not Found", 500: "服务端错误", 503: "服务不可用"}

_RTSP_TEXT = {200: "有录像", 401: "认证失败", 403: "无权限",
              404: "该时段无录像", 500: "服务端拒绝", 503: "服务不可用"}

_TFMT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #
@dataclass
class GateResult:
    key: str
    title: str
    status: str = FAIL
    summary: str = ""
    lines: List[str] = field(default_factory=list)
    warning: str = ""
    advice: str = ""
    artifact: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    @property
    def skipped(self) -> bool:
        return self.status == SKIP


@dataclass
class AcceptReport:
    gates: List[GateResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    verdict: str = VERDICT_FAIL
    artifact: str = ""
    error: str = ""


# --------------------------------------------------------------------------- #
#  候选验证时段
# --------------------------------------------------------------------------- #
def candidate_windows(now: Optional[datetime] = None
                      ) -> List[Tuple[str, datetime, datetime]]:
    """候选验证时段，由近及远；只要有一个有录像就够验证回放了。"""
    now = now or datetime.now()
    return [
        ("5 分钟前", now - timedelta(minutes=6), now - timedelta(minutes=5)),
        ("35 分钟前", now - timedelta(minutes=36), now - timedelta(minutes=35)),
        ("2 小时前", now - timedelta(hours=2, minutes=1), now - timedelta(hours=2)),
        ("昨天此刻", now - timedelta(hours=24, minutes=1), now - timedelta(hours=24)),
    ]


def _rtsp_text(r) -> str:
    if r.code is None:
        return "探测失败（%s）" % (r.detail or "未收到响应")
    text = _RTSP_TEXT.get(r.code, r.detail or "")
    return ("%s %s" % (r.code, text)).strip()


# --------------------------------------------------------------------------- #
#  门禁 1：能登录
# --------------------------------------------------------------------------- #
def gate_login(cfg: Config) -> Tuple[GateResult, str]:
    g = GateResult("login", "门禁 1 · 能登录（HTTP 认证）")
    g.lines.append("目标 : http://%s:%d/cgi-bin/configManager.cgi"
                   % (cfg.nvr.host, cfg.device.http_port))
    g.lines.append("账号 : %s" % (cfg.nvr.username or "(空)"))

    if not cfg.nvr.password:
        g.summary = "配置里没有填密码"
        g.advice = "把门店提供的密码填进 config.json 的 nvr.password（或对应的门店配置）。"
        return g, ""

    code, machine, err = probe_login(cfg.nvr.host, cfg.nvr.username, cfg.nvr.password,
                                     cfg.device.http_port, cfg.device.timeout_seconds)
    if code == 0:
        g.summary = err or "网络不可达"
        g.advice = ("确认门店机与录像机在同一网段、Web 端口 %d 可达；"
                    "跨网络部署需要 VPN 或端口映射。" % cfg.device.http_port)
        return g, ""

    g.lines.append("返回 : HTTP %s %s" % (code, _HTTP_TEXT.get(code, "")))

    if code == 200:
        g.status = PASS
        g.summary = "登录成功，账号密码有效"
        g.lines.append("设备名 : %s" % (machine or "(读不到 MachineName，但不影响登录结论)"))
        return g, machine

    if code == 401:
        g.summary = "账号或密码错误"
        g.advice = "请门店重新确认账号与密码（注意大小写、末尾空格、是否含特殊字符）。"
    elif code == 403:
        g.summary = "账号密码可能对，但无权访问该接口 / 本机 IP 被拒"
        g.advice = ("让门店在 录像机→系统→用户管理 里给该账号勾上『远程配置查询』权限；"
                    "若设备开了 IP 白名单，把门店机 IP 加进去。")
    elif code == 404:
        g.summary = "接口不存在（HTTP 404）"
        g.advice = ("该固件的 Web 端口可能不是 %d，核对录像机 Web 端口后改 config.json 的 "
                    "device.http_port。" % cfg.device.http_port)
    else:
        g.summary = "登录未成功（HTTP %s）" % code
        g.advice = "先用浏览器打开 http://%s:%d 手工确认 Web 是否可用。" % (
            cfg.nvr.host, cfg.device.http_port)
    return g, ""


# --------------------------------------------------------------------------- #
#  门禁 2：能回放
# --------------------------------------------------------------------------- #
def gate_playback(cfg: Config) -> Tuple[GateResult, Optional[Tuple[str, datetime, datetime]]]:
    g = GateResult("playback", "门禁 2 · 能回放（RTSP 认证 + 回放权限）")
    g.lines.append("目标 : rtsp://%s:%d/cam/playback?channel=%d&subtype=%d"
                   % (cfg.nvr.host, cfg.nvr.rtsp_port, cfg.nvr.channel, cfg.nvr.subtype))

    timeout = max(5.0, cfg.device.timeout_seconds)
    results = []
    hit: Optional[Tuple[str, datetime, datetime]] = None
    for label, s, e in candidate_windows():
        r = describe_playback(cfg.nvr, s, e, timeout)
        results.append((label, r))
        g.lines.append("探测 : %s %s ~ %s -> %s"
                       % (label, s.strftime("%H:%M:%S"), e.strftime("%H:%M:%S"), _rtsp_text(r)))
        if r.ok:
            hit = (label, s, e)
            break

    if hit:
        g.status = PASS
        g.summary = "RTSP 认证通过，已确认有录像可拉（%s）" % hit[0]
        return g, hit

    codes = [r.code for _, r in results]
    auth401 = any(r.code == 401 and r.stage == "auth" for _, r in results)
    auth403 = any(r.code == 403 and r.stage == "auth" for _, r in results)
    bad503 = any(r.code == 503 for _, r in results)
    no_conn = [r.detail for _, r in results if r.code is None]
    all_404 = bool(codes) and all(c == RTSP_NO_RECORD for c in codes)
    challenged = all(r.had_challenge for _, r in results)

    if auth401:
        g.summary = "RTSP 认证失败 —— 账号或密码不对"
        g.advice = ("与门禁 1 用的是同一份凭据。若门禁 1 通过而这里 401，"
                    "检查录像机是否单独限制了 RTSP 账号。")
    elif auth403:
        g.summary = "账号没有「远程回放」权限"
        g.advice = ("门店部署最常见的坑：大华账号权限是分项勾选的。请门店在 "
                    "录像机→系统→用户管理→该账号 里勾上『远程回放』"
                    "（部分固件写作『录像回放』或『远程回放/下载』），再重跑。")
    elif bad503:
        g.summary = "RTSP 服务拒绝了请求（503）"
        g.advice = ("RTSP 可能未开启，或设备并发流数已满 —— "
                    "先关掉正在看回放的客户端/网页，再重跑。")
    elif no_conn:
        g.summary = "RTSP 端口连不上"
        g.advice = "确认 %d 端口可达、录像机 RTSP 服务已开启。" % cfg.nvr.rtsp_port
    elif all_404 and challenged:
        g.status = SKIP
        g.summary = "认证已通过，但近 24 小时都没探测到录像，无法验证回放权限"
        g.warning = ("回放权限『未证实』。先核对两件事：① nvr.channel=%d 上是否真有摄像头"
                     "（跑 --channels 看）；② 录像机的录像计划是否已开启。"
                     % cfg.nvr.channel)
        g.advice = "在上述任一问题解决后，重跑 --accept。"
    elif all_404:
        g.summary = "DESCRIBE 返回 404 且未触发认证挑战 —— 无法确认回放权限"
        g.advice = "换有录像的时段重试；仍如此请检查 nvr.channel 与录像计划。"
    else:
        descs = sorted({("%s %s" % (r.code, r.detail)).strip() if r.code is not None
                        else (r.detail or "无响应")
                        for _, r in results})
        g.summary = "回放探测未通过：" + " / ".join(descs)
        g.advice = "把上面每一行的状态码反馈过来再定位。"
    return g, None


# --------------------------------------------------------------------------- #
#  门禁 3：拉得到
# --------------------------------------------------------------------------- #
def _pull_advice(detail: str) -> str:
    low = (detail or "").lower()
    if "401" in low:
        return "RTSP 认证失败，核对账号密码。"
    if "403" in low:
        return "账号缺「远程回放」权限，让门店在用户管理里勾上。"
    if "无录像" in detail:
        return "该时段其实没有录像数据，换时段重试。"
    if "卡死" in detail:
        return ("拉流中途断流且长时间无数据 —— 检查网络稳定性；"
                "若是无线网络，建议改有线。")
    if "timed out" in low or "refused" in low or "unreachable" in low:
        return "网络层问题：确认端口可达、传输方式为 tcp。"
    return "把上面的失败信息反馈过来再定位。"


def gate_pull(cfg: Config, logger, ffmpeg: str,
              window: Optional[Tuple[str, datetime, datetime]]) -> GateResult:
    g = GateResult("pull", "门禁 3 · 拉得到（端到端实拉 %d 秒 + 解码校验）" % PULL_SECONDS)
    if window is None:
        g.status = SKIP
        g.summary = "门禁 2 没找到有录像的时段，无法做实拉验证"
        g.advice = "在营业时段（设备确实在录的时候）重跑 --accept 再定论。"
        return g

    label, s, _e = window
    e = s + timedelta(seconds=PULL_SECONDS)
    out_dir = ensure_dirs(os.path.join(cfg.base_dir, "_accept"))
    out = os.path.join(out_dir, "accept_%s.mp4" % s.strftime("%Y%m%d_%H%M%S"))
    g.lines.append("时段 : %s（%s ~ %s）" % (label, s.strftime(_TFMT), e.strftime(_TFMT)))
    g.lines.append("样片 : %s" % out)

    res = pull_segment(cfg, ffmpeg, s, e, out, logger)
    if res.status != "ok":
        detail = res.detail or res.status
        g.summary = "实拉失败：%s" % detail
        g.advice = _pull_advice(detail)
        return g

    g.artifact = res.path
    meta = probe_stream_meta(ffmpeg, res.path)
    kbps = res.size * 8 / max(res.duration, 1) / 1000.0

    g.lines.append("体积 : %s    实际时长 : %s" % (human_size(res.size),
                                                   human_duration(res.duration)))
    desc = []
    if meta.get("codec"):
        desc.append(meta["codec"])
    if meta.get("width"):
        desc.append("%sx%s" % (meta["width"], meta["height"]))
    if meta.get("fps"):
        desc.append("%s fps" % meta["fps"])
    if desc:
        g.lines.append("流参数 : %s" % " / ".join(desc))
    g.lines.append("码率 : 约 %.0f kbps" % kbps)

    hours = 24.0
    if cfg.schedule.enabled:
        ws, we = schedule_times(cfg)
        a = datetime.combine(datetime.now().date(), ws)
        b = datetime.combine(datetime.now().date(), we)
        if b <= a:
            b += timedelta(days=1)
        hours = (b - a).total_seconds() / 3600.0
    per_hour = res.size * 3600.0 / res.duration if res.duration else 0.0
    days = max(1, int(cfg.retention.keep_days))
    g.lines.append("存储估算 : 每小时约 %s；每天（按 %.1f 小时拉取）约 %s；保留 %d 天约 %s"
                   % (human_size(per_hour), hours, human_size(per_hour * hours),
                      days, human_size(per_hour * hours * days)))

    ok, detail = probe_media(ffmpeg, res.path, deep=False)
    if not ok:
        g.status = FAIL
        g.summary = "拉到数据了，但文件解不开（%s）" % (detail or "未知原因")
        g.advice = ("该通道的编码参数异常或拉流丢包严重；"
                    "把录像机的编码改成 H.264 + 关闭非常规 profile，或调高码率后重试。")
        return g

    g.status = PASS
    g.summary = "实拉成功，样片可完整解码（本地能播）"
    g.lines.append("解码校验 : 通过")

    if res.duration and res.duration < PULL_SECONDS * 0.5:
        g.warning = ("申请 %d 秒只拿到 %s —— 该时段录像可能不连续，"
                     "上线后关注是否有漏段。" % (PULL_SECONDS, human_duration(res.duration)))
    return g


# --------------------------------------------------------------------------- #
#  部署前核对（不计入门禁，但会进告警）
# --------------------------------------------------------------------------- #
def collect_notes(cfg: Config) -> Tuple[List[str], List[str]]:
    notes: List[str] = []
    warns: List[str] = []

    # ---- 录像机时间 vs 本机时间（回放地址用的是设备本地时间）----
    dev_time = probe_device_time(cfg.nvr.host, cfg.nvr.username, cfg.nvr.password,
                                 cfg.device.http_port, cfg.device.timeout_seconds)
    if dev_time:
        drift = sign = None
        try:
            delta = (datetime.strptime(dev_time, _TFMT) - datetime.now()).total_seconds()
            drift = abs(delta)
            sign = "设备偏快" if delta >= 0 else "设备偏慢"
        except ValueError:
            drift = None
        notes.append("时钟 : 录像机 %s / 本机 %s%s"
                     % (dev_time, datetime.now().strftime(_TFMT),
                        "" if drift is None else "（相差 %.0f 秒）" % drift))
        if drift is not None and drift > 120:
            warns.append(
                "录像机时间与本机相差 %.0f 秒（%s）。回放地址用的是『设备本地时间』："
                "设备偏慢时脚本会向『还没录到』的未来要录像 → 探测 404 → 游标直接跳过 → "
                "该段永久漏掉。请把录像机与门店机对到同一 NTP 源；"
                "暂时无法同步时，把 config.json 里 archive.safety_lag_seconds "
                "调到大于 %.0f 再上线。" % (drift, sign, drift))
    else:
        notes.append("时钟 : 读不到录像机时间（接口不可用，可忽略）")

    # ---- 通道清单 ----
    try:
        cmap = fetch_channel_map(cfg.nvr.host, cfg.nvr.username, cfg.nvr.password,
                                 cfg.device.http_port, cfg.nvr.channel,
                                 cfg.device.timeout_seconds)
    except (OSError, IOError, ValueError) as exc:
        warns.append("读不到通道清单：%s（账号可能缺『远程配置查询』权限）" % exc)
        return notes, warns

    notes.append("设备 : %s  序列号 %s" % (cmap.model or "?", cmap.serial or "?"))
    notes.append("通道 : %s" % cmap.summary())
    if cmap.active:
        notes.append("已接入 : " + " / ".join(
            "通道%d %s(%s)" % (c.index, c.title or "未命名", c.camera_ip or "-")
            for c in cmap.active))

    if not any(c.index == cfg.nvr.channel and c.has_camera for c in cmap.active):
        warns.append("配置用的 nvr.channel = %d 上没有摄像头 —— 脚本会一直拉到空录像『且不报错』。"
                     "请改成 %s 再上线。"
                     % (cfg.nvr.channel,
                        "、".join(str(c.index) for c in cmap.active) or "实际有摄像头的那一路"))

    target = next((c for c in cmap.channels if c.index == cfg.nvr.channel), None)
    if target is not None and target.title_is_placeholder:
        warns.append("通道 %d 的名称仍是默认值（%s）—— 建议门店在录像机或大华云联里改成实际"
                     "位置名（如『收银台』），归档文件名才有辨识度；改完跑一次 --refresh-device。"
                     % (cfg.nvr.channel, target.title or "空"))

    if cmap.total and len(cmap.active) < cmap.total:
        notes.append("提示 : 设备支持 %d 路但只接入 %d 路；未接入通道上保留的历史名称是配置残留，"
                     "不代表真有摄像头。" % (cmap.total, len(cmap.active)))
    return notes, warns


# --------------------------------------------------------------------------- #
#  主流程
# --------------------------------------------------------------------------- #
def _verdict(gates: List[GateResult], warnings: List[str]) -> str:
    if any(g.failed for g in gates):
        return VERDICT_FAIL
    if any(g.skipped for g in gates) or any(g.warning for g in gates):
        return VERDICT_RETEST if any(g.skipped for g in gates) else VERDICT_CONDITIONAL
    if warnings:
        return VERDICT_CONDITIONAL
    return VERDICT_PASS


def run_acceptance(cfg: Config, logger) -> AcceptReport:
    """按顺序跑完三道门禁，返回完整报告。"""
    rep = AcceptReport()

    try:
        ffmpeg = find_ffmpeg(cfg)
    except FileNotFoundError as exc:
        rep.gates.append(GateResult("env", "运行环境", FAIL, summary=str(exc)))
        rep.error = str(exc)
        rep.verdict = VERDICT_FAIL
        return rep

    g1, machine = gate_login(cfg)
    rep.gates.append(g1)

    g2, window = gate_playback(cfg)
    rep.gates.append(g2)

    g3 = gate_pull(cfg, logger, ffmpeg, window)
    rep.gates.append(g3)
    rep.artifact = g3.artifact

    if machine:
        rep.notes.append("设备名 : %s（= 大华云联里的门店/分组名，将用于归档文件名）" % machine)
    if g1.passed or g2.passed:
        n, w = collect_notes(cfg)
        rep.notes.extend(n)
        rep.warnings.extend(w)

    rep.verdict = _verdict(rep.gates, rep.warnings)
    return rep


def render_report(cfg: Config, rep: AcceptReport, root: str) -> str:
    """把报告渲染成终端文本。"""
    L: List[str] = []
    L.append("=" * 78)
    L.append("门店部署前 · 账密验收")
    L.append("=" * 78)
    L.append("录像机   : %s   （RTSP %d / Web %d）"
             % (cfg.nvr.host, cfg.nvr.rtsp_port, cfg.device.http_port))
    L.append("账号     : %s" % (cfg.nvr.username or "(空)"))
    L.append("使用通道 : %d（%s）" % (cfg.nvr.channel,
                                     "主码流" if cfg.nvr.stream.lower() == "main" else "子码流"))
    L.append("归档目录 : %s" % root)
    L.append("验证时间 : %s" % datetime.now().strftime(_TFMT))

    for g in rep.gates:
        L.append("-" * 78)
        L.append("%s %s" % (_GATE_MARK.get(g.status, "[?]"), g.title))
        if g.summary:
            L.append("  结论 : %s" % g.summary)
        for ln in g.lines:
            L.append("  %s" % ln)
        if g.warning:
            L.append("  告警 : %s" % g.warning)
        if g.advice:
            L.append("  处理 : %s" % g.advice)

    if rep.notes:
        L.append("-" * 78)
        L.append("部署前核对")
        for n in rep.notes:
            L.append("  %s" % n)

    if rep.warnings:
        L.append("-" * 78)
        L.append("需要注意的告警（%d 条）" % len(rep.warnings))
        for i, w in enumerate(rep.warnings, 1):
            L.append("  %d) %s" % (i, w))

    L.append("-" * 78)
    L.append("验收结论：%s" % VERDICT_TEXT.get(rep.verdict, rep.verdict))
    if rep.artifact:
        L.append("样片     : %s" % rep.artifact)
        L.append("           （双击确认能否正常播放；确认后可自行删除）")
    if rep.error:
        L.append("错误     : %s" % rep.error)
    L.append("=" * 78)
    return "\n".join(L)
