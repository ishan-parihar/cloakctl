"""Browser lifecycle: launch, attach, close, status — across two engines.

Engines (default: obscura):

- **obscura** — a standalone stealth headless browser with its own CDP
  server. cloakctl starts a *keeper* daemon per profile that holds the
  engine process and the CDP session; verbs reach it over a local unix
  socket (see keeper.py). Page state persists for the profile lifetime;
  cookies persist on disk via `--storage-dir`.
- **cloakbrowser** (opt-in) — a Chromium-family binary launched with a
  persistent `--user-data-dir` per profile and `--remote-debugging-port=0`;
  the actual port is read back from DevToolsActivePort (no port guessing).
  Detached (setsid) so CLI exits never kill it.

Either way the lockfile in runtime/ is the single-writer contract; the
locked pid is the process owning the browser session (keeper or browser).
"""

from __future__ import annotations

import functools
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

from . import paths, util
from .cdp import CdpClient, http_json
from .engines import (ENGINE_CB, ENGINE_OBScura, find_cloakbrowser,
                      find_obscura, resolve_engine)
from .keeper import KeeperClient, wait_engine_port, wait_keeper
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


def _find_chromium_binary() -> str | None:
    """CloakBrowser first (stealth patches), then any stock Chromium."""
    env_override = os.environ.get("CLOAKCTL_BROWSER")
    if env_override:
        return which(env_override)
    for b in CANDIDATE_BINARIES:
        p = which(b)
        if p:
            return p
    return _search_home_cloakbrowser()


def find_browser_binary() -> str:
    """Chromium binary or raise — used by the cloakbrowser engine + doctor."""
    p = _find_chromium_binary()
    if p:
        return p
    raise RuntimeError(
        "no browser binary found; install cloakbrowser or set CLOAKCTL_BROWSER")


@functools.lru_cache(maxsize=1)
def _search_home_cloakbrowser() -> str | None:
    """Cached: walking ~/.cloakbrowser on every open/doctor is seconds."""
    # cloakbrowser npm package installs its binary under ~/.cloakbrowser;
    # resolve via $HOME (expanduser can disagree under systemd/cron).
    home_cb = (Path(os.environ.get("HOME") or Path.home())) / ".cloakbrowser"
    if home_cb.exists():
        for binpath in sorted(home_cb.rglob("chrome")):
            if binpath.is_file() and os.access(binpath, os.X_OK):
                return str(binpath)
    return None


# --- obscura engine -------------------------------------------------------------


def _obscura_engine_args(headed: bool, extra_args: list[str]) -> list[str]:
    """`--browser-arg` passthrough for `obscura serve`.

    e.g. `--browser-arg=--allow-file-access`, `--browser-arg=--proxy`,
    `--browser-arg=--v8-flags --browser-arg=--max-old-space-size=8192`.
    `--stealth` has a first-class `open --stealth` flag instead.
    obscura has no window: `--headed` is accepted and noted as a no-op.
    """
    return [a for a in extra_args if a]


