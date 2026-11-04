# winslim: runs once at the build's single autologon (creates the user profile
# inside the base image, so later SSH/GUI logons are fast), then reboots once more:
# files the shell kept locked are deleted during that boot, and final.ps1 (a one-time
# startup task) finishes the build and powers off.
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
    $left = Get-ChildItem -LiteralPath $wv -Recurse -Force -ErrorAction SilentlyContinue
    if ($left) {   # still locked: delete during the next boot (files first, then folders bottom-up)
        Add-Type -Namespace Winslim -Name Kernel32 -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool MoveFileEx(string from, string to, int flags);
'@
        $paths = @($left | Where-Object { -not $_.PSIsContainer }) + @($left | Where-Object PSIsContainer |
            Sort-Object { $_.FullName.Length } -Descending) + @(Get-Item -LiteralPath $wv -Force)
        foreach ($f in $paths) { [void][Winslim.Kernel32]::MoveFileEx($f.FullName, $null, 4) }   # DELAY_UNTIL_REBOOT
        Log ("  EdgeWebView: {0:N1} MB still locked, deleted at the next boot" -f
            (($left | Measure-Object Length -Sum).Sum / 1MB)) 'FL'
    } else {
        Log '  EdgeWebView removed' 'FL'
    }
}

# Per-user leftovers
Remove-Item -Force -ErrorAction SilentlyContinue "$env:USERPROFILE\Desktop\Microsoft Edge.lnk"
Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Finish in one more boot: locked files go away, then final.ps1 runs as SYSTEM at startup.
$act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptDir\final.ps1`""
$task = Register-ScheduledTask -TaskName 'winslim-final' -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -User 'SYSTEM' -RunLevel Highest -Force
if ($task) {
    Log 'rebooting once to finish' 'FL'
    & shutdown.exe /r /t 3 /c 'winslim: one more boot'
} else {   # without the task nothing would power the VM off after the reboot: finish now
    Log 'WARN could not register the startup task - finishing without the extra boot' 'FL'
    & "$ScriptDir\final.ps1"
}
