"""Obscura session keeper: one long-lived CDP connection per profile.

Obscura's page state is per-connection: each browser-endpoint WebSocket gets
its own page instance, and `Target.createTarget(about:blank)` on a fresh
connection navigates that page away. A CLI that connects per verb would lose
the session between commands — the opposite of cloakctl's persistent-session
contract.

The keeper is the fix: a tiny daemon (one per profile) that holds the CDP
connection for the profile's lifetime. Verbs reach it over a local unix
socket speaking JSON-lines:

  -> {"id": 1, "op": "call", "method": "Page.navigate", "params": {...}}
  <- {"id": 1, "ok": true, "result": {...}}

Ops: call (CDP passthrough), evaluate, status, ping, shutdown.

The keeper process is the lock target: `pid` in the lockfile is the keeper,
so existing liveness/staleness logic works unchanged. The browser itself
(`obscura serve`) is the keeper's child; killing the process group closes
both. If the keeper dies the page state is gone by design — cookies survive
in the profile storage dir, and `import`/`validate` re-establish the session.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SOCK_TIMEOUT = 60.0
CALL_WATCHDOG = 120.0  # hard ceiling on one CDP op inside the handler
CONNECT_TIMEOUT = 30.0


def _klog(profile: str, msg: str) -> None:
    """Append a lifecycle marker to runtime/keeper.<profile>.debug.log.
    Enabled when CLOAKCTL_KEEPER_DEBUG=1; doctor/troubleshooting read it."""
    if os.environ.get("CLOAKCTL_KEEPER_DEBUG") != "1":
        return
    try:
        home = os.environ.get("CLOAKCTL_HOME", str(Path.home() / ".cloakctl"))
        dbg = Path(home) / "runtime" / f"keeper.{profile}.debug.log"
        dbg.parent.mkdir(parents=True, exist_ok=True)
        with open(dbg, "a") as f:
            f.write(f"{time.time():.3f} {os.getpid()} {msg}\n")
    except Exception:
        pass


def keeper_sock_path(name: str) -> Path:
    home = os.environ.get("CLOAKCTL_HOME", str(Path.home() / ".cloakctl"))
    return Path(home) / "runtime" / f"keeper.{name}.sock"


def _clear_dead_socket(sock_path: Path) -> None:
    """Remove a unix socket file nobody is listening on (crashed keeper)."""
    if not sock_path.exists():
        return
    test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        test.settimeout(1.0)
        test.connect(str(sock_path))
        return  # somebody answered: alive, do not touch
    except OSError:
        try:
            sock_path.unlink()
        except OSError:
            pass
    finally:
        test.close()


class KeeperError(RuntimeError):
    pass


# --- client side ---------------------------------------------------------------


class KeeperClient:
    """JSON-lines client for the profile keeper. One connection per request
    by default (`with KeeperClient(name) as kc:`); the keeper handles
    concurrent connections, so `open` can status-probe while verbs run."""

    def __init__(self, name: str, timeout: float = SOCK_TIMEOUT):
        self.name = name
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._f = None
        self._next_id = 1

    def __enter__(self) -> "KeeperClient":
        sock_path = keeper_sock_path(self.name)
        if not sock_path.exists():
            raise KeeperError(
                f"keeper socket missing for profile {self.name!r} (not live?)")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        try:
            self._sock.connect(str(sock_path))
        except OSError as e:
            self._sock.close()
            raise KeeperError(f"cannot reach keeper for {self.name!r}: {e}") from e
        self._f = self._sock.makefile("r")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._f:
                self._f.close()
            if self._sock:
                self._sock.close()
        finally:
            self._f, self._sock = None, None

    def _request(self, payload: dict) -> dict:
        if not self._sock or not self._f:
            raise KeeperError("not connected")
        req_id = self._next_id
        self._next_id += 1
        line = json.dumps({"id": req_id, **payload}) + "\n"
        try:
            self._sock.sendall(line.encode())
        except OSError as e:
            raise KeeperError(f"keeper write failed: {e}") from e
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KeeperError(
                    f"keeper did not reply within {self.timeout:g}s "
                    "(page may have wedged the engine; try closing the profile)")
            self._sock.settimeout(remaining)
            try:
                raw = self._f.readline()
            except socket.timeout as e:
                raise KeeperError(
                    f"keeper reply timed out after {self.timeout:g}s "
                    "(page may have wedged the engine; try closing the profile)") from e
            if not raw:
                raise KeeperError("keeper closed the connection")
            try:
                resp = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if resp.get("id") != req_id:
                continue
            if not resp.get("ok"):
                raise KeeperError(str(resp.get("error", "keeper error")))
            return resp

    # typed ops ----------------------------------------------------------------

    def ping(self) -> dict:
        return self._request({"op": "ping"}).get("result", {})

    def status(self) -> dict:
        return self._request({"op": "status"}).get("result", {})

    def call(self, method: str, params: dict | None = None) -> dict:
        """CDP passthrough. Returns the raw CDP `result` dict."""
        return self._request({"op": "call", "method": method,
                              "params": params or {}}).get("result", {})

    def evaluate(self, expression: str):
        """Runtime.evaluate in the profile's persistent page."""
        return self._request({"op": "evaluate",
                              "expression": expression}).get("result", {}).get("value")

    def shutdown(self) -> None:
        try:
            self._request({"op": "shutdown"})
        except KeeperError:
            pass


