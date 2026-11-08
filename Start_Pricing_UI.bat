@echo off
rem ================================================
rem  Double-click this file to open the pricing UI
rem ================================================
cd /d "%~dp0"
rem Requires Python 3 (pandas, numpy, requests, tkinter) available on PATH.
where pythonw >nul 2>nul
if %errorlevel%==0 (start "" pythonw rms_gui.py) else (start "" pyw rms_gui.py)
