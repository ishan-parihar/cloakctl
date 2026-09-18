"""Unit tests for the engine layer: engines.py, keeper protocol, locks.

No engine binaries needed — everything is mocked or socket-level against a
fake keeper. Live engine coverage lives in tests/live_obscura.py.
"""
from __future__ import annotations

import json
import os
import socket
import threading

import pytest

from cloakctl import paths  # noqa: E402
from cloakctl.engines import (  # noqa: E402
    ENGINE_CB,
    ENGINE_OBScura,
    ENGINES,
    find_obscura,
    resolve_engine,
)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOAKCTL_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(paths, "HOME_STATE", tmp_path / "state")
    monkeypatch.setattr(paths, "PROFILES_DIR", tmp_path / "state" / "profiles")
    monkeypatch.setattr(paths, "RUNTIME_DIR", tmp_path / "state" / "runtime")
    yield tmp_path / "state"


# --- engines -----------------------------------------------------------------


def test_engine_constants():
    assert ENGINES == (ENGINE_OBScura, ENGINE_CB)
    assert ENGINE_OBScura == "obscura"
    assert ENGINE_CB == "cloakbrowser"


def test_resolve_explicit_beats_env_and_meta(state, monkeypatch):
    monkeypatch.setenv("CLOAKCTL_ENGINE", "cloakbrowser")
    assert resolve_engine("obscura", {"engine": "cloakbrowser"}) == ENGINE_OBScura
    assert resolve_engine(None, {"engine": "cloakbrowser"}) == ENGINE_CB


def test_resolve_env_beats_meta(state, monkeypatch):
    monkeypatch.setenv("CLOAKCTL_ENGINE", "obscura")
    assert resolve_engine(None, {"engine": "cloakbrowser"}) == ENGINE_OBScura


def test_resolve_meta_sticky(state, monkeypatch):
    monkeypatch.delenv("CLOAKCTL_ENGINE", raising=False)
    assert resolve_engine(None, {"engine": "cloakbrowser"}) == ENGINE_CB


def test_resolve_invalid_raises(state):
    with pytest.raises(ValueError, match="unknown engine"):
        resolve_engine("nonsense")


def test_resolve_default_obscura_when_installed(state, monkeypatch):
    monkeypatch.delenv("CLOAKCTL_ENGINE", raising=False)
    monkeypatch.setattr("cloakctl.engines.find_obscura", lambda: "/usr/bin/obscura")
    assert resolve_engine() == ENGINE_OBScura


def test_resolve_default_fallback_without_obscura(state, monkeypatch):
    monkeypatch.delenv("CLOAKCTL_ENGINE", raising=False)
    monkeypatch.setattr("cloakctl.engines.find_obscura", lambda: None)
    monkeypatch.setattr("cloakctl.engines.find_cloakbrowser", lambda: "/usr/bin/chrome")
    assert resolve_engine() == ENGINE_CB


def test_find_obscura_checks_local_bin(state, monkeypatch):
    monkeypatch.setattr("cloakctl.engines.shutil.which", lambda _: None)
    fake = state / ".local" / "bin" / "obscura"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(state))  # search is $HOME-relative
    assert find_obscura() == str(fake)


def test_find_obscura_missing(state, monkeypatch):
    monkeypatch.setattr("cloakctl.engines.shutil.which", lambda _: None)
    monkeypatch.setenv("HOME", str(state / "nope"))
    assert find_obscura() is None


# --- locks: engine field -------------------------------------------------------


def test_lock_engine_roundtrip(state):
    from cloakctl.locks import acquire, is_live, read_lock, release

    acquire("p1", pid=os.getpid(), cdp_port=9222, ws_endpoint=None,
            remote=False, engine=ENGINE_OBScura, headed=False)
    info = read_lock("p1")
    assert info is not None and info.engine == ENGINE_OBScura
    assert is_live(info)  # our own pid is alive
    release("p1")
    assert read_lock("p1") is None


def test_lock_engine_defaults_backcompat(state):
    from cloakctl.locks import read_lock

    p = paths.lock_path("p2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": os.getpid(), "cdpPort": 9222}))
    info = read_lock("p2")
    assert info is not None and info.engine == ENGINE_CB


# --- keeper protocol (fake keeper over a real unix socket) ---------------------


class FakeKeeper:
    """Minimal keeper: echoes the request id, serves canned results."""

    def __init__(self, responses: dict[str, object] | None = None):
        self.responses = responses or {}
        self.sock_path = None
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._stop = threading.Event()

    def start(self, tmp_path):
        self.sock_path = tmp_path / "keeper.test.sock"
        if self.sock_path.exists():
            self.sock_path.unlink()
        self._srv.bind(str(self.sock_path))
        self._srv.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        f = conn.makefile("r")
        try:
            for raw in f:
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                rid, op = req.get("id"), req.get("op")
                if op == "fail":
                    resp = {"id": rid, "ok": False, "error": "boom"}
                else:
                    result = self.responses.get(op, {"pong": True})
                    resp = {"id": rid, "ok": True, "result": result}
                conn.sendall((json.dumps(resp) + "\n").encode())
        finally:
            conn.close()

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        if self.sock_path and self.sock_path.exists():
            self.sock_path.unlink()


