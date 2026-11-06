"""Running PowerShell in the guest over SSH, so that it behaves like a local command:
exit code, stdin, streaming output with Unix line endings."""
from __future__ import annotations

import base64
import os
import re
import signal
import subprocess as sp
import sys

from .util import WAIT, TimedOut, time_limit, timing

TIMED_OUT = -124    # pipe(): its own limit ran out (no exit code can look like this)
NO_ANSWER = 124     # capture(): no answer in time; its output says so
CLEANUP = 30        # s for cleaning up in the guest: it must not hang past a limit that just ran out
EXIT_TIMED_OUT = 124   # what `winvm sh` exits with then, as `timeout` does
BOM = b"\xef\xbb\xbf"  # Windows PowerShell reads .ps1 files without it as ANSI, not UTF-8

PROLOGUE = r"""$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
$OutputEncoding = [Text.UTF8Encoding]::new($false)
function stdin {
    if ($null -eq $winvm_in) { while ($null -ne ($l = [Console]::In.ReadLine())) { $l }; return }
    $e = [Console]::InputEncoding                # (it decoded our UTF-8 with the console's code page)
    foreach ($l in $winvm_in) { [Text.Encoding]::UTF8.GetString($e.GetBytes($l)) }
}
"""
# PowerShell's own $input is the reliable way to read stdin: [Console]::In stalls the whole SSH
# connection from around 16 KB. But once a command mentions it, PowerShell keeps stdin from the
# native programs in that command. So only a command that calls `stdin` gets it; if this misses
# one (`iex`), that call falls back to [Console]::In.
CALLS_STDIN = re.compile(r"(?:^|[|;{(&\n])\s*stdin\b")
# A session gets its environment when sshd starts, so what an installer just set (PATH...)
# is missing until a reboot: take it from the registry, as a new logon would. USERNAME is
# SYSTEM there, and PowerShell makes its own PSModulePath.
REFRESH = r"""$m = [Environment]::GetEnvironmentVariables('Machine'); $u = [Environment]::GetEnvironmentVariables('User')
foreach ($s in $m, $u) { foreach ($k in $s.Keys) { if ($k -notin 'Path', 'PSModulePath', 'USERNAME') {
    Set-Item -LiteralPath "Env:$k" -Value $s[$k] -ErrorAction SilentlyContinue } } }
$env:Path = (@($m['Path'], $u['Path'], $env:Path) -join ';' -split ';' | Where-Object { $_ } | Select-Object -Unique) -join ';'
"""
# $rc: the native program's exit code, or 1 for a failed cmdlet / thrown error
EPILOGUE = "\n$rc = if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }\n"


