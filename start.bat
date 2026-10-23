@echo off
rem DC2 Upscale Manager - double-click to start; the page opens in your browser.
rem Close this window (or press Ctrl+C) to stop the app.
cd /d "%~dp0"
python app.py %*
if errorlevel 1 pause
