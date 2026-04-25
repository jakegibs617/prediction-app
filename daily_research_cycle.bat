@echo off
REM Daily research cycle for the prediction app.
REM Called by Windows Task Scheduler. One-shot: runs the full pipeline once and exits.
REM stdout/stderr are appended to logs/daily_cycle.log.

cd /d "C:\Users\jakeg\OneDrive\Desktop\prediction-app"

set LOGFILE=logs\daily_cycle.log
if not exist logs mkdir logs

echo ================================================================ >> %LOGFILE%
echo [%date% %time%] starting daily research cycle >> %LOGFILE%
echo ================================================================ >> %LOGFILE%

".\.venv-win\Scripts\python.exe" run_full_pipeline.py >> %LOGFILE% 2>&1
set EXITCODE=%ERRORLEVEL%

echo [%date% %time%] cycle finished with exitcode=%EXITCODE% >> %LOGFILE%
exit /b %EXITCODE%
