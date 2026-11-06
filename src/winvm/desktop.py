"""A logged-on desktop in the guest.

What runs through SSH has no desktop (session 0) and, logged on with a key, can't use DPAPI.
A real logon needs the user's password and a reboot: Windows only logs on by itself while
it boots (ending LogonUI, resetting the console session or ending its winlogon don't do it).
So auto-logon gets turned on (`arm`), and from then on every boot logs on.

A command for that session is a scheduled task. Its output goes to a file, which one SSH call
follows until the command is done, and its exit code to another file.

Builds use this for `desktop: true` and undo it before writing the layer. Instances use it
for `run: desktop: true` / `winvm up --desktop` / `winvm sh --desktop`, and undo it when
that goes off. MARKER says winvm turned it on, and holds the values to put back.
"""
from __future__ import annotations

import base64
import json
import secrets
import subprocess as sp
import tempfile
import time

from . import guest
from .guest import BOM
from .util import WAIT, Chan, Fatal, TimedOut, die, dos, log, time_limit, wait_end, warn

HOME = r"C:\ProgramData\winvm"
MARKER = HOME + r"\desktop.json"
STEPS = HOME + r"\steps"        # one folder per running command: script, runner, log, exit code
TASKS = "winvm-step"
VANISHED = -1                   # run(): the command is gone and left no exit code
LOST = -2                       # run(): the connection to the VM went away
GONE = 254                      # launch(): what it exits with for VANISHED
LONG = 8000                     # base64 characters of a script that still go on a command line
WINLOGON = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
POLICIES = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
LOGON_VALUES = [(WINLOGON, "AutoAdminLogon", "String"), (WINLOGON, "DefaultUserName", "String"),
                (WINLOGON, "DefaultPassword", "String"), (WINLOGON, "AutoLogonCount", "DWord"),
                (WINLOGON, "LegalNoticeCaption", "String"), (WINLOGON, "LegalNoticeText", "String"),
                (POLICIES, "legalnoticecaption", "String"), (POLICIES, "legalnoticetext", "String")]


SWEEP = f"""if (Test-Path -LiteralPath {guest.quote(STEPS)}) {{
    Get-ScheduledTask '{TASKS}-*' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
    Remove-Item -LiteralPath {guest.quote(STEPS)} -Recurse -Force -ErrorAction SilentlyContinue
}}"""


def user_of(vm) -> str:
    return str(vm.layer_meta().get("user") or vm.cfg.builtin["user"])


def password_of(vm) -> str:
    """`run: password:`, else the one the image was built with."""
    for v in (vm.settings.get("password"), vm.layer_meta().get("password"), vm.cfg.builtin["password"]):
        if v is not None:
            return str(v)
    return ""


def status(vm, clean: bool = False) -> tuple[bool, bool]:
    """(is auto-logon winvm's?, is the user logged on?) clean: also `sweep`, in one guest call."""
    r = guest.capture(vm.ssh, f"""{SWEEP if clean else ""}
"ARMED:" + (Test-Path -LiteralPath {guest.quote(MARKER)})
"ON:" + {on_probe(user_of(vm))}""")
    fields = dict(ln.partition(":")[::2] for ln in r.stdout.splitlines())
    if r.returncode or "ON" not in fields:
        die(f"could not ask {vm.name} whether it is logged on: {r.stdout.strip()[:300]}")
    return fields["ARMED"].strip() == "True", fields["ON"].strip() != "0"


def on_probe(user: str) -> str:
    """How many processes the user has outside session 0."""
    return (f"@(Get-Process -IncludeUserName | Where-Object {{ $_.SessionId -and "
            f"$_.UserName -like {guest.quote(chr(42) + chr(92) + user)} }}).Count")


def wait_logon(vm, what: str) -> None:
    user, end = user_of(vm), wait_end()
    try:
        while time.monotonic() < end:
            r = guest.capture(vm.ssh, on_probe(user), timeout=120)   # (a busy guest: just ask again)
            if r.returncode == 0 and r.stdout.strip().splitlines()[-1:] not in ([], ["0"]):
                return
            time.sleep(1)
    except TimedOut as e:
        raise TimedOut(f"{e}: {user} hadn't logged on yet{picture(vm)}", e.limit) from None
    die(f"{what}: {user} did not log on within {WAIT} s. A changed password or a policy can stop "
        f"auto-logon. If someone signed out, it logs on at the next boot: winvm -i {vm.name} restart{picture(vm)}")


