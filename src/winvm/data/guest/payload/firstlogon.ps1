# winslim: runs once at the build's single autologon (creates the user profile
# inside the base image, so later SSH/GUI logons are fast), then powers off.
$ErrorActionPreference = 'Continue'
$ScriptDir = $PSScriptRoot
. "$ScriptDir\common.ps1"
. "$ScriptDir\settings.ps1"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
Log "firstlogon as $env:USERNAME (elevated=$admin)" 'FL'

# Autologon was only for this one boot: make sure no password stays in the registry.
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $wl -Name AutoAdminLogon -Value '0' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $wl -Name DefaultPassword -ErrorAction SilentlyContinue
Set-ItemProperty -Path $wl -Name AutoLogonCount -Value 0 -ErrorAction SilentlyContinue

# Edge WebView2 runtime: only needed by stuff we removed (widgets, Copilot, Teams...).
# Done here, after the user OOBE, which may use it.
if ($RemoveWebView) {
    $wv = 'C:\Program Files (x86)\Microsoft\EdgeWebView'
    for ($i = 0; $i -lt 3 -and (Test-Path -LiteralPath $wv); $i++) {   # shell processes may hold it briefly
        Get-Process msedgewebview2 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $wv -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $wv) { Start-Sleep -Seconds 3 }
    }
    Remove-Item -LiteralPath 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Microsoft EdgeWebView' -Recurse -Force -ErrorAction SilentlyContinue
    $left = (Get-ChildItem -LiteralPath $wv -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    Log ("  EdgeWebView removed ({0:N1} MB still locked, the host cleans that up offline)" -f ($left/1MB)) 'FL'
}

# Per-user leftovers
Remove-Item -Force -ErrorAction SilentlyContinue "$env:USERPROFILE\Desktop\Microsoft Edge.lnk"
Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$used = (Get-PSDrive C).Used / 1GB
Log ("C: used {0:N2} GiB" -f $used) 'FL'
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue
Log 'WINSLIM-DONE' 'FL'
& shutdown.exe /s /t 3 /c 'winslim build complete'