@pytest.fixture()
def fake_keeper(tmp_path):
    k = FakeKeeper()
    k.start(tmp_path)
    yield k
    k.stop()


def test_keeper_client_ping_and_id_echo(state, fake_keeper, monkeypatch):
    from cloakctl.keeper import KeeperClient

    monkeypatch.setattr("cloakctl.keeper.keeper_sock_path",
                        lambda name: fake_keeper.sock_path)
    with KeeperClient("test") as kc:
        r1 = kc.ping()
        r2 = kc.ping()  # sequential ids must both match
        assert r1 == {"pong": True}
        assert r2 == {"pong": True}


def test_keeper_client_call_passthrough(state, fake_keeper, monkeypatch):
    from cloakctl.keeper import KeeperClient

    fake_keeper.responses["call"] = {"value": 42}
    monkeypatch.setattr("cloakctl.keeper.keeper_sock_path",
                        lambda name: fake_keeper.sock_path)
    with KeeperClient("test") as kc:
        assert kc.call("Runtime.evaluate", {"expression": "42"}) == {"value": 42}


def test_keeper_client_error_raises(state, fake_keeper, monkeypatch):
    from cloakctl.keeper import KeeperClient, KeeperError

    monkeypatch.setattr("cloakctl.keeper.keeper_sock_path",
                        lambda name: fake_keeper.sock_path)
    with KeeperClient("test") as kc:
        with pytest.raises(KeeperError, match="boom"):
            kc._request({"op": "fail"})


def test_keeper_client_not_connected(state):
    from cloakctl.keeper import KeeperClient, KeeperError

    kc = KeeperClient("ghost")
    with pytest.raises(KeeperError, match="not connected"):
        kc._request({"op": "ping"})


def test_keeper_sock_path_layout(state):
    from cloakctl.keeper import keeper_sock_path

    p = keeper_sock_path("prof")
    assert str(state) in str(p) and "prof" in str(p)


# --- keeper concurrency guard ---------------------------------------------------


def test_free_tcp_port_returns_bindable_port(state):
    from cloakctl.keeper import _free_tcp_port

    port = _free_tcp_port()
    s = socket.socket()
    s.bind(("127.0.0.1", port))  # must not collide
    s.close()


# --- launch memory guard (browser._guard_memory) --------------------------
def test_mem_guard_env_override_and_garbage(monkeypatch):
    from cloakctl import browser
    monkeypatch.setenv("CLOAKCTL_MIN_MEM_MB", "123")
    assert browser._min_mem_mb() == 123
    monkeypatch.setenv("CLOAKCTL_MIN_MEM_MB", "not-a-number")
    assert browser._min_mem_mb() == 400
    monkeypatch.setenv("CLOAKCTL_MIN_MEM_MB", "-5")
    assert browser._min_mem_mb() == 0


def test_mem_guard_blocks_under_floor(monkeypatch):
    from cloakctl import browser
    monkeypatch.setenv("CLOAKCTL_MIN_MEM_MB", "999999")
    with pytest.raises(RuntimeError, match="MB RAM available"):
        browser._guard_memory("obscura")
    with pytest.raises(RuntimeError, match="cloakbrowser"):
        browser._guard_memory("cloakbrowser")


def test_mem_guard_passes_and_disable(monkeypatch):
    from cloakctl import browser
    monkeypatch.setenv("CLOAKCTL_MIN_MEM_MB", "1")   # passes trivially
    browser._guard_memory("obscura")
    monkeypatch.setenv("CLOAKCTL_MIN_MEM_MB", "0")   # disabled
    browser._guard_memory("cloakbrowser")


# --- remote-mode honesty contract -------------------------------------------


def test_empty_endpoint_refuses_local_launch(state, monkeypatch):
    """--endpoint '' / empty env var must raise, never fall through to a
    local launch (a silent launch would put a browser on the VPS)."""
    from cloakctl import browser

    with pytest.raises(RuntimeError, match="--endpoint was empty"):
        browser.open_profile("p", endpoint="")
    with pytest.raises(RuntimeError, match="--endpoint was empty"):
        browser.open_profile("p", endpoint="   ")
    monkeypatch.setenv("CLOAKCTL_CDP_URL", "")
    with pytest.raises(RuntimeError, match="CLOAKCTL_CDP_URL was empty"):
        browser.open_profile("p")
    monkeypatch.setenv("CLOAKCTL_CDP_URL", "  ")
    with pytest.raises(RuntimeError, match="CLOAKCTL_CDP_URL was empty"):
        browser.open_profile("p")


def test_open_remote_reports_cloakbrowser_engine(state, monkeypatch):
    """A remote attach is Chromium-family by contract: the lock and the open
    doc say cloakbrowser, not the misreported obscura."""
    from cloakctl import browser
    from cloakctl.locks import read_lock

    class FakeVer(dict):
        pass

    monkeypatch.setattr("cloakctl.browser.CdpClient",
                        _FakeRemoteCdp)
    out = browser.open_profile("p", endpoint="ws://127.0.0.1:1/devtools/browser/x")
    assert out["remote"] is True and out["engine"] == ENGINE_CB
    info = read_lock("p")
    assert info is not None and info.remote and info.engine == ENGINE_CB


