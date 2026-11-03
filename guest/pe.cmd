@echo off
rem ===========================================================================
rem  winslim WinPE installer (runs instead of setup.exe, as WinPE's shell).
rem    1. safety checks (QEMU only, no existing winslim install)
rem    2. load virtio storage drivers, partition disk 0 (GPT: ESP + MSR + C:)
rem    3. dism /Apply-Image /Compact  (CompactOS from the start)
rem    4. offline debloat: appx, capabilities, features, services, registry
rem    5. inject virtio drivers, stage unattend + first-boot scripts, bcdboot
rem  Usage: pe.cmd <media-drive>      (called by winslim-start.cmd)
rem ===========================================================================
setlocal EnableExtensions DisableDelayedExpansion
set "M=%~1"
set "WS=%M%\winslim"
set "LOG=X:\winslim-pe.log"
set "SERIAL="
mode COM1 BAUD=115200 PARITY=n DATA=8 STOP=1 >nul 2>&1 && set "SERIAL=1"
call "%WS%\config.cmd"

call :log "winslim WinPE stage start (media %M%, edition '%WIN_EDITION%')"

rem ------------------------------------------------------------------ safety
set "ISQEMU="
for /f "tokens=2,*" %%a in ('reg query "HKLM\HARDWARE\DESCRIPTION\System\BIOS" /v SystemManufacturer 2^>nul ^| find "REG_"') do (
  echo %%b| find /i "QEMU" >nul && set "ISQEMU=1"
)
if not defined ISQEMU if not exist "%WS%\force" (
  call :fail "SystemManufacturer is not QEMU - refusing to wipe disk 0 (create winslim\force to override)"
  goto :eof
)

rem ------------------------------------------------------------------ storage drivers
for %%I in ("%M%\drivers\vioscsi\vioscsi.inf" "%M%\drivers\viostor\viostor.inf") do (
  if exist "%%~I" drvload "%%~I" >>"%LOG%" 2>&1
)
ping -n 4 127.0.0.1 >nul

for %%D in (C D E F G H I J K L N O P Q R S T U V W Y Z) do (
  if exist "%%D:\Windows\winslim.installed" if not exist "%WS%\force" (
    call :fail "existing winslim install found on %%D: - refusing to wipe again"
    goto :eof
  )
)

rem ------------------------------------------------------------------ locate install image
set "IMG="
set "SWM="
for %%D in (C D E F G H I J K L N O P Q R S T U V W Y Z) do (
  if not defined IMG if exist "%%D:\sources\install.wim" set "IMG=%%D:\sources\install.wim"
  if not defined IMG if exist "%%D:\sources\install.esd" set "IMG=%%D:\sources\install.esd"
  if not defined IMG if exist "%%D:\sources\install.swm" (
    set "IMG=%%D:\sources\install.swm"
    set "SWM=/SWMFile:%%D:\sources\install*.swm"
  )
)
if not defined IMG (
  call :fail "no sources\install.wim/esd/swm found on any drive - attach the Windows ISO"
  goto :eof
)
call :log "install image: %IMG%"

set "IDX=%WIN_INDEX%"
set "CUR="
if not defined IDX (
  for /f "tokens=1,* delims=:" %%a in ('dism /English /Get-ImageInfo /ImageFile:"%IMG%" %SWM%') do call :idxline "%%a" "%%b"
)
if not defined IDX (
  call :fail "edition '%WIN_EDITION%' not found in %IMG%"
  dism /English /Get-ImageInfo /ImageFile:"%IMG%" %SWM% >>"%LOG%" 2>&1
  goto :eof
)
call :log "edition '%WIN_EDITION%' = index %IDX%"

rem ------------------------------------------------------------------ partition
rem capture mode (./winvm iso) adds an O: volume that receives the serviced image
> X:\winslim-dp.txt echo select disk 0
>>X:\winslim-dp.txt echo clean
>>X:\winslim-dp.txt echo convert gpt
>>X:\winslim-dp.txt echo create partition efi size=100
>>X:\winslim-dp.txt echo format quick fs=fat32 label=System
>>X:\winslim-dp.txt echo assign letter=S
>>X:\winslim-dp.txt echo create partition msr size=16
if "%WINSLIM_MODE%"=="capture" (
  >>X:\winslim-dp.txt echo create partition primary size=40960
) else (
  >>X:\winslim-dp.txt echo create partition primary
)
>>X:\winslim-dp.txt echo format quick fs=ntfs label=Windows
>>X:\winslim-dp.txt echo assign letter=W
if "%WINSLIM_MODE%"=="capture" (
  >>X:\winslim-dp.txt echo create partition primary
  >>X:\winslim-dp.txt echo format quick fs=ntfs label=Output
  >>X:\winslim-dp.txt echo assign letter=O
)
call :log "partitioning disk 0"
diskpart /s X:\winslim-dp.txt >>"%LOG%" 2>&1
if not exist W:\ (
  call :fail "diskpart failed (no W: volume)"
  goto :eof
)

rem ------------------------------------------------------------------ apply
mkdir W:\scratch >nul 2>&1
set "COMPACT="
if "%WIN_COMPACT%"=="1" set "COMPACT=/Compact"
call :log "applying image %COMPACT%"
dism /English /Apply-Image /ImageFile:"%IMG%" %SWM% /Index:%IDX% /ApplyDir:W:\ %COMPACT% /ScratchDir:W:\scratch >>"%LOG%" 2>&1
if errorlevel 1 (
  call :fail "dism /Apply-Image failed"
  goto :eof
)
call :log "image applied"

rem From here on use the applied image's own tools: WinPE's DISM is older than the
rem image and lacks the AppX provider, and setup's WinPE has no findstr.exe.
set "DI=W:\Windows\System32\Dism.exe /English /Image:W:\ /ScratchDir:W:\scratch"
set "FINDSTR=W:\Windows\System32\findstr.exe"
if not exist W:\Windows\System32\Dism.exe set "DI=dism /English /Image:W:\ /ScratchDir:W:\scratch"

rem An image captured by ./winvm iso is already serviced: just stage and boot it.
if "%WIN_PRESERVICED%"=="1" (
  call :log "pre-serviced image - skipping offline servicing"
  goto :staging
)

rem ------------------------------------------------------------------ drivers
call :log "injecting virtio drivers"
%DI% /Add-Driver /Driver:"%M%\drivers" /Recurse >>"%LOG%" 2>&1 || call :log "WARN add-driver returned error"

rem ------------------------------------------------------------------ appx
call :log "removing provisioned appx packages"
%DI% /Get-ProvisionedAppxPackages > X:\winslim-appx.txt 2>&1
for /f "tokens=2 delims=: " %%p in ('%FINDSTR% /b /c:"PackageName" X:\winslim-appx.txt') do call :appx %%p

rem ------------------------------------------------------------------ capabilities
call :log "removing capabilities"
%DI% /Get-Capabilities /Format:Table > X:\winslim-caps.txt 2>&1
for /f "tokens=1,2 delims=| " %%a in (X:\winslim-caps.txt) do (
  if /i "%%b"=="Installed" call :cap %%a
)

rem ------------------------------------------------------------------ features
call :log "removing optional features"
for /f "usebackq delims=" %%f in ("%WS%\features-remove.txt") do (
  %DI% /Disable-Feature /FeatureName:%%f /Remove /NoRestart >>"%LOG%" 2>&1 && call :log "  removed feature %%f"
)
if "%WIN_STRIP_PAYLOADS%"=="1" call :strip_payloads

rem ------------------------------------------------------------------ component store
call :log "component store cleanup (ResetBase)"
%DI% /Cleanup-Image /StartComponentCleanup /ResetBase >>"%LOG%" 2>&1 || call :log "WARN component cleanup returned error"

rem ------------------------------------------------------------------ registry
call :log "offline registry tweaks"
reg load HKLM\zSOFTWARE W:\Windows\System32\config\SOFTWARE >nul
reg load HKLM\zSYSTEM   W:\Windows\System32\config\SYSTEM   >nul
reg load HKLM\zNTUSER   W:\Users\Default\NTUSER.DAT          >nul
set /a REGOK=0, REGFAIL=0
for /f "usebackq eol=# delims=" %%L in ("%WS%\offline-reg.txt") do (
  %%L >nul 2>&1 && (set /a REGOK+=1 >nul) || (
    set /a REGFAIL+=1 >nul
    >>"%LOG%" echo   reg FAILED: %%L
  )
)
call :log "  registry: %REGOK% ok, %REGFAIL% failed (deletes of absent keys are expected)"
for /f "usebackq delims=" %%s in ("%WS%\services-disable.txt") do (
  reg query "HKLM\zSYSTEM\ControlSet001\Services\%%s" >nul 2>&1 && (
    reg add "HKLM\zSYSTEM\ControlSet001\Services\%%s" /v Start /t REG_DWORD /d 4 /f >nul 2>&1 && (>>"%LOG%" echo   service disabled: %%s) || (>>"%LOG%" echo   service FAILED: %%s)
  )
)
call :unload zNTUSER
call :unload zSYSTEM
call :unload zSOFTWARE

rem ------------------------------------------------------------------ files
call :log "deleting leftovers (Edge, OneDrive setup, WinRE)"
for /f "usebackq delims=" %%P in ("%WS%\offline-delete.txt") do call :del "W:\%%P"

if "%WINSLIM_MODE%"=="capture" goto :capture

rem ------------------------------------------------------------------ first-boot payload
:staging
call :log "staging unattend.xml and first-boot scripts"
mkdir W:\Windows\Panther >nul 2>&1
copy /y "%WS%\unattend.xml" W:\Windows\Panther\unattend.xml >nul
xcopy /e /i /q /y "%WS%\payload" W:\Windows\Setup\Scripts >nul
>W:\Windows\winslim.installed echo winslim %WINSLIM_BUILD% edition=%WIN_EDITION% index=%IDX%

rem ------------------------------------------------------------------ boot files
call :log "bcdboot"
bcdboot W:\Windows /s S: /f UEFI >>"%LOG%" 2>&1
if errorlevel 1 (
  call :fail "bcdboot failed"
  goto :eof
)

rd /s /q W:\scratch >nul 2>&1
call :log "WINSLIM-PE-DONE - rebooting into Windows"
copy /y "%LOG%" W:\Windows\Setup\winslim-pe.log >nul
copy /y X:\winslim-appx.txt W:\Windows\Setup\ >nul
copy /y X:\winslim-caps.txt W:\Windows\Setup\ >nul
wpeutil reboot
exit /b 0


rem ------------------------------------------------------------------ capture (./winvm iso)
:capture
rd /s /q W:\scratch >nul 2>&1
mkdir O:\scratch >nul 2>&1
call :log "capturing the serviced image"
dism /English /Capture-Image /ImageFile:O:\install.wim /CaptureDir:W:\ /Name:"%WIN_EDITION%" /Description:"winslim %WINSLIM_BUILD%" /Compress:fast /ScratchDir:O:\scratch >>"%LOG%" 2>&1
if errorlevel 1 (
  call :fail "dism /Capture-Image failed"
  goto :eof
)
rem (the host re-exports it as solid LZMS with wimlib; WinPE lacks the RAM for that)
rd /s /q O:\scratch >nul 2>&1
call :log "WINSLIM-CAPTURE-DONE"
copy /y "%LOG%" O:\winslim-pe.log >nul
>O:\WINSLIM-CAPTURE-DONE echo ok
wpeutil shutdown
exit /b 0


rem ===========================================================================
:log
echo [%TIME%] %~1
>>"%LOG%" echo [%TIME%] %~1
if defined SERIAL 2>nul >COM1 echo [PE %TIME%] %~1
exit /b 0

:fail
call :log "WINSLIM-FAIL: %~1"
if exist W:\Windows\Setup copy /y "%LOG%" W:\Windows\Setup\winslim-pe.log >nul 2>&1
if "%WINSLIM_DEBUG%"=="1" (
  echo Debug mode: dropping to a shell. Log: %LOG%
  cmd.exe /k
)
wpeutil shutdown
exit /b 1

:idxline
set "k=%~1"
set "v=%~2"
set "k=%k: =%"
if defined v set "v=%v:~1%"
if /i "%k%"=="Index" set "CUR=%v%"
if /i "%k%"=="Name" if /i "%v%"=="%WIN_EDITION%" if not defined IDX set "IDX=%CUR%"
exit /b 0

:appx
echo %~1| %FINDSTR% /i /b /l /g:"%WS%\appx-keep.txt" >nul && (
  >>"%LOG%" echo   keep appx   %~1
  exit /b 0
)
%DI% /Remove-ProvisionedAppxPackage /PackageName:%~1 >nul 2>&1 && (
  >>"%LOG%" echo   removed appx %~1
) || (
  >>"%LOG%" echo   FAILED appx %~1
)
exit /b 0

:cap
echo %~1| %FINDSTR% /i /b /l /g:"%WS%\capabilities-remove.txt" >nul || exit /b 0
%DI% /Remove-Capability /CapabilityName:%~1 >nul 2>&1 && (
  >>"%LOG%" echo   removed capability %~1
) || (
  >>"%LOG%" echo   FAILED capability %~1
)
exit /b 0

:strip_payloads
rem Remove payloads of features that are disabled anyway (frees WinSxS space).
call :log "stripping payloads of disabled features"
%DI% /Get-Features /Format:Table > X:\winslim-features.txt 2>&1
set /a NSTRIP=0
for /f "tokens=1,2,3 delims=| " %%a in (X:\winslim-features.txt) do (
  if /i "%%b"=="Disabled" if "%%c"=="" (
    echo %%a| %FINDSTR% /i /b /l /g:"%WS%\features-keep-payload.txt" >nul || (
      %DI% /Disable-Feature /FeatureName:%%a /Remove /NoRestart >nul 2>&1 && (
        set /a NSTRIP+=1 >nul
        >>"%LOG%" echo   stripped payload %%a
      )
    )
  )
)
call :log "  stripped %NSTRIP% feature payloads"
copy /y X:\winslim-features.txt W:\Windows\Setup\ >nul 2>&1
exit /b 0

:unload
for /l %%n in (1,1,5) do (
  reg unload HKLM\%~1 >nul 2>&1 && exit /b 0
  ping -n 2 127.0.0.1 >nul
)
call :log "WARN could not unload %~1"
exit /b 0

:del
rem No parenthesised blocks here: paths like "Program Files (x86)" would break them.
if not exist "%~1" exit /b 0
takeown /f "%~1" /r /d y >nul 2>&1
icacls "%~1" /grant *S-1-5-18:F *S-1-5-32-544:F /t /c /q >nul 2>&1
attrib -r -s -h "%~1" >nul 2>&1
if exist "%~1\*" goto :del_dir
del /f /q "%~1" >nul 2>&1
goto :del_check
:del_dir
rd /s /q "%~1" >nul 2>&1
:del_check
if exist "%~1" >>"%LOG%" echo   FAILED delete %~1
if not exist "%~1" >>"%LOG%" echo   deleted %~1
exit /b 0
