"""Browser lifecycle: launch, attach, close, status.

Launches the cloakbrowser Chromium binary directly with a persistent
--user-data-dir per profile and --remote-debugging-port=0; the actual port is
read back from DevToolsActivePort in the profile dir (no port guessing).
The browser is detached (setsid) so CLI exits never kill it; the lockfile in
runtime/ is the single-writer contract.
"""

from __future__ import annotations

import functools
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

from . import paths, util
from .cdp import CdpClient, http_json
from .locks import LockInfo, ProfileInUseError, is_live, read_lock, release as release_lock
from .util import jar_fingerprint

CANDIDATE_BINARIES = (
    "cloakbrowser",
    "chromium",
    "chrome",
    "google-chrome",
    "brave",
    "brave-browser",
    "brave-origin",
    "microsoft-edge",
)


def find_browser_binary() -> str:
    """CloakBrowser first (stealth patches), then any stock Chromium."""
    env_override = os.environ.get("CLOAKCTL_BROWSER")
    if env_override:
        p = which(env_override)
        if p:
            return p
        raise RuntimeError(
            f"CLOAKCTL_BROWSER={env_override!r} not found on PATH")
    for b in CANDIDATE_BINARIES:
        p = which(b)
        if p:
            return p
    found = _search_home_cloakbrowser()
    if found:
        return found
    raise RuntimeError(
        "no browser binary found; install cloakbrowser or set CLOAKCTL_BROWSER"
    )


@functools.lru_cache(maxsize=1)
def _search_home_cloakbrowser() -> str | None:
    """Cached: walking ~/.cloakbrowser on every open/doctor is seconds."""
    # cloakbrowser npm package installs its binary under ~/.cloakbrowser
    home_cb = Path.home() / ".cloakbrowser"
    if home_cb.exists():
        for binpath in sorted(home_cb.rglob("chrome")):
            if binpath.is_file() and os.access(binpath, os.X_OK):
                return str(binpath)
    return None


def _wait_devtools(chrome_dir: Path, timeout: float = 20.0) -> tuple[int, str]:
    """Poll DevToolsActivePort until it appears; return (port, ws browser path)."""
    deadline = time.monotonic() + timeout
    devtools = chrome_dir / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if devtools.exists():
            try:
                lines = devtools.read_text().split()
                port = int(lines[0]) if lines else 0
                ws_path = lines[1] if len(lines) >= 2 else ""
            except (ValueError, IndexError, OSError):
                time.sleep(0.15)  # half-written file; keep polling
                continue
            if port > 0 and ws_path:
                try:
                    version = http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    ws = version.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{port}{ws_path}"
                    return port, ws
                except Exception:
                    pass  # socket not accepting yet; keep polling
        time.sleep(0.15)
    raise TimeoutError(f"DevTools endpoint did not come up within {timeout:.0f}s")


def _launch(name: str, headed: bool, extra_args: list[str]) -> tuple[int, int, str]:
    binary = find_browser_binary()
    cdir = paths.chrome_dir(name)
    cdir.mkdir(parents=True, exist_ok=True)
    args = [
        binary,
        f"--user-data-dir={cdir}",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-dev-shm-usage" if sys.platform == "linux" else "--disable-gpu",
    ]
    if not headed:
        args.append("--headless=new")
    args.extend(extra_args)

    # setsid: outlive the CLI process; stdout/stderr to the profile dir for triage.
    # The child inherits the fd; parent closes its copy immediately.
    log_fd = os.open(cdir / "chrome.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(
            args,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            close_fds=False,
        )
    finally:
        os.close(log_fd)
    try:
        port, ws = _wait_devtools(cdir)
    except Exception:
        _kill_tree(proc.pid)  # terminate() alone orphans renderers
        raise
    return proc.pid, port, ws


def _kill_tree(pid: int, grace: float = 3.0) -> None:
    """SIGTERM then SIGKILL a whole process group (launched with setsid)."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def _mem_available_mb() -> float | None:
    """Host available RAM (Linux /proc); None when unreadable."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return None


