@echo off
rem ============================================================
rem  Ji Xun  --  Build APK
rem
rem  This file is ASCII-only ON PURPOSE (see start.bat for why).
rem  All Chinese lives in scripts\build_apk.ps1 (UTF-8 with BOM).
rem
rem  Usage:
rem    double-click             -^> batch name "dev"
rem    build_apk.bat phase2     -^> batch name "phase2"
rem
rem  Output goes to dist\apk\ :
rem    jixun-^<batch^>-^<date^>.apk   and   app-release.apk
rem ============================================================

setlocal
cd /d "%~dp0"
title Ji Xun - Build APK

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_apk.ps1" -Batch "%~1"

if errorlevel 1 (
  echo.
  echo Build failed. See the error message above.
  pause
)

endlocal