def open_profile(name: str, *, headed: bool = False,
                 extra_args: list[str] | None = None,
                 endpoint: str | None = None,
                 engine: str | None = None,
                 stealth: bool = False) -> dict:
    """Launch or re-attach. Idempotent: a live profile is never launched twice.

    Engine resolution is sticky per profile: --engine > CLOAKCTL_ENGINE >
    profile meta > obscura when installed, else cloakbrowser.

    `endpoint` (or CLOAKCTL_CDP_URL) attaches a browser that lives on
    ANOTHER host (VPS-side CLI, local browser over a tunnel): no launch,
    no signals — reachability is liveness. Remote mode is a cloakbrowser
    (Chromium-family) feature: the remote browser must be one, because
    obscura's CDP is per-connection isolated — a remote attach would see
    an empty session (no cookies, no page)."""
    # An EXPLICITLY empty endpoint (or empty env var) must NEVER fall
    # through to a local launch: the caller asked for remote mode, and a
    # silent local launch would put a browser where none should exist
    # (VPS-side CLI). Check the raw arg BEFORE the `or` — an empty string
    # is falsy, so `endpoint or env` would otherwise swallow it.
    if endpoint is not None and not endpoint.strip():
        raise RuntimeError(
            "--endpoint was empty; set a ws:// or wss:// CDP URL "
            "(or unset it to launch a local browser)")
    endpoint = endpoint or os.environ.get("CLOAKCTL_CDP_URL")
    if endpoint is not None and not endpoint.strip():
        raise RuntimeError(
            "CLOAKCTL_CDP_URL was empty; set a ws:// or wss:// CDP URL "
            "(or unset it to launch a local browser)")
    if endpoint:
        return _open_remote(name, endpoint)
    paths.ensure_layout()
    # Local launches require an EXISTING profile: a typo in `open profilen`
    # must be a clean JSON error, never a surprise browser launch. (Remote
    # attach creates its attach-only record explicitly instead.)
    if not paths.profile_dir(name).exists():
        raise RuntimeError(
            f"profile {name!r} does not exist; create it first with "
            f"`cloakctl profiles create {name}`")

    existing = read_lock(name)
    if existing and is_live(existing):
        out = {
            "profile": name,
            "pid": existing.pid,
            "cdpPort": existing.cdp_port,
            "wsEndpoint": existing.ws_endpoint,
            "reattached": True,
            "engine": existing.engine,
        }
        if existing.engine == ENGINE_OBScura:
            try:
                with KeeperClient(name) as kc:
                    st = kc.status()
                out["cdpPort"] = st.get("cdpPort", existing.cdp_port)
                out["url"] = st.get("url", "")
            except Exception:
                pass  # status probe is best-effort
        return out
    if existing:
        release_lock(name)  # stale lock from a crashed run
        _sweep_profile_procs(name)  # reap orphans from the crash

    meta = paths.load_meta(name)
    resolved = resolve_engine(engine, meta)
    _guard_memory(resolved)  # refuse launches the host cannot afford
    if resolved == ENGINE_OBScura and find_obscura() is None:
        raise RuntimeError(
            "engine 'obscura' requested but no obscura binary found; "
            "install it with `./install.sh --obscura-only` (downloads the "
            "binary from GitHub releases) or `open --engine cloakbrowser`")
    if resolved == ENGINE_CB and find_browser_binary() is None:
        raise RuntimeError(
            "engine 'cloakbrowser' requested but no Chromium-family binary "
            "found; install cloakbrowser/chromium/chrome or `open --engine obscura`")
    meta["engine"] = resolved  # sticky: later verbs and status agree
    paths.save_meta(name, meta)

    if resolved == ENGINE_OBScura:
        return _open_obscura(name, headed=headed,
                             extra_args=extra_args or [], stealth=stealth)
    return _launch_chromium(name, headed=headed, extra_args=extra_args or [])


def _open_obscura(name: str, *, headed: bool, extra_args: list[str],
                  stealth: bool) -> dict:
    """Start the keeper (which supervises `obscura serve`) and lock it."""
    from . import keeper as keeper_mod
    from . import locks as _locks

    engine_bin = find_obscura()
    pid = keeper_mod.spawn(name, engine_bin=engine_bin, stealth=stealth,
                           engine_args=_obscura_engine_args(headed, extra_args))
    try:
        if not wait_keeper(name, timeout=CONNECT_TIMEOUT):
            raise RuntimeError(_keeper_startup_error(name))
        cdp_port = wait_engine_port(name, timeout=ENGINE_WAIT_TIMEOUT)
    except Exception:
        _kill_tree(pid)  # take the failed keeper + engine down with us
        raise
    try:
        _locks.acquire(name, pid=pid, cdp_port=cdp_port, ws_endpoint=None,
                       remote=False, engine=ENGINE_OBScura, headed=headed)
    except ProfileInUseError:
        # Raced a parallel launcher: theirs wins; take ours down.
        _kill_tree(pid)
        raise
    paths.touch_last_used(name)
    try:
        with KeeperClient(name) as kc:
            st = kc.status()
            url = st.get("url", "about:blank")
    except Exception:
        url = "about:blank"
    out = {"profile": name, "pid": pid, "cdpPort": cdp_port,
           "wsEndpoint": None, "reattached": False,
           "engine": ENGINE_OBScura, "url": url}
    if stealth:
        out["stealth"] = True
    if headed:
        out["note"] = "obscura is always headless; --headed ignored"
    return out


CONNECT_TIMEOUT = 30.0
ENGINE_WAIT_TIMEOUT = 45.0


def _keeper_startup_error(name: str) -> str:
    log = paths.RUNTIME_DIR / f"keeper.{name}.log"
    tail = ""
    try:
        tail = log.read_text(errors="replace")[-400:].strip()
    except OSError:
        pass
    msg = f"obscura keeper did not come up for {name!r}"
    if tail:
        msg += f": {tail}"
    return msg


