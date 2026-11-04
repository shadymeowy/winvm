# Shared helpers for winslim first-boot scripts.
$WinslimLog = 'C:\Windows\Setup\winslim.log'
function Log([string]$Msg, [string]$Tag = 'WIN') {
    $line = '[{0:HH:mm:ss}] {1}' -f (Get-Date), $Msg
    try { Add-Content -LiteralPath $WinslimLog -Value $line -ErrorAction Stop } catch {}
    Write-Output $line
    try {
        $p = New-Object System.IO.Ports.SerialPort 'COM1', 115200
        $p.Open(); $p.WriteLine("[$Tag] $Msg"); $p.Close()
    } catch {}
}
function Run([string]$File, [string[]]$Arguments) {
    $p = Start-Process -FilePath $File -ArgumentList $Arguments -Wait -PassThru -NoNewWindow
    Log ("  {0} -> exit {1}" -f (Split-Path $File -Leaf), $p.ExitCode)
    return $p.ExitCode
}
