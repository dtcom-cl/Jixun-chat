@echo off
rem ============================================================
rem  Ji Xun  --  Server launcher
rem
rem  This file is ASCII-only ON PURPOSE. A .bat containing Chinese
rem  text breaks cmd: GBK lead bytes can collide with ^ | ^& ^> ^< and
rem  split commands apart. All Chinese lives in
rem  scripts\start_server.ps1 (UTF-8 WITH BOM).
rem
rem  Keep this file pure ASCII. If you need to explain something to the
rem  user, put it in scripts\start_server.ps1, never here.
rem
rem  Double-click this file. It will:
rem    1. check Python
rem    2. create .venv and install deps on first run
rem    3. print this PC LAN IP   (type it into the phone App)
rem    4. start the server and show live logs
rem ============================================================

setlocal
cd /d "%~dp0"
title Ji Xun - Server

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure_utf8_bom.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_server.ps1"

rem Pause only on failure, so the Chinese reason printed by the ps1
rem script stays readable. A normal window close prints nothing here.
if errorlevel 1 pause

endlocal