def keeper_alive(name: str) -> bool:
    """Cheap liveness: can we connect to the keeper socket?"""
    sock_path = keeper_sock_path(name)
    if not sock_path.exists():
        return False
    test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        test.settimeout(1.0)
        test.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        test.close()


def wait_keeper(name: str, timeout: float = CONNECT_TIMEOUT) -> bool:
    """Poll until the keeper socket answers."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if keeper_alive(name):
            return True
        time.sleep(0.15)
    return False


def wait_engine_port(name: str, timeout: float = 30.0) -> int:
    """Ask the keeper which local port `obscura serve` bound."""
    end = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < end:
        try:
            with KeeperClient(name, timeout=5.0) as kc:
                st = kc.status()
            port = int(st.get("cdpPort") or 0)
            if port > 0:
                return port
        except (KeeperError, OSError, ValueError) as e:
            last_err = e
        time.sleep(0.2)
    raise TimeoutError(f"keeper never reported an engine port ({last_err})")


# --- server side ----------------------------------------------------------------


def _free_tcp_port() -> int:
    """Grab a free loopback port from the OS (best effort: small race window)."""
    import socket as _s

    with _s.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def try_kill(proc: subprocess.Popen) -> None:
    """Best-effort kill ladder: TERM → wait → KILL."""
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _wait_port_listening(port: int, timeout: float = 30.0) -> bool:
    """Poll until `port` accepts TCP connections (the engine is up)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with socket.socket() as sk:
            sk.settimeout(0.5)
            try:
                sk.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.15)
    return False


