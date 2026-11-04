# winslim: runs as SYSTEM from SetupComplete.cmd (after OOBE, before first logon).
# Installs guest tools + OpenSSH, configures the VM, re-checks the debloat online,
# then compacts and trims the disk. firstlogon.ps1 powers the VM off afterwards.
$ErrorActionPreference = 'Continue'
$ScriptDir = $PSScriptRoot
. "$ScriptDir\common.ps1"
. "$ScriptDir\settings.ps1"
Log 'setupcomplete start' 'SC'

# ------------------------------------------------------------------ guest tools
Log 'installing virtio guest tools (drivers, qemu-ga, spice-vdagent)' 'SC'
& certutil.exe -addstore -f TrustedPublisher "$ScriptDir\virtio-ca.cer" | Out-Null
Run "$ScriptDir\virtio-win-guest-tools.exe" @('/install', '/quiet', '/norestart') | Out-Null

# ------------------------------------------------------------------ OpenSSH server
Log 'installing OpenSSH server' 'SC'
Run 'msiexec.exe' @('/i', "`"$ScriptDir\OpenSSH.msi`"", '/qn', '/norestart', 'ADDLOCAL=Server') | Out-Null
# sshd starts subsystems through the default shell (PowerShell), so `Subsystem sftp sftp-server.exe`
# - and with it scp, which uses SFTP - only works when the OpenSSH folder is on PATH
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
[Environment]::SetEnvironmentVariable('Path', "$machinePath;$env:ProgramFiles\OpenSSH", 'Machine')
New-Item -ItemType Directory -Force -Path 'C:\ProgramData\ssh' | Out-Null
$ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
Copy-Item "$ScriptDir\authorized_keys" $ak -Force
& icacls.exe $ak /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
    -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' | Out-Null
Set-Service sshd -StartupType Automatic
Start-Service sshd   # generates host keys now, so they are baked into the base image
Log ("  sshd: {0}" -f (Get-Service sshd).Status) 'SC'

# ------------------------------------------------------------------ .NET native images
# Windows queues NGEN work and runs it when idle, which a fresh image never was. Without
# native images every PowerShell start JIT-compiles its engine: ~2 s instead of ~0.2 s
# for each `winvm sh` command.
Log 'compiling .NET native images (ngen)' 'SC'
& "$env:windir\Microsoft.NET\Framework64\v4.0.30319\ngen.exe" executeQueuedItems /nologo /silent | Out-Null
$sma = Get-ChildItem "$env:windir\Microsoft.Net\assembly\GAC_MSIL\System.Management.Automation" -Recurse -Filter *.dll | Select-Object -First 1
& "$env:windir\Microsoft.NET\Framework64\v4.0.30319\ngen.exe" install $sma.FullName /nologo /silent | Out-Null
Log ("  powershell startup: {0:N2}s" -f (Measure-Command { powershell -NoProfile -Command 1 }).TotalSeconds) 'SC'

# ------------------------------------------------------------------ network / sharing
Log 'firewall off (VM is only reachable through localhost port forwards)' 'SC'
Set-NetFirewallProfile -All -Enabled False
# host folders (winvm mounts) appear as links in C:\mnt, made by winvm at each start

# ------------------------------------------------------------------ power
& powercfg.exe /hibernate off
& powercfg.exe /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c   # High performance
foreach ($setting in 'monitor-timeout-ac', 'standby-timeout-ac', 'disk-timeout-ac') { & powercfg.exe /change $setting 0 }
& powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3   # power button = shut down
& powercfg.exe /setactive SCHEME_CURRENT

# ------------------------------------------------------------------ online debloat sweep (backup for offline pass)
# keep-list prefixes -> one anchored, case-insensitive regex (empty list => '^()' keeps everything)
$keepRx = '^(' + ((Get-Content "$ScriptDir\appx-keep.txt" | Where-Object { $_ } | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')'
Get-AppxProvisionedPackage -Online | Where-Object { $_.PackageName -notmatch $keepRx } | ForEach-Object {
    Log "  removing provisioned appx $($_.PackageName)" 'SC'
    Remove-AppxProvisionedPackage -Online -PackageName $_.PackageName -ErrorAction SilentlyContinue | Out-Null
}
& dism.exe /English /Online /Set-ReservedStorageState /State:Disabled /Quiet | Out-Null

foreach ($svc in Get-Content "$ScriptDir\services-disable.txt") {
    $o = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($o -and $o.StartType -ne 'Disabled') {
        Log "  service $svc still $($o.StartType) - disabling" 'SC'
        & sc.exe config $svc start= disabled | Out-Null
    }
}
$running = Get-Service WinDefend, wuauserv, MDCoreSvc -ErrorAction SilentlyContinue | Where-Object Status -eq Running
if ($running) { Log ("  WARN still running: {0}" -f ($running.Name -join ', ')) 'SC' }

# Edge leftovers (binaries were deleted offline)
Get-ScheduledTask -TaskName 'MicrosoftEdgeUpdate*' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
foreach ($svc in 'edgeupdate', 'edgeupdatem', 'MicrosoftEdgeElevationService') { & sc.exe delete $svc 2>&1 | Out-Null }

# Scheduled tasks that only make sense with telemetry/update/defender
$taskPaths = '\Microsoft\Windows\Windows Defender\', '\Microsoft\Windows\WindowsUpdate\', '\Microsoft\Windows\UpdateOrchestrator\',
    '\Microsoft\Windows\WaaSMedic\', '\Microsoft\Windows\Application Experience\', '\Microsoft\Windows\Customer Experience Improvement Program\',
    '\Microsoft\Windows\Feedback\Siuf\', '\Microsoft\Windows\Windows Error Reporting\', '\Microsoft\Windows\Maps\',
    '\Microsoft\Windows\DiskDiagnostic\', '\Microsoft\Windows\Flighting\',
    '\Microsoft\Windows\InstallService\', '\Microsoft\Windows\Autochk\', '\Microsoft\Windows\WindowsAI\',
    '\Microsoft\Windows\Recall\', '\Microsoft\Windows\Copilot\'
$n = 0
foreach ($tp in $taskPaths) {
    Get-ScheduledTask -TaskPath "$tp*" -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_ | Disable-ScheduledTask -ErrorAction SilentlyContinue) { $n++ }
    }
}
Log "  disabled $n scheduled tasks" 'SC'

# ------------------------------------------------------------------ the image's `setup` steps
# winvm stages them as extra\setup.ps1 plus the files they use. Runs as SYSTEM from inside
# extra\, after the debloat sweep (which would remove apps it adds) and before the shrink
# (which cleans up after it). A failure fails the build. The folder is deleted afterwards.
$extra = "$ScriptDir\extra"
foreach ($ps in Get-ChildItem -LiteralPath $extra -Filter *.ps1 -File -ErrorAction SilentlyContinue | Sort-Object Name) {
    Log "extra: $($ps.Name)" 'SC'
    Push-Location -LiteralPath $extra
    $global:LASTEXITCODE = 0
    try { & $ps.FullName *>&1 | ForEach-Object { Log "  $_" 'SC' } }
    catch { Log "WINSLIM-FAIL $($ps.Name): $_" 'SC' }
    if ($LASTEXITCODE) { Log "WINSLIM-FAIL $($ps.Name) exited with $LASTEXITCODE" 'SC' }
    Pop-Location
}
Remove-Item -LiteralPath $extra -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ shrink
Log 'component cleanup' 'SC'
& dism.exe /English /Online /Cleanup-Image /StartComponentCleanup /ResetBase /Quiet | Out-Null
if ($WinCompact) { Log 'CompactOS' 'SC'; & compact.exe /CompactOS:always | Out-Null }
else { & compact.exe /CompactOS:never | Out-Null }   # leave compression to the host (qcow2 zstd)
foreach ($p in 'C:\Windows\SoftwareDistribution\Download', 'C:\Windows\Temp', 'C:\Windows\Logs\CBS', 'C:\Windows\Logs\DISM') {
    Get-ChildItem -LiteralPath $p -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item -Force -ErrorAction SilentlyContinue "$ScriptDir\virtio-win-guest-tools.exe", "$ScriptDir\OpenSSH.msi"
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue
Log 'setupcomplete done' 'SC'
