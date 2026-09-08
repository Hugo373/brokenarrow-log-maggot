@echo off
setlocal
cd /d "%~dp0"
start "Broken Arrow Log Tool" wscript.exe "%CD%\启动BA工具.vbs"
endlocal
