# -*- coding: utf-8 -*-
"""临时：盯一会儿 FTP 推送节奏（用后即删）。

要回答的问题：设备补推了当天 00:00~14:14 之后，**还会不会继续推新录像**。
若不会，那"只留 FTP"就会丢掉之后的画面 —— 这是决策级问题，必须实测。
每 interval 秒采样一次：收件箱文件数、接收端日志末行、归档成品数。
"""
import os
import sys
import time
import datetime as dt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = r"<本机用户目录>\Desktop\测试门店A门店监控视频归档"
LOG = os.path.join(BASE, "logs", "ftp_receiver.log")
INBOX = os.path.join(BASE, "ftp-inbox")
DUR = 2400
INTERVAL = 120


def snap():
    n = sum(len(f) for _, _, f in os.walk(INBOX))
    prod = 0
    for dp, dn, fn in os.walk(ROOT):
        prod += len([f for f in fn if f.endswith(".mp4") and not f.startswith(".")])
    try:
        lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
        last = lines[-1][:120] if lines else "(空)"
    except OSError:
        last = "(读不到日志)"
    return n, prod, last


def main():
    print("开始盯盘 %d 分钟，每 %d 秒一采样（现在 %s）"
          % (DUR // 60, INTERVAL, dt.datetime.now().strftime("%H:%M:%S")), flush=True)
    t0 = time.time()
    prev_up = None
    while time.time() - t0 < DUR:
        n, prod, last = snap()
        ts = dt.datetime.now().strftime("%H:%M:%S")
        print("%s  收件箱 %3d 个 | 归档成品 %3d 个 | 日志末行: %s"
              % (ts, n, prod, last), flush=True)
        try:
            up = open(LOG, encoding="utf-8", errors="replace").read().count("[UPLOAD-OK")
        except OSError:
            up = 0
        if prev_up is not None and up > prev_up:
            print("   ^^ 新到 %d 个包" % (up - prev_up), flush=True)
        prev_up = up
        time.sleep(INTERVAL)
    n, prod, last = snap()
    print("结束：收件箱 %d 个，归档成品 %d 个，UPLOAD-OK 累计 %d 次" % (n, prod, prev_up))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