def _launch_chromium(name: str, headed: bool, extra_args: list[str]) -> dict:
    """The original cloakbrowser path (opt-in engine)."""
    from . import locks as _locks

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
    try:
        _locks.acquire(name, pid=proc.pid, cdp_port=port, ws_endpoint=ws,
                       remote=False, engine=ENGINE_CB, headed=headed)
    except ProfileInUseError:
        # Raced another launcher: ours lost; kill our whole tree
        # (kill(pid) alone would orphan our renderers), report theirs.
        _kill_tree(proc.pid)
        raise
    paths.touch_last_used(name)
    return {"profile": name, "pid": proc.pid, "cdpPort": port,
            "wsEndpoint": ws, "reattached": False, "engine": ENGINE_CB}


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


def _min_mem_mb() -> int:
    """Launch RAM floor; CLOAKCTL_MIN_MEM_MB overrides the default."""
    raw = os.environ.get("CLOAKCTL_MIN_MEM_MB", "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass  # garbage value: fall through to the default
    return 400


def _guard_memory(engine: str) -> None:
    """Refuse launches when free RAM cannot afford another engine instance.

    Best-effort: unreadable /proc/meminfo never blocks a launch. Override or
    disable with CLOAKCTL_MIN_MEM_MB (0 disables the guard)."""
    floor = _min_mem_mb()
    if floor <= 0:
        return
    avail = _mem_available_mb()
    if avail is None or avail >= floor:
        return
    need = 1200 if engine == ENGINE_CB else 100
    raise RuntimeError(
        f"only {avail:.0f} MB RAM available (floor {floor} MB); an {engine} "
        f"profile needs ~{need} MB — free memory, lower CLOAKCTL_MIN_MEM_MB, "
        "or close other profiles (`cloakctl status`)"
    )


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
    """SIGKILL any leftover processes bound to this profile.

    A violently-killed browser (SIGKILL/crash/OOM) orphans renderer children
    outside the new process group; close/open only signal the lock pid.
    Matching is exact on engine markers so other profiles are untouched:
    - cloakbrowser: `--user-data-dir=<profile chrome dir>`
    - obscura: the profile's `--storage-dir=<chrome dir>` (keeper + serve)
    - the keeper itself: `--home <state dir> --profile <name>` (its own
      cmdline carries neither engine marker; restated at spawn time)
    Returns the kill count."""
    import os as _os

    cdir = str(paths.chrome_dir(name))
    home = str(paths.HOME_STATE)
    markers = (f"--user-data-dir={cdir}", f"--storage-dir={cdir}")
    keeper_marker = ("-m", "cloakctl.keeper")
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
        is_keeper = (keeper_marker[0] in cmd and keeper_marker[1] in cmd)
        if any(m in cmd for m in markers) or (
                is_keeper and f"--profile {name} " in cmd
                and f"--home {home} " in cmd):
            try:
                _os.kill(int(entry), signal.SIGKILL)
                killed += 1
            except Exception:
                pass
    return killed


# --- remote attach --------------------------------------------------------------


def _open_remote(name: str, endpoint: str) -> dict:
    """Attach-only open against a remote CDP endpoint (tunnel).

    The endpoint must front a Chromium-family browser (cloakbrowser engine
    on the host): obscura's CDP is per-connection isolated, so an attach
    would observe an empty session. Warned on attach, documented in the
    remote-vps reference."""
    from urllib.parse import urlparse

    import re as _re

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
    # Soft honesty check: obscura reports a degenerate product string
    # ("Chrome/145.0.0.0") — real Chromium builds always carry a build id.
    # A tunneled obscura serve accepts the attach but every verb would run
    # in an EMPTY session (its CDP is per-connection): warn loudly.
    product = str(ver.get("product") or "")
    if re.fullmatch(r"Chrome/\d+\.0\.0\.0", product):
        print(
            "warning: the remote browser looks like obscura (product "
            f"{product!r}). obscura's CDP is per-connection isolated, so "
            "this attach sees an EMPTY session — no cookies, no page state. "
            "Remote mode needs a Chromium-family browser on the host "
            "(`open --engine cloakbrowser` there, then tunnel its ws "
            "endpoint).", file=sys.stderr)
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
            "browser": ver.get("product"),
            "engine": ENGINE_CB}


# --- close ------------------------------------------------------------------


