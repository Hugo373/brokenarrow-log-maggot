@echo off
setlocal
cd /d "%~dp0"
start "Broken Arrow Log Tool" wscript.exe "%CD%\start.vbs"
endlocal
