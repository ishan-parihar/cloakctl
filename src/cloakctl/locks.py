"""Per-profile single-writer lock.

`open` on an already-live profile must re-attach, never launch a second
chrome (BF-fix-2's guarantee, enforced locally). The lockfile records
pid + startedAt; a live check verifies the pid actually exists and its
start time matches, so stale pid reuse cannot fool us.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .util import atomic_write_json


class ProfileInUseError(RuntimeError):
    def __init__(self, name: str, info: "LockInfo"):
        super().__init__(
            f"profile {name!r} is already live (pid {info.pid}, cdp port {info.cdp_port}); "
            f"use `cloakctl attach {name}` to re-attach"
        )
        self.info = info


@dataclass
class LockInfo:
    pid: int
    started_at: float  # /proc/<pid>/stat field 22 (jiffies since boot)
    cdp_port: int
    ws_endpoint: str | None = None
    remote: bool = False  # browser lives elsewhere; lock is attach-only


def _proc_start_time(pid: int) -> float:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # field 22 (starttime) sits after the comm field which may contain spaces
        after_comm = stat[stat.rindex(")") + 2 :].split()
        return float(after_comm[19])  # starttime is the 20th field after comm
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return -1.0


def read_lock(name: str) -> LockInfo | None:
    p = paths.lock_path(name)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return LockInfo(
            pid=int(raw["pid"]),
            started_at=float(raw.get("startedAt", -1.0)),
            cdp_port=int(raw["cdpPort"]),
            ws_endpoint=raw.get("wsEndpoint"),
            remote=bool(raw.get("remote", False)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def is_live(info: LockInfo) -> bool:
    """True when the locked pid exists AND its start time matches the record."""
    if info.remote:
        return remote_reachable(info.ws_endpoint)
    if info.pid <= 0:
        return False
    try:
        os.kill(info.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists but owned by someone else — still "live"
    if info.started_at <= 0:
        return True  # pre-starttime lock: trust pid liveness
    return _proc_start_time(info.pid) == info.started_at


def remote_reachable(ws_endpoint: str | None, timeout: float = 5.0) -> bool:
    """Liveness for remote browsers: a WS handshake + one round-trip.

    No pid exists to check across hosts, so reachability IS liveness.
    Short timeout — this runs inside status checks on every verb."""
    if not ws_endpoint:
        return False
    try:
        from .cdp import CdpClient

        with CdpClient(ws_endpoint, timeout=timeout) as cdp:
            cdp.list_targets()
        return True
    except Exception:
        return False


def acquire(name: str, pid: int, cdp_port: int, ws_endpoint: str | None,
            remote: bool = False) -> None:
    """Write the lock. Raises ProfileInUseError if a live lock already exists."""
    existing = read_lock(name)
    if existing and is_live(existing):
        raise ProfileInUseError(name, existing)
    paths.ensure_layout()
    atomic_write_json(paths.lock_path(name),
                        {"pid": pid,
                         "startedAt": _proc_start_time(pid) if not remote else 0.0,
                         "cdpPort": cdp_port,
                         "wsEndpoint": ws_endpoint,
                         "remote": remote})


def release(name: str, *, expect_pid: int | None = None) -> bool:
    p = paths.lock_path(name)
    if not p.exists():
        return False
    if expect_pid is not None:
        info = read_lock(name)
        if info and info.pid != expect_pid:
            return False  # not ours; leave it alone
    p.unlink(missing_ok=True)
    return True


def find_stale() -> list[str]:
    """Profiles whose lockfiles point at dead processes (for `doctor`)."""
    stale = []
    if not paths.RUNTIME_DIR.exists():
        return stale
    for p in paths.RUNTIME_DIR.glob("lock.*.json"):
        name = p.name[len("lock.") : -len(".json")]
        info = read_lock(name)
        if info is None or not is_live(info):
            stale.append(name)
    return stale
