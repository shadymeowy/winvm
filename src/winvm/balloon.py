"""Dynamic memory: keep a guest's RAM near what it uses, between `ram: {min, max}`.

The VM gets `max`; a small helper (this module, one detached process per instance, on its
own QMP socket) reads the guest's memory stats from the virtio-balloon driver and moves the
balloon: it grows the guest at once when free memory runs short and gives memory back to
the host only after it has been free for a while, in steps. A pagefile in the guest absorbs
spikes faster than the balloon can deflate (~240 MB/s). If the stats stop or the helper is
told to quit, it hands the guest its full `max` back.

  python -m winvm.balloon NAME      (started by `winvm up`; logs to <run dir>/balloon.log)
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from .config import Config
from .util import Chan, Fatal, meminfo, mib

POLL = 2            # s between looks at the guest (its stats update this often)
HOLD = 30           # s memory must stay free before the guest gives it back
STEP = 512          # MiB given back per step
STALE = 20          # s without fresh stats: give everything back


def ram_range(run: dict) -> tuple[int, int] | None:
    """`ram: {min, max}` -> (min, max) in MiB; None for a fixed `ram: 4G`."""
    ram = run.get("ram")
    if not isinstance(ram, dict):
        return None
    lo, hi = mib(ram.get("min") or ram.get("max")), mib(ram.get("max") or ram.get("min"))
    return (min(lo, hi), max(lo, hi))


def headroom(used: int) -> int:
    """Free memory the guest should have: 1 GiB, or a quarter of what it uses."""
    return max(1024, used // 4)


class Helper:
    def __init__(self, run_dir: Path, lo: int, hi: int, reserve: int):
        self.dir, self.lo, self.hi, self.reserve = run_dir, lo, hi, reserve
        self.qmp = Chan(run_dir / "qmp-balloon.sock", qmp=True)
        self.path = "/machine/peripheral/balloon0"
        self.qmp("qom-set", {"path": self.path, "property": "guest-stats-polling-interval", "value": POLL})
        self.free_since: float | None = None

    def actual(self) -> int:
        return self.qmp("query-balloon")["actual"] >> 20

    def set(self, target: int, why: str) -> None:
        self.qmp("balloon", {"value": target << 20})
        log(f"{why}: {target} MiB")

    def step(self) -> dict:
        st = self.qmp("qom-get", {"path": self.path, "property": "guest-stats"})
        actual = self.actual()
        if time.time() - st.get("last-update", 0) > STALE:            # guest's balloon service silent
            if actual < self.hi:
                self.set(self.hi, "no memory stats from the guest - giving it all back")
            return {"actual": actual, "state": "no stats"}
        avail = st["stats"]["stat-available-memory"] >> 20
        used = max(0, actual - avail)
        room = headroom(used)
        pressed = meminfo("MemAvailable") < 2 * self.reserve          # host short: give back sooner
        if pressed:
            room = max(512, used // 8)
        want = min(self.hi, max(self.lo, used + room))
        if avail < room * 3 // 4 and actual < self.hi:                 # short: grow at once, a bit more
            self.set(min(self.hi, used + room * 5 // 4), f"guest needs memory ({avail} MiB free)")
            self.free_since = None
        elif want < actual - STEP // 2:                               # spare: after HOLD, give it back
            self.free_since = self.free_since or time.time()          # step by step (one per poll)
            if time.time() - self.free_since >= (5 if pressed else HOLD):
                self.set(max(want, actual - STEP), "giving memory back" + (" (host is short)" if pressed else ""))
        else:
            self.free_since = None
        return {"actual": actual, "used": used, "free": avail, "state": "auto"}

    def run(self) -> None:
        status = self.dir / "balloon.json"
        while True:
            state = self.step()
            tmp = status.with_suffix(".tmp")
            tmp.write_text(json.dumps({**state, "min": self.lo, "max": self.hi, "time": int(time.time())}) + "\n")
            tmp.replace(status)
            time.sleep(POLL)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def main(name: str) -> int:
    cfg = Config()                      # same settings as the winvm that started us (env)
    from .vm import Instance
    vm = Instance(cfg, name)
    rng = ram_range(vm.settings)
    if not rng:
        return 0
    helper = None

    def quit_(*_):
        if helper:                       # don't leave the guest small without anyone to grow it
            try:
                helper.set(helper.hi, "stopping - guest gets all its memory back")
            except (Fatal, OSError):
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, quit_)
    signal.signal(signal.SIGINT, quit_)
    (vm.dir / "balloon.pid").write_text(f"{os.getpid()}\n")
    try:
        helper = Helper(vm.dir, *rng, mib(cfg.s.get("mem_reserve") or 0))
        log(f"auto memory for {name}: {rng[0]}-{rng[1]} MiB")
        helper.run()
    except (Fatal, OSError, ValueError, KeyError) as e:   # QEMU gone (socket closed) or no stats
        if vm.alive and helper:
            quit_()
        log(f"stopped: {e}")
    finally:
        (vm.dir / "balloon.pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