def close_profile(name: str, *, timeout: float = 15.0) -> bool:
    """Graceful close: SIGTERM the process group, then SIGKILL. Flushes cookies.

    Remote profiles detach (the browser outlives us on its own host).
    obscura profiles: ask the keeper to shut down first (flushes engine
    state), then make sure the whole process group is gone."""
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
    if info.engine == ENGINE_OBScura:
        try:
            with KeeperClient(name, timeout=5.0) as kc:
                kc.shutdown()
        except Exception:
            pass  # fall through to signals
        # The shutdown op may be wedged behind a stuck CDP op (runaway page):
        # never wait on grace — kill the process group immediately after the
        # shutdown request (SIGTERM to the group, then SIGKILL below if needed).
    try:
        os.killpg(info.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    def _really_dead(p: int) -> bool:
        # os.kill succeeds on zombies: a dead-but-unreaped keeper must count
        # as dead, or close waits out the whole grace period (15s).
        if _is_zombie(p):
            return True
        try:
            os.kill(p, 0)
        except ProcessLookupError:
            return True
        return False

    from .locks import _is_zombie  # local import to avoid cycles
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _really_dead(info.pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(info.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    release_lock(name)
    _sweep_profile_procs(name)  # renderers/engines that outlived the lock pid
    from .keeper import keeper_sock_path
    try:
        keeper_sock_path(name).unlink(missing_ok=True)
    except OSError:
        pass
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
        out["engine"] = info.engine
        out["wsEndpoint"] = info.ws_endpoint
        if not info.remote:
            out["pid"] = info.pid
            out["cdpPort"] = info.cdp_port
            if info.engine == ENGINE_OBScura:
                # The keeper fronts the engine; cdpPort is `obscura serve`'s
                # local loopback port. Status + cookies go through the keeper.
                out["headed"] = False
                try:
                    from .keeper import KeeperClient as _KC
                    with _KC(name, timeout=10.0) as kc:
                        st = kc.status()
                    out["cdpPort"] = st.get("cdpPort", info.cdp_port)
                    out["url"] = st.get("url", "")
                    out["engineUptimeSec"] = st.get("uptimeSec")
                except Exception as exc:
                    out["keeperError"] = str(exc)
                try:
                    with _KC(name, timeout=15.0) as kc:
                        jar = kc.call("Storage.getCookies").get("cookies", [])
                    out["cookieCount"] = len(jar)
                    out["jarFingerprint"] = jar_fingerprint(jar)
                except Exception as exc:
                    out["cookieError"] = str(exc)
                return out
            try:
                version = http_json(
                    f"http://127.0.0.1:{info.cdp_port}/json/version", timeout=3)
                out["browser"] = version.get("Browser")
            except Exception:
                out["browser"] = None
        else:
            # Remote is Chromium-family by contract (obscura cannot serve
            # remote mode: per-connection CDP). Report the product, and if
            # it still looks like obscura, say so instead of mislabeling.
            try:
                with CdpClient(info.ws_endpoint, timeout=10) as cdp:
                    ver = cdp.call("Browser.getVersion")
                product = str(ver.get("product") or "")
                if re.fullmatch(r"Chrome/\d+\.0\.0\.0", product):
                    out["browser"] = None
                    out["remoteWarning"] = (
                        "endpoint appears to be obscura; remote mode requires "
                        "a Chromium-family browser — verbs will see an empty "
                        "session")
                else:
                    out["browser"] = product
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
    from .engines import engine_info

    engines = engine_info()
    stale = []
    try:
        from .locks import find_stale

        stale = find_stale()
    except Exception:
        pass
    profiles = []
    mem_profiles = []
    if paths.PROFILES_DIR.exists():
        for d in sorted(paths.PROFILES_DIR.iterdir()):
            if not d.is_dir():
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            info = read_lock(d.name)
            live = bool(info and is_live(info))
            profiles.append({"profile": d.name, "size": size,
                             "engine": (info.engine if info else
                                        (d and paths.load_meta(d.name).get("engine")) or None),
                             "live": live})
            if live:
                # Remote browsers have no local pid: rss would walk pid 0 and
                # sum half the machine. Report None, never a bogus number.
                rss = (None if (info.remote or info.pid <= 0) else
                       round(_rss_tree_mb(info.pid), 1))
                mem_profiles.append({"profile": d.name, "pid": info.pid,
                                     "rssMB": rss,
                                     "remote": bool(info.remote),
                                     "engine": info.engine})
    return {
        "engines": engines,
        "defaultEngine": (ENGINE_OBScura if engines["obscura"]
                          else ENGINE_CB),
        "browserBinary": engines["cloakbrowser"],
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
