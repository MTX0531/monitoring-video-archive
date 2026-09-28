@echo off
REM ---------------------------------------------------------------
REM  NVR FTP receive session: open the device-side uploader, receive
REM  until idle, then finish and close the device-side uploader again.
REM
REM  This is the "ignition" half. The receiver itself decides when the
REM  work is done (see ftp.idle_exit_minutes in config.json) and closes
REM  the device-side "Record FTP upload" switch on its way out.
REM
REM  Schedule this once (e.g. 08:00) with Task Scheduler if you want it
REM  fully hands-off:
REM    schtasks /Create /TN "NVR FTP Session" /SC DAILY /ST 08:00 ^
REM      /TR "<full path>\run_ftp.bat" /RL HIGHEST /F
REM
REM  Deleting it is always safe: the NVR keeps a one-way cursor and
REM  resumes from the exact byte it stopped at, so footage is never
REM  lost by starting late or forgetting a day.
REM
REM  Exit codes: 0 finished cleanly | 2 start failed | 3 port busy |
REM              4 finished but ingest queue never drained (see log)
REM
REM  Requires python on PATH (3.8+). ffmpeg is bundled in bin\.
REM ---------------------------------------------------------------
cd /d "%~dp0"
if not exist logs mkdir logs

set LOG=logs\ftp_session.log
echo. >> "%LOG%"
echo --------------------------------------------------------------- >> "%LOG%"
echo [%DATE% %TIME%] FTP session starting >> "%LOG%"

REM --- 1) Turn the device-side uploader ON (otherwise it never pushes) ---
python tools\ftp_device_switch.py --on
set SWRC=%ERRORLEVEL%
if not "%SWRC%"=="0" (
  echo [%DATE% %TIME%] WARNING: could not confirm device upload is ON ^(rc=%SWRC%^) >> "%LOG%"
  REM 不中止：设备可能只是暂时不可达，接收端仍应起来等着；期间设备会自己重试
)

REM --- 2) Run the receiver. It exits on its own once work is done,
REM        and closes the device-side uploader on the way out.
python tools\ftp_receiver.py
set RC=%ERRORLEVEL%

if "%RC%"=="0" (
  echo [%DATE% %TIME%] FTP session finished cleanly >> "%LOG%"
  goto done
)
if "%RC%"=="3" (
  echo [%DATE% %TIME%] another receiver is already listening, skipped >> "%LOG%"
  goto done
)
if "%RC%"=="4" (
  echo [%DATE% %TIME%] finished but ingest queue never drained; check ftp_receiver.log >> "%LOG%"
  goto done
)
echo [%DATE% %TIME%] FTP session FAILED rc=%RC% >> "%LOG%"

:done
exit /b %RC%
