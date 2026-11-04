@echo off
rem Run by Windows Setup (autounattend.xml, windowsPE pass) from winvm's payload CD:
rem   start.cmd <payload-drive>
rem Runs pe.cmd in a child cmd, so even a script error ends up here. pe.cmd reboots
rem (or, in capture mode, reboots to report success); on failure: power off, or with
rem WINSLIM_DEBUG=1 a visible shell (Setup runs this script hidden).
set "MEDIA=%~1"
wpeutil DisableFirewall >nul 2>&1
call "%MEDIA%\winslim\config.cmd"
cmd.exe /c "%MEDIA%\winslim\pe.cmd" %MEDIA%
if not errorlevel 1 (
  rem pe.cmd succeeded and requested a reboot
  ping -n 60 127.0.0.1 >nul
  exit /b 0
)
if "%WINSLIM_DEBUG%"=="1" (
  start "winslim failed - log: X:\winslim-pe.log" cmd.exe /k type X:\winslim-pe.log
  ping -n 86400 127.0.0.1 >nul
)
wpeutil shutdown