class _FakeRemoteCdp:
    """Stand-in for CdpClient in remote-attach tests (no real browser)."""

    _product = "HeadlessChrome/145.0.7400.0"  # real Chromium build string

    def __init__(self, ws_url, timeout=30.0):
        self.ws_url = ws_url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, params=None):
        assert method == "Browser.getVersion"
        return {"product": self._product}


def test_open_remote_warns_on_obscura_endpoint(state, monkeypatch, capsys):
    """An endpoint fronting obscura (degenerate Chrome/N.0.0.0 product) must
    attach only with a loud warning — never silently."""
    from cloakctl import browser

    monkeypatch.setattr("cloakctl.browser.CdpClient", _FakeRemoteCdp)
    _FakeRemoteCdp._product = "Chrome/145.0.0.0"
    try:
        browser.open_profile("p", endpoint="ws://127.0.0.1:1/devtools/browser/x")
        err = capsys.readouterr().err
        assert "looks like obscura" in err
    finally:
        _FakeRemoteCdp._product = "HeadlessChrome/145.0.7400.0"


def test_clear_active_only_own_record(state):
    """clear_active(name, tid) clears ONLY when the record points at tid."""
    from cloakctl import paths
    from cloakctl.tabs import clear_active, record_active

    record_active("p", "tab-aaa")
    clear_active("p", "tab-bbb")  # different tid: record must survive
    assert paths.load_meta("p").get("activeTab") == "tab-aaa"
    clear_active("p", "tab-aaa")  # matching tid: record is dropped
    assert paths.load_meta("p").get("activeTab") is None


# --- keeper bridge -----------------------------------------------------------------


def test_bridge_token_validation():
    """_path_token extracts the token; token_for_port gates the handler."""
    from cloakctl import bridge

    assert bridge._path_token("/devtools/browser/abc123") == "abc123"
    assert bridge._path_token("/devtools/page/tok") == "tok"
    assert bridge._path_token("/nope") == ""
    assert bridge._path_token("") == ""
    bridge._active_tokens[5999] = "secrets"
    try:
        assert bridge.token_for_port(5999) == "secrets"
        assert bridge.token_for_port(6000) == ""
    finally:
        bridge._active_tokens.pop(5999, None)


def test_browser_level_method_set():
    """Session-less calls route only for known browser-level methods."""
    from cloakctl.bridge import _BROWSER_LEVEL

    for m in ("Target.getTargets", "Target.createTarget",
              "Storage.getCookies", "Browser.getVersion"):
        assert m in _BROWSER_LEVEL
    assert "Page.navigate" not in _BROWSER_LEVEL
    assert "Runtime.evaluate" not in _BROWSER_LEVEL


def test_attach_reports_obscura_bridge(state, monkeypatch, capsys):
    """`cloakctl attach` on a live obscura profile serves the keeper bridge
    endpoint (engine=obscura, loopback ws URL) instead of refusing."""
    from cloakctl import browser, cli

    monkeypatch.setattr(browser, "status_profile", lambda name: {
        "profile": name, "live": True, "engine": ENGINE_OBScura,
        "remote": False, "pid": 4242, "cdpPort": 50171,
        "url": "https://example.com/",
        "bridgePort": 48755,
        "wsEndpoint": "ws://127.0.0.1:48755/devtools/browser/t0k3n",
    })
    ns = cli.build_parser().parse_args(["attach", "p", "--json"])
    rc = ns.func(ns)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["engine"] == "obscura"
    assert out["bridgePort"] == 48755
    assert out["wsEndpoint"].startswith("ws://127.0.0.1:48755/devtools/browser/")


def test_attach_errors_when_bridge_missing(state, monkeypatch, capsys):
    """No bridgePort reported -> attach fails honestly (no dead URL)."""
    from cloakctl import browser, cli

    monkeypatch.setattr(browser, "status_profile", lambda name: {
        "profile": name, "live": True, "engine": ENGINE_OBScura,
        "remote": False, "pid": 4242, "cdpPort": 50171,
    })
    ns = cli.build_parser().parse_args(["attach", "p", "--json"])
    with pytest.raises(RuntimeError) as ei:
        ns.func(ns)
    assert "bridge" in str(ei.value)


def test_keeper_spawn_carries_home_marker(state, monkeypatch):
    """Keeper cmdlines self-describe (--home) so sweeps can identify strays."""
    from cloakctl import keeper

    captured = {}

    class FakeProc:
        pid = 424242

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(keeper.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("CLOAKCTL_HOME", str(state))
    keeper.spawn("p", engine_bin="/usr/bin/obscura")
    cmd = captured["cmd"]
    assert "--home" in cmd and str(state) in cmd
    assert cmd[cmd.index("--profile") + 1] == "p"