def _serve_profile(profile: str, engine_bin: str, stealth: bool,
                   engine_args: list[str], serve_port: int | None) -> int:
    """Keeper main loop. Exits nonzero on startup failure."""
    from . import paths
    from .bridge import BridgeError, MasterConnection, start_bridge
    from .cdp import CdpError

    _klog(profile, "serve: start")
    sock_path = keeper_sock_path(profile)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    _clear_dead_socket(sock_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
    except OSError as e:
        print(json.dumps({"error": f"bind {sock_path}: {e}"}), file=sys.stderr)
        return 3
    os.chmod(sock_path, 0o600)
    server.listen(16)
    _klog(profile, "serve: unix socket listening")

    cdir = paths.chrome_dir(profile)
    cdir.mkdir(parents=True, exist_ok=True)
    # obscura treats --port 0 literally (binds no port), so pick a free
    # port ourselves; the caller may pin one via --serve-port.
    cdp_port = serve_port or _free_tcp_port()
    serve_args = [engine_bin, "serve",
                  "--port", str(cdp_port),
                  "--storage-dir", str(cdir)]
    if stealth:
        serve_args.append("--stealth")
    serve_args.extend(engine_args)
    log_path = cdir / "obscura.log"
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def _pdeathsig():
        # Kernel-level orphan guard: if the keeper is SIGKILLed (no handlers
        # run, no sweep possible), the kernel delivers SIGTERM to the engine.
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
                raise OSError(ctypes.get_errno())
        except Exception:
            pass  # best-effort; _sweep_profile_procs covers normal teardown

    try:
        engine = subprocess.Popen(
            serve_args, stdout=log_fd, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, close_fds=False,
            preexec_fn=_pdeathsig)
    finally:
        os.close(log_fd)

    _klog(profile, f"serve: engine spawned pid={engine.pid} port={cdp_port}")
    if not _wait_port_listening(cdp_port, timeout=30):
        _klog(profile, "serve: FAIL engine port never opened")
        try: engine.kill()
        except Exception: pass
        return 4
    _klog(profile, "serve: engine port listening")

    # The master connection: the keeper's own session on the engine. All
    # local ops AND the external bridge ride this one connection.
    ws = f"ws://127.0.0.1:{cdp_port}/devtools/browser"
    master = None
    deadline = time.monotonic() + 30
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            master = MasterConnection(ws)
            master.start()
            break
        except Exception as e:
            last_err = e
            if master is not None:
                try: master.close()
                except Exception: pass
                master = None
            time.sleep(0.2)
    if master is None:
        _klog(profile, f"serve: FAIL master connect ({last_err})")
        try: engine.kill()
        except Exception: pass
        return 5
    _klog(profile, "serve: master connected")

    # Bind the page ONCE — the keeper's session is the profile's session.
    try:
        master.bind_page("about:blank")
    except (CdpError, Exception) as e:
        _klog(profile, f"serve: FAIL page bind: {type(e).__name__}: {e}")
        try: engine.kill()
        except Exception: pass
        return 6
    _klog(profile, "serve: page bound")

    # Shareable endpoint: a loopback CDP bridge over the master connection.
    # External clients (hermes/agent-browser --cdp) attach HERE and get the
    # profile's true session. Failure is non-fatal: local CLI automation
    # works without it; open()/attach just report no shareable endpoint.
    bridge_port, bridge_token = 0, ""
    try:
        bridge_port, bridge_token = start_bridge(master, profile)
        _klog(profile, f"serve: bridge ready port={bridge_port}")
    except Exception as e:
        _klog(profile, f"serve: bridge unavailable ({type(e).__name__}: {e})")

    state = {"engine_port": cdp_port, "started": time.time(),
             "bridge_port": bridge_port, "bridge_token": bridge_token}

    def handle(conn: socket.socket) -> None:
        _klog(profile, "handle: connection accepted")
        f = conn.makefile("r")
        try:
            for raw in f:
                _klog(profile, f"handle: recv {raw[:120]!r}")
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                op = req.get("op")
                rid = req.get("id")  # echo in every reply for client matching
                try:
                    if op == "ping":
                        out = {"id": rid, "ok": True, "result": {"pong": True}}
                    elif op == "status":
                        try:
                            url = master.evaluate("location.href") or ""
                        except CdpError:
                            url = ""
                        res = {"profile": profile, "engine": "obscura",
                               "cdpPort": state["engine_port"], "url": url,
                               "uptimeSec": round(time.time() - state["started"], 1)}
                        if state["bridge_port"]:
                            res["bridgePort"] = state["bridge_port"]
                            res["bridgeToken"] = state["bridge_token"]
                            res["wsEndpoint"] = (
                                f"ws://127.0.0.1:{state['bridge_port']}"
                                f"/devtools/browser/{state['bridge_token']}")
                        out = {"id": rid, "ok": True, "result": res}
                    elif op == "call":
                        _arm_watchdog(engine, profile)
                        try:
                            res = master.call(req.get("method", ""),
                                              req.get("params") or {})
                        finally:
                            _disarm_watchdog()
                        out = {"id": rid, "ok": True, "result": res}
                    elif op == "evaluate":
                        _arm_watchdog(engine, profile)
                        try:
                            val = master.evaluate(req.get("expression", ""))
                        finally:
                            _disarm_watchdog()
                        out = {"id": rid, "ok": True, "result": {"value": val}}
                    elif op == "shutdown":
                        out = {"id": rid, "ok": True, "result": {"bye": True}}
                        try:
                            conn.sendall((json.dumps(out) + "\n").encode())
                        except OSError:
                            pass
                        _klog(profile, "handle: shutdown requested — tearing down")
                        # Stop accepting, stop the engine; the supervise
                        # wait() in the main thread returns and runs cleanup.
                        try: server.close()
                        except Exception: pass
                        try: engine.terminate()
                        except Exception: pass
                        threading.Timer(3.0, lambda: (
                            try_kill(engine), None)[-1]).start()
                        return
                    else:
                        out = {"id": rid, "ok": False, "error": f"unknown op {op!r}"}
                except CdpError as e:
                    out = {"id": rid, "ok": False, "error": f"CdpError: {e}"}
                except Exception as e:
                    out = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
                try:
                    conn.sendall((json.dumps(out) + "\n").encode())
                    _klog(profile, f"handle: sent reply for op={op!r}")
                except OSError:
                    return
        except Exception as e:
            _klog(profile, f"handle: DIED {type(e).__name__}: {e}")
        finally:
            try: f.close()
            except Exception: pass
            try: conn.close()
            except Exception: pass

    threading.Thread(
        target=lambda: _accept_loop(server, handle, profile), daemon=True).start()
    _klog(profile, "serve: accept loop running")
    # Supervise: when the engine dies, the keeper dies (or is told to die
    # via the shutdown op, which terminates the engine from a handler).
    try:
        engine.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _klog(profile, "serve: engine exited — tearing down")
        try: engine.kill()
        except Exception: pass
        try: master.close()
        except Exception: pass
        try: server.close()
        except Exception: pass
        try: sock_path.unlink(missing_ok=True)
        except Exception: pass
    return 0


_watchdog_timer: list = []


def _arm_watchdog(engine, profile: str) -> None:
    """Hard ceiling on one CDP op. A runaway page (while(true){}) can stall
    obscura's CDP server forever; without this the keeper wedges forever and
    every later verb on the profile hangs. Firing = SIGKILL the engine: the
    supervise loop tears the session down and the client gets a clean
    'keeper closed the connection' error instead of an eternal block."""
    _disarm_watchdog()
    t = threading.Timer(
        CALL_WATCHDOG,
        lambda: (_klog(profile, f"WATCHDOG: CDP op exceeded {CALL_WATCHDOG:g}s "
                       "- killing engine to unwedge the session"),
                 try_kill(engine))[-1])
    t.daemon = True
    t.start()
    _watchdog_timer.append(t)


def _disarm_watchdog() -> None:
    while _watchdog_timer:
        t = _watchdog_timer.pop()
        t.cancel()


def _accept_loop(server: socket.socket, handle, profile: str = "") -> None:
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            if profile:
                _klog(profile, "accept loop: OSError — exiting")
            return
        if profile:
            _klog(profile, "accept loop: accepted, spawning handler")
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def spawn(name: str, *, engine_bin: str, stealth: bool = False,
          engine_args: list[str] | None = None,
          serve_port: int | None = None,
          log_path: Path | None = None) -> int:
    """Start the keeper detached (setsid) for profile `name`. Returns pid.

    The keeper cmdline carries `--home <state dir>` explicitly: orphan
    sweeps and test teardowns can then identify WHICH state dir a stray
    keeper belongs to (the inherited env var is invisible to pgrep)."""
    from . import paths

    home = os.environ.get("CLOAKCTL_HOME", str(Path.home() / ".cloakctl"))
    cmd = [sys.executable, "-m", "cloakctl.keeper", "--profile", name,
           "--engine-bin", engine_bin, "--home", home]
    if stealth:
        cmd.append("--stealth")
    if serve_port is not None:
        cmd.extend(["--serve-port", str(serve_port)])
    for a in engine_args or []:
        # `--engine-arg=<value>` (attached form): values often start with
        # `--` themselves (e.g. --allow-private-network) and argparse in
        # separate-token form would parse them as new flags.
        cmd.append(f"--engine-arg={a}")
    log_path = log_path or paths.RUNTIME_DIR / f"keeper.{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(
            cmd, stdout=fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=False)
    finally:
        os.close(fd)
    return proc.pid


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="cloakctl-keeper")
    p.add_argument("--profile", required=True)
    p.add_argument("--engine-bin", required=True)
    p.add_argument("--stealth", action="store_true")
    p.add_argument("--serve-port", type=int, default=None)
    p.add_argument("--engine-arg", action="append", default=[])
    p.add_argument("--home", default=None,
                   help="state dir (restated from the spawner's env so the "
                        "cmdline self-describes for sweeps/pgreps)")
    args = p.parse_args(argv)
    if args.home:
        os.environ["CLOAKCTL_HOME"] = args.home
    import signal as _signal
    _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
    return _serve_profile(args.profile, args.engine_bin, args.stealth,
                          args.engine_arg, args.serve_port)


if __name__ == "__main__":
    raise SystemExit(main())
