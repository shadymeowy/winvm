# winslim: one-time startup task (registered by firstlogon.ps1), as SYSTEM, in the
# build's last boot: what the shell had locked is gone now; trim and power off.
$ErrorActionPreference = 'Continue'
$ScriptDir = $PSScriptRoot
. "$ScriptDir\common.ps1"
. "$ScriptDir\settings.ps1"
Unregister-ScheduledTask -TaskName 'winslim-final' -Confirm:$false -ErrorAction SilentlyContinue
$wv = 'C:\Program Files (x86)\Microsoft\EdgeWebView'
if ($RemoveWebView -and (Test-Path -LiteralPath $wv)) {
    Remove-Item -LiteralPath $wv -Recurse -Force -ErrorAction SilentlyContinue
    Log ("  EdgeWebView left: {0}" -f (Test-Path -LiteralPath $wv)) 'FL'
}
Log ("C: used {0:N2} GiB" -f ((Get-PSDrive C).Used / 1GB)) 'FL'
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue
Log 'WINSLIM-DONE' 'FL'
& shutdown.exe /s /t 3 /c 'winslim build complete'
