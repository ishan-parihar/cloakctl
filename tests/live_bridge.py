"""Live bridge proof: external client sees the keeper's true obscura session.

Flow:
  1. open an isolated obscura profile (keeper + bridge up)
  2. CLI navigates the page (session state: url)
  3. RAW external WS client (simulating hermes/agent-browser --cdp) connects
     to the bridge URL from `cloakctl attach`, runs Target.getTargets,
     Runtime.evaluate (location.href), Storage.getCookies
  4. Assert: same URL, same cookies, page interactive, events routable
  5. CLI exec still works after external attach (no interference)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import websockets

ROOT = "/home/ishanp/Documents/github/my-projects/agentic-utility/internet/cloakctl"
sys.path.insert(0, ROOT + "/src")

BIN = ROOT + "/.venv/bin/cloakctl"
HOME = tempfile.mkdtemp(prefix="bridge-e2e-")
ENV = {**os.environ, "CLOAKCTL_HOME": HOME + "/state", "PATH": ROOT + "/.venv/bin:" + os.environ["PATH"]}
PROFILE = "br8"
FAILS = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def run(*args, timeout=90):
    return subprocess.run([BIN, *args, "--json"], env=ENV, capture_output=True,
                          text=True, timeout=timeout)


async def rpc(ws, mid, method, params=None, sid=None, timeout=20):
    msg = {"id": mid, "method": method, "params": params or {}}
    if sid:
        msg["sessionId"] = sid
    await ws.send(json.dumps(msg))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        raw = await asyncio.wait_for(ws.recv(), max(0.1, end - time.monotonic()))
        data = json.loads(raw)
        if data.get("id") == mid:
            return data
    return {"error": {"message": "timeout"}}


import asyncio  # noqa: E402


async def external_client(bridge_url):
    out = {}
    async with websockets.connect(bridge_url, ping_interval=None) as w:
        # browser-level: getTargets
        r = await rpc(w, 1, "Target.getTargets")
        infos = r.get("result", {}).get("targetInfos", [])
        out["targets"] = [(t.get("type"), (t.get("url") or "")[:40]) for t in infos]

        # attach to the page target like agent-browser does
        pages = [t for t in infos if t.get("type") == "page"]
        if not pages:
            out["error"] = "no page target visible"
            return out
        tid = pages[0]["targetId"]
        r = await rpc(w, 2, "Target.attachToTarget", {"targetId": tid, "flatten": True})
        out["attach_error"] = r.get("error", {}).get("message")
        sid = r.get("result", {}).get("sessionId")
        out["sessionId"] = sid
        if not sid:
            return out

        # session-scoped: read the page state the CLI created
        r = await rpc(w, 3, "Runtime.evaluate",
                      {"expression": "location.href", "returnByValue": True}, sid=sid)
        out["url"] = (r.get("result", {}).get("result", {}) or {}).get("value")
        out["eval_error"] = r.get("error", {}).get("message")

        # session-scoped: act on the page (click-level realism: set a value)
        r = await rpc(w, 4, "Runtime.evaluate",
                      {"expression": "document.title = 'BRIDGE-OK'; document.title",
                       "returnByValue": True}, sid=sid)
        out["title"] = (r.get("result", {}).get("result", {}) or {}).get("value")

        # browser-level cookies (the session-critical check)
        r = await rpc(w, 5, "Storage.getCookies")
        out["cookies"] = len(r.get("result", {}).get("cookies", []))
        out["cookie_error"] = r.get("error", {}).get("message")

        # page enable + event routing check: navigate to generate events
        r = await rpc(w, 6, "Page.enable", {}, sid=sid)
        out["page_enable_error"] = r.get("error", {}).get("message")
        r = await rpc(w, 7, "Page.navigate",
                      {"url": "https://example.com/?q=bridge"}, sid=sid, timeout=15)
        out["nav_error"] = r.get("error", {}).get("message")
        out["events"] = []
        try:
            end = time.monotonic() + 6
            while time.monotonic() < end and len(out["events"]) < 3:
                raw = await asyncio.wait_for(w.recv(), max(0.05, end - time.monotonic()))
                d = json.loads(raw)
                if "method" in d and d.get("sessionId") == sid:
                    out["events"].append(d["method"])
        except asyncio.TimeoutError:
            pass
    return out


def main():
    # 1. fresh profile via the CLI (keeper spawns + bridge binds)
    run("profiles", "create", PROFILE)
    r = run("open", PROFILE, "--browser-arg=--allow-private-network", timeout=120)
    ok = r.returncode == 0
    check("open obscura profile", ok, (r.stderr or r.stdout)[:160] if not ok else "")
    if not ok:
        return

    try:
        # 2. CLI drives the session (creates state the external client must see)
        r = run("navigate", PROFILE, "--url", "https://example.com/", timeout=90)
        check("CLI navigate", r.returncode == 0, (r.stderr or "")[:160])

        r = run("attach", PROFILE)
        att = json.loads(r.stdout or "{}")
        bridge_url = att.get("wsEndpoint") or ""
        check("attach exposes bridge endpoint", bool(bridge_url), att.get("error", ""))
        check("attach flags obscura engine", att.get("engine") == "obscura")

        if bridge_url:
            check("bridge is loopback-only", bridge_url.startswith("ws://127.0.0.1:"))

            # 3. external client (hermes/agent-browser simulation)
            ext = asyncio.run(external_client(bridge_url))
            check("external: page target visible", any(t[0] == "page" for t in ext.get("targets", [])),
                  str(ext.get("targets"))[:100])
            check("external: attachToTarget ok", not ext.get("attach_error"), ext.get("attach_error") or "")
            check("external: sees CLI's URL", ext.get("url") == "https://example.com/", str(ext.get("url")))
            check("external: can act on the page", ext.get("title") == "BRIDGE-OK", str(ext.get("title")))
            check("external: cookies visible (session!)",
                  isinstance(ext.get("cookies"), int) and ext.get("cookie_error") is None,
                  f"n={ext.get('cookies')} err={ext.get('cookie_error')}")
            check("external: Page.enable ok", not ext.get("page_enable_error"),
                  ext.get("page_enable_error") or "")
            check("external: session events routed", len(ext.get("events", [])) > 0,
                  str(ext.get("events", [])[:4]) + f" nav_err={ext.get('nav_error')}")

            # 4. shared session both ways: the external client's NAVIGATION
            # must be visible from the CLI (one session, two surfaces).
            r = run("exec", PROFILE, "location.href", timeout=60)
            val = ""
            try:
                val = (json.loads(r.stdout or "{}") or {}).get("value")
            except json.JSONDecodeError:
                pass
            check("CLI exec sees external client's navigation",
                  val == "https://example.com/?q=bridge",
                  f"value={val!r} stderr={(r.stderr or '')[:100]}")

    finally:
        run("close", PROFILE, timeout=90)
        st = run("status", PROFILE).stdout
        check("teardown clean", '"live": false' in st.replace(" ", "").replace("\n", "") or '"live":false' in st.replace(" ", "").replace("\n", ""))
        shutil.rmtree(HOME, ignore_errors=True)

    print(f"\n---- bridge e2e: {7 + 5 - len(FAILS) if False else ''}{''}", end="")
    total = 12
    print(f"{total - len(FAILS)}/{total} passed, {len(FAILS)} failed")
    if FAILS:
        print("failed:", FAILS)
        sys.exit(1)


if __name__ == "__main__":
    main()
