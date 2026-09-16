"""Page verbs: navigate, act, wait, read, grep, screenshot, pdf, download, upload, run, diff."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import snap, tabs as tabs_mod
from .cdp import CdpClient, CdpError

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
    from . import browser as browser_mod

    st = browser_mod.status_profile(name)
    if not st.get("live"):
        raise RuntimeError(f"profile {name!r} is not live; run `cloakctl open {name}` first")
    cdp = CdpClient(st["wsEndpoint"])
    cdp.connect()
    try:
        if tab:
            tid = tabs_mod._resolve_target(cdp, tab, name)
            cdp.bind_target(tid)
            return cdp, tid
        rec = tabs_mod.recorded_active(name, cdp)
        if rec:
            cdp.bind_target(rec)
            return cdp, rec
        active = cdp.active_page_target()
        if active is None:
            cdp.ensure_page_session()
            assert cdp._target_id is not None
            return cdp, cdp._target_id
        aid = tabs_mod.tid(active)
        cdp.bind_target(aid)
        return cdp, aid
    except Exception:
        cdp.close()
        raise


def do_navigate(name: str, action: str, url: str | None, tab: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        res = cdp.navigate_action(action, url)
        final = cdp.evaluate("location.href") or (url or "")
        title = cdp.evaluate("document.title") or ""
    return {"profile": name, "tab": tid[:8], "url": final, "title": title, **res}


def do_snapshot(name: str, tab: str | None, depth: int, mode: str) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        if mode == "text":
            text = cdp.evaluate("document.body ? document.body.innerText.slice(0,30000) : ''") or ""
            return {"profile": name, "tab": tid[:8], "mode": "text",
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
    return {"profile": name, "tab": tid[:8], "url": url, "changes": changes,
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
                    return {"profile": name, "tab": tid[:8], "clicked": ref, "at": [round(cx), round(cy)]}
                except CdpError:
                    obj = cdp.resolve_object(backend)
                    cdp.call_on(obj, "function(){ this.click(); }")
                    return {"profile": name, "tab": tid[:8], "clicked": ref, "via": "js"}
            if x is None or y is None:
                raise CdpError("act click: need --ref or --x/--y")
            cdp.mouse_click(x, y, button, count)
            return {"profile": name, "tab": tid[:8], "clicked": [x, y]}
        if kind == "type":
            if not ref or text is None:
                raise CdpError("act type: need --ref and --text")
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            cdp.call_on(obj, "function(){ this.focus(); }")
            cdp.insert_text(text)
            return {"profile": name, "tab": tid[:8], "typed": ref, "chars": len(text)}
        if kind == "clear":
            if not ref:
                raise CdpError("act clear: need --ref")
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            cdp.call_on(obj, "function(){ this.focus(); this.select(); }")
            cdp.press_key("Backspace")
            return {"profile": name, "tab": tid[:8], "cleared": ref}
        if kind == "key":
            if key is None:
                raise CdpError("act key: need --key")
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                cdp.call_on(cdp.resolve_object(backend), "function(){ this.focus(); }")
            cdp.press_key(key)
            return {"profile": name, "tab": tid[:8], "key": key}
        if kind == "hover":
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                cx, cy = cdp.box_center(backend)
            elif x is not None and y is not None:
                cx, cy = x, y
            else:
                raise CdpError("act hover: need --ref or --x/--y")
            cdp.mouse_move(cx, cy)
            return {"profile": name, "tab": tid[:8], "hover": [round(cx), round(cy)]}
        if kind == "scroll":
            if ref:
                backend = _backend(name, tab, ref, cdp, tid)
                obj = cdp.resolve_object(backend)
                cdp.call_on(obj, "function(){ this.scrollIntoView({block:'center'}); }")
                return {"profile": name, "tab": tid[:8], "scrolledTo": ref}
            if x is None or y is None:
                vw = cdp.evaluate("window.innerWidth/2") or 400
                vh = cdp.evaluate("window.innerHeight/2") or 300
                x, y = float(vw), float(vh)
            cdp.scroll_at(x, y, dx, dy)
            return {"profile": name, "tab": tid[:8], "scrolled": [x, y, dx, dy]}
        if kind == "select":
            if not ref or value is None:
                raise CdpError("act select: need --ref and --value")
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            ok = cdp.call_on(obj, "function(v){ this.value = v; this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true})); return this.value === v; }",
                             [{"value": value}])
            return {"profile": name, "tab": tid[:8], "selected": ref, "ok": bool(ok)}
        if kind == "focus":
            if not ref:
                raise CdpError("act focus: need --ref")
            backend = _backend(name, tab, ref, cdp, tid)
            cdp.call_on(cdp.resolve_object(backend), "function(){ this.focus(); }")
            return {"profile": name, "tab": tid[:8], "focused": ref}
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
            return {"profile": name, "tab": tid[:8], "filled": done,
                    "failed": [r for r, _ in pairs if r not in done]}
        if kind in ("check", "uncheck"):
            if not ref:
                raise CdpError(f"act {kind}: need --ref")
            want = kind == "check"
            backend = _backend(name, tab, ref, cdp, tid)
            obj = cdp.resolve_object(backend)
            state = cdp.call_on(obj, "function(w){ if(this.checked!==w){ this.click(); }"
                " return this.checked; }", [{"value": want}])
            return {"profile": name, "tab": tid[:8], kind + "ed": ref,
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
            return {"profile": name, "tab": tid[:8], "drag": [round(sx), round(sy), round(ex), round(ey)]}
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
                        return {"profile": name, "tab": tid[:8], "matched": f"text:{text}"}
                else:
                    assert selector is not None
                    found = cdp.evaluate(
                        f"!!document.querySelector({selector!r})")
                    if found:
                        return {"profile": name, "tab": tid[:8], "matched": f"selector:{selector}"}
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
            return {"profile": name, "tab": tid[:8], "url": url, "errors": cdp.console_errors()}
        if fmt == "links":
            links = cdp.evaluate(
                "[...document.querySelectorAll('a[href]')].slice(0,300).map(a=>({text:(a.innerText||'').trim().slice(0,120),href:a.href}))") or []
            return {"profile": name, "tab": tid[:8], "url": url, "links": links}
        if selector:
            text = cdp.evaluate(
                f"(()=>{{const el=document.querySelector({selector!r});return el?el.innerText.slice(0,30000):''}})()") or ""
        elif fmt == "markdown":
            text = cdp.evaluate(_MD_WALKER) or ""
        else:
            text = cdp.evaluate("document.body ? document.body.innerText.slice(0,30000) : ''") or ""
    return {"profile": name, "tab": tid[:8], "url": url, "format": fmt,
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
    return {"profile": name, "tab": tid[:8], "url": url, "pattern": pattern,
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
    return {"profile": name, "tab": tid[:8], "path": str(path), "bytes": len(raw)}


def do_pdf(name: str, tab: str | None, landscape: bool, background: bool,
           out: str | None) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    with cdp:
        raw = cdp.print_pdf(landscape, background)
    from . import paths as _paths
    _paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(out) if out else _paths.RUNTIME_DIR / f"page.{name}.{int(time.time())}.pdf"
    path.write_bytes(raw)
    return {"profile": name, "tab": tid[:8], "path": str(path), "bytes": len(raw)}


def do_download(name: str, tab: str | None, ref: str, out_dir: str,
                timeout: float = 30.0) -> dict[str, Any]:
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    cdp, tid = _page_client(name, tab)
    with cdp:
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
            after = {p.name for p in dest.iterdir()}
            new = [dest / n for n in (after - before)
                   if not n.endswith((".crdownload", ".part", ".download"))]
            done = [p for p in new if p.is_file()]
            if done:
                biggest = max(done, key=lambda p: p.stat().st_size)
                return {"profile": name, "tab": tid[:8], "path": str(biggest),
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
    for f in files:
        if not Path(f).is_file():
            raise CdpError(f"upload: not a file: {f}")
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
        cdp.set_input_files(obj, [str(Path(f).resolve()) for f in files])
    return {"profile": name, "tab": tid[:8], "uploaded": via, "files": files}


def do_run(name: str, tab: str | None, code: str, timeout: float = 30.0) -> dict[str, Any]:
    cdp, tid = _page_client(name, tab)
    old = cdp.timeout
    cdp.timeout = timeout
    try:
        with cdp:
            value = cdp.run_program(code)
    finally:
        cdp.timeout = old
    return {"profile": name, "tab": tid[:8], "value": value}