def _rss_tree_mb(pid: int) -> float:
    """RSS of pid + all descendants (Linux /proc)."""
    import os as _os

    try:
        page = _os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0.0

    def stat_parts(p: int) -> list[str] | None:
        try:
            data = open(f"/proc/{p}/stat").read()
        except Exception:
            return None
        tail = data[data.rfind(")") + 2:].split()
        return tail

    def rss_mb(p: int) -> float:
        parts = stat_parts(p)
        if not parts or len(parts) < 22:
            return 0.0
        try:
            return int(parts[21]) * page / 1024 / 1024
        except Exception:
            return 0.0

    total = rss_mb(pid)
    try:
        for entry in _os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == pid:
                continue
            parts = stat_parts(int(entry))
            if parts and len(parts) > 3:
                try:
                    if int(parts[1]) == pid:
                        total += _rss_tree_mb(int(entry))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _sweep_profile_procs(name: str) -> int:
    """SIGKILL any leftover processes bound to this profile's chrome dir.

    A violently-killed browser (SIGKILL/crash/OOM) orphans renderer children
    outside the new process group; close/open only signal the lock pid.
    Matching is exact on --user-data-dir so other profiles are untouched.
    Returns the kill count.
    """
    import os as _os

    marker = f"--user-data-dir={paths.chrome_dir(name)}"
    me = _os.getpid()
    killed = 0
    for entry in _os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        if marker in cmd:
            try:
                _os.kill(int(entry), signal.SIGKILL)
                killed += 1
            except Exception:
                pass
    return killed


def open_profile(name: str, *, headed: bool = False,
                 extra_args: list[str] | None = None,
                 endpoint: str | None = None) -> dict:
    """Launch or re-attach. Idempotent: a live profile is never launched twice.

    `endpoint` (or CLOAKCTL_CDP_URL) attaches a browser that lives on
    ANOTHER host (VPS-side CLI, local browser over a tunnel): no launch,
    no signals, no DevToolsActivePort — reachability is liveness."""
    endpoint = endpoint or os.environ.get("CLOAKCTL_CDP_URL")
    if endpoint:
        return _open_remote(name, endpoint)
    paths.ensure_layout()
    existing_pre = read_lock(name)
    if not (existing_pre and is_live(existing_pre)):
        try:
            floor = float(os.environ.get("CLOAKCTL_MIN_MEM_MB", "400"))
        except ValueError:
            floor = 400.0
        avail = _mem_available_mb()
        if avail is not None and avail < floor:
            print(f"warning: only {avail:.0f}MB RAM available; a profile browser needs ~450MB "
                  f"(tune with CLOAKCTL_MIN_MEM_MB)", file=sys.stderr)
    if not paths.profile_dir(name).exists():
        paths.profile_dir(name).mkdir(parents=True, exist_ok=True)

    existing = read_lock(name)
    if existing and is_live(existing):
        return {
            "profile": name,
            "pid": existing.pid,
            "cdpPort": existing.cdp_port,
            "wsEndpoint": existing.ws_endpoint,
            "reattached": True,
        }
    if existing:
        release_lock(name)  # stale lock from a crashed run
        _sweep_profile_procs(name)  # reap orphaned renderers from the crash

    pid, port, ws = _launch(name, headed=headed, extra_args=extra_args or [])
    from . import locks as _locks

    try:
        _locks.acquire(name, pid=pid, cdp_port=port, ws_endpoint=ws)
    except ProfileInUseError:
        # Raced with another launcher: ours lost; kill our whole tree
        # (kill(pid) alone would orphan our renderers), report theirs.
        _kill_tree(pid)
        raise
    paths.touch_last_used(name)
    return {"profile": name, "pid": pid, "cdpPort": port, "wsEndpoint": ws, "reattached": False}


