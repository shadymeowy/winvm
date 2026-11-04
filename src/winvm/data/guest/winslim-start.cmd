@echo off
rem Injected into boot.wim as WinPE's shell (via winpeshl.ini). Finds the winslim
rem media and runs pe.cmd in a child cmd, so even a script error ends up here:
rem reported on COM1 as WINSLIM-FAIL, then power off (or a shell in debug mode).
title winslim
wpeutil DisableFirewall >nul 2>&1
set "MEDIA="
for /l %%n in (1,1,30) do (
  if not defined MEDIA for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
    if not defined MEDIA if exist "%%D:\winslim\pe.cmd" set "MEDIA=%%D:"
  )
  if not defined MEDIA ping -n 2 127.0.0.1 >nul
)
if not defined MEDIA (
  echo winslim media not found.
  cmd.exe /k
  exit /b
)
call "%MEDIA%\winslim\config.cmd"
cmd.exe /c "%MEDIA%\winslim\pe.cmd" %MEDIA%
if not errorlevel 1 (
  rem pe.cmd succeeded and requested a reboot
  ping -n 60 127.0.0.1 >nul
  exit /b
)
mode COM1 BAUD=115200 PARITY=n DATA=8 STOP=1 >nul 2>&1
2>nul >COM1 echo [PE] WINSLIM-FAIL: pe.cmd exited with an error, see X:\winslim-pe.log
echo WINSLIM-FAIL: pe.cmd exited with an error. Log: X:\winslim-pe.log
if "%WINSLIM_DEBUG%"=="1" (
  cmd.exe /k
  exit /b
)
wpeutil shutdown
