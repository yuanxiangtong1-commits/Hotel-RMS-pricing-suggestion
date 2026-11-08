@echo off
rem ================================================
rem  Double-click: run one pricing cycle immediately
rem ================================================
cd /d "%~dp0"
rem Requires Python 3 (pandas, numpy, requests) available on PATH.
where python >nul 2>nul
if %errorlevel%==0 (python "hotel rms.py") else (py "hotel rms.py")
pause
