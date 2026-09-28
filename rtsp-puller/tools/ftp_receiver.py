# -*- coding: utf-8 -*-
"""NVR 录像 FTP 接收端。

大华录像机的「录像 FTP 上传」会把**新录制完成的**录像文件主动推上来，
它是 push 模型：录像机连过来、把文件放进本机的收件箱目录。

与 RTSP 回放拉取的区别：
  - RTSP 是 pull，可以指定任意历史时段（补历史靠它）
  - FTP 是 push，只能在「录像机录完之后」自动送到（跟实时，不补历史）

用法：
    python tools/ftp_receiver.py                     # 用 config.json 的 ftp 段
    python tools/ftp_receiver.py --port 21
    python tools/ftp_receiver.py --dump-config       # 打印生效配置
    python tools/ftp_receiver.py --status            # 只读自查：东西收到哪了

每一条连接/登录/上传都会写进日志，并用得着的落一行清单到
state/ftp_received.jsonl，便于后续和归档台账对账。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import queue
import re
import signal
import socket
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

DEFAULTS = {
    "enabled": True,
    "bind": "0.0.0.0",
    "port": 21,
    "user": "nvrftp",
    "password": "<FTP_PASSWORD>",
    "root": "ftp-inbox",
    "passive_port_start": 60000,
    "passive_port_end": 60099,
    "max_connections": 32,
    "max_connections_per_ip": 16,
    "banner": "nvr-ftp-test ready",
    "min_free_gb": 3.0,
    "ingest": True,
    # ---- 自动收工 ----
    "idle_exit_minutes": 0,
    "idle_check_seconds": 30,
    "conn_active_seconds": 0,
    "drain_timeout_minutes": 60,
    "close_device_on_exit": True,
}


def free_gb(path):
    try:
        import shutil
        return shutil.disk_usage(path).free / float(1 << 30)
    except Exception:
        return float("inf")


# ---------------------------------------------------------------- 配置

def load_ftp_config(base_dir=BASE_DIR):
    """读 config.json 的 ftp 段；缺项用 DEFAULTS 补齐。"""
    cfg = dict(DEFAULTS)
    path = os.path.join(base_dir, "config.json")
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return cfg
    section = raw.get("ftp") or {}
    for k, v in section.items():
        if k.startswith("_"):
            continue
        if k in cfg:
            cfg[k] = v
    return cfg


def _abs(base_dir, p):
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def port_in_use(bind_addr, port):
    """判断端口是否已被别的进程占着。

    Windows 的 SO_REUSEADDR 语义与 Linux 不同——它允许两个进程绑同一个端口
    （后绑的静默接管），bind() 不会抛错。这里用一个**不带** SO_REUSEADDR
    的裸 socket 去试绑：只要有别的实例在监听，这一步就会失败。
    """
    import socket
    if int(port) == 0:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((bind_addr, int(port)))
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass
    return False


# ---------------------------------------------------------------- 日志

class EventLog:
    """同时写文件与 stdout 的极简事件日志（线程安全）。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def __call__(self, kind, msg):
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "%s [%-14s] %s" % (ts, kind, msg)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass
        return line


