"""Running PowerShell in the guest over SSH, so that it behaves like a local command:
exit code, stdin, streaming output with Unix line endings."""
from __future__ import annotations

import base64
import os
import subprocess as sp
import sys

PROLOGUE = r"""$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
$OutputEncoding = [Text.UTF8Encoding]::new($false)
function stdin { while ($null -ne ($l = [Console]::In.ReadLine())) { $l } }
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
    return f"iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"


def command_line(words: list[str]) -> str:
    """`winvm sh` words -> one PowerShell line: a single word is code as is; otherwise empty
    words and words containing whitespace are quoted so they stay one argument."""
    if len(words) == 1:
        return words[0]
    return " ".join(quote(w) if not w or any(c.isspace() for c in w) else w for w in words)


def script(line: str, cwd: str | None = None, strict=False) -> str:
    """Full guest script for one command line; strict: a cmdlet error stops it (build steps)."""
    head = PROLOGUE + ("$ErrorActionPreference = 'Stop'\n" if strict else "")
    cd = f"Set-Location -LiteralPath {quote(cwd)}\n" if cwd else ""
    if strict:   # a thrown error: print just its message and exit 1
        line = ("$global:LASTEXITCODE = 0\ntry {\n" + line +
                "\n} catch { [Console]::Error.WriteLine(\"error: $($_.Exception.Message)\"); exit 1 }")
    return head + cd + line + EPILOGUE + "exit $rc\n"


# `ssh` below is a function: ssh(*options) -> ssh argv up to and including the host name

def pipe(ssh, script_: str, stdin=None) -> int:
    """Run without a terminal: stdin passes through, stdout streams with CRLF -> LF."""
    proc = sp.Popen(ssh("-o", "BatchMode=yes") + [encoded(script_)], stdin=stdin, stdout=sp.PIPE)
    out, carry = sys.stdout.buffer, b""
    try:
        while chunk := proc.stdout.read1(1 << 16):
            chunk = carry + chunk
            carry = b"\r" if chunk.endswith(b"\r") else b""   # a CRLF may straddle two chunks
            out.write(chunk[: len(chunk) - len(carry)].replace(b"\r\n", b"\n"))
            out.flush()
        out.write(carry)
        out.flush()
    except BrokenPipeError:                                    # e.g. | head
        proc.kill()
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    return proc.wait()


def tty(ssh, script_: str) -> int:
    """Run with a terminal. Windows' sshd drops the exit code of terminal sessions, so the
    guest leaves it in a file that a second (multiplexed, cheap) call picks up."""
    rc = f"$env:TEMP\\winvm-{os.getpid()}.rc"
    body = script_.removesuffix("exit $rc\n")
    sp.run(ssh("-t") + [encoded(f"{body}Set-Content {rc} $rc\nexit $rc\n")])
    r = sp.run(ssh("-o", "BatchMode=yes") + [encoded(f"Get-Content {rc}; Remove-Item {rc}")],
               stdin=sp.DEVNULL, stdout=sp.PIPE, stderr=sp.DEVNULL, text=True)
    return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 1


def capture(ssh, script_: str) -> sp.CompletedProcess:
    """Run quietly, collecting output (for winvm's own housekeeping in the guest)."""
    return sp.run(ssh("-o", "BatchMode=yes") + [encoded(PROLOGUE + script_)],
                  stdin=sp.DEVNULL, stdout=sp.PIPE, stderr=sp.STDOUT, text=True)
