@echo off
rem Runs as SYSTEM after OOBE, before first logon.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setupcomplete.ps1" >> C:\Windows\Setup\winslim-setupcomplete.log 2>&1
exit /b 0