def append_manifest(path, record):
    """每收到一个文件落一行，供后续对账。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 设备端 FTP 上传开关

def _device_nas_keys(body):
    """从 NAS 配置响应里取出 Enable 等关键项。"""
    out = {}
    for line in (body or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _device_http_target(proj):
    """从**项目配置对象**里取设备 HTTP 访问信息 (host, port, username, password)。"""
    if proj is None:
        raise ValueError("缺少项目配置（config.json 的 nvr/device 段），无法访问设备")
    dev = proj.nvr
    port = int(getattr(getattr(proj, "device", None), "http_port", 80) or 80)
    return dev.host, port, dev.username, dev.password


def read_nas_switch(proj, timeout=6.0):
    """读设备端 FTP 上传开关。返回 dict，**不抛异常**。"""
    from nvrcore.deviceinfo import _DigestAuth, _http_get
    try:
        host, port, user, pwd = _device_http_target(proj)
        auth = _DigestAuth(user, pwd)
        code, body = _http_get(host, port,
                               "/cgi-bin/configManager.cgi?action=getConfig&name=NAS",
                               auth, timeout)
    except Exception as exc:                     # noqa: BLE001
        return {"code": 0, "enable": None, "values": {}, "body": "",
                "error": "%r" % exc}
    values = _device_nas_keys(body) if code == 200 else {}
    raw = values.get("table.NAS[0].Enable", "")
    enable = None
    if raw:
        enable = raw.strip().lower() in ("true", "1", "yes", "on")
    return {"code": code, "enable": enable, "values": values, "body": body,
            "error": "" if code == 200 else "HTTP %s" % code}


def set_nas_switch(proj, enable, timeout=6.0):
    """设置设备端 FTP 上传开关，并**回读确认**。**不抛异常**。"""
    from nvrcore.deviceinfo import _DigestAuth, _http_get
    want_bool = bool(enable)
    want = "true" if want_bool else "false"
    try:
        host, port, user, pwd = _device_http_target(proj)
        auth = _DigestAuth(user, pwd)
        path = ("/cgi-bin/configManager.cgi?action=setConfig&NAS[0].Enable=" + want)
        code, body = _http_get(host, port, path, auth, timeout)
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "want": want_bool, "readback": None,
                "detail": "setConfig 请求失败：%r" % exc}
    if code != 200:
        return {"ok": False, "want": want_bool, "readback": None,
                "detail": "setConfig 返回 HTTP %s" % code}
    back = read_nas_switch(proj, timeout=timeout)
    rb = back.get("enable")
    if rb is None:
        return {"ok": False, "want": want_bool, "readback": None,
                "detail": "回读没拿到 Enable 字段（%s）"
                          % (back.get("error") or ("HTTP %s" % back.get("code")))}
    return {"ok": bool(rb) == want_bool, "want": want_bool, "readback": rb,
            "detail": "" if bool(rb) == want_bool else "回读值未生效"}


def save_nas_snapshot(proj, base, log, tag):
    """把当前 NAS 配置存一份快照，供人工回滚。返回快照路径（失败返回 ""）。"""
    try:
        info = read_nas_switch(proj)
    except Exception as exc:                     # noqa: BLE001
        log("NAS-SNAPSHOT", "读设备配置异常：%r（未生成快照）" % exc)
        return ""
    body = (info.get("body") or "").strip()
    if info.get("enable") is None or not body:
        log("NAS-SNAPSHOT", "未读到设备配置（%s）——不生成快照，避免留下空壳备份"
            % (info.get("error") or "无有效内容"))
        return ""
    try:
        path = os.path.join(_abs(base, "state"),
                            "nas_%s_%s.txt" % (tag,
                                               _dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 采集时间：%s\n" % _dt.datetime.now().isoformat(timespec="seconds"))
            f.write("# 说明：这是关闭设备端 FTP 上传**之前**的原始配置，"
                    "需要恢复时把 Enable 改回原值即可\n")
            f.write(body + "\n")
        log("NAS-SNAPSHOT", "已存快照 %s（Enable=%s）"
            % (os.path.basename(path), info.get("enable")))
        return path
    except Exception as exc:                     # noqa: BLE001
        log("NAS-SNAPSHOT", "写快照失败：%r（不影响收工）" % exc)
        return ""


# ---------------------------------------------------------------- 入库（转成归档案）

class EventLogAdapter:
    """把 nvrcore 的 logging 风格调用接到事件日志上。"""

    def __init__(self, log):
        self._log = log

    def _emit(self, kind, msg, args):
        text = (msg % args) if args else str(msg)
        try:
            self._log(kind, text)
        except Exception:                    # noqa: BLE001
            pass

    def debug(self, msg, *args, **kwargs):
        pass

    def info(self, msg, *args, **kwargs):
        self._emit("INGEST", msg, args)

    def warning(self, msg, *args, **kwargs):
        self._emit("INGEST-WARN", msg, args)

    def error(self, msg, *args, **kwargs):
        self._emit("INGEST-ERR", msg, args)


class IngestWorker:
    """后台把收到的文件转成归档案（mp4 + 归档命名）。

    为什么必须异步：转封装/转码可能几十秒到几分钟，而 `on_file_received` 是在
    FTP **控制连接**上同步回调的——在这里干等会让设备以为上传没结束，
    进而超时重传。所以收到就先应答，转换交给后台线程。
    """

    def __init__(self, proj_cfg, log, ffmpeg, root=None, failed_dir=None):
        self.cfg = proj_cfg
        self.log = log
        self.ffmpeg = ffmpeg
        self.root = root
        self.failed_dir = failed_dir
        self.adapter = EventLogAdapter(log)
        self.q = queue.Queue()
        self.current = None
        self.done = 0
        self.failed = 0
        self._t = threading.Thread(target=self._run, name="ftp-ingest",
                                   daemon=True)
        self._t.start()

    def submit(self, path, name_hint=None):
        name_hint = name_hint or os.path.basename(path)
        self.q.put((path, name_hint))
        self.log("INGEST-QUEUE", "已排队入库：%s" % name_hint)

    def busy(self):
        return self.current is not None or not self.q.empty()

    def snapshot(self):
        return {"current": self.current, "queued": self.q.qsize(),
                "done": self.done, "failed": self.failed}

    def _run(self):
        from nvrcore import ftp_ingest          # 延迟导入：关闭入库时零开销
        while True:
            path, name_hint = self.q.get()
            self.current = name_hint
            try:
                res = ftp_ingest.ingest(self.cfg, path, self.adapter,
                                        ffmpeg=self.ffmpeg, root=self.root,
                                        name_hint=name_hint,
                                        failed_dir=self.failed_dir)
                status = res.get("status")
                if status == "ok":
                    self.done += 1
                    self.log("INGEST-OK", "%s -> %s" % (
                        name_hint, os.path.basename(res["path"])))
                elif status == "skipped":
                    self.log("INGEST-SKIP", "%s（目标已存在，未覆盖）" % name_hint)
                else:
                    self.failed += 1
                    self.log("INGEST-FAIL", "%s：%s"
                             % (name_hint, res.get("detail", "")))
            except Exception as exc:             # noqa: BLE001
                self.failed += 1
                self.log("INGEST-ERR", "%s: %r" % (name_hint, exc))
            finally:
                self.current = None
                self.q.task_done()


# ---------------------------------------------------------------- 状态自查

_IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def has_remote_ip(line):
    """行里是否存在**非回环**的 IPv4 地址。"""
    for m in _IPV4_RE.finditer(line):
        addr = m.group(0)
        if addr.startswith("127.") or addr == "0.0.0.0":
            continue
        return True
    return False


def collect_status(cfg, base):
    """汇总接收端的落盘与运行状态。**只读**。"""
    root = _abs(base, cfg["root"])
    manifest = os.path.join(_abs(base, "state"), "ftp_received.jsonl")
    log_path = _abs(base, os.path.join("logs", "ftp_receiver.log"))

    files = []
    if os.path.isdir(root):
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                fp = os.path.join(dp, f)
                try:
                    files.append((os.path.relpath(fp, root), os.path.getsize(fp)))
                except OSError:
                    pass
    files.sort()

    records = []
    if os.path.isfile(manifest):
        for line in open(manifest, encoding="utf-8",
                         errors="replace").read().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass

    device_events = []
    if os.path.isfile(log_path):
        for line in open(log_path, encoding="utf-8",
                         errors="replace").read().splitlines():
            if not any(k in line for k in
                       ("CONNECT", "LOGIN", "UPLOAD", "REJECT", "MKDIR")):
                continue
            if has_remote_ip(line):
                device_events.append(line)

    ingests = []
    index_path = None
    try:
        from nvrcore.config import load_config
        proj = load_config(None, base_dir=base)
        index_path = proj.index_file
    except Exception:                        # noqa: BLE001
        index_path = os.path.join(_abs(base, "state"), "index.jsonl")
    if os.path.isfile(index_path or ""):
        loaded = []
        for line in open(index_path, encoding="utf-8",
                         errors="replace").read().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:                # noqa: BLE001
                continue
            if rec.get("source") == "ftp":
                loaded.append(rec)
        ingests = loaded[-10:]

    return {
        "root": root,
        "files": files,
        "manifest": manifest,
        "records": records,
        "log": log_path,
        "device_events": device_events,
        "free_gb": free_gb(root),
        "min_free_gb": float(cfg["min_free_gb"]),
        "port_busy": port_in_use(cfg["bind"], cfg["port"]),
        "bind": cfg["bind"],
        "port": cfg["port"],
        "ingest_enabled": bool(cfg.get("ingest", True)),
        "index_path": index_path,
        "ingests": ingests,
    }


def format_status(st):
    """把 collect_status 的结果排成给人看的文本。"""
    out = []
    total = sum(sz for _r, sz in st["files"])

    out.append("收件箱 : %s" % st["root"])
    out.append("  文件 %d 个，共 %.2f MB" % (len(st["files"]), total / 1048576.0))
    for rel, sz in st["files"][-10:]:
        out.append("    %-56s %8.2f MB" % (rel, sz / 1048576.0))
    if not st["files"]:
        out.append("    （空 —— 录像机至今没有推过任何文件）")

    out.append("")
    out.append("接收端 : %s:%s  %s" % (
        st["bind"], st["port"],
        "有实例在监听" if st["port_busy"] else "没有实例在监听"))
    free = st["free_gb"]
    gate = st["min_free_gb"]
    if free == float("inf"):
        out.append("磁盘   : 无法读取余量")
    else:
        out.append("磁盘   : 可用 %.2f GB，拒收闸门 %.2f GB，%s" % (
            free, gate,
            "正常收件" if free >= gate else "已触发闸门，上传会被 452 拒收"))

    out.append("")
    out.append("到货清单: %s" % st["manifest"])
    out.append("  记录 %d 条" % len(st["records"]))
    for r in st["records"][-5:]:
        out.append("    %s  %s  %s 字节" % (
            r.get("received_at", "?"), r.get("rel_path", "?"), r.get("size", "?")))

    out.append("")
    out.append("设备事件（已排除本机自测）: %d 条" % len(st["device_events"]))
    if st["device_events"]:
        for line in st["device_events"][-8:]:
            out.append("    " + line)
    else:
        out.append("    （无 —— 录像机从未连接过本接收端）")

    out.append("")
    if st.get("ingest_enabled"):
        out.append("入库   : 已启用 —— 收到后转封装成 mp4，"
                   "按 <门店>_<通道>_<起>_<止>_device.mp4 命名，"
                   "放进与 RTSP 相同的归档根目录")
    else:
        out.append("入库   : 已关闭 —— 文件原样留在收件箱")
    out.append("归档台账: %s" % (st.get("index_path") or "?"))
    ingests = st.get("ingests") or []
    out.append("  其中 FTP 入库 %d 条（显示最近 %d 条）"
               % (len(ingests), min(10, len(ingests))))
    for rec in ingests:
        out.append("    %s  %s" % (rec.get("requested_start", "?"),
                                   rec.get("file", "?")))
    if not ingests:
        out.append("    （无 —— 还没有文件通过 FTP 入库）")
    return "\n".join(out)


# ---------------------------------------------------------------- 服务

def build_server(cfg, base_dir, log, monitor=None):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = _abs(base_dir, cfg["root"])
    os.makedirs(root, exist_ok=True)

    auth = DummyAuthorizer()
    auth.add_user(cfg["user"], cfg["password"], root, perm="elradfmw")

    manifest = os.path.join(_abs(base_dir, "state"), "ftp_received.jsonl")

    worker = None
    proj = None
    try:
        from nvrcore.config import load_config
        proj = load_config(None, base_dir=base_dir)
    except Exception as exc:                     # noqa: BLE001
        log("CONFIG", "读取项目配置失败：%r（入库与设备开关将不可用）" % exc)
        proj = None
    if cfg.get("ingest", True) and proj is not None:
        try:
            from nvrcore.downloader import find_ffmpeg
            worker = IngestWorker(proj, log, find_ffmpeg(proj))
            log("INGEST", "已启用入库：推来的文件会转封装成 mp4，按 "
                          "<门店>_<通道>_<起>_<止>_device.mp4 命名，"
                          "放进与 RTSP 拉取相同的归档根目录")
        except Exception as exc:                 # noqa: BLE001
            log("INGEST-OFF", "入库不可用（%r）——文件将原样留在收件箱" % exc)
            worker = None

    def guarded(fn):
        """回调里任何异常都不能把连接搞断。"""
        def wrapper(self, *a, **kw):
            try:
                return fn(self, *a, **kw)
            except Exception as e:            # noqa: BLE001
                try:
                    log("HANDLER-ERR", "%s: %r" % (fn.__name__, e))
                except Exception:
                    pass
        wrapper.__name__ = fn.__name__
        return wrapper

    class Handler(FTPHandler):
        banner = cfg["banner"]
        passive_ports = range(int(cfg["passive_port_start"]),
                              int(cfg["passive_port_end"]) + 1)

        def _owner(self):
            return getattr(self, "username", None) or "-"

        @guarded
        def ftp_STOR(self, file, mode="w"):
            """磁盘闸门 + 自动补齐父目录。"""
            if monitor is not None:
                monitor.conn_activity("STOR")
            limit = float(cfg["min_free_gb"])
            avail = free_gb(root)
            if avail < limit:
                log("REJECT", "来自 %s  磁盘不足(剩余 %.2fGB < %.2fGB)，拒收 %s" %
                    (self.remote_ip, avail, limit, file))
                self.respond("452 磁盘空间不足，暂不接受上传")
                return

            parent = os.path.dirname(file)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                    log("MKDIR", "来自 %s  自动创建目录 %s"
                        % (self.remote_ip, os.path.relpath(parent, root)))
                except OSError as e:
                    log("MKDIR-FAIL", "来自 %s  %s (%s)"
                        % (self.remote_ip, parent, e))
                    self.respond("550 无法创建目标目录")
                    return
            return super().ftp_STOR(file, mode)

        @guarded
        def ftp_APPE(self, file):
            parent = os.path.dirname(file)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    self.respond("550 无法创建目标目录")
                    return
            return super().ftp_APPE(file)

        @guarded
        def on_connect(self):
            if monitor is not None:
                monitor.conn_open()
            log("CONNECT", "来自 %s:%s" % (self.remote_ip, self.remote_port))

        @guarded
        def on_disconnect(self):
            if monitor is not None:
                monitor.conn_close()
            log("DISCONNECT", "%s:%s 断开" % (self.remote_ip, self.remote_port))

        @guarded
        def on_login(self, username):
            log("LOGIN-OK", "用户 %s 来自 %s" % (username, self.remote_ip))

        @guarded
        def on_login_failed(self, username, password):
            log("LOGIN-FAIL", "用户 %r 来自 %s" % (username, self.remote_ip))

        @guarded
        def on_file_received(self, file):
            if monitor is not None:
                monitor.conn_activity("FILE")
            try:
                size = os.path.getsize(file)
            except Exception:
                size = -1
            rel = os.path.relpath(file, root)
            log("UPLOAD-OK", "来自 %s  %s  %d 字节 (%.2f MB)" %
                (self.remote_ip, rel, size,
                 size / 1048576.0 if size > 0 else 0))
            append_manifest(manifest, {
                "received_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "remote_ip": self.remote_ip,
                "user": self._owner(),
                "rel_path": rel,
                "abs_path": os.path.abspath(file),
                "size": size,
                "ingest": "queued" if worker is not None else "off",
            })
            if worker is not None:
                worker.submit(file, name_hint=rel)
            else:
                log("KEEP", "未启用入库，文件原样留在收件箱：%s" % rel)

        @guarded
        def on_incomplete_file_received(self, file):
            log("UPLOAD-BAD", "未完成: %s 已删除" % os.path.relpath(file, root))

        @guarded
        def on_file_sent(self, file):
            log("DOWNLOAD", os.path.relpath(file, root))

    Handler.authorizer = auth
    server = FTPServer((cfg["bind"], int(cfg["port"])), Handler)
    server.max_cons = int(cfg["max_connections"])
    server.max_cons_per_ip = int(cfg["max_connections_per_ip"])
    return server, root, worker, proj


# ---------------------------------------------------------------- 自动收工

class IdleMonitor:
    """盯住接收端"还有没有活"，连续空闲够久就判定可以收工。

    为什么不用「最后一个文件到货时间 + N 分钟」这么简单：
    设备推完一批会断开、过一会儿再连下一批。断开的那一刻"最后到货时间"
    可能是几分钟前了，如果恰好超过 N 分钟就会误判成收工——而设备下一批
    正在路上。误判的代价是整个任务提前结束、设备被关掉、后续录像要等下次
    点火才补。所以判定必须同时看三件事，且要**连续**成立：

      a) 连接上没有近期动作 —— 设备确实不在传
      b) 收件箱里没有新文件  —— 没有刚落盘还没进队列的
      c) 入库队列彻底消化完  —— 含正在转码的那一件，不能掐断

    任一条不成立就把计时清零重来。
    """

    def __init__(self, cfg, base, log, worker, inbox_root):
        self.cfg = cfg
        self.base = base
        self.log = log
        self.worker = worker
        self.inbox = inbox_root
        self.idle_limit = max(0.0, float(cfg.get("idle_exit_minutes") or 0)) * 60.0
        self.interval = max(5.0, float(cfg.get("idle_check_seconds") or 30))
        self.enabled = self.idle_limit > 0
        self.conn_active_seconds = max(
            30.0, float(cfg.get("conn_active_seconds") or 0) or self.interval * 2)
        self._lock = threading.Lock()
        self._active = 0
        self._last_conn_event = 0.0
        self._last_touch = time.time()
        self._stop = threading.Event()
        self._t = None
        self.started_at = time.time()
        self.busy_reason = "启动"

    def conn_open(self):
        with self._lock:
            self._active += 1
            self._last_conn_event = time.time()
            self._last_touch = time.time()

    def conn_close(self):
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_conn_event = time.time()
            self._last_touch = time.time()

    def conn_activity(self, what=""):
        with self._lock:
            self._last_conn_event = time.time()
            self._last_touch = time.time()

    @property
    def active_connections(self):
        with self._lock:
            return self._active

    def conn_active(self):
        """连接上是否有**近期动作**（上传/列目录）。

        ★ 为什么不能拿"连接数 > 0"当忙碌依据：录像机推完一批后**保持长连接
        不断开**，若按"有连接就算忙"，空闲判定永远进不去，自动收工形同虚设。
        """
        with self._lock:
            opened = self._last_conn_event
        return (time.time() - opened) < self.conn_active_seconds

    def inbox_pending(self):
        """收件箱里是否还有**正在被处理**的文件（尚未收尾）。"""
        has_file = False
        try:
            for _dp, _dn, fn in os.walk(self.inbox):
                if fn:
                    has_file = True
                    break
        except OSError:
            return False
        if not has_file:
            return False
        if self.worker is not None and self.worker.busy():
            return False
        if self.worker is None:
            return False
        return not self.conn_active()

    def idle_seconds(self):
        """连续空闲了多久。只要有活，返回 0。"""
        reason = None
        if self.conn_active():
            reason = "%d 个连接，最近 %ds 内有动作" % (self.active_connections,
                                                        self.conn_active_seconds)
        elif self.worker is not None and self.worker.busy():
            reason = "入库队列未消化（正在处理 %s）" % (self.worker.current or "-")
        elif self.inbox_pending():
            reason = "收件箱还有文件"
        if reason:
            with self._lock:
                self._last_touch = time.time()
            self.busy_reason = reason
            return 0.0
        with self._lock:
            last = self._last_touch
        return max(0.0, time.time() - last)

    def should_finish(self):
        return self.enabled and self.idle_seconds() >= self.idle_limit

    def _loop(self, on_finish):
        while not self._stop.wait(self.interval):
            if self.should_finish():
                on_finish()
                return

    def start(self, on_finish):
        if not self.enabled:
            return
        self._t = threading.Thread(target=self._loop, args=(on_finish,),
                                   name="ftp-idle", daemon=True)
        self._t.start()
        self.log("IDLE-WATCH", "已启用自动收工：连续空闲 %.0f 分钟（每 %.0f 秒探一次）"
                               "即退出，退出前会关掉设备端 FTP 上传"
                               % (self.idle_limit / 60.0, self.interval))

    def stop(self):
        self._stop.set()


def wait_ingest_drain(worker, log, timeout_minutes, interval=5.0):
    """等入库干完。返回 True 表示确实干完了。"""
    if worker is None:
        return True
    deadline = time.time() + max(0.0, float(timeout_minutes or 0)) * 60.0
    while worker.busy():
        if time.time() >= deadline:
            log("DRAIN-TIMEOUT", "入库等了 %.0f 分钟仍未干完（当前 %s，队列 %d），"
                                 "不再等待——未完成的段会由下次运行或 repair 工具处理"
                % (timeout_minutes, worker.current or "-", worker.q.qsize()))
            return False
        time.sleep(interval)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="NVR 录像 FTP 接收端")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default=None)
    ap.add_argument("--root", default=None, help="收件箱目录")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--log", default=None, help="日志文件，默认 logs/ftp_receiver.log")
    ap.add_argument("--base-dir", default=BASE_DIR)
    ap.add_argument("--dump-config", action="store_true",
                    help="打印生效配置后退出（不启动服务）")
    ap.add_argument("--status", action="store_true",
                    help="只读自查：收件箱位置与文件数、磁盘余量、到货清单、"
                         "设备是否连过（不启动服务）")
    ap.add_argument("--idle-exit", type=float, default=None, metavar="分钟",
                    help="连续空闲这么多分钟后自动收工退出（0=一直跑）")
    ap.add_argument("--no-close-device", action="store_true",
                    help="收工时**不要**动设备端的 FTP 上传开关")
    ap.add_argument("--max-hours", type=float, default=None, metavar="小时",
                    help="硬性最长运行时长，到点无条件收工（兜底）")
    args = ap.parse_args(argv)

    base = os.path.abspath(args.base_dir)
    cfg = load_ftp_config(base)
    for key, val in (("port", args.port), ("bind", args.bind), ("root", args.root),
                     ("user", args.user), ("password", args.password)):
        if val is not None:
            cfg[key] = val
    if args.idle_exit is not None:
        cfg["idle_exit_minutes"] = max(0.0, float(args.idle_exit))
    if args.no_close_device:
        cfg["close_device_on_exit"] = False

    log_path = _abs(base, args.log or os.path.join("logs", "ftp_receiver.log"))
    log = EventLog(log_path)

    root = _abs(base, cfg["root"])
    if args.dump_config:
        info = dict(cfg)
        info["root_abs"] = root
        info["log"] = log_path
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.status:
        print(format_status(collect_status(cfg, base)))
        return 0

    try:
        if port_in_use(cfg["bind"], cfg["port"]):
            log("BUSY", "端口 %s:%s 已被占用——多半已有一个接收端在跑，"
                        "本次不启动（避免两个实例抢同一端口、日志分叉）"
                % (cfg["bind"], cfg["port"]))
            print("端口已被占用，退出。停止旧实例：taskkill /PID <pid> /F")
            return 3
        monitor = IdleMonitor(cfg, base, log, None, _abs(base, cfg["root"]))
        server, root, worker, proj = build_server(cfg, base, log, monitor=monitor)
        monitor.worker = worker
    except OSError as e:
        print("启动失败：%s" % e)
        print("端口 %s 可能被占用，或需要管理员权限（21 是特权端口）。" % cfg["port"])
        return 2

    log("START", "监听 %s:%s，收件箱 %s，用户 %s，被动端口 %s-%s" % (
        cfg["bind"], cfg["port"], root, cfg["user"],
        cfg["passive_port_start"], cfg["passive_port_end"]))

    finish_reason = []
    finish_evt = threading.Event()

    def request_finish(reason):
        if finish_evt.is_set():
            return
        finish_reason.append(reason)
        finish_evt.set()
        try:
            server.close_all()
        except Exception:                        # noqa: BLE001
            pass

    if monitor.enabled:
        monitor.start(lambda: request_finish("连续空闲满 %.0f 分钟" % (monitor.idle_limit / 60.0)))
    else:
        log("IDLE-WATCH", "未启用自动收工（idle_exit_minutes=0）——将一直运行到人工停止")

    if args.max_hours and float(args.max_hours) > 0:
        def _hard_stop():
            if not finish_evt.wait(float(args.max_hours) * 3600.0):
                request_finish("已达硬性最长运行时长 %.1f 小时" % float(args.max_hours))
        threading.Thread(target=_hard_stop, name="ftp-maxhours", daemon=True).start()
        log("MAX-HOURS", "已设定最长运行 %.1f 小时，到点无条件收工" % float(args.max_hours))

    def _on_signal(signum, _frame):
        request_finish("收到信号 %s" % signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError, AttributeError):
            pass

    rc = 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        request_finish("收到中断")
    finally:
        try:
            server.close_all()
        except Exception:                        # noqa: BLE001
            pass

    monitor.stop()
    reason = finish_reason[0] if finish_reason else "serve_forever 自行返回"
    elapsed = time.time() - monitor.started_at

    drained = wait_ingest_drain(worker, log,
                                cfg.get("drain_timeout_minutes", 60))
    snap = worker.snapshot() if worker is not None else {"done": 0, "failed": 0}
    log("DRAIN", "收工前入库状态：完成 %s，失败 %s，剩余队列 %s，%s"
        % (snap.get("done"), snap.get("failed"), snap.get("queued"),
           "已全部消化" if drained else "仍有未完成项（已如实记录，未强行打断）"))

    close_device = bool(cfg.get("close_device_on_exit", True))
    dev_result = "未处理"
    if not close_device:
        dev_result = "按配置跳过"
        log("DEVICE-OFF", "按配置未关闭设备端 FTP 上传")
    elif proj is None:
        dev_result = "跳过（未读到项目配置，无法定位设备）"
        log("DEVICE-OFF-FAIL", "未读到项目配置，无法关闭设备端 FTP 上传——"
                               "请手动登录录像机确认 NAS 开关状态")
    else:
        try:
            save_nas_snapshot(proj, base, log, "before_idle_exit")
            res = set_nas_switch(proj, False)
            if res["ok"]:
                dev_result = "已关闭并回读确认"
                log("DEVICE-OFF", "设备端『录像 FTP 上传』已关闭（回读 Enable=false）")
            else:
                dev_result = "关闭失败：%s" % (res.get("detail") or "未知")
                log("DEVICE-OFF-FAIL",
                    "设备端开关关闭未确认：%s（回读=%s）——请手动确认，"
                    "本机已停收，设备侧会继续重试"
                    % (res.get("detail") or "未知", res.get("readback")))
        except Exception as exc:                 # noqa: BLE001
            dev_result = "关闭异常：%r" % exc
            log("DEVICE-OFF-FAIL", "关闭设备端开关时异常：%r" % exc)

    hours = elapsed / 3600.0
    summary = (
        "本次运行 %.1f 小时（%s ~ %s）\n"
        "收工原因：%s\n"
        "入库：完成 %s 个，失败 %s 个\n"
        "设备端 FTP 上传：%s"
        % (hours,
           _dt.datetime.fromtimestamp(monitor.started_at).strftime("%Y-%m-%d %H:%M:%S"),
           _dt.datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S"),
           reason, snap.get("done"), snap.get("failed"), dev_result)
    )
    log("FINISH", summary.replace("\n", " | "))
    try:
        print(summary, flush=True)
    except Exception:                            # noqa: BLE001
        pass

    lock = os.path.join(_abs(base, "state"), "ftp_idle_exit.json")
    append_manifest(lock, {
        "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "elapsed_seconds": int(elapsed),
        "ingest_done": snap.get("done"),
        "ingest_failed": snap.get("failed"),
        "drained": drained,
        "device_closed": dev_result,
    })
    if not drained:
        rc = 4
    return rc


if __name__ == "__main__":
    sys.exit(main())
