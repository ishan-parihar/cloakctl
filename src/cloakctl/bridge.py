"""CDP bridge — share the keeper's ONE true obscura session with external clients.

obscura's CDP is per-connection isolated: a fresh connection sees zero targets
and an empty cookie jar, so a raw `ws://` attach gets an empty session (the
silent data-loss trap). The fix is architectural: the keeper already HOLDS the
profile's real session on its master CDP connection — this module turns that
connection into a shareable endpoint.

Two pieces:

`MasterConnection`
    Owns the keeper's obscura WS on a dedicated asyncio loop with a single
    reader task. Responses are matched to waiters by message id (futures);
    events are fanned out to subscriber queues. The keeper's local JSON-lines
    ops (call/evaluate/status) and the bridge clients all route through it —
    one reader means no recv races.

`start_bridge` / `_client_handler`
    A loopback-only WebSocket server speaking REAL CDP. External clients
    (hermes/agent-browser via `--cdp`, playwright `connect_over_cdp`) connect
    to `ws://127.0.0.1:<port>/devtools/browser/<token>` and get the master's
    live session — same page, same cookies, same storage. attachToTarget is
    passed through (obscura mints additional flat sessions per attach —
    verified against the live engine), events are routed per sessionId, and
    browser-level calls (Target.getTargets, Storage.*, ...) forward without a
    session id exactly as Chromium expects.

Auth: loopback bind + a per-keeper random token in the URL path. The token
is defense-in-depth against other local users; the loopback bind is the
primary guard. The endpoint is a LAN-local automation surface, not a network
service — remote exposure stays a cloakbrowser/tunnel concern.

Lifecycle: the bridge lives inside the keeper process. Engine exit tears down
master + bridge; `close_profile` needs no bridge-specific handling.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from typing import Any

import websockets

from .cdp import CdpError

_CALL_TIMEOUT = 120.0
_CONNECT_TIMEOUT = 40.0

# Port -> expected URL token for running bridge servers. Checked by
# _client_handler against the connection's local port.
_active_tokens: dict[int, str] = {}


def token_for_port(port: int) -> str:
    return _active_tokens.get(port, "")


class BridgeError(RuntimeError):
    """The bridge endpoint could not be established."""

# Browser-level methods: forwarded WITHOUT a sessionId. Session-scoped
# Target.* variants exist in Chromium; obscura's server handles the
# browser-level forms and everything else is per-session.
_BROWSER_LEVEL = {
    "Target.getTargets", "Target.createTarget", "Target.closeTarget",
    "Target.activateTarget", "Target.attachToTarget",
    "Target.setDiscoverTargets", "Target.discoverTargets",
    "Target.getTargetInfo", "Target.autoAttachRelatedToTarget",
    "Storage.getCookies", "Storage.setCookies", "Storage.clearCookies",
    "Browser.getVersion", "Browser.close", "Browser.getWindowForTarget",
    "Browser.setWindowBounds", "Browser.getWindowBounds",
}


class MasterConnection:
    """The keeper's obscura WS: one loop, one reader, futures per call id."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.page_session_id: str | None = None
        self.page_target_id: str | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True, name="cloakctl-bridge")
        self._ws: Any = None
        self._reader: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._event_listeners: set[asyncio.Queue] = set()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._closed = False

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Connect + verify with Browser.getVersion. Raises CdpError on failure."""
        self._thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._aconnect(), self._loop
                                             ).result(_CONNECT_TIMEOUT)
        except Exception as e:
            raise CdpError(f"master connect: {e}") from e

    async def _aconnect(self) -> None:
        self._ws = await websockets.connect(
            self.ws_url, max_size=64 * 1024 * 1024, ping_interval=None)
        self._reader = asyncio.ensure_future(self._aread())
        await asyncio.wait_for(self._acall("Browser.getVersion", {}, None, 15.0),
                              20.0)

    async def _aread(self) -> None:
        """Single reader: responses -> futures, events -> subscriber queues."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw if isinstance(raw, str) else raw.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if "id" in data and "method" not in data:
                    fut = self._pending.pop(data.get("id"), None)
                    if fut is not None and not fut.done():
                        if "error" in data:
                            fut.set_exception(CdpError(
                                f"{data['error'].get('message', 'cdp error')}"))
                        else:
                            fut.set_result(data.get("result", {}))
                elif "method" in data:
                    for q in list(self._event_listeners):
                        try:
                            q.put_nowait(data)
                        except asyncio.QueueFull:
                            pass  # slow consumer: drop rather than wedge the reader
        except Exception:
            pass  # connection died: fail pending below
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("master connection closed"))
            self._pending.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._aclose(), self._loop
                                             ).result(5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _aclose(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # --- calls ----------------------------------------------------------------

    async def _acall(self, method: str, params: dict | None,
                     session_id: str | None, timeout: float) -> Any:
        if self._ws is None:
            raise CdpError("master connection not open")
        with self._id_lock:
            mid = self._next_id
            self._next_id += 1
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = self._loop.create_future()
        self._pending[mid] = fut
        await asyncio.wait_for(self._ws.send(json.dumps(msg)), timeout)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    # Session sentinel: local keeper ops always target the profile's bound
    # page (mirrors the old keeper-side CdpClient, which attached its session
    # id to every call). The bridge passes explicit sessions or None.
    USE_PAGE = object()

    def call(self, method: str, params: dict | None = None,
             session_id: Any = USE_PAGE, timeout: float = _CALL_TIMEOUT) -> Any:
        """Thread-safe CDP call. USE_PAGE -> bound page session; None -> browser-level."""
        if self._closed:
            raise CdpError("master connection closed")
        sid = self.page_session_id if session_id is MasterConnection.USE_PAGE \
            else session_id
        return asyncio.run_coroutine_threadsafe(
            self._acall(method, params, sid, timeout), self._loop
        ).result(timeout + 5)

    def evaluate(self, expression: str, timeout: float = _CALL_TIMEOUT) -> Any:
        """Runtime.evaluate on the bound page, value-extracted (keeper contract)."""
        res = self.call("Runtime.evaluate",
                        {"expression": expression, "returnByValue": True,
                         "awaitPromise": False}, timeout=timeout)
        exc = res.get("exceptionDetails")
        if exc:
            text = exc.get("text") or (
                exc.get("exception", {}).get("description", "JS exception"))
            raise CdpError(f"Runtime.evaluate: {text}")
        return res.get("result", {}).get("value")

    def bind_page(self, url: str = "about:blank") -> str:
        """Create + attach the profile's persistent page (keeper startup)."""
        res = self.call("Target.createTarget", {"url": url}, session_id=None)
        self.page_target_id = res.get("targetId")
        res = self.call("Target.attachToTarget",
                        {"targetId": self.page_target_id, "flatten": True},
                        session_id=None)
        self.page_session_id = res.get("sessionId")
        return self.page_session_id

    def subscribe_events(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._event_listeners.add(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        self._event_listeners.discard(q)


# --- bridge server --------------------------------------------------------------


def _path_token(path: str) -> str:
    """Extract the token from a /devtools/{browser|page}/<token> path."""
    parts = [p for p in (path or "").split("/") if p]
    if len(parts) >= 3 and parts[0] == "devtools":
        return parts[2]
    return ""


async def _client_handler(conn: Any, master: MasterConnection) -> None:
    """One external CDP client: proxy calls, route events by sessionId."""
    path = getattr(getattr(conn, "request", None), "path", "") or ""
    try:
        local_port = conn.local_address[1]
    except Exception:
        local_port = 0
    expected = token_for_port(local_port)
    if not expected or _path_token(path) != expected:
        try:
            await conn.close(code=4401, reason="invalid bridge token")
        except Exception:
            pass
        return

    q = master.subscribe_events()
    registered: set[str] = set()

    async def pump_out() -> None:
        while True:
            ev = await q.get()
            sid = ev.get("sessionId")
            # Session events only to the session's owner; browser-level
            # events (no sessionId) go to every client.
            if sid and sid not in registered:
                continue
            await conn.send(json.dumps(ev))

    async def pump_in() -> None:
        async for raw in conn:
            try:
                msg = json.loads(raw if isinstance(raw, str) else raw.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            method = msg.get("method")
            if not method:
                continue
            mid = msg.get("id")
            params = msg.get("params") or {}
            csid = msg.get("sessionId")
            try:
                if method == "Target.attachToTarget" and not csid:
                    res = await master._acall(method, params, None, _CALL_TIMEOUT)
                    new_sid = res.get("sessionId")
                    if new_sid:
                        registered.add(new_sid)
                    if mid is not None:
                        await conn.send(json.dumps({"id": mid, "result": res}))
                    continue
                if not csid and method in _BROWSER_LEVEL:
                    res = await master._acall(method, params, None, _CALL_TIMEOUT)
                    if mid is not None:
                        await conn.send(json.dumps({"id": mid, "result": res}))
                    continue
                res = await master._acall(method, params, csid, _CALL_TIMEOUT)
                if mid is not None:
                    await conn.send(json.dumps({"id": mid, "result": res}))
            except CdpError as e:
                if mid is not None:
                    await conn.send(json.dumps(
                        {"id": mid, "error": {"code": -32603,
                                              "message": str(e)}}))
            except (asyncio.TimeoutError, TimeoutError):
                if mid is not None:
                    await conn.send(json.dumps(
                        {"id": mid, "error": {"code": -32603,
                                              "message": f"{method}: bridge timeout"}}))
            except Exception as e:
                if mid is not None:
                    await conn.send(json.dumps(
                        {"id": mid, "error": {"code": -32603,
                                              "message": f"{type(e).__name__}: {e}"}}))

    out_task = asyncio.ensure_future(pump_out())
    try:
        await pump_in()
    except Exception:
        pass
    finally:
        master.unsubscribe_events(q)
        out_task.cancel()
        # Sessions stay alive on the master: the profile's session outlives
        # any single client (persistent contract).


def start_bridge(master: MasterConnection, profile: str,
                 host: str = "127.0.0.1") -> tuple[int, str]:
    """Serve the CDP bridge on master's loop. Returns (port, token).

    Tries up to 3 free ports. Raises BridgeError if none bind. The expected
    token is registered per local port; _client_handler validates the URL
    path token against it.
    """
    from .keeper import _free_tcp_port, _klog

    token = secrets.token_hex(16)

    async def _aserve() -> int:
        last: Exception | None = None
        for _ in range(3):
            port = _free_tcp_port()

            async def handler(conn, _m=master):
                await _client_handler(conn, _m)

            try:
                await websockets.serve(
                    handler, host, port, max_size=64 * 1024 * 1024,
                    ping_interval=None)
                _active_tokens[port] = token
                return port
            except OSError as e:
                last = e
        raise BridgeError(f"bridge bind failed: {last}")

    fut = asyncio.run_coroutine_threadsafe(_aserve(), master._loop)
    port = fut.result(15)
    _klog(profile, f"bridge: listening on {host}:{port}")
    return port, token