def _open_remote(name: str, endpoint: str) -> dict:
    """Attach-only open against a remote CDP endpoint (tunnel)."""
    from urllib.parse import urlparse

    from . import locks as _locks

    paths.ensure_layout()
    if not paths.profile_dir(name).exists():
        paths.profile_dir(name).mkdir(parents=True, exist_ok=True)
    existing = read_lock(name)
    if existing and is_live(existing):
        return {"profile": name, "pid": existing.pid,
                "cdpPort": existing.cdp_port,
                "wsEndpoint": existing.ws_endpoint,
                "reattached": True, "remote": existing.remote}
    host = (urlparse(endpoint).hostname or "")
    if urlparse(endpoint).scheme == "ws" and host not in (
            "localhost", "127.0.0.1", "::1"):
        print(f"warning: remote endpoint {endpoint!r} is unencrypted ws://; "
              f"serve it over wss:// (e.g. Cloudflare Access)",
              file=sys.stderr)
    try:
        with CdpClient(endpoint, timeout=10) as cdp:
            ver = cdp.call("Browser.getVersion")
    except Exception as exc:
        raise RuntimeError(f"remote endpoint unreachable: {exc}")
    try:
        _locks.acquire(name, pid=0, cdp_port=0, ws_endpoint=endpoint,
                       remote=True)
    except ProfileInUseError:
        existing = read_lock(name)  # raced a parallel attach; theirs wins
        return {"profile": name, "pid": existing.pid if existing else 0,
                "cdpPort": existing.cdp_port if existing else 0,
                "wsEndpoint": existing.ws_endpoint if existing else endpoint,
                "reattached": True, "remote": True}
    paths.touch_last_used(name)
    return {"profile": name, "pid": 0, "cdpPort": 0,
            "wsEndpoint": endpoint, "reattached": False, "remote": True,
            "browser": ver.get("product")}


