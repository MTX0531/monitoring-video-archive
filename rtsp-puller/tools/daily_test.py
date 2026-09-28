# -*- coding: utf-8 -*-
"""按日归档（--day）的验证脚本。

覆盖：
  1. --day 取值解析：today / yesterday / 前天 / 具体日期 / 大小写与空白 / 非法值
  2. 目标日窗口：启用时段取该日时段、禁用时段取全天、跨午夜时段止点顺延
  3. run_day 主流程：起点前移、分段对齐窗口末尾、游标推进、汇总结论
  4. run_day 幂等：目标日已归档完成时直接退出，不产生任何拉取
  5. run_day 边界：目标日尚未录到 -> 退出码 3；--redo 从头重拉
  6. 并发：分段计划与去重、连续前缀游标、并发跑批、错峰启动
  7. 自动补漏：游标落后时逐日补齐、断点跨天续拉、回溯上限与告警

全部离线运行，不连录像机（拉取动作被替换为假实现）。

用法：python tools/daily_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import nvr_puller                                             # noqa: E402
from nvrcore.config import Config                             # noqa: E402
from nvrcore.state import State                               # noqa: E402

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s\n         期望=%r\n         实得=%r" % (label, want, got))


def check_true(label, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, note))


class _Logger:
    def __init__(self):
        self.lines = []

    def _add(self, lvl, fmt, *a):
        try:
            self.lines.append("%s %s" % (lvl, fmt % a if a else fmt))
        except (TypeError, ValueError):
            self.lines.append("%s %s" % (lvl, fmt))

    def info(self, fmt, *a):
        self._add("INFO", fmt, *a)

    warning = error = debug = exception = info

    def text(self):
        return "\n".join(self.lines)


def make_cfg(tmp, sched=True, start="09:30", end="22:30"):
    cfg = Config(base_dir=tmp)
    cfg.archive.store_name = "测试门店"
    cfg.archive.channel_name = "IPC"
    cfg.archive.folder_name = "测试门店监控视频归档"
    cfg.archive.segment_minutes = 30
    cfg.archive.safety_lag_seconds = 180
    cfg.archive.organize_by_date = True
    cfg.schedule.enabled = sched
    cfg.schedule.start = start
    cfg.schedule.end = end
    cfg.schedule.mode = "footage"
    cfg.retention.enabled = False
    cfg.runtime.parallel_streams = 1          # 顺序语义的断言需要确定顺序
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return cfg


def set_cursor(cfg, dt):
    st = State()
    st.set_next_start(dt)
    st.save(cfg.state_file)


def get_cursor(cfg):
    return State.load(cfg.state_file).get_next_start()


class _PullSpy:
    """替换真实拉取：只记录区间，用于离线验证主流程。"""

    def __init__(self, write_file=False, sleep=0.0, fail_on=None):
        self.calls = []
        self.starts = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.write_file = write_file
        self.sleep = sleep
        self.fail_on = set(fail_on or ())

    def __call__(self, cfg, ffmpeg, start, end, root, logger, label=""):
        with self.lock:
            self.calls.append((start, end))
            self.starts.append(time.time())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.sleep:
                time.sleep(self.sleep)
            if start in self.fail_on:
                return None
            if self.write_file:
                p = nvr_puller.segment_path(cfg, root, start, end)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(b"fake")
            return SimpleNamespace(status="ok", size=1024, path="x.mp4",
                                   elapsed=1.0, duration=(end - start).total_seconds())
        finally:
            with self.lock:
                self.active -= 1


def install_stubs(spy):
    """装好假依赖，返回还原函数。"""
    orig = (nvr_puller.pull_window, nvr_puller.find_ffmpeg,
            nvr_puller.ensure_space)
    nvr_puller.pull_window = spy
    nvr_puller.find_ffmpeg = lambda cfg: "ffmpeg"
    nvr_puller.ensure_space = lambda cfg, root, logger: True

    def restore():
        (nvr_puller.pull_window, nvr_puller.find_ffmpeg,
         nvr_puller.ensure_space) = orig
    return restore


# --------------------------------------------------------------------------- #
def test_parse_day_arg():
    print("\n[1] --day 取值解析")
    today = datetime.now().date()
    p = nvr_puller.parse_day_arg

    check("yesterday -> 昨天", p("yesterday").date(), today - timedelta(days=1))
    check("today -> 今天", p("today").date(), today)
    check("前天 -> 前天", p("前天").date(), today - timedelta(days=2))
    check("大写 YESTERDAY 也认", p("YESTERDAY").date(), today - timedelta(days=1))
    check("两侧空白自动去掉", p("  yesterday  ").date(), today - timedelta(days=1))
    check("时分秒归零", (p("yesterday").hour, p("yesterday").minute,
                        p("yesterday").second), (0, 0, 0))

    check("具体日期 YYYY-MM-DD", p("2026-09-15").strftime("%Y-%m-%d"), "2026-09-15")
    check("斜杠日期 YYYY/MM/DD", p("2026/09/15").strftime("%Y-%m-%d"), "2026-09-15")
    check("紧凑日期 YYYYMMDD", p("20260915").strftime("%Y-%m-%d"), "2026-09-15")

    for bad in ("", "abc", "2026-13-99", "9/15"):
        try:
            p(bad)
            check_true("非法值 %r 应报错" % bad, False, "没有抛 ValueError")
        except ValueError:
            check_true("非法值 %r 明确报错" % bad, True)


def test_day_window():
    print("\n[2] 目标日窗口计算")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp, sched=True)
        s, e = nvr_puller.day_window(cfg, datetime(2026, 9, 15))
        check("启用时段 -> 该日时段起", s.strftime("%Y-%m-%d %H:%M"), "2026-09-15 09:30")
        check("启用时段 -> 该日时段止", e.strftime("%Y-%m-%d %H:%M"), "2026-09-15 22:30")

        cfg2 = make_cfg(tmp, sched=False)
        s2, e2 = nvr_puller.day_window(cfg2, datetime(2026, 9, 15))
        check("禁用时段 -> 全天起", s2.strftime("%Y-%m-%d %H:%M"), "2026-09-15 00:00")
        check("禁用时段 -> 全天止", e2.strftime("%Y-%m-%d %H:%M"), "2026-09-16 00:00")

        cfg3 = make_cfg(tmp, sched=True, start="22:00", end="02:00")
        s3, e3 = nvr_puller.day_window(cfg3, datetime(2026, 9, 15))
        check("跨午夜时段 起", s3.strftime("%Y-%m-%d %H:%M"), "2026-09-15 22:00")
        check("跨午夜时段 止顺延次日", e3.strftime("%Y-%m-%d %H:%M"), "2026-09-16 02:00")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_day_flow():
    print("\n[3] run_day 主流程（起点前移 + 分段 + 汇总）")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        set_cursor(cfg, datetime(2026, 9, 15))
        spy = _PullSpy()
        restore = install_stubs(spy)
        log = _Logger()
        try:
            rc = nvr_puller.run_day(cfg, log, tmp, datetime(2026, 9, 15))
        finally:
            restore()

        check("退出码 0（完成）", rc, 0)
        check("起点前移到窗口开头", spy.calls[0][0].strftime("%Y-%m-%d %H:%M"),
              "2026-09-15 09:30")
        check("切出 26 段", len(spy.calls), 26)
        check("末段止于窗口末尾", spy.calls[-1][1].strftime("%Y-%m-%d %H:%M"),
              "2026-09-15 22:30")
        check("每段均为 30 分钟",
              sorted({int((b - a).total_seconds()) for a, b in spy.calls}), [1800])
        check("收尾后游标停在窗口末尾", get_cursor(cfg).strftime("%Y-%m-%d %H:%M"),
              "2026-09-15 22:30")
        check_true("日志含目标日", "2026-09-15" in log.text())
        check_true("日志给出 1 倍速提示", "1 倍速" in log.text())

        spy2 = _PullSpy()
        restore = install_stubs(spy2)
        log2 = _Logger()
        try:
            rc2 = nvr_puller.run_day(cfg, log2, tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("重复执行退出码 0", rc2, 0)
        check("重复执行不再拉取", len(spy2.calls), 0)
        check_true("重复执行说明已归档完成", "已归档完成" in log2.text())

        set_cursor(cfg, datetime(2026, 9, 15, 15, 0))
        spy3 = _PullSpy()
        restore = install_stubs(spy3)
        try:
            nvr_puller.run_day(cfg, _Logger(), tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("从断点 15:00 继续", spy3.calls[0][0].strftime("%H:%M"), "15:00")
        check("续拉 15 段（15:00~22:30）", len(spy3.calls), 15)

        set_cursor(cfg, datetime(2026, 9, 15, 15, 0))
        spy4 = _PullSpy()
        restore = install_stubs(spy4)
        log4 = _Logger()
        try:
            nvr_puller.run_day(cfg, log4, tmp, datetime(2026, 9, 15), redo=True)
        finally:
            restore()
        check("--redo 从窗口开头重拉", spy4.calls[0][0].strftime("%H:%M"), "09:30")
        check("--redo 段数 26", len(spy4.calls), 26)
        check_true("--redo 提示会覆盖同名文件", "覆盖" in log4.text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_day_guards():
    print("\n[4] run_day 边界与互斥")
    tmp = tempfile.mkdtemp()
    spy = _PullSpy()
    restore = install_stubs(spy)
    try:
        cfg = make_cfg(tmp)

        set_cursor(cfg, datetime(2026, 9, 15))
        log = _Logger()
        rc = nvr_puller.run_day(cfg, log, tmp, datetime(2030, 1, 1))
        check("未来日期退出码 3（尚不可归档）", rc, 3)
        check("未来日期不拉取", len(spy.calls), 0)
        check_true("未来日期有明确说明", "尚未录到" in log.text())

        for extra in (["--from", "2026-09-15 00:00:00"], ["--to", "2026-09-15 00:00:00"],
                      ["--once"]):
            rc2 = nvr_puller.main(["--day", "yesterday"] + extra)
            check("--day 与 %s 互斥" % extra[0], rc2, 2)

        check("--day 非法取值退出码 2", nvr_puller.main(["--day", "某天"]), 2)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_and_split():
    print("\n[5] 并发：分段计划 + 「已拉过的段不再拉」")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        plan = nvr_puller.day_plan_windows(
            cfg, datetime(2026, 9, 15, 9, 30), datetime(2026, 9, 15, 22, 30))
        check("13 小时切 26 段", len(plan), 26)
        check("首段起点", plan[0][0].strftime("%H:%M"), "09:30")
        check("末段止于窗口末尾", plan[-1][1].strftime("%H:%M"), "22:30")

        done, pending = nvr_puller.split_batch(cfg, tmp, plan)
        check("初次全部待拉", (done, len(pending)), (0, 26))

        p0 = nvr_puller.segment_path(cfg, tmp, plan[0][0], plan[0][1])
        check_true("路径落在归档根目录内",
                   os.path.abspath(p0).startswith(os.path.abspath(tmp)), p0)
        check_true("按日期分层", os.sep + "2026-09-15" + os.sep in p0, p0)
        check_true("文件名含门店与通道", "测试门店_IPC_" in os.path.basename(p0),
                   os.path.basename(p0))

        for s, e in (plan[0], plan[1]):
            p = nvr_puller.segment_path(cfg, tmp, s, e)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()
        done, pending = nvr_puller.split_batch(cfg, tmp, plan)
        check("已存在的段判为完成", done, 2)
        check("待拉剩 24 段", len(pending), 24)
        check("待拉从第 3 段开始", pending[0][0].strftime("%H:%M"), "10:30")

        done_r, pending_r = nvr_puller.split_batch(cfg, tmp, plan, skip_existing=False)
        check("--redo 时 26 段全部重拉", (done_r, len(pending_r)), (0, 26))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cursor_prefix():
    print("\n[6] 并发：游标只按「连续完成前缀」推进（有空洞不许跳过）")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        start = datetime(2026, 9, 15, 9, 30)
        plan = nvr_puller.day_plan_windows(cfg, start, datetime(2026, 9, 15, 11, 30))
        check("4 段", len(plan), 4)

        heights = {plan[0]: "ok", plan[1]: "fail", plan[2]: "ok", plan[3]: "ok"}
        cur = nvr_puller._resolve_cursor(cfg, tmp, plan, heights, start)
        check("中途失败时游标停在其之前（不能越过失败段）",
              cur.strftime("%H:%M"), "10:00")

        cur2 = nvr_puller._resolve_cursor(cfg, tmp, plan, {w: "ok" for w in plan}, start)
        check("全部完成时游标到末尾", cur2.strftime("%H:%M"), "11:30")

        cur3 = nvr_puller._resolve_cursor(
            cfg, tmp, plan,
            {plan[0]: "ok", plan[1]: "gap", plan[2]: "ok", plan[3]: "ok"}, start)
        check("无录像的段视为已了结，游标继续推进", cur3.strftime("%H:%M"), "11:30")

        p = nvr_puller.segment_path(cfg, tmp, plan[0][0], plan[0][1])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").close()
        cur4 = nvr_puller._resolve_cursor(cfg, tmp, plan, {}, start)
        check("盘上已有文件也算完成（只推 1 段）", cur4.strftime("%H:%M"), "10:00")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_parallel_batch():
    print("\n[7] 并发跑批：确实并发、账目正确、断点续拉只拉缺的段")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        cfg.runtime.parallel_streams = 6
        cfg.runtime.start_stagger_seconds = 0.1
        set_cursor(cfg, datetime(2026, 9, 15))
        spy = _PullSpy(write_file=True, sleep=0.35)
        restore = install_stubs(spy)
        log = _Logger()
        try:
            rc = nvr_puller.run_day(cfg, log, tmp, datetime(2026, 9, 15))
        finally:
            restore()

        check("退出码 0", rc, 0)
        check("26 段全部拉到", len(spy.calls), 26)
        check_true("确实并发了（峰值 >1）", spy.max_active > 1,
                   "峰值=%d" % spy.max_active)
        check_true("并发不超过设定路数", spy.max_active <= 6,
                   "峰值=%d" % spy.max_active)
        gaps_sp = [b - a for a, b in zip(spy.starts, spy.starts[1:])]
        check_true("各次启动确有最小间隔（错峰生效）",
                   all(g >= 0.06 for g in gaps_sp),
                   "最小间隔=%.3fs" % (min(gaps_sp) if gaps_sp else 0))
        check("游标推进到窗口末尾", get_cursor(cfg).strftime("%H:%M"), "22:30")
        check_true("日志说明并发路数", "6 路" in log.text())
        check_true("日志给出并发后的预计耗时", "单路约需" in log.text())

        idx = cfg.index_file
        lines = ([ln for ln in open(idx, encoding="utf-8").read().splitlines() if ln.strip()]
                 if os.path.isfile(idx) else [])
        check("索引写入 26 条（每段一条）", len(lines), 26)

        set_cursor(cfg, datetime(2026, 9, 15))
        spy2 = _PullSpy(write_file=True)
        restore = install_stubs(spy2)
        log2 = _Logger()
        try:
            rc2 = nvr_puller.run_day(cfg, log2, tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("文件齐了再跑退出码 0", rc2, 0)
        check("文件齐了不再重拉任何一段", len(spy2.calls), 0)
        check_true("日志说明此前已归档", "此前已归档" in log2.text())

        victim = nvr_puller.segment_path(cfg, tmp, *nvr_puller.day_plan_windows(
            cfg, datetime(2026, 9, 15, 9, 30),
            datetime(2026, 9, 15, 22, 30))[10])
        os.remove(victim)
        set_cursor(cfg, datetime(2026, 9, 15))
        spy3 = _PullSpy(write_file=True)
        restore = install_stubs(spy3)
        try:
            nvr_puller.run_day(cfg, _Logger(), tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("只补缺失的那一段", len(spy3.calls), 1)
        check("补的正是缺失的那段", spy3.calls[0][0].strftime("%H:%M"), "14:30")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_batch_recent_failure():
    print("\n[8] 并发：最近时段某段失败 -> 记为失败且游标不越过它")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        cfg.runtime.parallel_streams = 4
        now = datetime.now().replace(microsecond=0)
        start = now - timedelta(minutes=50)
        hard_end = now - timedelta(minutes=5)
        plan = nvr_puller.day_plan_windows(cfg, start, hard_end)
        check("切出 2 段", len(plan), 2)

        state = State.load(cfg.state_file)
        state.set_next_start(start)
        state.save(cfg.state_file)

        spy = _PullSpy(write_file=True, fail_on={plan[0][0]})
        restore = install_stubs(spy)
        log = _Logger()
        try:
            stats = nvr_puller.run_batch(cfg, "ffmpeg", state, tmp, log, plan, 4)
        finally:
            restore()

        check("成功 1 段", stats["saved"], 1)
        check("失败 1 段", len(stats["failed"]), 1)
        check("失败段正是第 1 段", stats["failed"][0][0], plan[0][0])
        check("游标未越过失败段", state.get_next_start(), start)
        check_true("日志要求重跑", "留待下次重跑" in log.text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stagger_gate():
    print("\n[9] 错峰启动：拉开各路的发起时刻，关掉即恢复同时发起")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        cfg.runtime.parallel_streams = 6
        plan = nvr_puller.day_plan_windows(
            cfg, datetime(2026, 9, 15, 10, 0), datetime(2026, 9, 15, 12, 0))
        check("切出 4 段", len(plan), 4)

        def _run(stagger, tag):
            cfg.runtime.start_stagger_seconds = stagger
            root = os.path.join(tmp, tag)
            state = State.load(cfg.state_file)
            state.set_next_start(plan[0][0])
            state.save(cfg.state_file)
            spy = _PullSpy(write_file=True, sleep=0.02)
            restore = install_stubs(spy)
            try:
                nvr_puller.run_batch(cfg, "ffmpeg", state, root, _Logger(), plan, 6)
            finally:
                restore()
            return spy

        spy_on = _run(0.2, "on")
        check("开启错峰：4 段都拉了", len(spy_on.calls), 4)
        gaps = [b - a for a, b in zip(spy_on.starts, spy_on.starts[1:])]
        check_true("开启错峰：各次启动被拉开（最小间隔 ≥0.15s）",
                   bool(gaps) and min(gaps) >= 0.15,
                   "最小间隔=%.3fs" % (min(gaps) if gaps else 0))
        check_true("开启错峰且段耗时远小于间隔时，实际不会并发",
                   spy_on.max_active == 1, "峰值=%d" % spy_on.max_active)

        spy_off = _run(0.0, "off")
        check_true("关闭错峰：立即恢复并发",
                   spy_off.max_active > 1, "峰值=%d" % spy_off.max_active)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_catchup():
    print("\n[10] 自动补漏：游标落后于目标日时逐日补齐，不再永久丢天")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        set_cursor(cfg, datetime(2026, 9, 13, 22, 30))
        spy = _PullSpy()
        restore = install_stubs(spy)
        log = _Logger()
        try:
            rc = nvr_puller.run_day(cfg, log, tmp, datetime(2026, 9, 15))
        finally:
            restore()

        check("退出码 0", rc, 0)
        check("两天共 52 段（每天 26 段）", len(spy.calls), 52)
        check("从最早缺的那天开始", spy.calls[0][0].strftime("%Y-%m-%d %H:%M"),
              "2026-09-14 09:30")
        check("最后一段止于目标日末尾", spy.calls[-1][1].strftime("%Y-%m-%d %H:%M"),
              "2026-09-15 22:30")
        days = sorted({a.date().strftime("%m-%d") for a, _b in spy.calls})
        check("覆盖了 9-14 与 9-15", days, ["09-14", "09-15"])
        check_true("日志说明本次要补几天", "本次需归档 2 天" in log.text())
        check("收尾游标在目标日末尾", get_cursor(cfg).strftime("%Y-%m-%d %H:%M"),
              "2026-09-15 22:30")

        set_cursor(cfg, datetime(2026, 9, 14, 15, 0))
        spy2 = _PullSpy()
        restore = install_stubs(spy2)
        try:
            nvr_puller.run_day(cfg, _Logger(), tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("从 9-14 的断点续拉（而不是从 9-15 开始）",
              spy2.calls[0][0].strftime("%Y-%m-%d %H:%M"), "2026-09-14 15:00")
        check("续拉 9-14 的 15 段 + 9-15 的 26 段", len(spy2.calls), 41)

        spy3 = _PullSpy()
        restore = install_stubs(spy3)
        log3 = _Logger()
        try:
            rc3 = nvr_puller.run_day(cfg, log3, tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("补完再跑退出码 0", rc3, 0)
        check("补完再跑不再拉取", len(spy3.calls), 0)
        check_true("明确说明已归档完成", "已归档完成" in log3.text())

        set_cursor(cfg, datetime(2026, 9, 13, 22, 30))
        spy4 = _PullSpy()
        restore = install_stubs(spy4)
        try:
            nvr_puller.run_day(cfg, _Logger(), tmp, datetime(2026, 9, 15), redo=True)
        finally:
            restore()
        check("--redo 只重拉目标日 26 段", len(spy4.calls), 26)
        check("--redo 从目标日窗口开头开始",
              spy4.calls[0][0].strftime("%Y-%m-%d %H:%M"), "2026-09-15 09:30")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_catchup_cap():
    print("\n[11] 补漏上限：停机太久只补最近 N 天，超出的区间明确告警")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        cfg.runtime.catchup_max_days = 3
        set_cursor(cfg, datetime(2026, 9, 1))
        spy = _PullSpy()
        restore = install_stubs(spy)
        log = _Logger()
        try:
            rc = nvr_puller.run_day(cfg, log, tmp, datetime(2026, 9, 15))
        finally:
            restore()

        check("退出码 0", rc, 0)
        days = sorted({a.date().strftime("%m-%d") for a, _b in spy.calls})
        check("只补最近 3 天", days, ["09-13", "09-14", "09-15"])
        check("3 天共 78 段", len(spy.calls), 78)
        check_true("告警说明已超出上限", "已超出" in log.text())
        check_true("告警给出被跳过的区间",
                   "2026-09-01 ~ 2026-09-12" in log.text())

        cfg.runtime.catchup_max_days = 1
        set_cursor(cfg, datetime(2026, 9, 1))
        spy2 = _PullSpy()
        restore = install_stubs(spy2)
        try:
            nvr_puller.run_day(cfg, _Logger(), tmp, datetime(2026, 9, 15))
        finally:
            restore()
        check("上限 1 天时只拉目标日（26 段）", len(spy2.calls), 26)
        check("只拉目标日那一天", spy2.calls[0][0].strftime("%Y-%m-%d"), "2026-09-15")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 70)
    print("按日归档（--day）验证")
    print("=" * 70)
    test_parse_day_arg()
    test_day_window()
    test_run_day_flow()
    test_run_day_guards()
    test_plan_and_split()
    test_cursor_prefix()
    test_parallel_batch()
    test_batch_recent_failure()
    test_stagger_gate()
    test_catchup()
    test_catchup_cap()
    print("\n" + "=" * 70)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
