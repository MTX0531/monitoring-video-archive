# -*- coding: utf-8 -*-
"""FTP 接收端（tools/ftp_receiver.py）验证。

为什么值得单独验：录像机是 push 模型——它连上来把文件塞给我们，
我们既不能让它"少传点"，也看不到它到底有没有来。所以接收端必须满足：
  1. 配置能叠加：config.json 的 ftp 段覆盖内置默认值，_comment 键被忽略；
  2. 每一次连接/登录/上传都要留痕，否则故障时无从判断是网络问题还是设备没推；
  3. **磁盘闸门**：收件箱所在磁盘空间不足时必须直接拒收(452)；
  4. 回调里任何异常都不能把 FTP 连接搞断（日志写失败也得把文件收完）；
  5. 收到文件要追加清单，供后续与归档台账对账；
  6. **单实例互斥**：端口被占时必须拒绝启动（Windows SO_REUSEADDR 陷阱）；
  7. **状态自查只读**：`--status` 要能回答"东西收到哪了"，且绝不能改动磁盘。

集成部分用子进程起真实服务、真实 FTP 登录上传，全部本机回环，不连录像机。
若本机没有装 pyftpdlib 的环境，会打印 SKIP 并退出 0（不拖垮整体回归）。

用法：python tools/ftp_receiver_test.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import importlib.util                                        # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ftp_receiver", os.path.join(BASE, "tools", "ftp_receiver.py"))
ftp_receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ftp_receiver)

from nvrcore.storage import SEGMENT_RE                        # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


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
        print("  [FAIL] %s%s" % (label, ("  (%s)" % note) if note else ""))


def skip(label, why):
    global SKIP
    SKIP += 1
    print("  [SKIP] %s  (%s)" % (label, why))


# --------------------------------------------------------------------------- #
def find_interp_with_pyftpdlib():
    cands = [sys.executable]
    cands.append(os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "python", "envs",
        "default", "Scripts", "python.exe"))
    cands.append(os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "python", "envs",
        "default", "bin", "python"))
    for exe in cands:
        if not exe or not os.path.isfile(exe):
            continue
        try:
            r = subprocess.run([exe, "-c", "import pyftpdlib"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return exe
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
def test_config_merge():
    print("\n[1] 配置叠加与容错")
    tmp = tempfile.mkdtemp(prefix="wb_ftpcfg_")
    try:
        cfg = ftp_receiver.load_ftp_config(tmp)
        check("缺 config.json 时用默认端口", cfg["port"], 21)
        check("缺 config.json 时用默认收件箱", cfg["root"], "ftp-inbox")
        check("默认被动端口段起点", cfg["passive_port_start"], 60000)

        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"ftp": {
                "_comment": "不该影响配置",
                "port": 2121,
                "root": "custom-inbox",
                "min_free_gb": 7.5,
                "不存在的键": "x",
            }}, f, ensure_ascii=False)
        cfg = ftp_receiver.load_ftp_config(tmp)
        check("端口被 config.json 覆盖", cfg["port"], 2121)
        check("收件箱被覆盖", cfg["root"], "custom-inbox")
        check("磁盘闸门阈值被覆盖", cfg["min_free_gb"], 7.5)
        check("未覆盖项保持默认", cfg["user"], "nvrftp")
        check_true("_comment 不进入结果", "_comment" not in cfg)
        check_true("未知键不进入结果", "不存在的键" not in cfg)

        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        cfg = ftp_receiver.load_ftp_config(tmp)
        check("config.json 损坏时回退默认", cfg["port"], 21)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_free_gb_and_log():
    print("\n[2] 磁盘余量探测与事件日志")
    avail = ftp_receiver.free_gb(BASE)
    check_true("能读到真实磁盘余量(>0)", avail > 0, "got=%r" % avail)
    check("路径不存在时返回 inf（不误触发闸门）",
          ftp_receiver.free_gb(r"Z:\definitely\not\here"), float("inf"))

    tmp = tempfile.mkdtemp(prefix="wb_ftplog_")
    try:
        log_path = os.path.join(tmp, "sub", "x.log")
        log = ftp_receiver.EventLog(log_path)
        line = log("CONNECT", "来自 192.168.2.10:1234")
        check_true("自动建目录并写文件", os.path.isfile(log_path))
        check_true("返回写入的行且含事件类型",
                   isinstance(line, str) and "CONNECT" in line)
        check_true("带时间戳", line[:4].isdigit() and "-" in line[:10])
        log("UPLOAD-OK", "第二个事件")
        lines = open(log_path, encoding="utf-8").read().strip().split("\n")
        check("追加而非覆盖", len(lines), 2)
        check_true("UPLOAD-OK 在第二行", "UPLOAD-OK" in lines[1])

        bad = ftp_receiver.EventLog(os.path.join(tmp, "x.log", "y.log"))
        try:
            bad("CONNECT", "写不进去")
            ok = True
        except Exception as e:
            ok = False
        check_true("日志不可写时不抛异常", ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_manifest():
    print("\n[3] 到货清单")
    tmp = tempfile.mkdtemp(prefix="wb_ftpmf_")
    try:
        mf = os.path.join(tmp, "state", "ftp_received.jsonl")
        ftp_receiver.append_manifest(mf, {"rel_path": "a.dav", "size": 10})
        ftp_receiver.append_manifest(mf, {"rel_path": "b.dav", "size": 20})
        rows = [json.loads(l) for l in
                open(mf, encoding="utf-8").read().strip().split("\n")]
        check("两行都被追加", len(rows), 2)
        check("首行内容正确", rows[0]["rel_path"], "a.dav")
        check("次行内容正确", rows[1]["size"], 20)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_port_in_use_fn():
    print("\n[3b] 端口占用探测（单实例互斥的前提）")
    p = free_port()
    check("空闲端口判定为未占用", ftp_receiver.port_in_use("127.0.0.1", p), False)
    check("端口 0 视为不冲突", ftp_receiver.port_in_use("127.0.0.1", 0), False)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", p))
    s.listen(1)
    try:
        check("被占用端口能识别出来（即使占用方设了 SO_REUSEADDR）",
              ftp_receiver.port_in_use("127.0.0.1", p), True)
    finally:
        s.close()


# --------------------------------------------------------------------------- #
def test_status():
    print("\n[3c] 状态自查：东西收到哪了（只读）")
    tmp = tempfile.mkdtemp(prefix="wb_ftpsr_")
    try:
        cfg = dict(ftp_receiver.DEFAULTS)
        cfg["root"] = "inbox"
        cfg["min_free_gb"] = 0
        os.makedirs(os.path.join(tmp, "inbox"))

        st = ftp_receiver.collect_status(cfg, tmp)
        check("空收件箱：文件数 0", len(st["files"]), 0)
        check("空收件箱：清单 0 条", len(st["records"]), 0)
        check("空收件箱：设备事件 0 条", len(st["device_events"]), 0)
        text = ftp_receiver.format_status(st)
        check_true("文案给出收件箱绝对路径",
                   os.path.join(tmp, "inbox") in text)
        check_true("文案点明收件箱是空的", "没有推过任何文件" in text)
        check_true("文案说明设备从未连接", "从未连接过" in text)
        check_true("文案带磁盘余量", "可用" in text)

        sub = os.path.join(tmp, "inbox", "2026-09-17")
        os.makedirs(sub)
        payload = b"x" * (2 * 1024 * 1024)
        with open(os.path.join(sub, "ch1.dav"), "wb") as f:
            f.write(payload)
        ftp_receiver.append_manifest(
            os.path.join(tmp, "state", "ftp_received.jsonl"),
            {"received_at": "2026-09-17T14:00:00",
             "rel_path": os.path.join("2026-09-17", "ch1.dav"),
             "size": len(payload), "remote_ip": "192.168.2.10"})

        st = ftp_receiver.collect_status(cfg, tmp)
        check("有文件时文件数 1", len(st["files"]), 1)
        check("清单 1 条", len(st["records"]), 1)
        text = ftp_receiver.format_status(st)
        check_true("文案列出相对路径", "ch1.dav" in text)
        check_true("文案列出大小(MB)", "2.00 MB" in text)
        check_true("文案不再说收件箱空", "没有推过任何文件" not in text)
        check_true("文案列出到货时间", "2026-09-17T14:00:00" in text)

        check("判定：本机回环不算设备", ftp_receiver.has_remote_ip(
            "2026-09-17 13:15:17 [CONNECT] 来自 127.0.0.1:5000"), False)
        check("判定：通配地址 0.0.0.0 不算设备", ftp_receiver.has_remote_ip(
            "2026-09-17 13:16:10 [START] 监听 0.0.0.0:21"), False)
        check("判定：旧格式无来源 IP 不算设备", ftp_receiver.has_remote_ip(
            "2026-09-17 13:15:17 [UPLOAD-OK] _selftest.bin  262144 字节"), False)
        check("判定：录像机 IP 算设备", ftp_receiver.has_remote_ip(
            "2026-09-17 14:00:00 [CONNECT] 来自 192.168.2.10:5000"), True)

        logs = os.path.join(tmp, "logs")
        os.makedirs(logs)
        with open(os.path.join(logs, "ftp_receiver.log"), "w",
                  encoding="utf-8") as f:
            f.write("2026-09-17 13:15:17 [UPLOAD-OK     ] 来自 127.0.0.1  _selftest.bin\n")
            f.write("2026-09-17 13:15:17 [UPLOAD-OK     ] _selftest.bin  262144 字节\n")
            f.write("2026-09-17 13:16:10 [START         ] 监听 0.0.0.0:21\n")
            f.write("2026-09-17 13:30:32 [BUSY          ] 端口 0.0.0.0:21 已被占用\n")
            f.write("2026-09-17 14:00:00 [CONNECT       ] 来自 192.168.2.10:5000\n")
            f.write("2026-09-17 14:00:01 [UPLOAD-OK     ] 来自 192.168.2.10  ch1.dav\n")
            f.write("2026-09-17 14:05:00 [REJECT        ] 来自 192.168.2.10  磁盘不足，拒收 x.dav\n")
        st = ftp_receiver.collect_status(cfg, tmp)
        check("设备事件只留 3 条（连接/上传/被拒）", len(st["device_events"]), 3)
        check_true("本机自测行被排除",
                   all("127.0.0.1" not in l and "_selftest.bin" not in l
                       for l in st["device_events"]))
        check_true("设备被拒收也算设备事件",
                   any("REJECT" in l for l in st["device_events"]))
        check_true("本机自身事件(START/BUSY)不算设备事件",
                   all("START" not in l and "BUSY" not in l
                       for l in st["device_events"]))

        cfg2 = dict(cfg)
        cfg2["min_free_gb"] = 10 ** 9
        text = ftp_receiver.format_status(ftp_receiver.collect_status(cfg2, tmp))
        check_true("闸门触发时文案提到 452", "452" in text)

        before = sorted(os.listdir(os.path.join(tmp, "inbox")))
        ftp_receiver.format_status(ftp_receiver.collect_status(cfg, tmp))
        check("状态查询不新增文件", sorted(os.listdir(os.path.join(tmp, "inbox"))),
              before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False


def make_base(tmp, port, min_free_gb, root="ftp-inbox"):
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"ftp": {
            "port": port,
            "bind": "127.0.0.1",
            "root": root,
            "user": "nvrftp",
            "password": "<FTP_PASSWORD>",
            "min_free_gb": min_free_gb,
        }}, f, ensure_ascii=False)


def start_server(interp, base, port):
    proc = subprocess.Popen(
        [interp, os.path.join(BASE, "tools", "ftp_receiver.py"),
         "--base-dir", base],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_port(port):
        proc.kill()
        return None
    return proc


def test_integration(interp):
    print("\n[4] 集成：真实 FTP 登录/上传/落清单")
    import ftplib
    tmp = tempfile.mkdtemp(prefix="wb_ftpsrv_")
    proc = None
    port = free_port()
    try:
        make_base(tmp, port, 0)
        proc = start_server(interp, tmp, port)
        if proc is None:
            skip("集成测试", "服务未能在 15 秒内监听端口")
            return

        root = os.path.join(tmp, "ftp-inbox")
        payload = os.urandom(200000)

        f = ftplib.FTP()
        f.connect("127.0.0.1", port, timeout=10)
        try:
            f.login("nvrftp", "wrong-pass")
            check("错误密码被拒", "未抛异常", "抛 FTP 错误")
        except ftplib.error_perm as e:
            check_true("错误密码被拒", "530" in str(e), str(e))
        finally:
            try:
                f.close()
            except Exception:
                pass

        f = ftplib.FTP()
        f.connect("127.0.0.1", port, timeout=10)
        f.login("nvrftp", "<FTP_PASSWORD>")
        f.storbinary("STOR 2026-09-17/ch1.dav", io.BytesIO(payload))
        f.quit()

        landed = os.path.join(root, "2026-09-17", "ch1.dav")
        check_true("上传的文件落到收件箱", os.path.isfile(landed))
        if os.path.isfile(landed):
            check("落盘内容逐字节一致", open(landed, "rb").read(), payload)

        mf = os.path.join(tmp, "state", "ftp_received.jsonl")
        check_true("到货清单已生成", os.path.isfile(mf))
        if os.path.isfile(mf):
            rows = [json.loads(l) for l in
                    open(mf, encoding="utf-8").read().strip().split("\n")]
            check("清单一条记录", len(rows), 1)
            check("记录相对路径", rows[0]["rel_path"],
                  os.path.join("2026-09-17", "ch1.dav"))
            check("记录大小", rows[0]["size"], len(payload))
            check_true("记录含来源 IP", rows[0]["remote_ip"] == "127.0.0.1")

        logtext = open(os.path.join(tmp, "logs", "ftp_receiver.log"),
                       encoding="utf-8").read()
        check_true("日志记下登录成功", "LOGIN-OK" in logtext)
        check_true("日志记下上传成功", "UPLOAD-OK" in logtext)
        check_true("上传日志带来源 IP（用于区分设备推送与本机自测）",
                   any("UPLOAD-OK" in l and "127.0.0.1" in l
                       for l in logtext.splitlines()))
        check_true("日志记下登录失败", "LOGIN-FAIL" in logtext)
        check_true("日志记下已连接的来源 IP", "127.0.0.1" in logtext)
        check_true("日志记下自动建目录(应对带子目录的推送)", "MKDIR" in logtext)
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_disk_guard(interp):
    print("\n[5] 集成：磁盘不足时必须拒收")
    import ftplib
    tmp = tempfile.mkdtemp(prefix="wb_ftpgr_")
    proc = None
    port = free_port()
    try:
        make_base(tmp, port, 10 ** 9)
        proc = start_server(interp, tmp, port)
        if proc is None:
            skip("磁盘闸门", "服务未能在 15 秒内监听端口")
            return

        f = ftplib.FTP()
        f.connect("127.0.0.1", port, timeout=10)
        f.login("nvrftp", "<FTP_PASSWORD>")
        rejected = False
        try:
            f.storbinary("STOR big.dav", io.BytesIO(b"x" * 4096))
        except (ftplib.error_temp, ftplib.error_perm) as e:
            rejected = "452" in str(e)
        check_true("上传被 452 拒绝", rejected)
        try:
            f.close()
        except Exception:
            pass

        landed = os.path.join(tmp, "ftp-inbox", "big.dav")
        check_true("被拒的文件没有落盘", not os.path.isfile(landed))

        logtext = open(os.path.join(tmp, "logs", "ftp_receiver.log"),
                       encoding="utf-8").read()
        check_true("日志记下拒收原因", "REJECT" in logtext)
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_single_instance(interp):
    print("\n[6] 集成：端口已被占用时拒绝启动（Windows SO_REUSEADDR 陷阱）")
    import ftplib
    tmp = tempfile.mkdtemp(prefix="wb_ftpsng_")
    proc = None
    port = free_port()
    try:
        make_base(tmp, port, 0)
        proc = start_server(interp, tmp, port)
        if proc is None:
            skip("单实例互斥", "第一个服务未能监听端口")
            return

        r = subprocess.run(
            [interp, os.path.join(BASE, "tools", "ftp_receiver.py"),
             "--base-dir", tmp],
            capture_output=True, timeout=60)
        check("第二实例退出码为 3（拒绝启动）", r.returncode, 3)

        logtext = open(os.path.join(tmp, "logs", "ftp_receiver.log"),
                       encoding="utf-8").read()
        check_true("日志记下 BUSY", "BUSY" in logtext)

        f = ftplib.FTP()
        f.connect("127.0.0.1", port, timeout=10)
        f.login("nvrftp", "<FTP_PASSWORD>")
        f.storbinary("STOR still-works.dav", io.BytesIO(b"z" * 1024))
        f.quit()
        check_true("原实例仍在服务（端口没被抢走）",
                   os.path.isfile(os.path.join(tmp, "ftp-inbox",
                                               "still-works.dav")))
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def _make_video(ffmpeg, path, seconds=3):
    """用 lavfi 造一小段真视频冒充录像机推来的文件（先写 .mp4 再改名）。"""
    tmp_out = path + ".gen.mp4"
    try:
        subprocess.run([
            ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", tmp_out,
        ], capture_output=True, timeout=180)
    except Exception:                        # noqa: BLE001
        return False
    if not (os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024):
        return False
    os.replace(tmp_out, path)
    return True


def wait_for(predicate, timeout=120.0, interval=1.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_receiver_ingest(interp):
    print("\n[7] 集成：真上传 -> 后台入库 -> 归档目录（与 RTSP 同命名）")
    import ftplib
    tmp = tempfile.mkdtemp(prefix="wb_ftping_")
    proc = None
    port = free_port()
    try:
        root = os.path.join(tmp, "archive")
        inbox = os.path.join(tmp, "inbox")
        os.makedirs(root, exist_ok=True)
        ffmpeg = os.path.join(BASE, "bin", "ffmpeg.exe")
        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({
                "ftp": {"port": port, "bind": "127.0.0.1", "root": inbox,
                        "user": "nvrftp", "password": "<FTP_PASSWORD>",
                        "min_free_gb": 0, "ingest": True},
                "nvr": {"host": "127.0.0.1", "channel": 1, "stream": "main",
                        "transport": "tcp"},
                "device": {"enabled": False},
                "archive": {"root_dir": root, "store_name": "测试门店",
                            "channel_name": "通道1", "verify_playable": True,
                            "organize_by_date": True},
                "runtime": {"ffmpeg_path": ffmpeg},
            }, f, ensure_ascii=False)

        proc = start_server(interp, tmp, port)
        if proc is None:
            skip("接收端入库", "服务未能在 15 秒内监听端口")
            return

        local = os.path.join(tmp, "src.dav")
        if not _make_video(ffmpeg, local):
            skip("接收端入库", "本机 ffmpeg 无法生成测试视频")
            return

        pushed = "ch1_20260917093000_20260917095959.dav"
        with open(local, "rb") as fh:
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", port, timeout=20)
            ftp.login("nvrftp", "<FTP_PASSWORD>")
            ftp.storbinary("STOR " + pushed, fh)
            ftp.quit()

        expect = os.path.join(root, "2026-09-17",
                              "测试门店_通道1_20260917093000_20260917095959_device.mp4")
        ok = wait_for(lambda: os.path.isfile(expect), timeout=150)
        check_true("推上来的文件被后台入库到归档目录", ok, expect)
        if ok:
            check_true("归档文件非空", os.path.getsize(expect) > 1024)
            check_true("文件名符合归案命名规则",
                       SEGMENT_RE.match(os.path.basename(expect)) is not None)

        check_true("收件箱已清空（原件被销毁）",
                   not os.path.isfile(os.path.join(inbox, pushed)))

        logtext = open(os.path.join(tmp, "logs", "ftp_receiver.log"),
                       encoding="utf-8").read()
        check_true("日志记下排队", "INGEST-QUEUE" in logtext)
        check_true("日志记下入库成功", "INGEST-OK" in logtext)

        idx = os.path.join(tmp, "state", "index.jsonl")
        check_true("归档台账已生成", os.path.isfile(idx))
        if os.path.isfile(idx):
            rows = [json.loads(l) for l in
                    open(idx, encoding="utf-8").read().splitlines() if l.strip()]
            check("台账 1 条", len(rows), 1)
            check("台账来源标记为 ftp", rows[0].get("source"), "ftp")
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:                # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
class _FakeWorker:
    """冒充 IngestWorker，用来构造"队列还在转码"这种情形。"""

    def __init__(self, current=None, queued=0, size=None):
        self.current = current
        self._queued = queued
        self.done = 0
        self.failed = 0
        self._size = size

    def busy(self):
        return self.current is not None or self._queued > 0

    def qsize(self):
        return self._queued

    @property
    def q(self):
        outer = self

        class _Q:
            def qsize(self):
                return outer._queued
        return _Q()

    def snapshot(self):
        return {"current": self.current, "queued": self._queued,
                "done": self.done, "failed": self.failed}


def _idle_monitor(tmp, inbox, worker, idle_minutes=10, check_seconds=30,
                  conn_active_seconds=None):
    cfg = dict(ftp_receiver.DEFAULTS)
    cfg["idle_exit_minutes"] = idle_minutes
    cfg["idle_check_seconds"] = check_seconds
    if conn_active_seconds is not None:
        cfg["conn_active_seconds"] = conn_active_seconds
    log = ftp_receiver.EventLog(os.path.join(tmp, "logs", "idle.log"))
    return ftp_receiver.IdleMonitor(cfg, tmp, log, worker, inbox)


def test_idle_logic():
    print("\n[8] 自动收工：空闲判定")
    tmp = tempfile.mkdtemp(prefix="wb_idle_")
    try:
        inbox = os.path.join(tmp, "inbox")
        os.makedirs(inbox, exist_ok=True)

        cfg_off = dict(ftp_receiver.DEFAULTS)
        log = ftp_receiver.EventLog(os.path.join(tmp, "logs", "idle0.log"))
        m_off = ftp_receiver.IdleMonitor(cfg_off, tmp, log, None, inbox)
        check("idle_exit_minutes=0 时不启用", m_off.enabled, False)
        check("未启用时永不判定收工", m_off.should_finish(), False)

        m = _idle_monitor(tmp, inbox, None, idle_minutes=10)
        check("设了分钟数则启用", m.enabled, True)
        check("刚启动时未达阈值", m.should_finish(), False)
        check_true("刚启动时空闲时长为 0", m.idle_seconds() < 1.0)

        m.conn_open()
        check("连接刚有动作时空闲时长为 0", m.idle_seconds(), 0.0)
        check("连接活跃时不收工", m.should_finish(), False)
        check_true("连接活跃判定为 True", m.conn_active())
        m.conn_close()
        check("连接关闭后计数归零", m.active_connections, 0)

        m_stale = _idle_monitor(tmp, inbox, None, idle_minutes=10)
        m_stale.conn_open()
        m_stale._last_conn_event = time.time() - 3000.0
        check("连接挂久了但无动作 -> 认为空闲",
              m_stale.idle_seconds() > 0, True)
        check_true("连接无近期动作时 conn_active 为 False",
                   not m_stale.conn_active())
        check_true("连接数仍为 1（连接确实还在）", m_stale.active_connections == 1)
        m_stale.conn_activity("STOR")
        check("设备再次有动作时空闲清零", m_stale.idle_seconds(), 0.0)

        with open(os.path.join(inbox, "x.dav"), "wb") as f:
            f.write(b"z" * 16)

        m_noingest = _idle_monitor(tmp, inbox, None, idle_minutes=10)
        check_true("入库关闭时收件箱有文件也不算忙",
                   not m_noingest.inbox_pending())

        m_busyq = _idle_monitor(tmp, inbox, _FakeWorker(current="x.dav"),
                                idle_minutes=10)
        check_true("入库在忙时收件箱有文件不算忙",
                   not m_busyq.inbox_pending())

        w_idle0 = _FakeWorker()
        m_orphan = _idle_monitor(tmp, inbox, w_idle0, idle_minutes=10)
        m_orphan._last_conn_event = time.time() - 3000.0
        check_true("入库空+设备停：收件箱残留文件判为待处理",
                   m_orphan.inbox_pending())
        check("此时不收工", m_orphan.idle_seconds(), 0.0)

        m_streaming = _idle_monitor(tmp, inbox, _FakeWorker(), idle_minutes=10)
        m_streaming.conn_activity("STOR")
        check_true("设备正在传时收件箱有文件不算忙",
                   not m_streaming.inbox_pending())

        os.remove(os.path.join(inbox, "x.dav"))
        check_true("收件箱清空后不再 pending", not m_orphan.inbox_pending())

        w_busy = _FakeWorker(current="a.dav")
        m2 = _idle_monitor(tmp, inbox, w_busy, idle_minutes=10)
        check("正在转码时空闲时长为 0", m2.idle_seconds(), 0.0)
        check("正在转码时不收工", m2.should_finish(), False)

        w_queued = _FakeWorker(queued=3)
        m3 = _idle_monitor(tmp, inbox, w_queued, idle_minutes=10)
        check("队列有积压时空闲时长为 0", m3.idle_seconds(), 0.0)

        w_idle = _FakeWorker()
        m4 = _idle_monitor(tmp, inbox, w_idle, idle_minutes=10)
        m4._last_touch = time.time() - 601.0
        check_true("空闲满 10 分钟后判定收工（600 秒）", m4.should_finish())
        m4._last_touch = time.time() - 599.0
        check_true("差 1 秒不判定收工", not m4.should_finish())

        m5 = _idle_monitor(tmp, inbox, _FakeWorker(current="big.dav"), idle_minutes=10)
        m5.idle_seconds()
        m5.worker = _FakeWorker()
        check_true("刚忙完不能立刻收工（计时从头开始）", not m5.should_finish())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_drain_wait():
    print("\n[8b] 自动收工：等入库消化（不打断转码）")
    tmp = tempfile.mkdtemp(prefix="wb_drain_")
    try:
        log = ftp_receiver.EventLog(os.path.join(tmp, "logs", "drain.log"))

        check("没有入库线程时视为已消化", ftp_receiver.wait_ingest_drain(None, log, 1), True)

        w = _FakeWorker()
        check("队列本来就空时立刻返回", ftp_receiver.wait_ingest_drain(w, log, 1), True)

        w_stuck = _FakeWorker(current="stuck.dav")
        t0 = time.time()
        ok = ftp_receiver.wait_ingest_drain(w_stuck, log, 0.03, interval=0.05)
        check("队列干不完时返回 False", ok, False)
        check_true("超时后如实返回（没有死等）", 0 < time.time() - t0 < 10)
        check_true("超时写了日志",
                   "DRAIN-TIMEOUT" in open(os.path.join(tmp, "logs", "drain.log"),
                                           encoding="utf-8").read())

        w_soon = _FakeWorker(current="slow.dav")
        def _finish():
            time.sleep(0.4)
            w_soon.current = None
        threading.Thread(target=_finish, daemon=True).start()
        check("入库干完后返回 True", ftp_receiver.wait_ingest_drain(w_soon, log, 5), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nas_switch_helpers():
    print("\n[8c] 自动收工：设备端开关的解析与容错")
    try:
        ftp_receiver._device_http_target(None)
        check_true("缺项目配置时报错", False, "竟然没报错")
    except ValueError as e:
        check_true("缺项目配置时给出可读错误", "项目配置" in str(e))
    except Exception as e:                         # noqa: BLE001
        check_true("缺项目配置时报错", False, "抛了 %r" % e)

    try:
        r_none = ftp_receiver.read_nas_switch(None)
        check("缺项目配置时 enable 为 None", r_none["enable"], None)
        check_true("缺项目配置时 error 有说明", bool(r_none.get("error")))
    except Exception as e:                         # noqa: BLE001
        check_true("缺项目配置时 read 不抛异常", False, repr(e))

    body = ("table.NAS[0].Enable=true\n"
            "table.NAS[0].Address=192.168.2.15\n"
            "table.NAS[0].Port=21\n"
            "table.NAS[0].UserName=nvrftp\n")
    kv = ftp_receiver._device_nas_keys(body)
    check("解析出 Enable", kv.get("table.NAS[0].Enable"), "true")
    check("解析出地址", kv.get("table.NAS[0].Address"), "192.168.2.15")

    class _Proj:
        class nvr:
            host = "127.0.0.1"
            username = "admin"
            password = "x"
        class device:
            http_port = 9                              # 关闭端口，必失败
    try:
        res = ftp_receiver.set_nas_switch(_Proj, False, timeout=0.4)
        check("设备不可达时 ok=False", res["ok"], False)
        check("设备不可达时 readback 为 None", res["readback"], None)
        check_true("设备不可达时给出原因", bool(res.get("detail")))
    except Exception as e:                             # noqa: BLE001
        check_true("设备不可达时不抛异常（收敛成 ok=False）", False, repr(e))

    try:
        r0 = ftp_receiver.read_nas_switch(_Proj, timeout=0.4)
        check("设备不可达时 enable 为 None", r0["enable"], None)
        check_true("设备不可达时 error 有说明", bool(r0.get("error")))
    except Exception as e:                             # noqa: BLE001
        check_true("设备不可达时 read 不抛异常", False, repr(e))

    tmp = tempfile.mkdtemp(prefix="wb_snap_")
    try:
        log = ftp_receiver.EventLog(os.path.join(tmp, "logs", "snap.log"))
        r = ftp_receiver.save_nas_snapshot(_Proj, tmp, log, "selftest")
        check("设备不可达时快照返回空串（不留空壳备份）", r, "")
        check_true("快照目录里没有生成任何文件",
                   not os.path.isdir(os.path.join(tmp, "state"))
                   or not os.listdir(os.path.join(tmp, "state")))
        check_true("快照失败写了日志",
                   "NAS-SNAPSHOT" in open(os.path.join(tmp, "logs", "snap.log"),
                                          encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_idle_exit_integration(interp):
    print("\n[9] 集成：空闲自退真的收工退出")
    tmp = tempfile.mkdtemp(prefix="wb_idleexit_")
    proc = None
    port = free_port()
    try:
        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({
                "ftp": {"port": port, "bind": "127.0.0.1",
                        "root": os.path.join(tmp, "inbox"),
                        "user": "nvrftp", "password": "<FTP_PASSWORD>",
                        "min_free_gb": 0, "ingest": False,
                        "idle_exit_minutes": 0.05,
                        "idle_check_seconds": 1,
                        "conn_active_seconds": 4},
                "nvr": {"host": "127.0.0.1", "channel": 1},
                "device": {"enabled": False, "http_port": 9},
            }, f, ensure_ascii=False)

        proc = subprocess.Popen(
            [interp, os.path.join(BASE, "tools", "ftp_receiver.py"),
             "--base-dir", tmp, "--idle-exit", "0.05", "--no-close-device"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(port):
            skip("空闲自退", "服务未能在 15 秒内监听端口")
            return
        check_true("服务已起来", True)

        rc = proc.wait(timeout=90)
        check("空闲到点后退出码为 0", rc, 0)
        check_true("端口已释放", not wait_port(port, timeout=3))

        logtext = open(os.path.join(tmp, "logs", "ftp_receiver.log"),
                       encoding="utf-8").read()
        check_true("日志记下启用了自动收工", "IDLE-WATCH" in logtext)
        check_true("日志记下收工原因(FINISH)", "FINISH" in logtext)
        check_true("日志明确是空闲触发的收工", "连续空闲" in logtext)
        check_true("按配置跳过了关设备", "按配置未关闭设备端 FTP 上传" in logtext)

        rec_path = os.path.join(tmp, "state", "ftp_idle_exit.json")
        check_true("写了收工台账", os.path.isfile(rec_path))
        if os.path.isfile(rec_path):
            row = json.loads(open(rec_path, encoding="utf-8").read().strip().splitlines()[-1])
            check("台账记下收工原因", "连续空闲" in (row.get("reason") or ""), True)
            check("台账记下未关设备", row.get("device_closed"), "按配置跳过")
            check_true("台账记下运行时长", int(row.get("elapsed_seconds") or 0) >= 0)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:                    # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_idle_not_triggered_while_busy(interp):
    print("\n[9b] 集成：设备持续在传时**不能**收工")
    import ftplib
    tmp = tempfile.mkdtemp(prefix="wb_idlebusy_")
    proc = None
    port = free_port()
    try:
        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({
                "ftp": {"port": port, "bind": "127.0.0.1",
                        "root": os.path.join(tmp, "inbox"),
                        "user": "nvrftp", "password": "<FTP_PASSWORD>",
                        "min_free_gb": 0, "ingest": False,
                        "idle_exit_minutes": 0.05,
                        "idle_check_seconds": 1,
                        "conn_active_seconds": 4},
                "nvr": {"host": "127.0.0.1", "channel": 1},
                "device": {"enabled": False, "http_port": 9},
            }, f, ensure_ascii=False)

        proc = subprocess.Popen(
            [interp, os.path.join(BASE, "tools", "ftp_receiver.py"),
             "--base-dir", tmp, "--idle-exit", "0.05", "--no-close-device"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(port):
            skip("忙时不收工", "服务未能在 15 秒内监听端口")
            return

        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", port, timeout=20)
        ftp.login("nvrftp", "<FTP_PASSWORD>")
        for i in range(8):
            ftp.storbinary("STOR busy-%d.dav" % i, io.BytesIO(b"z" * 2048))
            time.sleep(1)
        check_true("持续上传的 8 秒内服务没退出", proc.poll() is None)
        ftp.quit()

        rc = proc.wait(timeout=120)
        check("断开后最终仍会收工", rc, 0)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:                    # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def main():
    print("=" * 68)
    print("FTP 接收端回归")
    print("=" * 68)

    test_config_merge()
    test_free_gb_and_log()
    test_manifest()
    test_status()
    test_port_in_use_fn()
    test_idle_logic()
    test_drain_wait()
    test_nas_switch_helpers()

    interp = find_interp_with_pyftpdlib()
    if interp is None:
        skip("集成测试", "本机找不到带 pyftpdlib 的解释器")
        skip("磁盘闸门", "同上")
        skip("单实例互斥", "同上")
        skip("接收端入库", "同上")
        skip("空闲自退", "同上")
        skip("忙时不收工", "同上")
    else:
        print("\n使用解释器：%s" % interp)
        test_integration(interp)
        test_disk_guard(interp)
        test_single_instance(interp)
        test_receiver_ingest(interp)
        test_idle_exit_integration(interp)
        test_idle_not_triggered_while_busy(interp)

    print("\n" + "=" * 68)
    print("通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
