"""Page verbs: navigate, act, wait, read, grep, screenshot, pdf, download, upload, run, diff."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import snap, tabs as tabs_mod
from .cdp import CdpClient, CdpError
from .locks import read_lock as _read_lock

_MD_WALKER = """(() => {
  const out = [];
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','PATH']);
  function walk(el, listDepth) {
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        const t = n.textContent.replace(/\\s+/g, ' ').trim();
        if (t) out.push('  '.repeat(listDepth) + t);
      } else if (n.nodeType === 1 && !skip.has(n.tagName)) {
        const tag = n.tagName;
        if (/^H[1-6]$/.test(tag)) out.push('#'.repeat(+tag[1]) + ' ' + n.innerText.replace(/\\s+/g,' ').trim());
        else if (tag === 'A' && n.href) out.push(`[${n.innerText.replace(/\\s+/g,' ').trim()}](${n.href})`);
        else if (tag === 'LI') { out.push('- ' + n.innerText.replace(/\\s+/g,' ').trim()); }
        else if (tag === 'UL' || tag === 'OL') walk(n, listDepth + 1);
        else if (tag === 'BR') out.push('');
        else walk(n, listDepth);
      }
    }
  }
  walk(document.body || document.documentElement, 0);
  return out.join('\\n').slice(0, 60000);
})()"""


def _page_client(name: str, tab: str | None = None) -> tuple[CdpClient, str]:
    """Client bound to the profile's page session, whichever engine.

    obscura: keeper passthrough (the persistent page IS the session; tab
    selection only scopes snapshot refs).
    """
    from .cdp import open_client

    cdp = open_client(name)
    try:
        cdp.connect()  # no-op for keeper-bound; direct-WS clients need the loop up before target resolution
        if tab and tab != "active":
            tid = tabs_mod._resolve_target(cdp, tab, name)
        else:
            rec = tabs_mod.recorded_active(name, cdp)
            if rec:
                tid = rec
            else:
                active = cdp.active_page_target()
                if active is not None:
                    tid = tabs_mod.tid(active)
                else:
                    cdp.ensure_page_session()
                    tid = cdp._target_id or "default"
        cdp.bind_target(tid)
        # Record what we ACTUALLY bound: the next verb's `active` alias
        # must resolve to this page, not an arbitrary one (otherwise two
        # verbs in a row can bind different pages and silently disagree).
        try:
            tabs_mod.record_active(name, tid)
        except Exception:
            pass
        return cdp, tid
    except Exception:
        cdp.close()
        raise


def do_navigate(name: str, action: str, url: str | None, tab: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        res = cdp.navigate_action(action, url)
        final = cdp.evaluate("location.href") or (url or "")
        title = cdp.evaluate("document.title") or ""
    return {"profile": name, "tab": str(tid)[:8], "url": final, "title": title, **res}


def do_snapshot(name: str, tab: str | None, depth: int, mode: str) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        if mode == "text":
            text = cdp.evaluate("document.body ? document.body.innerText.slice(0,30000) : ''") or ""
            return {"profile": name, "tab": str(tid)[:8], "mode": "text",
                    "snapshot": snap.trust_wrap(text, cdp.evaluate("location.href") or "")}
        res = snap.take_snapshot(cdp, name, tid, depth)
    res["profile"] = name
    res.pop("raw", None)
    return res


def do_diff(name: str, tab: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        nodes = cdp.ax_tree()
        url = cdp.evaluate("location.href") or ""
    text, refmap = snap.render_snapshot(nodes, url)
    changes = snap.diff_snapshots(name, tid, text)
    ref_path, txt_path = snap._ref_paths(name, tid)
    from . import paths as _paths
    from .util import atomic_write_json, atomic_write_text
    _paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ref_path, {"url": url, "refs": refmap})
    atomic_write_text(txt_path, text)
    return {"profile": name, "tab": str(tid)[:8], "url": url, "changes": changes,
            "changeCount": len(changes.splitlines())}


def _backend(name: str, tab: str | None, ref: str, cdp: CdpClient, tid: str) -> int:
    try:
        return snap.load_ref(name, tid, ref)
    except KeyError as e:
        raise CdpError(str(e))


def _fill_one(cdp, backend: int, text: str) -> bool:
    """Set an input's value directly with proper events (neo fill parity)."""
    obj = cdp.resolve_object(backend)
    return bool(cdp.call_on(obj, "function(v){ this.focus(); this.value = v;"
        " this.dispatchEvent(new Event('input',{bubbles:true}));"
        " this.dispatchEvent(new Event('change',{bubbles:true}));"
        " return this.value === v; }", [{"value": text}]))


