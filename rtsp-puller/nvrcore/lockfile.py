# -*- coding: utf-8 -*-
"""单实例锁：同一时刻只允许一个归档进程动录像机。

为什么需要它
------------
调度器（任务计划 / cron）只判断"上次启动的进程退出了没有"，看不到
"上一批还没跑完"。而按日归档 13 小时素材在 6 路并发下仍要跑约 2 小时。
一旦触发间隔短于单批耗时，就会出现两个实例同时在跑，后果有三个：

  1. 并发路数翻倍（6 路变 12 路），可能把门店录像机打满、影响实时预览；
  2. 两批都会写同一个最终文件名 —— 后完成的把先完成的覆盖掉；
  3. 两批各自独立推进 `state/state.json` 的游标，互相踩。

所以必须在脚本内部自守，不能指望调度器。

可靠性要点
----------
* 锁文件用 ``O_CREAT | O_EXCL`` **原子**创建，内容记 pid / 启动时间 / 模式。
* 进程被强杀或断电会留下**陈旧锁**。下次启动时检测到锁里的 pid 已经不存在，
  就自动接管并告警 —— 绝不会把自己永久锁死。
* pid 存活判断拿不到权限（Access denied）时**按存活处理**，宁可让运维手动确认，
  也不要误判成"没人跑"而放两个实例进来。
* 释放前先核对锁文件里还是自己的 pid，避免把别人接管的锁删掉。
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime
from typing import Optional

_FMT = "%Y-%m-%d %H:%M:%S"

LOCK_FILE_NAME = "puller.lock"


def default_lock_path(state_file: str) -> str:
    """锁文件与状态文件同目录。

    自定义了 `runtime.state_file` 的实例（多门店并行、测试场景）
    会自带一份锁，互不干扰。
    """
    return os.path.join(os.path.dirname(os.path.abspath(state_file)), LOCK_FILE_NAME)


class LockBusy(RuntimeError):
    """已有另一个实例持有锁。"""

    def __init__(self, info: Optional[dict]):
        self.info = info or {}
        pid = self.info.get("pid")
        since = self.info.get("started_at") or "未知时间"
        mode = self.info.get("mode") or "归档"
        super().__init__("已有归档进程在运行（pid=%s，%s，启动于 %s）"
                         % (pid, mode, since))


def describe(info: Optional[dict]) -> str:
    if not info:
        return "(锁文件内容无法解析)"
    return "pid=%s  模式=%s  启动于 %s  主机=%s" % (
        info.get("pid"), info.get("mode"), info.get("started_at"),
        info.get("host") or "-")


def _pid_alive(pid) -> bool:
    """进程是否还存在。拿不准时一律返回 True（保守，宁可误报占用）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class SingleInstance:
    """基于锁文件的单实例守卫。用 with 语句或显式 release() 释放。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.info: Optional[dict] = None

    # ------------------------------------------------------------------ #
    def _read(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def acquire(self, mode: str = "", ignore: bool = False,
                logger=None) -> Optional[dict]:
        """抢锁成功返回被接管的陈旧锁信息（没有则为 None）。

        被占用时抛 LockBusy；ignore=True 表示无视占用强行接管
        （`--ignore-lock`，留给运维手工介入用）。
        """
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        info = {
            "pid": os.getpid(),
            "started_at": datetime.now().strftime(_FMT),
            "mode": mode or "归档",
            "host": _hostname(),
        }

        stale: Optional[dict] = None
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                old = self._read()
                pid = (old or {}).get("pid")
                if not ignore and _pid_alive(pid):
                    raise LockBusy(old)
                stale = old
                if logger is not None:
                    if ignore:
                        logger.warning("--ignore-lock：无视锁文件强行启动（原锁：%s）",
                                       describe(old))
                    else:
                        logger.warning("发现陈旧锁文件（%s），原进程已不存在，自动接管",
                                       describe(old))
                removed = True
                try:
                    os.remove(self.path)
                except Exception:               # pylint: disable=broad-except
                    removed = False
                if not removed:
                    if ignore:
                        try:
                            with open(self.path, "w", encoding="utf-8") as f:
                                json.dump(info, f, ensure_ascii=False, indent=2)
                            self.info = info
                            return stale
                        except Exception:       # pylint: disable=broad-except
                            pass
                    if logger is not None:
                        logger.warning("陈旧锁文件无法删除（%s），重试中；"
                                       "若持续失败可用 --ignore-lock 强行接管", self.path)
                continue

            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            self.info = info
            return stale

        raise LockBusy(self._read())

    def release(self) -> bool:
        """释放锁。锁已被别人接管时不误删，返回 False。"""
        cur = self._read()
        if cur is not None and cur.get("pid") != os.getpid():
            self.info = None
            return False
        try:
            os.remove(self.path)
            self.info = None
            return True
        except Exception:                       # pylint: disable=broad-except
            self.info = None
            return False

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:                                   # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
#  进程信息（--status 用）
# --------------------------------------------------------------------------- #
def current_holder(lock_path: str) -> Optional[dict]:
    """锁文件当前的内容（没人持有时返回 None）。"""
    inst = SingleInstance(lock_path)
    return inst._read()


if __name__ == "__main__":                              # pragma: no cover
    p = default_lock_path(sys.argv[1] if len(sys.argv) > 1 else "state/state.json")
    print("锁文件：%s" % p)
    print("当前持有：%s" % describe(current_holder(p)))