def picture(vm) -> str:
    """A picture of the VM's screen, as text for an error message."""
    out = vm.cfg.build / f"{vm.name}-failed.png"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        Chan(vm.dir / "qmp.sock", qmp=True, timeout=5)("screendump", {"filename": str(out), "format": "png"})
    except (Fatal, OSError):
        return ""
    return f" (its screen: {out})"


def arm(vm, password: str, wrong: str) -> None:
    """Turn auto-logon on; it takes effect at the next boot. `wrong`: what to tell the user
    about a wrong password. Fails before anything changes. The values it replaces go to
    MARKER, unless that is already there (as in an image saved with this on)."""
    user = user_of(vm)
    r = guest.capture(vm.ssh, f"""Add-Type -AssemblyName System.DirectoryServices.AccountManagement
try {{ $c = [DirectoryServices.AccountManagement.PrincipalContext]::new('Machine')
    if ($c.ValidateCredentials({guest.quote(user)}, {guest.quote(password)})) {{ 'GOOD' }} else {{ 'WRONG' }} }}
catch {{ 'UNKNOWN' }}""")                                       # (a blank password can't be checked)
    if r.stdout.strip().endswith("WRONG"):
        die(f"{user} has to log on, and the password winvm knows is not that user's: {wrong}")
    read = ", ".join(f"(Get-ItemProperty -LiteralPath {guest.quote(p)} -Name {n} -ErrorAction SilentlyContinue).{n}"
                     for p, n, _ in LOGON_VALUES)
    notices = "".join(f"if ((Get-ItemProperty -LiteralPath {guest.quote(p)} -Name {n} -ErrorAction SilentlyContinue).{n}) "
                      f"{{ Set-ItemProperty -LiteralPath {guest.quote(p)} -Name {n} -Value '' }}\n"    # would wait for OK
                      for p, n, _ in LOGON_VALUES if "egal" in n)
    vm.call(f"""$w = {guest.quote(WINLOGON)}
New-Item -ItemType Directory -Force {guest.quote(HOME)} | Out-Null
if (-not (Test-Path -LiteralPath {guest.quote(MARKER)})) {{
    Set-Content -Encoding UTF8 -LiteralPath {guest.quote(MARKER)} -Value (ConvertTo-Json -Compress @({read}))
}}
Remove-ItemProperty -LiteralPath $w -Name AutoLogonCount -ErrorAction SilentlyContinue
Set-ItemProperty -LiteralPath $w -Name AutoAdminLogon -Value '1'
Set-ItemProperty -LiteralPath $w -Name DefaultUserName -Value {guest.quote(user)}
Set-ItemProperty -LiteralPath $w -Name DefaultPassword -Value {guest.quote(password)}
{notices}""")


def disarm(vm) -> None:
    """The logon settings as they were before `arm`. A session that is on stays on."""
    r = guest.capture(vm.ssh, f"Get-Content -Raw -Encoding UTF8 -LiteralPath {guest.quote(MARKER)}")
    try:
        old = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        die(f"could not read the old logon settings of {vm.name}: {r.stdout.strip()[:300]}")
    back = "\n".join(
        f"Remove-ItemProperty -LiteralPath {guest.quote(p)} -Name {n} -ErrorAction SilentlyContinue" if v is None else
        f"New-ItemProperty -LiteralPath {guest.quote(p)} -Name {n} -PropertyType {t} -Force "
        f"-Value {v if t == 'DWord' else guest.quote(str(v))} | Out-Null"
        for (p, n, t), v in zip(LOGON_VALUES, old))
    vm.call(f"{back}\nRemove-Item -LiteralPath {guest.quote(MARKER)}")


def sweep(vm) -> None:
    """What commands left behind (a VM that stopped under them)."""
    r = guest.capture(vm.ssh, SWEEP)
    if r.returncode:
        warn(f"could not clean up old commands in {vm.name}: {r.stdout.strip()[:200]}")


