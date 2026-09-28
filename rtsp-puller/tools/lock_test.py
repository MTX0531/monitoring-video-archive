# -*- coding: utf-8 -*-
"""单实例锁验证。

覆盖：
  1. 锁文件路径：与 state_file 同目录
  2. 抢锁成功：写下的 pid / 模式 / 启动时间正确
  3. 已被占用：另一个真实存活的进程持锁 -> 拒绝并抛 LockBusy
  4. 陈旧锁：持锁进程已退出 -> 自动接管
  5. ignore：显式无视占用强行接管
  6. 释放：删掉锁文件；但锁已被别人接管时不误删
  7. 命令行：有实例在跑时 --day 直接以 4 退出；加 --ignore-lock 才放行
  8. 常驻模式与按日模式共用同一把锁（互斥）

除第 7/8 会用到临时配置外，全部离线运行，不连录像机。
用法：python tools/lock_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import nvr_puller                                             # noqa: E402
from nvrcore.lockfile import (SingleInstance, LockBusy,       # noqa: E402
                              default_lock_path, describe, _pid_alive)

PASS = 0
FAIL = 0
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


# --------------------------------------------------------------------------- #
_HOLDER_SRC = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from nvrcore.lockfile import SingleInstance
inst = SingleInstance(sys.argv[2])
inst.acquire(mode="holder-test")
print("READY", os.getpid(), flush=True)
time.sleep(float(sys.argv[3]))
inst.release()
'''


class Holder:
    """在子进程里真的抢住锁，父进程可以观察到"另一个实例正在跑"。"""

    def __init__(self, lock_path, seconds=60):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(_HOLDER_SRC)
            self.script = f.name
        self.proc = subprocess.Popen(
            [sys.executable, self.script, BASE, lock_path, str(seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        self.proc_pid = self.proc.pid
        self.pid = self.proc.pid
        deadline = time.time() + 20
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            parts = line.split()
            if parts and parts[0] == "READY":
                if len(parts) > 1 and parts[1].isdigit():
                    self.pid = int(parts[1])
                return
        raise RuntimeError("持锁子进程没有就绪：%s" % (self.proc.stderr.read() or ""))

    def kill(self):
        """强杀（模拟断电/被任务计划强行结束），锁文件故意留在盘上。"""
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:                                       # noqa: BLE001
            pass

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:                                       # noqa: BLE001
            self.kill()
        try:
            os.remove(self.script)
        except OSError:
            pass


def _write_config(tmp, store="测试门店", channel="IPC"):
    """写一份自包含的临时配置。路径一律写绝对路径。"""
    cfg_path = os.path.join(tmp, "config.json")
    data = {
        "nvr": {"host": "127.0.0.1", "channel": 1},
        "archive": {"root_dir": os.path.join(tmp, "archive"),
                    "store_name": store, "channel_name": channel},
        "schedule": {"enabled": True, "start": "09:30", "end": "22:30",
                     "mode": "footage"},
        "retention": {"enabled": False},
        "device": {"enabled": False,
                   "cache_file": os.path.join(tmp, "state", "device.json")},
        "runtime": {"state_file": os.path.join(tmp, "state", "state.json"),
                    "log_dir": os.path.join(tmp, "logs"),
                    "parallel_streams": 1},
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return cfg_path


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


# --------------------------------------------------------------------------- #
def test_lock_path():
    print("\n[1] 锁文件位置：与状态文件同目录")
    tmp = tempfile.mkdtemp()
    try:
        p = default_lock_path(os.path.join(tmp, "state", "state.json"))
        check("锁落在 state 目录", os.path.dirname(p), os.path.join(tmp, "state"))
        check("锁文件名", os.path.basename(p), "puller.lock")

        a = default_lock_path(os.path.join(tmp, "shopA", "state.json"))
        b = default_lock_path(os.path.join(tmp, "shopB", "state.json"))
        check_true("不同实例的锁互不干扰", a != b, "%s vs %s" % (a, b))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_acquire_and_release():
    print("\n[2] 抢锁成功 / 释放")
    tmp = tempfile.mkdtemp()
    try:
        lock = os.path.join(tmp, "puller.lock")
        inst = SingleInstance(lock)
        stale = inst.acquire(mode="按日归档")
        check_true("锁文件已创建", os.path.isfile(lock), lock)
        check("没有陈旧锁可接管", stale, None)

        with open(lock, "r", encoding="utf-8") as f:
            info = json.load(f)
        check("锁里记的是自己的 pid", info.get("pid"), os.getpid())
        check("锁里记了模式", info.get("mode"), "按日归档")
        check_true("锁里记了启动时间", bool(info.get("started_at")), describe(info))

        check_true("重复抢同一把锁会被拒",
                   _raises_lock_busy(lambda: SingleInstance(lock).acquire()))
        check("释放成功", inst.release(), True)
        check_true("释放后锁文件消失", not os.path.exists(lock), lock)
        check("重复释放不报错", inst.release(), False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _raises_lock_busy(fn):
    try:
        fn()
        return False
    except LockBusy:
        return True


def test_busy_real_process():
    print("\n[3] 有真实存活进程持锁 -> 拒绝启动")
    tmp = tempfile.mkdtemp()
    holder = None
    try:
        lock = os.path.join(tmp, "puller.lock")
        holder = Holder(lock)
        check_true("持锁进程确实活着", _pid_alive(holder.proc_pid),
                   "pid=%s" % holder.proc_pid)

        try:
            SingleInstance(lock).acquire()
            check_true("应被拒绝", False, "居然抢到了锁")
        except LockBusy as exc:
            check_true("抛 LockBusy 并带上占用者信息", exc.info.get("pid") == holder.pid,
                       str(exc.info))

        inst = SingleInstance(lock)
        stale = inst.acquire(mode="强行", ignore=True)
        check_true("--ignore-lock 能强行接管", inst.info is not None)
        check("接管时把原锁信息交回来", (stale or {}).get("pid"), holder.pid)
        inst.release()
    finally:
        if holder:
            holder.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_lock():
    print("\n[4] 陈旧锁（进程已被强杀）-> 自动接管")
    tmp = tempfile.mkdtemp()
    holder = None
    try:
        lock = os.path.join(tmp, "puller.lock")
        holder = Holder(lock)
        dead_pid = holder.pid
        holder.kill()
        time.sleep(0.5)
        check_true("被强杀的 pid 已不存在", not _pid_alive(holder.proc_pid),
                   "pid=%s" % holder.proc_pid)
        check_true("锁文件仍在盘上（陈旧锁）", os.path.isfile(lock), lock)

        log = _Logger()
        inst = SingleInstance(lock)
        stale = inst.acquire(mode="恢复", logger=log)
        check("陈旧锁被接管", (stale or {}).get("pid"), dead_pid)
        check_true("日志明确告警自动接管", "自动接管" in log.text())
        check("接管后锁归自己", json.load(open(lock, encoding="utf-8")).get("pid"),
              os.getpid())
        inst.release()
        holder.close()
        holder = None
    finally:
        if holder:
            holder.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_release_never_steals():
    print("\n[5] 释放时不误删别人接管过去的锁")
    tmp = tempfile.mkdtemp()
    try:
        lock = os.path.join(tmp, "puller.lock")
        inst = SingleInstance(lock)
        inst.acquire(mode="本进程")
        with open(lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid() + 1, "mode": "别人接管了",
                       "started_at": "2026-09-16 10:00:00"}, f)
        check("发现锁已易主，释放返回 False", inst.release(), False)
        check_true("别人接管的锁文件没被删掉", os.path.isfile(lock), lock)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_refuses_second_instance():
    print("\n[6] 命令行：有实例在跑时 --day 以 4 退出")
    tmp = tempfile.mkdtemp()
    holder = None
    calls = []
    orig_run_day = nvr_puller.run_day
    try:
        cfg_path = _write_config(tmp)
        lock = default_lock_path(os.path.join(tmp, "state", "state.json"))
        nvr_puller.run_day = lambda *a, **k: (calls.append(a), 0)[1]

        holder = Holder(lock)

        rc = nvr_puller.main(["--config", cfg_path, "--day", "yesterday"])
        check("第二个实例退出码 4", rc, 4)
        check("完全没有进入拉取流程", len(calls), 0)

        rc2 = nvr_puller.main(["--config", cfg_path, "--once"])
        check("常驻模式同样被拦住", rc2, 4)

        rc3 = nvr_puller.main(["--config", cfg_path, "--status"])
        check("--status 不受锁影响（只读）", rc3, 0)

        rc4 = nvr_puller.main(["--config", cfg_path, "--day", "yesterday",
                               "--ignore-lock"])
        check("--ignore-lock 放行并跑起来", rc4, 0)
        check("放行后确实调用了按日归档", len(calls), 1)

        holder.close()
        holder = None
        time.sleep(0.3)
        calls.clear()
        rc5 = nvr_puller.main(["--config", cfg_path, "--day", "yesterday"])
        check("持锁进程退出后恢复正常启动", rc5, 0)
        check("恢复后确实调用了按日归档", len(calls), 1)
    finally:
        nvr_puller.run_day = orig_run_day
        if holder:
            holder.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_lock_released_on_exit():
    print("\n[7] 正常跑完会释放锁")
    tmp = tempfile.mkdtemp()
    orig_run_day = nvr_puller.run_day
    try:
        cfg_path = _write_config(tmp)
        lock = default_lock_path(os.path.join(tmp, "state", "state.json"))
        nvr_puller.run_day = lambda *a, **k: 0

        rc = nvr_puller.main(["--config", cfg_path, "--day", "yesterday"])
        check("第一次跑退出码 0", rc, 0)
        check_true("跑完后锁文件已被清掉", not os.path.exists(lock), lock)

        rc2 = nvr_puller.main(["--config", cfg_path, "--day", "yesterday"])
        check("紧接着第二次跑照样能启动", rc2, 0)
    finally:
        nvr_puller.run_day = orig_run_day
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_day_and_loop_share_lock():
    print("\n[8] 常驻归档持有锁期间，按日归档进不来")
    tmp = tempfile.mkdtemp()
    orig_run_loop = nvr_puller.run_loop
    try:
        cfg_path = _write_config(tmp)
        lock = default_lock_path(os.path.join(tmp, "state", "state.json"))
        seen = {"n": 0}

        def fake_loop(cfg, logger, root, once=False, sweep=True):
            seen["n"] += 1
            seen["locked"] = os.path.isfile(lock)
            return 0

        nvr_puller.run_loop = fake_loop
        rc = nvr_puller.main(["--config", cfg_path, "--once"])
        check("常驻跑退出码 0", rc, 0)
        check_true("跑的过程中锁是持有的", seen.get("locked") is True,
                   "locked=%r" % seen.get("locked"))
        check_true("跑完后锁已释放", not os.path.exists(lock), lock)
    finally:
        nvr_puller.run_loop = orig_run_loop
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 70)
    print("单实例锁验证")
    print("=" * 70)
    test_lock_path()
    test_acquire_and_release()
    test_busy_real_process()
    test_stale_lock()
    test_release_never_steals()
    test_cli_refuses_second_instance()
    test_lock_released_on_exit()
    test_run_day_and_loop_share_lock()
    print("\n" + "=" * 70)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