def close_profile(name: str, *, timeout: float = 15.0) -> bool:
    """Graceful close: SIGTERM the process group, then SIGKILL. Flushes cookies.

    Remote profiles detach (the browser outlives us on its own host)."""
    info = read_lock(name)
    if info is not None and info.remote:
        release_lock(name)  # detach only: no signals across hosts
        paths.touch_last_used(name)
        return True
    if info is None:
        _sweep_profile_procs(name)  # lockless orphans (crashed runs)
        return False
    if not is_live(info):
        release_lock(name)
        _sweep_profile_procs(name)  # main is dead; renderers may not be
        return False
    try:
        os.killpg(info.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(info.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(info.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    release_lock(name)
    _sweep_profile_procs(name)  # renderers that outlived the main pid
    paths.touch_last_used(name)
    return True


_AUDIT_MAX_LINES = 2000  # trail rotates: compounding never grows it


def audit_log(profile: str, cmd: str, rc: int, ms: int) -> None:
    """Append-only per-profile command trail (neo audit parity, secret-safe:
    command name + outcome only, never argv). Best-effort, never raises."""
    try:
        name = paths.check_name(profile)
        paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        p = paths.RUNTIME_DIR / f"audit.{name}.jsonl"
        with open(p, "a") as f:
            f.write(json.dumps({"ts": util.now_iso(), "cmd": cmd, "rc": rc,
                                "ms": ms}) + "\n")
        if p.stat().st_size > 1_000_000:  # rotate cheaply, keep the tail
            lines = p.read_text().splitlines()[-_AUDIT_MAX_LINES:]
            p.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def read_audit(profile: str, limit: int = 20) -> dict:
    p = paths.RUNTIME_DIR / f"audit.{paths.check_name(profile)}.jsonl"
    rows = []
    try:
        lines = p.read_text().splitlines()[-limit:]
        rows = [json.loads(l) for l in lines if l.strip()]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"profile": profile, "commands": rows, "shown": len(rows)}


def status_profile(name: str) -> dict:
    info = read_lock(name)
    live = bool(info and is_live(info))
    out: dict = {"profile": name, "live": live}
    if not paths.profile_dir(name).exists():
        out["exists"] = False
        return out
    out["exists"] = True
    meta = paths.load_meta(name)
    out["created"] = meta.get("created")
    out["lastUsed"] = meta.get("lastUsed")
    if live and info is not None:
        out["remote"] = bool(info.remote)
        out["wsEndpoint"] = info.ws_endpoint
        if not info.remote:
            out["pid"] = info.pid
            out["cdpPort"] = info.cdp_port
            try:
                version = http_json(f"http://127.0.0.1:{info.cdp_port}/json/version", timeout=3)
                out["browser"] = version.get("Browser")
            except Exception:
                out["browser"] = None
        else:
            try:
                with CdpClient(info.ws_endpoint, timeout=10) as cdp:
                    ver = cdp.call("Browser.getVersion")
                out["browser"] = ver.get("product")
            except Exception:
                out["browser"] = None
        try:
            endpoint = info.ws_endpoint or f"ws://127.0.0.1:{info.cdp_port}/devtools/browser"
            with CdpClient(endpoint) as cdp:
                jar = cdp.get_cookies()
            out["cookieCount"] = len(jar)
            out["jarFingerprint"] = jar_fingerprint(jar)
        except Exception as exc:  # cookie read is best-effort
            out["cookieError"] = str(exc)
    return out


def list_profiles() -> list[dict]:
    paths.ensure_layout()
    out = []
    if not paths.PROFILES_DIR.exists():
        return out
    for d in sorted(paths.PROFILES_DIR.iterdir()):
        if d.is_dir():
            out.append(status_profile(d.name))
    return out


def doctor() -> dict:
    binary = None
    try:
        binary = find_browser_binary()
    except RuntimeError as exc:
        binary_error = str(exc)
    else:
        binary_error = None
    stale = []
    try:
        from .locks import find_stale

        stale = find_stale()
    except Exception:
        pass
    profiles = []
    if paths.PROFILES_DIR.exists():
        for d in sorted(paths.PROFILES_DIR.iterdir()):
            if not d.is_dir():
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            profiles.append({"profile": d.name, "size": size})
    mem_profiles = []
    for d in (sorted(paths.PROFILES_DIR.iterdir()) if paths.PROFILES_DIR.exists() else []):
        if not d.is_dir():
            continue
        info = read_lock(d.name)
        if info and is_live(info):
            # Remote browsers have no local pid: rss would walk pid 0 and
            # sum half the machine. Report None, never a bogus number.
            rss = (None if (info.remote or info.pid <= 0) else
                   round(_rss_tree_mb(info.pid), 1))
            mem_profiles.append({"profile": d.name, "pid": info.pid,
                                 "rssMB": rss,
                                 "remote": bool(info.remote)})
    return {
        "browserBinary": binary,
        "binaryError": binary_error,
        "staleLocks": stale,
        "profiles": profiles,
        "stateDir": str(paths.HOME_STATE),
        "memory": {"availableMB": _mem_available_mb(), "liveProfiles": mem_profiles},
        "workflows": workflow_hints(),
    }


_EXTRACT_VERBS = {"snapshot", "read", "grep", "act", "run", "exec"}


def workflow_hints() -> dict:
    """Evolution-loop signals: stale units + repeated un-saved sequences.

    Suggest-only: counts repeated extract-verb audit traffic with no matching
    workflow/skill runs, and flags units with zero clean runs past STALE_DAYS.
    """
    try:
        from . import skills as _skills
        from . import workflows as _wf
    except Exception:
        return {}
    try:
        wfs = _wf.list_workflows()["workflows"]
    except Exception:
        wfs = []
    stale_wf = [w["workflow"] for w in wfs if w.get("stale")]
    try:
        sks = _skills.list_skills()["skills"]
    except Exception:
        sks = []
    stale_sk = [s["skill"] for s in sks
                 if not s.get("runs") and s.get("ageDays", 0) > 30]
    extract_runs = 0
    saved_runs = 0
    if paths.RUNTIME_DIR.exists():
        import json as _json
        for f in paths.RUNTIME_DIR.glob("audit.*.jsonl"):
            try:
                for line in f.read_text().splitlines()[-100:]:
                    try:
                        cmd = _json.loads(line).get("cmd", "")
                    except Exception:
                        continue
                    if cmd in _EXTRACT_VERBS:
                        extract_runs += 1
                    if cmd in ("skill", "wf"):
                        saved_runs += 1
            except Exception:
                continue
    hint = ""
    if extract_runs >= 5 and saved_runs == 0:
        hint = (f"{extract_runs} extract-verb runs with no skill/wf runs: "
                "consider `skill save` for the repeated sequence, then "
                "`skill promote` to harden it into a workflow.")
    return {"workflows": len(wfs), "staleWorkflows": stale_wf,
            "staleSkills": stale_sk, "extractVerbRuns": extract_runs,
            "savedUnitRuns": saved_runs, "hint": hint}


def remove_profile(name: str, *, force: bool = False) -> bool:
    info = read_lock(name)
    if info and is_live(info):
        if not force:
            raise RuntimeError(f"profile {name!r} is live; close it first (or pass --force)")
        close_profile(name)
    import shutil

    pd = paths.profile_dir(name)
    if not pd.exists():
        return False
    shutil.rmtree(pd)
    release_lock(name)
    return True