def quote(s: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + s.replace("'", "''") + "'"


def encoded(script: str) -> str:
    """Remote command for the guest's PowerShell that runs `script` exactly as written:
    shipped as base64, so no quoting survives ssh, sshd and Windows command-line parsing."""
    b64 = base64.b64encode(script.encode()).decode()
    grab = "$winvm_in = $input; " if CALLS_STDIN.search(script.replace(PROLOGUE, "")) else ""
    return f"{grab}iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"


def command_line(words: list[str]) -> str:
    """`winvm sh` words -> one PowerShell line: a single word is code as is; otherwise empty
    words and words containing whitespace are quoted so they stay one argument."""
    if len(words) == 1:
        return words[0]
    return " ".join(quote(w) if not w or any(c.isspace() for c in w) else w for w in words)


def script(line: str, cwd: str | None = None, strict=False, console=False, pid_file: str | None = None) -> str:
    """Full guest script for one command line; strict: a cmdlet error stops it (build steps);
    pid_file: keep the PowerShell's pid there while it runs, so that it can be ended (`end`).
    PowerShell holds objects without a table view (`select`) back for a moment to size the
    table, and `exit` would lose them: the command gets its own output pipeline, which ends
    first. Not with console=True (a terminal), where programs need the real console."""
    head = PROLOGUE + REFRESH + ("$ErrorActionPreference = 'Stop'\n" if strict else "")
    # a command must never run somewhere else than where it was asked to
    cd = (f"try {{ Set-Location -LiteralPath {quote(cwd)} -ErrorAction Stop }} catch {{ "
          f"[Console]::Error.WriteLine(\"error: can't run in {cwd.replace(chr(34), '').replace('$', '`$')}: $($_.Exception.Message)\"); exit 1 }}\n") if cwd else ""
    if strict:   # a thrown error: print just its message and exit 1
        line = ("$global:LASTEXITCODE = 0\ntry {\n" + line +
                "\n} catch { [Console]::Error.WriteLine(\"error: $($_.Exception.Message)\"); exit 1 }")
    body = line + EPILOGUE
    run = body if console else ". {\n" + body + "} | Out-Default\n"
    if pid_file:     # (the folder may not exist yet, on a VM whose first start was cut short;
        # `finally` also sees the command's own `exit`)
        run = (f"[void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName({quote(pid_file)}))\n"
               f"[IO.File]::WriteAllText({quote(pid_file)}, \"$PID\")\n"
               f"try {{\n{run}}} finally {{ Remove-Item -LiteralPath {quote(pid_file)} -ErrorAction SilentlyContinue }}\n")
    return head + cd + run + "exit $rc\n"


# (what taskkill exits with says little: it fails for a tree whose child went first. So look)
KILL_TREE = ("taskkill.exe /F /T /PID $r 2>&1 | Out-Null; $n = 0; "
             "while ((Get-Process -Id $r -ErrorAction SilentlyContinue) -and $n++ -lt 30) { Start-Sleep -Milliseconds 100 }")


def fresh(ssh):
    """`ssh` on a connection of its own: a command blocked on stdin holds up the shared one."""
    return lambda *opts: ssh("-S", "none", *opts)


def end(ssh, pid_file: str) -> bool:
    """End the command that keeps its pid in pid_file, with everything it started. Did that work?"""
    # (no file: it has finished, as dropping the connection can end it. Or it hadn't started yet: wait a moment)
    r = capture(fresh(ssh), f"""$f = {quote(pid_file)}; $n = 0
while (-not (Test-Path -LiteralPath $f) -and $n++ -lt 30) {{ Start-Sleep -Milliseconds 100 }}
$r = (Get-Content -LiteralPath $f -ErrorAction SilentlyContinue) -as [int]
if ($r) {{ {KILL_TREE} }}
if (-not $r -or -not (Get-Process -Id $r -ErrorAction SilentlyContinue)) {{ 'ENDED' }}
Remove-Item -LiteralPath $f -ErrorAction SilentlyContinue""", timeout=CLEANUP)
    return "ENDED" in r.stdout


# `ssh` below is a function: ssh(*options) -> ssh argv up to and including the host name

def pipe(ssh, script_: str, stdin=None, timeout: int | None = None) -> int:
    """Run without a terminal: stdin passes through, stdout streams with CRLF -> LF. After
    `timeout` s returns TIMED_OUT; the command's own --timeout raises TimedOut here too. Either
    way the command goes on in the guest until someone ends it, as ssh is only the messenger."""
    proc = sp.Popen(ssh("-o", "BatchMode=yes") + [encoded(script_)], stdin=stdin, stdout=sp.PIPE)
    out, carry, limit = sys.stdout.buffer, b"", None
    try:
        with time_limit(timeout) as limit:                         # (also through a full pipe to a slow reader)
            while chunk := proc.stdout.read1(1 << 16):
                chunk = carry + chunk
                carry = b"\r" if chunk.endswith(b"\r") else b""   # a CRLF may straddle two chunks
                out.write(chunk[: len(chunk) - len(carry)].replace(b"\r\n", b"\n"))
                out.flush()
            out.write(carry)
            out.flush()
            return proc.wait(WAIT)                             # (stdout closed: it has ended)
    except sp.TimeoutExpired:
        proc.kill()
        return 255
    except BrokenPipeError:                                    # e.g. | head
        proc.kill()
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except TimedOut as e:
        proc.kill()
        if limit and e.limit is limit:
            return TIMED_OUT
        raise
    except BaseException:                                      # Ctrl-C
        proc.kill()
        raise


def terminal(argv: list[str]) -> int:
    """Run an ssh terminal session. Windows' pseudo-console (ConPTY) clears the screen
    (not the scrollback) and then draws at fixed positions, and it sets the window title.
    So first scroll what is on screen up into the scrollback - like `clear` on Linux:
    nothing is lost, the session's output stays after it - and save/restore the title
    (xterm's title stack; terminals without one ignore it)."""
    tty_out = sys.stdout.isatty()
    if tty_out:
        sys.stdout.write("\n" * os.get_terminal_size().lines + "\033[22;0t")
        sys.stdout.flush()
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)       # Ctrl-C belongs to the session
    try:
        return sp.call(argv)
    finally:
        signal.signal(signal.SIGINT, old)
        if tty_out:
            sys.stdout.write("\033[23;0t")
            sys.stdout.flush()


def tty(ssh, script_: str) -> int:
    """Run with a terminal. Windows' sshd drops the exit code of terminal sessions, so the
    command runs in a child PowerShell (an `exit N` in it can't skip what follows) and its
    exit code goes to a file that a second (multiplexed, cheap) call picks up."""
    rc = f"$env:TEMP\\winvm-{os.getpid()}.rc"
    child = base64.b64encode(script_.encode("utf-16-le")).decode()   # -EncodedCommand wants UTF-16
    terminal(ssh("-t") + [encoded(f"powershell -NoLogo -NoProfile -EncodedCommand {child}\n"
                                  f"Set-Content {rc} $LASTEXITCODE\nexit $LASTEXITCODE\n")])
    r = capture(ssh, f"Get-Content {rc}; Remove-Item {rc}", timeout=CLEANUP)
    return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 1


def capture(ssh, script_: str, timeout: int | None = None) -> sp.CompletedProcess:
    """Run quietly, collecting output (for winvm's own housekeeping in the guest). A guest can hang
    while SSH stays fine, so this always ends: after `timeout` s, or WAIT if no limit is running.
    Then the result is NO_ANSWER, with output saying so. If a limit is running it decides."""
    argv = ssh("-o", "BatchMode=yes") + [encoded(PROLOGUE + script_)]
    limit = timeout or (None if timing() else WAIT)
    try:
        return sp.run(argv, stdin=sp.DEVNULL, stdout=sp.PIPE, stderr=sp.STDOUT, text=True, timeout=limit)
    except sp.TimeoutExpired:
        return sp.CompletedProcess(argv, NO_ANSWER, f"no answer from the guest within {limit} s", "")