def do_act(name: str, kind: str, tab: str | None, ref: str | None = None,
         text: str | None = None, key: str | None = None, x: float | None = None,
         y: float | None = None, dx: float = 0, dy: float = 500,
         button: str = "left", count: int = 1, value: str | None = None,
         fields: list[str] | None = None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        if kind == "click":
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                try:
                    cx, cy = cdp.box_center(backend)
                    cdp.mouse_click(cx, cy, button, count)
                    return {"profile": name, "tab": str(tid)[:8], "clicked": ref, "at": [round(cx), round(cy)]}
                except CdpError:
                    obj = cdp.resolve_object(backend)
                    cdp.call_on(obj, "function(){ this.click(); }")
                    return {"profile": name, "tab": str(tid)[:8], "clicked": ref, "via": "js"}
            if x is None or y is None:
                raise CdpError("act click: need --ref or --x/--y")
            cdp.mouse_click(x, y, button, count)
            return {"profile": name, "tab": str(tid)[:8], "clicked": [x, y]}
        if kind == "type":
            if not ref or text is None:
                raise CdpError("act type: need --ref and --text")
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            cdp.call_on(obj, "function(){ this.focus(); }")
            cdp.insert_text(text)
            return {"profile": name, "tab": str(tid)[:8], "typed": ref, "chars": len(text)}
        if kind == "clear":
            if not ref:
                raise CdpError("act clear: need --ref")
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            cdp.call_on(obj, "function(){ this.focus(); this.select(); }")
            cdp.press_key("Backspace")
            return {"profile": name, "tab": str(tid)[:8], "cleared": ref}
        if kind == "key":
            if key is None:
                raise CdpError("act key: need --key")
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                cdp.call_on(cdp.resolve_object(backend), "function(){ this.focus(); }")
            cdp.press_key(key)
            return {"profile": name, "tab": str(tid)[:8], "key": key}
        if kind == "hover":
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                cx, cy = cdp.box_center(backend)
            elif x is not None and y is not None:
                cx, cy = x, y
            else:
                raise CdpError("act hover: need --ref or --x/--y")
            cdp.mouse_move(cx, cy)
            return {"profile": name, "tab": str(tid)[:8], "hover": [round(cx), round(cy)]}
        if kind == "scroll":
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                obj = cdp.resolve_object(backend)
                cdp.call_on(obj, "function(){ this.scrollIntoView({block:'center'}); }")
                return {"profile": name, "tab": str(tid)[:8], "scrolledTo": ref}
            if x is None or y is None:
                vw = cdp.evaluate("window.innerWidth/2") or 400
                vh = cdp.evaluate("window.innerHeight/2") or 300
                x, y = float(vw), float(vh)
            cdp.scroll_at(x, y, dx, dy)
            return {"profile": name, "tab": str(tid)[:8], "scrolled": [x, y, dx, dy]}
        if kind == "select":
            if not ref or value is None:
                raise CdpError("act select: need --ref and --value")
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            ok = cdp.call_on(obj, "function(v){ this.value = v; this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true})); return this.value === v; }",
                             [{"value": value}])
            return {"profile": name, "tab": str(tid)[:8], "selected": ref, "ok": bool(ok)}
        if kind == "focus":
            if not ref:
                raise CdpError("act focus: need --ref")
            backend = _backend(name, tab, ref, cdp, tid)
            cdp.call_on(cdp.resolve_object(backend), "function(){ this.focus(); }")
            return {"profile": name, "tab": str(tid)[:8], "focused": ref}
        if kind == "fill":
            pairs: list[tuple[str, str]] = []
            if ref is not None and text is not None:
                pairs.append((ref, text))
            for f in fields or []:
                r, _, v = f.partition("=")
                if not r or not v:
                    raise CdpError(f"act fill: bad --field {f!r}, want ref=value")
                pairs.append((r, v))
            if not pairs:
                raise CdpError("act fill: need --ref+--text or --field ref=value")
            done = [r for r, v in pairs
                    if _fill_one(cdp, _backend(name, tab, r, cdp, tid), v)]
            return {"profile": name, "tab": str(tid)[:8], "filled": done,
                    "failed": [r for r, _ in pairs if r not in done]}
        if kind in ("check", "uncheck"):
            if not ref:
                raise CdpError(f"act {kind}: need --ref")
            want = kind == "check"
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            state = cdp.call_on(obj, "function(w){ if(this.checked!==w){ this.click(); }"
                " return this.checked; }", [{"value": want}])
            return {"profile": name, "tab": str(tid)[:8], kind + "ed": ref,
                    "checked": bool(state)}
        if kind == "drag":
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                sx, sy = cdp.box_center(backend)
            elif x is not None and y is not None:
                sx, sy = x, y
            else:
                raise CdpError("act drag: need --ref or --x/--y plus --dx/--dy")
            ex, ey = sx + dx, sy + dy
            cdp.mouse_move(sx, sy)
            cdp.mouse_down(sx, sy, button)
            cdp.mouse_move(ex, ey)
            cdp.mouse_up(ex, ey, button)
            return {"profile": name, "tab": str(tid)[:8], "drag": [round(sx), round(sy), round(ex), round(ey)]}
    raise CdpError(f"act: unknown kind {kind!r} (click|type|clear|key|hover|scroll|select|focus|fill|check|uncheck|drag)")


def do_wait(name: str, tab: str | None, text: str | None, selector: str | None,
            timeout: float = 15.0) -> dict[str, Any]:
    if not text and not selector:
        raise CdpError("wait: need --text or --selector")
    cdp, tid = _page_client(name, tab)
    deadline = time.monotonic() + timeout
    with cdp:
        while True:
            try:  # one slow poll must not abort the whole wait
                if text:
                    body = cdp.evaluate("document.body ? document.body.innerText.slice(0,60000) : ''") or ""
                    if text.lower() in body.lower():
                        return {"profile": name, "tab": str(tid)[:8], "matched": f"text:{text}"}
                else:
                    assert selector is not None
                    found = cdp.evaluate(
                        f"!!document.querySelector({selector!r})")
                    if found:
                        return {"profile": name, "tab": str(tid)[:8], "matched": f"selector:{selector}"}
            except CdpError:
                pass
            if time.monotonic() >= deadline:
                raise CdpError(f"wait: timeout after {timeout}s")
            time.sleep(0.5)


def do_read(name: str, tab: str | None, fmt: str, selector: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        url = cdp.evaluate("location.href") or ""
        if fmt == "console":
            cdp.enable_console_capture()
            time.sleep(1.0)
            return {"profile": name, "tab": str(tid)[:8], "url": url, "errors": cdp.console_errors()}
        if fmt == "links":
            links = cdp.evaluate(
                "[...document.querySelectorAll('a[href]')].slice(0,300).map(a=>({text:(a.innerText||'').trim().slice(0,120),href:a.href}))") or []
            return {"profile": name, "tab": str(tid)[:8], "url": url, "links": links}
        if selector:
            text = cdp.evaluate(
                f"(()=>{{const el=document.querySelector({selector!r});return el?el.innerText.slice(0,30000):''}})()") or ""
        elif fmt == "markdown":
            text = cdp.evaluate(_MD_WALKER) or ""
        else:
            text = cdp.evaluate("document.body ? document.body.innerText.slice(0,30000) : ''") or ""
    return {"profile": name, "tab": str(tid)[:8], "url": url, "format": fmt,
            "content": snap.trust_wrap(text, url or "unknown")}


def do_grep(name: str, tab: str | None, pattern: str, over: str, limit: int) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        url = cdp.evaluate("location.href") or ""
        if over == "ax":
            nodes = cdp.ax_tree()
            text, _ = snap.render_snapshot(nodes, url)
        else:
            text = cdp.evaluate("document.body ? document.body.innerText.slice(0,60000) : ''") or ""
    matches = snap.grep_lines(text, pattern, limit)
    return {"profile": name, "tab": str(tid)[:8], "url": url, "pattern": pattern,
            "matches": snap.trust_wrap("\n".join(matches), url or "unknown"), "count": len(matches)}


def _shot_path(name: str, fmt: str) -> Path:
    from . import paths as _paths
    _paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return _paths.RUNTIME_DIR / f"shot.{name}.{int(time.time())}.{fmt if fmt != 'jpeg' else 'jpg'}"


def do_screenshot(name: str, tab: str | None, fmt: str, quality: int,
                  full: bool, out: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        raw = cdp.capture_screenshot(fmt, quality, full)
    path = Path(out) if out else _shot_path(name, fmt)
    path.write_bytes(raw)
    return {"profile": name, "tab": str(tid)[:8], "path": str(path), "bytes": len(raw)}


def do_pdf(name: str, tab: str | None, landscape: bool, background: bool,
           out: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        raw = cdp.print_pdf(landscape, background)
    from . import paths as _paths
    _paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(out) if out else _paths.RUNTIME_DIR / f"page.{name}.{int(time.time())}.pdf"
    path.write_bytes(raw)
    return {"profile": name, "tab": str(tid)[:8], "path": str(path), "bytes": len(raw)}


def _is_remote(name: str) -> bool:
    info = _read_lock(name)
    return bool(info and info.remote)


def do_download(name: str, tab: str | None, ref: str, out_dir: str,
                timeout: float = 30.0) -> dict[str, Any]:
    from .engines import ENGINE_OBScura
    from .keeper import KeeperError

    info = _read_lock(name)
    remote = bool(info and info.remote)
    if info is not None and not remote and info.engine == ENGINE_OBScura:
        raise CdpError(
            "download: not supported by the obscura engine (its CDP accepts "
            "Browser.setDownloadBehavior but no file is written and no "
            "downloadProgress events fire). Fetch bytes with `cloakctl run "
            "'await fetch(url).then(r=>r.blob())'`-style JS or download "
            "outside the browser; use --engine cloakbrowser for CDP downloads.")
    if remote:
        # Remote is Chromium-family by contract; if the endpoint fronts an
        # obscura anyway, downloads will silently land nowhere. Detect it.
        try:
            cdp_probe = CdpClient(info.ws_endpoint, timeout=8)
            with cdp_probe:
                ver = cdp_probe.call("Browser.getVersion")
            if re.fullmatch(r"Chrome/\d+\.0\.0\.0", str(ver.get("product") or "")):
                raise CdpError(
                    "download: the remote endpoint fronts an obscura engine, "
                    "which writes no download files and fires no progress "
                    "events. Remote downloads need a Chromium-family browser "
                    "on the host (open --engine cloakbrowser there).")
        except CdpError:
            raise
        except Exception:
            pass  # probe is best-effort; the verb itself will report errors
    dest = Path(out_dir)
    if not remote:
        dest.mkdir(parents=True, exist_ok=True)
    cdp, tid = _page_client(name, tab)
    with cdp:
        before: set[str] = set()
        completed: dict[str, str] = {}
        dl_dir = out_dir
        if remote:
            # Browser-side files are invisible here: confirm completion via
            # downloadProgress events, not the local fs. downloadPath is
            # mandatory and browser-side; out_dir usually doesn't exist
            # there, so fall back to the host /tmp (documented).
            cdp.on_event("Browser.downloadProgress",
                         lambda p: completed.update(
                             {p.get("guid", ""): p.get("state", "")}))
            try:
                cdp.call("Browser.setDownloadBehavior",
                         {"behavior": "allow", "downloadPath": out_dir,
                          "eventsEnabled": True})
            except CdpError:
                dl_dir = "/tmp"
                cdp.call("Browser.setDownloadBehavior",
                         {"behavior": "allow", "downloadPath": "/tmp",
                          "eventsEnabled": True})
        else:
            cdp.allow_downloads(str(dest))
            before = {p.name for p in dest.iterdir()}
        backend = _backend(name, tab, ref, cdp, tid)
        try:
            cx, cy = cdp.box_center(backend)
            cdp.mouse_click(cx, cy)
        except CdpError:
            cdp.call_on(cdp.resolve_object(backend), "function(){ this.click(); }")
        deadline = time.monotonic() + timeout
        while True:
            if remote:
                try:
                    cdp.call("Browser.getVersion")  # pump: dispatch events
                except CdpError:
                    pass
                guids = [g for g, s in completed.items() if s == "completed"]
                if guids:
                    return {"profile": name, "tab": str(tid)[:8], "remote": True,
                            "guid": guids[0], "dir": dl_dir,
                            "note": "remote browser: file landed browser-side "
                                    f"under {dl_dir}"}
                if time.monotonic() >= deadline:
                    raise CdpError(
                        f"download: timeout after {timeout}s (remote)")
                time.sleep(0.5)
                continue
            after = {p.name for p in dest.iterdir()}
            new = [dest / n for n in (after - before)
                   if not n.endswith((".crdownload", ".part", ".download"))]
            done = [p for p in new if p.is_file()]
            if done:
                biggest = max(done, key=lambda p: p.stat().st_size)
                return {"profile": name, "tab": str(tid)[:8], "path": str(biggest),
                        "bytes": biggest.stat().st_size}
            if time.monotonic() >= deadline:
                raise CdpError(f"download: timeout after {timeout}s")
            time.sleep(0.5)


def object_for_selector(cdp, selector: str) -> str:
    """Resolve a CSS selector to a Runtime objectId (no snapshot ref needed)."""
    doc = cdp.call("DOM.getDocument", {"depth": 0})
    root = doc.get("root", {}).get("nodeId")
    found = cdp.call("DOM.querySelector", {"nodeId": root, "selector": selector})
    node_id = found.get("nodeId")
    if not node_id:
        raise CdpError(f"no element matching {selector!r}")
    desc = cdp.call("DOM.describeNode", {"nodeId": node_id})
    backend = desc.get("node", {}).get("backendNodeId")
    if backend is None:
        raise CdpError(f"cannot resolve {selector!r}")
    return cdp.resolve_object(backend)


def do_upload(name: str, tab: str | None, ref: str | None, files: list[str],
              selector: str | None = None) -> dict[str, Any]:
    from .engines import ENGINE_OBScura

    info = _read_lock(name)
    remote = bool(info and info.remote)
    obscura = bool(info and not remote and info.engine == ENGINE_OBScura)
    for f in files:
        if not Path(f).is_file():
            hint = (" (remote profile: stage the file on the BROWSER host "
                    "first — paths resolve browser-side)" if remote else "")
            raise CdpError(f"upload: not a file: {f}{hint}")
    if not ref and not selector:
        raise CdpError("upload: need --ref or --selector")
    cdp, tid = _page_client(name, tab)
    with cdp:
        if selector:
            obj = object_for_selector(cdp, selector)
            via = selector
        else:
            assert ref is not None
            obj = cdp.resolve_object(_backend(name, tab, ref, cdp, tid))
            via = ref
        try:
            # Cross-engine guard: chromium rejects non-file inputs at the CDP
            # layer, obscura silently accepts them. Fail the same way on both.
            is_file = cdp.call_on(obj, "function(){ return this.tagName === 'INPUT' "
                                       "&& String(this.type).toLowerCase() === 'file'; }")
            if not is_file:
                raise CdpError("upload: element is not a file input "
                               "(use input[type=file])")
            cdp.set_input_files(obj, [str(Path(f).resolve()) for f in files])
        except CdpError as e:
            if remote:
                raise CdpError(f"{e} (remote profile: paths resolve on the "
                               f"BROWSER host, not this machine)")
            if obscura and "disabled" in str(e).lower():
                raise CdpError(
                    f"{e} — obscura gates DOM.setFileInputFiles behind "
                    "--allow-file-access; re-open with "
                    "`cloakctl open <profile> --browser-arg=--allow-file-access`")
            raise
    doc: dict[str, Any] = {"profile": name, "tab": str(tid)[:8],
                           "uploaded": via, "files": files}
    if remote:
        doc["remote"] = True
    return doc


def do_run(name: str, tab: str | None, code: str, timeout: float = 30.0) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    old = cdp.timeout
    cdp.timeout = timeout
    try:
        with cdp:
            value = cdp.run_program(code)
    finally:
        cdp.timeout = old
    return {"profile": name, "tab": str(tid)[:8], "value": value}
