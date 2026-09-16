"""Tab, window, and (logical) group management over the browser CDP endpoint.

Tab groups are logical labels stored in cloakctl meta (Chromium exposes no
CDP API for tab groups); they scope `tabs list` output, nothing more.
"""

from __future__ import annotations

import json
from typing import Any

from . import paths
from .cdp import CdpClient


def tid(t: dict) -> str:
    """Target id across CDP versions (id -> targetId rename)."""
    return str(t.get("targetId") or t.get("id") or "")


def _browser_client(name: str) -> tuple[CdpClient, str]:
    from . import browser as browser_mod

    st = browser_mod.status_profile(name)
    if not st.get("live"):
        raise RuntimeError(f"profile {name!r} is not live; run `cloakctl open {name}` first")
    return CdpClient(st["wsEndpoint"]), st["wsEndpoint"]


def _pages(cdp: CdpClient) -> list[dict]:
    return [t for t in cdp.list_targets()
            if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")]


def _groups(name: str) -> dict[str, str]:
    meta = paths.load_meta(name)
    g = meta.get("tabGroups")
    return dict(g) if isinstance(g, dict) else {}


def _save_groups(name: str, groups: dict[str, str]) -> None:
    meta = paths.load_meta(name)
    meta["tabGroups"] = groups
    paths.save_meta(name, meta)


def list_groups(name: str) -> dict[str, Any]:
    """Logical tab groups, self-healing: labels pointing at dead targetIds
    (crashed tabs, window closes outside close_tab) are dropped on read so
    stale labels can never accumulate across long-lived profiles."""
    from . import browser as browser_mod

    groups = _groups(name)
    if not groups:
        return {"profile": name, "groups": {}}
    try:
        st = browser_mod.status_profile(name)
        live = bool(st.get("live"))
        if live:
            cdp, _ = _browser_client(name)
            with cdp:
                alive = {tid(t) for t in _pages(cdp)}
        else:
            alive = set()
    except Exception:
        return {"profile": name, "groups": groups}
    pruned = {t: g for t, g in groups.items() if t in alive}
    if len(pruned) != len(groups):
        _save_groups(name, pruned)
    return {"profile": name, "groups": pruned,
            "droppedStale": len(groups) - len(pruned)}


def list_tabs(name: str) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        pages = _pages(cdp)
        wins: dict[str, list] = {}
        for t in pages:
            try:
                w = cdp.window_for_target(tid(t)).get("windowId")
            except Exception:
                w = "?"
            wins.setdefault(str(w), []).append(t)
    groups = _groups(name)
    tabs = [{"id": tid(t)[:8], "targetId": tid(t), "url": t.get("url", ""),
             "title": t.get("title", ""), "group": groups.get(tid(t), "")} for t in pages]
    return {"profile": name, "tabs": tabs,
            "windows": [{"windowId": w, "tabs": len(ts)} for w, ts in wins.items()]}


def new_tab(name: str, url: str = "about:blank", background: bool = False) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        t = cdp.create_target(url, background)
        if not background:
            try:
                cdp.activate_target(t["targetId"])
            except Exception:
                pass
            record_active(name, t["targetId"])
    return {"profile": name, "targetId": t["targetId"], "id": t["targetId"][:8], "url": url}


def close_tab(name: str, target: str) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        tid = _resolve_target(cdp, target, name)
        cdp.close_other_target(tid)
    groups = _groups(name)
    groups.pop(tid, None)
    _save_groups(name, groups)
    return {"profile": name, "closed": tid[:8]}


def record_active(name: str, target_id: str) -> None:
    """Remember the focused tab; headless CDP marks several tabs attached."""
    try:
        meta = paths.load_meta(name)
        meta["activeTab"] = target_id
        paths.save_meta(name, meta)
    except Exception:
        pass


def recorded_active(name: str, cdp: CdpClient) -> str | None:
    try:
        want = paths.load_meta(name).get("activeTab")
    except Exception:
        want = None
    if want and any(tid(t) == want for t in _pages(cdp)):
        return want
    return None


def _resolve_target(cdp: CdpClient, target: str, profile: str | None = None) -> str:
    if target == "active":  # macro-friendly alias for the visible tab
        if profile:
            rec = recorded_active(profile, cdp)
            if rec:
                return rec
        active = cdp.active_page_target()
        if active is not None:
            return tid(active)
        raise KeyError("no active tab")
    for t in _pages(cdp):
        i = tid(t)
        if i == target or (target and i.startswith(target)):
            return i
    raise KeyError(f"no tab matching {target!r}")


def activate_tab(name: str, target: str) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        tid = _resolve_target(cdp, target, name)
        cdp.activate_target(tid)
        record_active(name, tid)
    return {"profile": name, "active": tid[:8]}


def list_windows(name: str) -> dict[str, Any]:
    return list_tabs(name)


def activate_window(name: str, window_id: str) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        pages = _pages(cdp)
        for t in pages:
            try:
                w = str(cdp.window_for_target(tid(t)).get("windowId"))
            except Exception:
                continue
            if w == str(window_id):
                cdp.activate_target(tid(t))
                return {"profile": name, "window": w, "activeTab": tid(t)[:8]}
    raise KeyError(f"no window {window_id!r}")


def close_window(name: str, window_id: str) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        pages = _pages(cdp)
        tids = []
        for t in pages:
            try:
                w = str(cdp.window_for_target(tid(t)).get("windowId"))
            except Exception:
                continue
            if w == str(window_id):
                tids.append(tid(t))
        if not tids:
            raise KeyError(f"no window {window_id!r}")
        for tid in tids:
            try:
                cdp.close_other_target(tid)
            except Exception:
                pass
    groups = _groups(name)  # drop group labels of tabs that no longer exist
    for tid in tids:
        groups.pop(tid, None)
    _save_groups(name, groups)
    return {"profile": name, "window": str(window_id), "closedTabs": len(tids)}


def group_tabs(name: str, targets: list[str], group: str) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        tids = [_resolve_target(cdp, t, name) for t in targets]
    groups = _groups(name)
    for tid in tids:
        groups[tid] = group
    _save_groups(name, groups)
    return {"profile": name, "group": group, "tabs": [t[:8] for t in tids]}


def ungroup_tabs(name: str, targets: list[str]) -> dict[str, Any]:
    cdp, _ = _browser_client(name)
    with cdp:
        tids = [_resolve_target(cdp, t, name) for t in targets]
    groups = _groups(name)
    for tid in tids:
        groups.pop(tid, None)
    _save_groups(name, groups)
    return {"profile": name, "ungrouped": [t[:8] for t in tids]}


def rename_group(name: str, old: str, new: str) -> dict[str, Any]:
    groups = _groups(name)
    n = sum(1 for v in groups.values() if v == old)
    for k, v in list(groups.items()):
        if v == old:
            groups[k] = new
    _save_groups(name, groups)
    return {"profile": name, "from": old, "to": new, "tabs": n}