def sync(vm, may_reboot: bool, fresh: bool = False) -> None:
    """Make the guest match the instance's `desktop` setting. fresh: it has just booted."""
    want = bool(vm.settings.get("desktop"))
    armed, on = status(vm, clean=fresh)
    if not want:
        if armed:
            disarm(vm)
            log(f"desktop mode off for {vm.name}: its password is out of the registry"
                + (", and the session stays until it reboots" if on else ""))
        return
    what = f"{vm.name} in desktop mode"
    if not armed:
        arm(vm, password_of(vm), "put the current one in the image file or ~/.config/winvm/winvm.yaml (`run: password:`)")
        if not on and not may_reboot:
            warn(f"{vm.name} logs on from its next boot (winvm -i {vm.name} restart)")
            return
    if not on:
        if not armed:
            log(f"desktop mode: rebooting {vm.name} to log on as {user_of(vm)} (once)")
            vm.reboot()
        wait_logon(vm, what)
    log(f"{vm.name} is logged on as {user_of(vm)}")


# Runs in the desktop session: the command, its output to a file, then its exit code (atomically).
# Nobody can answer that session's console, so asking for input fails instead of hanging.
RUNNER = r"""$d = $PSScriptRoot
[IO.File]::WriteAllText("$d\pid", "$PID")
$utf8 = [Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = $utf8 } catch {}
Set-Location -LiteralPath $env:USERPROFILE
try {
    @() | & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$d\step.ps1" 2>&1 |
        ForEach-Object { [IO.File]::AppendAllText("$d\log", "$_`r`n", $utf8) }
    $rc = $LASTEXITCODE
} catch { [IO.File]::AppendAllText("$d\log", "error: $_`r`n", $utf8); $rc = 1 }
[IO.File]::WriteAllText("$d\rc.tmp", "$rc")
Move-Item -Force "$d\rc.tmp" "$d\rc"
"""


def launch(d: str, task: str, user: str, step: str | None) -> str:
    """Guest script, all in one SSH call: write the runner and the command's script (`step`,
    base64; None: run() has put it there), start them as a task (through the COM
    API, as the ScheduledTasks module takes a second to load), copy
    the command's log to stdout until it is done (`rc` is looked at before the log is read: everything
    is in the log once it exists), clean up and exit with its exit code. If that goes wrong, or the
    command is gone without an exit code, the folder stays: that's how run() can tell."""
    action = f'-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{d}\\runner.ps1"'
    write_step = f"""[IO.File]::WriteAllBytes("$d\\step.ps1", [Convert]::FromBase64String('{step}'))""" if step else ""
    return f"""$ErrorActionPreference = 'Stop'
$d = {guest.quote(d)}
New-Item -ItemType Directory -Force $d | Out-Null
[IO.File]::WriteAllBytes("$d\\runner.ps1", [Convert]::FromBase64String('{base64.b64encode(BOM + dos(RUNNER)).decode()}'))
{write_step}
$s = New-Object -ComObject Schedule.Service; $s.Connect(); $root = $s.GetFolder('\\')
$t = $s.NewTask(0)
$t.Principal.UserId = {guest.quote(user)}; $t.Principal.LogonType = 3; $t.Principal.RunLevel = 1
$t.Settings.ExecutionTimeLimit = 'PT0S'; $t.Settings.DisallowStartIfOnBatteries = $false; $t.Settings.StopIfGoingOnBatteries = $false
$a = $t.Actions.Create(0); $a.Path = 'powershell.exe'; $a.Arguments = {guest.quote(action)}
[void]$root.RegisterTaskDefinition('{task}', $t, 6, $null, $null, 3).Run($null)
$ErrorActionPreference = 'Continue'
$out = [Console]::OpenStandardOutput(); $buf = New-Object byte[] 65536
$pos = 0; $gone = 0; $pid_ = $null
while (Test-Path -LiteralPath $d) {{
    $done = Test-Path "$d\\rc"; $n = 0
    if (Test-Path "$d\\log") {{
        $f = [IO.File]::Open("$d\\log", 'Open', 'Read', 'ReadWrite,Delete')
        $f.Position = $pos; $n = $f.Read($buf, 0, $buf.Length); $f.Close()
    }}
    if ($n) {{ $out.Write($buf, 0, $n); $out.Flush(); $pos += $n; continue }}
    if ($done) {{ break }}
    if (-not $pid_ -and (Test-Path "$d\\pid")) {{ $pid_ = (Get-Content "$d\\pid") -as [int] }}
    $alive = if ($pid_) {{ [bool](Get-Process -Id $pid_ -ErrorAction SilentlyContinue) }}
             else {{ $root.GetTask('{task}').State -in 2, 4 }}     # queued or running: it just takes a while
    $gone = if ($alive) {{ 0 }} else {{ $gone + 1 }}
    if ($gone -ge $(if ($pid_) {{ 5 }} else {{ 100 }})) {{ exit {GONE} }}
    Start-Sleep -Milliseconds 50
}}
$rc = (Get-Content "$d\\rc") -as [int]
$root.DeleteTask('{task}', 0)
Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue
# a shell only sees the low byte, and 256 must not look like success
exit $(if ($rc -and -not ($rc -band 255)) {{ 1 }} else {{ $rc -band 255 }})
"""


