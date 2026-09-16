"""Minimal CDP client over WebSocket (websockets lib).

Speaks just enough DevTools Protocol for cloakctl:
- Target.createTarget / Target.attachToTarget (flat session binding)
- Storage.getCookies / Storage.setCookies
- Page.navigate (with load wait) / Runtime.evaluate

Design: one background thread owns one asyncio event loop for the lifetime of
the connection (websockets connections are loop-bound — creating a new loop
per call fails with "attached to a different loop"). Public methods are
synchronous and thread-safe enough for CLI use.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import websockets  # type: ignore


class CdpError(RuntimeError):
    pass


class CdpClient:
    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._next_id = 1
        self._session_id: str | None = None
        self._target_id: str | None = None
        self._owned_target = False  # only close targets WE created
        self._send_lock = threading.Lock()
        self._event_handlers: dict[str, list] = {}
        self.console_buffer: list[dict] = []

    # --- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "CdpClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        if self._ws is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="cloakctl-cdp")
        self._thread.start()
        try:
            async def _aconnect():
                return await websockets.connect(
                    self.ws_url, max_size=64 * 1024 * 1024
                )

            self._ws = self._run(_aconnect())
        except Exception:
            self._stop_loop()
            raise

    def on_event(self, method: str, handler) -> None:
        """Register a callback(event_dict) for a CDP event method."""
        self._event_handlers.setdefault(method, []).append(handler)

    def _dispatch_event(self, data: dict) -> None:
        method = data.get("method")
        if not method:
            return
        for handler in self._event_handlers.get(method, []):
            try:
                handler(data.get("params", {}))
            except Exception:
                pass

    def enable_console_capture(self) -> None:
        """Buffer console errors + JS exceptions for later drain."""
        self.ensure_page_session()
        try:
            self.call("Log.enable")
        except CdpError:
            pass
        try:
            self.call("Runtime.enable")
        except CdpError:
            pass
        self.on_event("Log.entryAdded", lambda p: self.console_buffer.append(
            {"level": p.get("entry", {}).get("level"), "text": p.get("entry", {}).get("text", "")[:500], "url": p.get("entry", {}).get("url", "")}))
        self.on_event("Runtime.exceptionThrown", lambda p: self.console_buffer.append(
            {"level": "error", "text": str(p.get("exceptionDetails", {}).get("text", "JS exception"))[:500], "url": ""}))

    def console_errors(self) -> list[dict]:
        errs = [e for e in self.console_buffer if str(e.get("level", "")).lower() in ("error",)]
        self.console_buffer = [e for e in self.console_buffer if str(e.get("level", "")).lower() not in ("error",)]
        return errs

    def close(self) -> None:
        if self._target_id is not None and self._owned_target:
            try:
                self._run(self._acall("Target.closeTarget", {"targetId": self._target_id}))
            except Exception:
                pass
            self._target_id = None
            self._session_id = None
            self._owned_target = False
        if self._ws is not None:
            try:
                self._run(self._ws.close())
            except Exception:
                pass
            self._ws = None
        self._stop_loop()

    def _stop_loop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None
            self._thread = None

    def _run(self, coro) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self.timeout)

    # --- command/response ----------------------------------------------------

    async def _acall(self, method: str, params: dict | None = None) -> Any:
        assert self._ws is not None
        msg_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if self._session_id:
            msg["sessionId"] = self._session_id
        await self._ws.send(json.dumps(msg))

        for _ in range(1000):
            raw = await asyncio.wait_for(self._ws.recv(), self.timeout)
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            if "method" in data and "id" not in data:
                self._dispatch_event(data)
                continue
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CdpError(f"{method}: {data['error'].get('message')}")
                return data.get("result", {})
            # Interleaved events are ignored here; navigate() consumes its own.
        raise CdpError(f"{method}: no response")

    def call(self, method: str, params: dict | None = None) -> Any:
        with self._send_lock:
            return self._run(self._acall(method, params))

    # --- domain helpers --------------------------------------------------------

    def ensure_page_session(self) -> str:
        """Attach a flat session to a fresh page target; bind subsequent calls."""
        if self._session_id:
            return self._session_id
        res = self.call("Target.createTarget", {"url": "about:blank"})
        self._target_id = res["targetId"]
        self._owned_target = True
        res = self.call("Target.attachToTarget", {"targetId": self._target_id, "flatten": True})
        self._session_id = res["sessionId"]
        return self._session_id

    def bind_target(self, target_id: str) -> str:
        """Attach to an EXISTING page target (a tab). Never closed by us."""
        res = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self._target_id = target_id
        self._owned_target = False
        self._session_id = res["sessionId"]
        return self._session_id

    # --- browser-level targets (no page session needed) ------------------------

    def list_targets(self) -> list[dict]:
        return self.call("Target.getTargets").get("targetInfos", [])

    def create_target(self, url: str = "about:blank", background: bool = False) -> dict:
        return self.call("Target.createTarget", {"url": url, "background": background})

    def activate_target(self, target_id: str) -> None:
        self.call("Target.activateTarget", {"targetId": target_id})

    def close_other_target(self, target_id: str) -> None:
        self.call("Target.closeTarget", {"targetId": target_id})

    def active_page_target(self) -> dict | None:
        """The currently active page target, or None."""
        for t in self.list_targets():
            if t.get("type") == "page" and not t.get("url", "").startswith("devtools://"):
                if t.get("attached"):
                    return t
        for t in self.list_targets():
            if t.get("type") == "page" and not t.get("url", "").startswith("devtools://"):
                return t
        return None

    def window_for_target(self, target_id: str) -> dict:
        return self.call("Browser.getWindowForTarget", {"targetId": target_id})

    def set_window_bounds(self, window_id: int, bounds: dict) -> None:
        self.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})

    # --- navigation ------------------------------------------------------------

    def navigate_action(self, action: str, url: str | None = None) -> dict:
        """url | back | forward | reload on the bound page."""
        self.ensure_page_session()
        if action == "url":
            if not url:
                raise CdpError("navigate url: missing url")
            return self.navigate(url)
        if action == "reload":
            self.call("Page.reload")
            return {"action": "reload"}
        if action in ("back", "forward"):
            hist = self.call("Page.getNavigationHistory")
            idx = hist.get("currentIndex", 0)
            entries = hist.get("entries", [])
            nxt = idx - 1 if action == "back" else idx + 1
            if not entries or nxt < 0 or nxt >= len(entries):
                raise CdpError(f"navigate {action}: no history entry")
            self.call("Page.navigateToHistoryEntry", {"entryId": entries[nxt]["id"]})
            return {"action": action, "url": entries[nxt].get("url")}
        raise CdpError(f"navigate: unknown action {action!r}")

    # --- accessibility snapshot --------------------------------------------------

    def ax_tree(self) -> list[dict]:
        """Full accessibility tree of the bound page."""
        self.ensure_page_session()
        try:
            self.call("Accessibility.enable")
        except CdpError:
            pass
        return self.call("Accessibility.getFullAXTree").get("nodes", [])

    # --- DOM geometry + input ------------------------------------------------------

    def box_center(self, backend_node_id: int) -> tuple[float, float]:
        box = self.call("DOM.getBoxModel", {"backendNodeId": backend_node_id})
        quad = box.get("model", {}).get("content", box.get("model", {}).get("border", []))
        if len(quad) < 8:
            raise CdpError("act: element has no visible box")
        xs = quad[0::2]
        ys = quad[1::2]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def resolve_object(self, backend_node_id: int) -> str:
        res = self.call("DOM.resolveNode", {"backendNodeId": backend_node_id})
        obj_id = res.get("object", {}).get("objectId")
        if not obj_id:
            raise CdpError("act: cannot resolve element")
        return obj_id

    def call_on(self, object_id: str, function: str, args: list | None = None) -> Any:
        res = self.call("Runtime.callFunctionOn", {
            "objectId": object_id, "functionDeclaration": function,
            "arguments": args or [], "returnByValue": True, "awaitPromise": True,
        })
        exc = res.get("exceptionDetails")
        if exc:
            raise CdpError(f"act: {exc.get('text', 'JS exception')}")
        return res.get("result", {}).get("value")

    def mouse_click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": button, "clickCount": count})

    def mouse_down(self, x: float, y: float, button: str = "left") -> None:
        self.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": 1})

    def mouse_up(self, x: float, y: float, button: str = "left") -> None:
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": 1})

    def mouse_move(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", {"text": text})

    _KEY_CODES = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46,
                  "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
                  "Home": 36, "End": 35, " ": 32}

    def press_key(self, key: str) -> None:
        if len(key) == 1:
            self.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": key})
            self.call("Input.dispatchKeyEvent", {"type": "keyUp"})
            return
        code = self._KEY_CODES.get(key, 13 if key == "Return" else 0)
        for kind in ("rawKeyDown", "keyUp"):
            params: dict[str, Any] = {"type": kind, "key": key, "code": key,
                                      "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
            if kind == "rawKeyDown" and key == "Enter":
                params["text"] = "\r"
            self.call("Input.dispatchKeyEvent", params)

    def scroll_at(self, x: float, y: float, dx: float, dy: float) -> None:
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})

    def set_input_files(self, object_id: str, files: list[str]) -> None:
        self.call("DOM.setFileInputFiles", {"objectId": object_id, "files": files})

    # --- capture -------------------------------------------------------------------

    def capture_screenshot(self, fmt: str = "jpeg", quality: int = 80, full_page: bool = False) -> bytes:
        import base64

        params: dict[str, Any] = {"format": fmt, "captureBeyondViewport": full_page}
        if fmt == "jpeg":
            params["quality"] = quality
        res = self.call("Page.captureScreenshot", params)
        return base64.b64decode(res["data"])

    def print_pdf(self, landscape: bool = False, background: bool = False) -> bytes:
        import base64

        res = self.call("Page.printToPDF", {"landscape": landscape, "printBackground": background})
        return base64.b64decode(res["data"])

    # --- downloads -------------------------------------------------------------------

    def allow_downloads(self, path: str) -> None:
        self.call("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": path, "eventsEnabled": True})

    # --- programs ----------------------------------------------------------------------

    def run_program(self, code: str) -> Any:
        """Evaluate possibly-async JS (awaitPromise=True).

        Top-level await is NOT valid in a bare evaluate expression, so a
        single expression containing `await` runs as `return await (code)`
        inside an async IIFE. Statement batches with await are not supported
        (write the IIFE explicitly). Sync code evaluates directly.
        """
        self.ensure_page_session()
        expr = f"(async () => {{ return await ({code}); }})()" if "await" in code else code
        res = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        exc = res.get("exceptionDetails")
        if exc:
            raise CdpError(f"run: {exc.get('text', 'JS exception')}")
        result = res.get("result", {})
        if result.get("subtype") == "error":
            raise CdpError(f"run: {result.get('description', 'JS error')}")
        return result.get("value")

    def get_cookies(self, urls: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {"urls": urls} if urls else {}
        return self.call("Storage.getCookies", params).get("cookies", [])

    def set_cookies(self, cookies: list[dict]) -> None:
        if cookies:
            self.call("Storage.setCookies", {"cookies": cookies})

    async def _anavigate_and_wait(self, url: str, load_timeout: float) -> dict:
        assert self._ws is not None
        res = await self._acall("Page.navigate", {"url": url})
        deadline = asyncio.get_event_loop().time() + load_timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break  # best-effort: return even if load never fired
            try:
                raw = await asyncio.wait_for(self._ws.recv(), remaining)
            except (asyncio.TimeoutError, TimeoutError):
                break
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            if "method" in data and "id" not in data:
                self._dispatch_event(data)
            if (
                data.get("method") == "Page.loadEventFired"
                and data.get("sessionId") == self._session_id
            ):
                break
        return res

    def navigate(self, url: str, load_timeout: float = 30.0) -> dict:
        """Navigate the bound page; wait for load (best-effort)."""
        self.ensure_page_session()
        self.call("Page.enable")  # needed for loadEventFired
        with self._send_lock:
            return self._run(self._anavigate_and_wait(url, load_timeout))

    def evaluate(self, expression: str) -> Any:
        self.ensure_page_session()
        res = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        # A thrown JS exception is NOT a protocol error — surface it, or
        # agents cannot distinguish success-null from failure.
        exc = res.get("exceptionDetails")
        if exc:
            text = exc.get("text") or (
                exc.get("exception", {}).get("description", "JS exception")
            )
            raise CdpError(f"evaluate: {text}")
        result = res.get("result", {})
        if result.get("subtype") == "error":
            raise CdpError(f"evaluate: {result.get('description', 'JS error')}")
        return result.get("value")


def http_json(url: str, timeout: float = 5.0) -> dict:
    """GET a DevTools HTTP endpoint (e.g. /json/version) without heavy deps."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def cdp_call(fn, *args, **kwargs):
    """Run a blocking CdpClient method in a worker thread (for asyncio callers)."""
    return asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


__all__ = ["CdpClient", "CdpError", "cdp_call", "http_json"]
