@echo off
REM ---------------------------------------------------------------
REM  Daily archive job: pull YESTERDAY's footage, then exit.
REM
REM  Schedule this once a day (e.g. 09:30) with Task Scheduler:
REM    schtasks /Create /TN "NVR Daily Archive" /SC DAILY /ST 09:30 ^
REM      /TR "<full path>\run_daily.bat" /RL HIGHEST /F
REM
REM  Safe to re-run any time: it resumes from where it stopped and
REM  exits immediately for days that are already archived.
REM
REM  If it misses a day (machine off / task failed), the next run
REM  automatically catches up every missing day, up to
REM  runtime.catchup_max_days days back.
REM
REM  Exit codes: 0 done | 1 failed (re-run resumes) | 3 target day not
REM  recorded yet | 4 another instance is already running (skipped).
REM
REM  Requires python on PATH (3.8+). ffmpeg is bundled in bin\.
REM ---------------------------------------------------------------
cd /d "%~dp0"
if not exist logs mkdir logs

python nvr_puller.py --day yesterday
set RC=%ERRORLEVEL%

if "%RC%"=="0" goto done
if "%RC%"=="3" goto notready
if "%RC%"=="4" goto busy
echo [%DATE% %TIME%] daily archive FAILED rc=%RC% >> logs\daily_errors.log
goto done

:busy
echo [%DATE% %TIME%] another archive instance is still running (rc=4), skipped >> logs\daily_errors.log
goto done

:notready
echo [%DATE% %TIME%] target day has no footage yet (rc=3), will retry next run >> logs\daily_errors.log

:done
exit /b %RC%