def run(vm, code: str, limit: int | None = None, cwd: str | None = None, strict: bool = False) -> tuple[int, str]:
    """Run PowerShell in the logged-on session, its output (stderr too) on our stdout as it
    comes. Returns its exit code, or guest.TIMED_OUT (`limit` ran out) / VANISHED / LOST / 141 (our
    stdout went away); and text for an error message: a picture of the screen, taken before the
    command is ended, and whether it was. The command's own --timeout raises TimedOut with that
    text instead. Each call has its own folder and task: they can overlap."""
    tag = secrets.token_hex(4)
    d, task = f"{STEPS}\\{tag}", f"{TASKS}-{tag}"
    script = BOM + dos(guest.script(code, cwd=cwd, strict=strict))
    step = base64.b64encode(script).decode()
    own = None
    try:
        with time_limit(limit) as own:
            if len(step) > LONG:                               # (a command line has a limit)
                vm.call(f"New-Item -ItemType Directory -Force {guest.quote(d)} | Out-Null")
                with tempfile.NamedTemporaryFile(dir=vm.dir, suffix=".ps1") as f:
                    f.write(script)
                    f.flush()
                    vm.scp(f.name, f"{d}\\step.ps1", timeout=WAIT)
                step = None
            rc = guest.pipe(vm.ssh, launch(d, task, user_of(vm), step), stdin=sp.DEVNULL)
    except TimedOut as e:
        note = picture(vm) + ended(finish(vm, d, task))        # (the picture first: it shows why)
        if own and e.limit is own:
            return guest.TIMED_OUT, note
        raise TimedOut(f"{e}{note}", e.limit) from None
    except BaseException:                                      # Ctrl-C: nothing stays running
        finish(vm, d, task)
        raise
    if rc not in (141, GONE, 255):
        return rc, ""
    note = picture(vm) if rc == GONE else ""
    state = finish(vm, d, task)
    if state == "done":                                        # 141, 254, 255: the command's own
        return rc, ""
    if state == "lost" or rc == 255:
        return LOST, ""                                        # ("stuck" with GONE: nothing to end)
    return (141, "") if rc == 141 else (VANISHED, note)


def ended(state: str) -> str:
    """finish()'s result, for an error message."""
    return "; the command had just finished" if state == "done" else "; ended the command" if state == "left" else \
        "; couldn't end the command, so it may still be running"


def finish(vm, d: str, task: str) -> str:
    """End a command that is still there and remove what is left of it, with its folder. What
    was there: "done" (nothing, it had finished by itself), "left", "stuck" (it wouldn't end), or
    "lost" (no connection, or no answer in time: a limit may just have run out, so this must not
    hang either)."""
    r = guest.capture(guest.fresh(vm.ssh), f"""$d = {guest.quote(d)}
if (-not (Test-Path -LiteralPath $d)) {{ 'STATE:done'; exit 0 }}
$r = (Get-Content "$d\\pid" -ErrorAction SilentlyContinue) -as [int]
if ($r) {{ {guest.KILL_TREE} }}
$s = New-Object -ComObject Schedule.Service; $s.Connect()
try {{ $s.GetFolder('\\').DeleteTask('{task}', 0) }} catch {{}}
Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue
if ($r -and (Get-Process -Id $r -ErrorAction SilentlyContinue)) {{ 'STATE:stuck' }} else {{ 'STATE:left' }}""", timeout=guest.CLEANUP)
    return "lost" if r.returncode else "done" if "STATE:done" in r.stdout else "stuck" if "STATE:stuck" in r.stdout else "left"
