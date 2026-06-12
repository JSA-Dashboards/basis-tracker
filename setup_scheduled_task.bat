@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM  Basis Tracker — Windows Task Scheduler setup
REM  Runs auto_import.py every weekday at 5:00 PM (local time)
REM  to pull in the latest ADM bid sheet emails automatically.
REM ============================================================

set TASK_NAME=BasisTrackerAutoImport
set SCRIPT_DIR=%~dp0
set SCRIPT=%SCRIPT_DIR%auto_import.py

REM Use py launcher if available, otherwise fall back to python
where py >nul 2>&1
if %errorlevel%==0 (
    set PYTHON=py
) else (
    set PYTHON=python
)

echo.
echo ============================================================
echo  Basis Tracker — Task Scheduler Setup
echo ============================================================
echo  Task name : %TASK_NAME%
echo  Script    : %SCRIPT%
echo  Runs      : Mon-Fri at 5:00 PM (local time)
echo  Python    : %PYTHON%
echo ============================================================
echo.

REM Create the task — runs daily at 3:45 PM (15:45), starts immediately if missed
schtasks /create /tn "%TASK_NAME%" ^
  /tr "\"%PYTHON%\" \"%SCRIPT%\"" ^
  /sc DAILY ^
  /st 15:45 ^
  /ru "%USERNAME%" ^
  /f

if %errorlevel%==0 (
    echo.
    echo [OK] Task "%TASK_NAME%" created successfully.
    echo.
    echo Useful commands:
    echo   Run now:     schtasks /run /tn "%TASK_NAME%"
    echo   View status: schtasks /query /tn "%TASK_NAME%" /fo LIST /v
    echo   Delete:      schtasks /delete /tn "%TASK_NAME%" /f
    echo.
    echo TIP: Run retroactive import once to pull all historical emails:
    echo   cd "%SCRIPT_DIR%"
    echo   python auto_import.py --all
) else (
    echo.
    echo [ERROR] Task creation failed.
    echo Try right-clicking this .bat file and selecting "Run as administrator".
)

echo.
pause
