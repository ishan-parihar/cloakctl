"""cloakctl CLI — verbs over JSON output.

Every command supports --json for agent consumption. Cookie values are never
printed by any command.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import browser, cookies, hist, pageops, paths, skills, snap, tabs, workflows


def _toon_scalar(v) -> str:
    """One TOON cell/field: numbers, bools, null bare; strings quoted only when needed."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s.startswith("~") or len(s) > 160:
        n = len(s)
        s = s[:120] + f"…(+{n - 120} chars)"
    if any(c in s for c in (",", "\"", "\n")):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


def _toon(data, _depth: int = 0) -> str:
    """Render a JSON-shaped doc as TOON (token-oriented output) at the boundary.
    Internal logic stays JSON — this is display-only (AXI §1)."""
    pad = "  " * _depth
    if isinstance(data, dict):
        if not data:
            return f"{pad}{{}}"
        lines = []
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                if isinstance(v, list) and v and all(not isinstance(i, (dict, list)) for i in v):
                    lines.append(f"{pad}{k}[{len(v)}]: " + ", ".join(_toon_scalar(i) for i in v))
                elif isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
                    keys: list[str] = []
                    for item in v:
                        for ik in item:
                            if ik not in keys:
                                keys.append(ik)
                    lines.append(f"{pad}{k}[{len(v)}]{{{','.join(keys)}}}:")
                    for item in v:
                        lines.append(f"{pad}  " + ",".join(_toon_scalar(item.get(kk)) for kk in keys))
                elif not v:
                    lines.append(f"{pad}{k}[0]:")
                else:
                    lines.append(f"{pad}{k}:")
                    lines.append(_toon(v, _depth + 1))
            else:
                lines.append(f"{pad}{k}: {_toon_scalar(v)}")
        return "\n".join(lines)
    if isinstance(data, list):
        if not data:
            return f"{pad}[]"
        if all(not isinstance(i, (dict, list)) for i in data):
            return f"{pad}" + ", ".join(_toon_scalar(i) for i in data)
        return "\n".join(f"{pad}- {_toon(i, _depth + 1).lstrip()}" if isinstance(i, (dict, list))
                         else f"{pad}- {_toon_scalar(i)}" for i in data)
    return f"{pad}{_toon_scalar(data)}"


# Output mode for _emit, set once in main(). Module-level so the 40+ call
# sites stay untouched: cmd functions are also called directly by tests.
_OUT_TOON = False


def _emit(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif _OUT_TOON:
        print(_toon(data))
    else:
        for k, v in data.items():
            if isinstance(v, list):
                print(f"{k}:")
                for item in v:
                    if isinstance(item, dict):
                        print(f"  - {item.get('profile', item)}")
                        for ik, iv in item.items():
                            if ik != "profile":
                                print(f"      {ik}: {iv}")
                    else:
                        print(f"  - {item}")
            elif isinstance(v, dict):
                print(f"{k}:")
                for ik, iv in v.items():
                    print(f"  {ik}: {iv}")
            else:
                print(f"{k}: {v}")


def cmd_profiles(args) -> int:
    if args.profile_cmd == "create":
        paths.ensure_layout()
        paths.profile_dir(args.name).mkdir(parents=True, exist_ok=True)
        meta = paths.load_meta(args.name)
        paths.save_meta(args.name, meta)
        _emit({"created": args.name, "path": str(paths.profile_dir(args.name))}, args.json_)
        return 0
    if args.profile_cmd == "rm":
        removed = browser.remove_profile(args.name, force=args.force)
        _emit({"removed": args.name, "existed": removed}, args.json_)
        return 0
    _emit({"profiles": browser.list_profiles()}, args.json_)
    return 0


def cmd_open(args) -> int:
    res = browser.open_profile(args.profile, headed=args.headed,
                               extra_args=args.browser_arg,
                               endpoint=args.endpoint,
                               engine=getattr(args, "engine", None),
                               stealth=getattr(args, "stealth", False))
    _emit(res, args.json_)
    return 0


def cmd_close(args) -> int:
    from .locks import read_lock as _read_lock
    info = _read_lock(args.profile)
    detached = bool(info and info.remote)
    closed = browser.close_profile(args.profile)
    doc = {"profile": args.profile, "closed": closed}
    if detached:
        doc["detached"] = True  # remote browser keeps running on its host
    _emit(doc, args.json_)
    return 0


def cmd_status(args) -> int:
    if args.profile:
        _emit(browser.status_profile(args.profile), args.json_)
    else:
        _emit({"profiles": browser.list_profiles()}, args.json_)
    return 0


def cmd_attach(args) -> int:
    st = browser.status_profile(args.profile)
    if not st.get("live"):
        raise RuntimeError(f"profile {args.profile!r} is not live; run `cloakctl open {args.profile}`")
    if st.get("engine") == "obscura":
        # obscura's CDP is per-connection isolated: there is no shareable
        # ws endpoint. Say so instead of printing a null that remote attach
        # would silently ignore.
        raise RuntimeError(
            "obscura profiles expose no attach endpoint (per-connection CDP "
            "— a second connection sees an empty session). Remote mode is a "
            "cloakbrowser feature: open the profile with "
            "`--engine cloakbrowser` on the browser host, then attach.")
    _emit({"profile": args.profile, "wsEndpoint": st["wsEndpoint"], "cdpPort": st["cdpPort"]}, args.json_)
    return 0


def cmd_import(args) -> int:
    try:
        summary = cookies.import_to_profile(
            args.profile,
            args.source,
            domains=args.domain or None,
            dry_run=args.dry_run,
            assume_yes=args.yes,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc))
    _emit(summary, args.json_)
    return 0


def cmd_validate(args) -> int:
    res = cookies.validate_context(args.profile, probe_url=args.url)
    _emit(res, args.json_)
    return 0 if res["verdict"] in ("logged_in", "alive") else 2


def cmd_exec(args) -> int:
    from .cdp import open_client
    from . import tabs as tabs_mod

    with open_client(args.profile) as cdp:
        # Evaluate in the visible page (active tab), not a fresh blank target.
        if getattr(args, "tab", None):
            tid = tabs_mod._resolve_target(cdp, args.tab, args.profile)
            cdp.bind_target(tid)
        else:
            rec = tabs_mod.recorded_active(args.profile, cdp)
            if rec is not None:
                tid = rec
                cdp.bind_target(rec)
            else:
                active = cdp.active_page_target()
                if active is not None:
                    tid = tabs_mod.tid(active)
                    cdp.bind_target(tid)
                else:
                    cdp.ensure_page_session()
                    tid = None
        if tid:
            # Record what we actually bound (same contract as page verbs).
            try:
                tabs_mod.record_active(args.profile, tid)
            except Exception:
                pass
        value = cdp.evaluate(args.js)
    _emit({"profile": args.profile, "value": value}, args.json_)
    return 0


def cmd_doctor(args) -> int:
    _emit(browser.doctor(), args.json_)
    return 0


def _tab(args) -> str | None:
    return getattr(args, "tab", None)


def cmd_navigate(args) -> int:
    if args.url:
        action, url = "url", args.url
    elif args.back:
        action, url = "back", None
    elif args.forward:
        action, url = "forward", None
    else:
        action, url = "reload", None
    _emit(pageops.do_navigate(args.profile, action, url, _tab(args)), args.json_)
    return 0


def cmd_snapshot(args) -> int:
    _emit(pageops.do_snapshot(args.profile, _tab(args), args.depth, args.mode), args.json_)
    return 0


def cmd_diff(args) -> int:
    _emit(pageops.do_diff(args.profile, _tab(args)), args.json_)
    return 0


def cmd_act(args) -> int:
    _emit(pageops.do_act(args.profile, args.kind, _tab(args), ref=args.ref, text=args.text,
                          key=args.key, x=args.x, y=args.y, dx=args.dx, dy=args.dy,
                          button=args.button, count=args.count, value=args.value,
                          fields=args.field), args.json_)
    return 0


def cmd_wait(args) -> int:
    _emit(pageops.do_wait(args.profile, _tab(args), args.text, args.selector, args.timeout), args.json_)
    return 0


def cmd_tabs(args) -> int:
    if args.tabs_cmd == "list":
        _emit(tabs.list_tabs(args.profile), args.json_)
    elif args.tabs_cmd == "new":
        _emit(tabs.new_tab(args.profile, args.url or "about:blank", args.background), args.json_)
    elif args.tabs_cmd == "close":
        _emit(tabs.close_tab(args.profile, args.tab_id), args.json_)
    else:
        _emit(tabs.activate_tab(args.profile, args.tab_id), args.json_)
    return 0


def cmd_windows(args) -> int:
    if args.windows_cmd == "list":
        _emit(tabs.list_windows(args.profile), args.json_)
    elif args.windows_cmd == "activate":
        _emit(tabs.activate_window(args.profile, args.window), args.json_)
    else:
        _emit(tabs.close_window(args.profile, args.window), args.json_)
    return 0


def cmd_groups(args) -> int:
    if args.groups_cmd == "list":
        _emit(tabs.list_groups(args.profile), args.json_)
    elif args.groups_cmd == "group":
        _emit(tabs.group_tabs(args.profile, args.tab_id, args.name), args.json_)
    elif args.groups_cmd == "ungroup":
        _emit(tabs.ungroup_tabs(args.profile, args.tab_id), args.json_)
    else:
        _emit(tabs.rename_group(args.profile, args.old, args.new), args.json_)
    return 0


def cmd_read(args) -> int:
    _emit(pageops.do_read(args.profile, _tab(args), args.format, args.selector), args.json_)
    return 0


def cmd_grep(args) -> int:
    _emit(pageops.do_grep(args.profile, _tab(args), args.pattern, args.over, args.limit), args.json_)
    return 0


def cmd_screenshot(args) -> int:
    _emit(pageops.do_screenshot(args.profile, _tab(args), args.format, args.quality,
                                args.full, args.out), args.json_)
    return 0


def cmd_pdf(args) -> int:
    _emit(pageops.do_pdf(args.profile, _tab(args), args.landscape, args.background, args.out), args.json_)
    return 0


def cmd_download(args) -> int:
    _emit(pageops.do_download(args.profile, _tab(args), args.ref, args.out_dir, args.timeout), args.json_)
    return 0


def cmd_upload(args) -> int:
    _emit(pageops.do_upload(args.profile, _tab(args), args.ref, args.file,
                            selector=getattr(args, "selector", None)), args.json_)
    return 0


def cmd_run(args) -> int:
    _emit(pageops.do_run(args.profile, _tab(args), args.code, args.timeout), args.json_)
    return 0


def cmd_history(args) -> int:
    _emit(hist.read_history(args.profile, args.limit, args.query), args.json_)
    return 0


def cmd_session(args) -> int:
    _emit(skills.name_session(args.profile, args.label, args.summary or "",
                              getattr(args, "category", "") or ""), args.json_)
    return 0


def cmd_audit(args) -> int:
    _emit(browser.read_audit(args.profile, args.limit), args.json_)
    return 0


def cmd_wf(args) -> int:
    import json as _json
    if args.wf_cmd == "save":
        inputs = _json.loads(args.inputs) if args.inputs else None
        depends = [d.strip() for d in (args.depends or "").split(",") if d.strip()]
        _emit(workflows.save_workflow(
            args.name, args.file, description=args.description or "",
            site=args.site or "", inputs=inputs, depends_on=depends,
            recursive=args.recursive, max_depth=args.max_depth,
            max_steps=args.max_steps, module_source=args.code),
            args.json_)
    elif args.wf_cmd == "rm":
        _emit(workflows.rm_workflow(args.name), args.json_)
    elif args.wf_cmd == "rename":
        _emit(workflows.rename_workflow(args.name, args.new), args.json_)
    elif args.wf_cmd == "runs":
        _emit(workflows.runs_workflow(args.name, args.limit, args.output),
               args.json_)
    elif args.wf_cmd == "prune":
        _emit(workflows.prune_workflow(args.name, args.keep), args.json_)
    elif args.wf_cmd == "list":
        _emit(workflows.list_workflows(), args.json_)
    elif args.wf_cmd == "show":
        _emit(workflows.show_workflow(args.name,
                                      include_source=args.source),
                args.json_)
    elif args.wf_cmd == "search":
        _emit(workflows.search_workflows(args.text), args.json_)
    elif args.wf_cmd == "export":
        res = workflows.export_workflow(args.name)
        if args.out:
            from pathlib import Path as _P
            _P(args.out).write_text(res["markdown"])
            _emit({"workflow": args.name, "exported": args.out}, args.json_)
        elif args.json_:
            _emit(res, args.json_)
        else:
            print(res["markdown"])
    elif args.wf_cmd == "log-run":
        _emit(workflows.log_run(args.name, not args.failed, args.note or ""), args.json_)
    else:
        inputs = _json.loads(args.input) if args.input else {}
        for s in args.set or []:
            k, _, v = s.partition("=")
            try:
                inputs[k] = _json.loads(v)
            except Exception:
                inputs[k] = v
        res = workflows.run_workflow(args.name, inputs, args.profile,
                                     args.tab, timeout=args.timeout,
                                     new_tab=args.new_tab)
        _emit(res, args.json_)
        return 0 if res.get("ok") else 1
    return 0


def cmd_skill(args) -> int:
    import json as _json
    if args.skill_cmd == "save":
        steps = _json.loads(args.steps) if args.steps else []
        _emit(skills.save_skill(args.name, args.description or "", args.site or "",
                                steps, args.notes or ""), args.json_)
    elif args.skill_cmd == "list":
        _emit(skills.list_skills(), args.json_)
    elif args.skill_cmd == "show":
        _emit(skills.show_skill(args.name), args.json_)
    elif args.skill_cmd == "log-run":
        _emit(skills.log_run(args.name, not args.failed, args.note or ""), args.json_)
    elif args.skill_cmd == "search":
        _emit(skills.search_skills(args.text), args.json_)
    elif args.skill_cmd == "promote":
        _emit(skills.promote_skill(args.name, args.to), args.json_)
    elif args.skill_cmd == "rm":
        _emit(skills.rm_skill(args.name), args.json_)
    elif args.skill_cmd == "rename":
        _emit(skills.rename_skill(args.name, args.new), args.json_)
    else:
        res = skills.run_skill(args.name, timeout=args.timeout)
        _emit(res, args.json_)
        return 0 if res.get("ok") else 1
    return 0


_JSON_PARENT = argparse.ArgumentParser(add_help=False)
_JSON_PARENT.add_argument("--json", dest="json_", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output")
_JSON_PARENT.add_argument("--toon", dest="toon_", action="store_true", default=argparse.SUPPRESS, help="token-efficient TOON output (AXI)")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("cloakctl")
    except Exception:
        return "0.1.0"


def _home_view() -> int:
    """AXI §8/§10: no args → identify + live content first, help as `help` hints."""
    print(f"bin: {_self_path()}")
    print("description: Persistent stealth browser automation for AI agents "
          "— one profile = one session.")
    try:
        profiles = browser.list_profiles()
    except Exception:
        profiles = []
    live = [p for p in profiles if p.get("live")]
    print(f"profiles: {len(profiles)} ({len(live)} live)")
    if profiles:
        for p in profiles[:6]:
            url = p.get("url") or "-"
            print(f"  {p.get('profile')}: {p.get('engine') or '-'} "
                  f"live={str(bool(p.get('live'))).lower()} url={url}")
        if len(profiles) > 6:
            print(f"  …+{len(profiles) - 6} more — run 'cloakctl profiles list' for all")
    else:
        print("profiles: no profiles yet")
    print("help[2]:")
    print("  Run 'cloakctl profiles create <name>' to create a profile")
    print("  Run 'cloakctl open <name>' to launch; 'cloakctl --help' for all verbs")
    return 0


def _self_path() -> str:
    from pathlib import Path
    try:
        p = Path(sys.argv[0]).resolve()
    except Exception:
        return "cloakctl"
    home = os.path.expanduser("~")
    s = str(p)
    return "~" + s[len(home):] if home and s.startswith(home) else s


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloakctl",
        description="lean browser CLI for AI agents",
        parents=[_JSON_PARENT],
        epilog="defaults: output is human; --json gives machine JSON; "
               "--toon gives token-efficient TOON (AXI)",
    )
    p.add_argument("--version", action="version", version=f"cloakctl {_version()}")
    sub = p.add_subparsers(dest="cmd", required=False)  # no args → home view

    pp = sub.add_parser("profiles", help="list/create/remove profiles", parents=[_JSON_PARENT])
    psub = pp.add_subparsers(dest="profile_cmd", required=True)
    psub.add_parser("list", parents=[_JSON_PARENT])
    pc = psub.add_parser("create", parents=[_JSON_PARENT])
    pc.add_argument("name")
    pr = psub.add_parser("rm", parents=[_JSON_PARENT])
    pr.add_argument("name")
    pr.add_argument("--force", action="store_true")
    pp.set_defaults(func=cmd_profiles)

    po = sub.add_parser("open", help="launch or re-attach the profile browser", parents=[_JSON_PARENT])
    po.add_argument("profile")
    po.add_argument("--headed", action="store_true")
    po.add_argument("--browser-arg", action="append", default=[],
                    help="extra engine flag (repeatable; e.g. --browser-arg=--allow-file-access)")
    po.add_argument("--engine", default=None, choices=["obscura", "cloakbrowser"],
                    help="browser engine (default: obscura; falls back to CLOAKCTL_ENGINE, "
                    "profile meta, then auto-detect). cloakbrowser = Chromium-family binary.")
    po.add_argument("--stealth", action="store_true",
                    help="obscura only: anti-detection + tracker blocking")
    po.add_argument("--endpoint", default=None,
                    help="remote CDP ws endpoint (VPS-side CLI, browser elsewhere; "
                    "falls back to CLOAKCTL_CDP_URL). No launch, no signals.")
    po.set_defaults(func=cmd_open)

    pcl = sub.add_parser("close", help="gracefully close the profile browser", parents=[_JSON_PARENT])
    pcl.add_argument("profile")
    pcl.set_defaults(func=cmd_close)

    ps = sub.add_parser("status", help="show live state and jar fingerprint", parents=[_JSON_PARENT])
    ps.add_argument("profile", nargs="?")
    ps.set_defaults(func=cmd_status)

    pa = sub.add_parser("attach", help="print the CDP ws endpoint", parents=[_JSON_PARENT])
    pa.add_argument("profile")
    pa.set_defaults(func=cmd_attach)

    pi = sub.add_parser("import", help="import active browser cookies into a live profile", parents=[_JSON_PARENT])
    pi.add_argument("profile")
    pi.add_argument("source", help="brave|chrome|chromium|edge[:profile] or file:<cookies.json>")
    pi.add_argument("--domain", action="append", help="restrict to domain (repeatable)")
    pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--yes", "-y", action="store_true")
    pi.set_defaults(func=cmd_import)

    pv = sub.add_parser("validate", help="probe the live session in-browser and classify it", parents=[_JSON_PARENT])
    pv.add_argument("profile")
    pv.add_argument("--url", default=None)
    pv.set_defaults(func=cmd_validate)

    pe = sub.add_parser("exec", help="evaluate JS in the live profile browser", parents=[_JSON_PARENT])
    pe.add_argument("profile")
    pe.add_argument("js")
    pe.add_argument("--tab", default=None, help="target id (prefix ok); default: active tab")
    pe.set_defaults(func=cmd_exec)

    pd = sub.add_parser("doctor", help="binary, locks, profiles, disk", parents=[_JSON_PARENT])
    pd.set_defaults(func=cmd_doctor)

    def _tab_arg(pp) -> None:
        pp.add_argument("--tab", default=None, help="target id (prefix ok); default: active tab")

    pn = sub.add_parser("navigate", help="load url / back / forward / reload", parents=[_JSON_PARENT])
    pn.add_argument("profile")
    _tab_arg(pn)
    pn.add_argument("--url", default=None)
    pn.add_argument("--back", action="store_true")
    pn.add_argument("--forward", action="store_true")
    pn.add_argument("--reload", action="store_true")
    pn.set_defaults(func=cmd_navigate)

    psn = sub.add_parser("snapshot", help="ref-tagged accessibility tree of the page", parents=[_JSON_PARENT])
    psn.add_argument("profile")
    _tab_arg(psn)
    psn.add_argument("--depth", type=int, default=12)
    psn.add_argument("--mode", choices=["ax", "text"], default="ax")
    psn.set_defaults(func=cmd_snapshot)

    pdf_ = sub.add_parser("diff", help="what changed since the last snapshot", parents=[_JSON_PARENT])
    pdf_.add_argument("profile")
    _tab_arg(pdf_)
    pdf_.set_defaults(func=cmd_diff)

    pa_ = sub.add_parser("act", help="click|type|clear|key|hover|scroll|select|focus|fill|check|uncheck|drag", parents=[_JSON_PARENT])
    pa_.add_argument("profile")
    pa_.add_argument("kind")
    _tab_arg(pa_)
    pa_.add_argument("--ref", default=None)
    pa_.add_argument("--text", default=None)
    pa_.add_argument("--key", default=None)
    pa_.add_argument("--value", default=None)
    pa_.add_argument("--x", type=float, default=None)
    pa_.add_argument("--y", type=float, default=None)
    pa_.add_argument("--dx", type=float, default=0)
    pa_.add_argument("--dy", type=float, default=500)
    pa_.add_argument("--button", default="left")
    pa_.add_argument("--count", type=int, default=1)
    pa_.add_argument("--field", action="append", default=None, help="fill batch: ref=value (repeatable)")
    pa_.set_defaults(func=cmd_act)

    pw = sub.add_parser("wait", help="wait for text or selector", parents=[_JSON_PARENT])
    pw.add_argument("profile")
    _tab_arg(pw)
    pw.add_argument("--text", default=None)
    pw.add_argument("--selector", default=None)
    pw.add_argument("--timeout", type=float, default=15.0)
    pw.set_defaults(func=cmd_wait)

    pt = sub.add_parser("tabs", help="list/new/close/activate tabs", parents=[_JSON_PARENT])
    pt.add_argument("profile")
    ptsub = pt.add_subparsers(dest="tabs_cmd", required=True)
    ptsub.add_parser("list", parents=[_JSON_PARENT])
    ptn = ptsub.add_parser("new", parents=[_JSON_PARENT])
    ptn.add_argument("--url", default=None)
    ptn.add_argument("--background", action="store_true")
    ptc = ptsub.add_parser("close", parents=[_JSON_PARENT])
    ptc.add_argument("tab_id")
    pta = ptsub.add_parser("activate", parents=[_JSON_PARENT])
    pta.add_argument("tab_id")
    pt.set_defaults(func=cmd_tabs)

    pwin = sub.add_parser("windows", help="list/activate/close windows", parents=[_JSON_PARENT])
    pwin.add_argument("profile")
    pwsub = pwin.add_subparsers(dest="windows_cmd", required=True)
    pwsub.add_parser("list", parents=[_JSON_PARENT])
    pwa = pwsub.add_parser("activate", parents=[_JSON_PARENT])
    pwa.add_argument("window")
    pwc = pwsub.add_parser("close", parents=[_JSON_PARENT])
    pwc.add_argument("window")
    pwin.set_defaults(func=cmd_windows)

    pg = sub.add_parser("groups", help="logical tab groups (list/group/ungroup/rename)", parents=[_JSON_PARENT])
    pg.add_argument("profile")
    pgsub = pg.add_subparsers(dest="groups_cmd", required=True)
    pgsub.add_parser("list", parents=[_JSON_PARENT])
    pgg = pgsub.add_parser("group", parents=[_JSON_PARENT])
    pgg.add_argument("name")
    pgg.add_argument("tab_id", nargs="+")
    pgu = pgsub.add_parser("ungroup", parents=[_JSON_PARENT])
    pgu.add_argument("tab_id", nargs="+")
    pgr = pgsub.add_parser("rename", parents=[_JSON_PARENT])
    pgr.add_argument("old")
    pgr.add_argument("new")
    pg.set_defaults(func=cmd_groups)

    pr = sub.add_parser("read", help="page content: markdown|text|links|console", parents=[_JSON_PARENT])
    pr.add_argument("profile")
    _tab_arg(pr)
    pr.add_argument("--format", choices=["markdown", "text", "links", "console"], default="markdown")
    pr.add_argument("--selector", default=None)
    pr.set_defaults(func=cmd_read)

    pgr_ = sub.add_parser("grep", help="search the page without dumping it", parents=[_JSON_PARENT])
    pgr_.add_argument("profile")
    pgr_.add_argument("pattern")
    _tab_arg(pgr_)
    pgr_.add_argument("--over", choices=["ax", "text"], default="ax")
    pgr_.add_argument("--limit", type=int, default=30)
    pgr_.set_defaults(func=cmd_grep)

    pss = sub.add_parser("screenshot", help="capture the page", parents=[_JSON_PARENT])
    pss.add_argument("profile")
    _tab_arg(pss)
    pss.add_argument("--format", choices=["jpeg", "png"], default="jpeg")
    pss.add_argument("--quality", type=int, default=80)
    pss.add_argument("--full", action="store_true")
    pss.add_argument("--out", default=None)
    pss.set_defaults(func=cmd_screenshot)

    ppdf = sub.add_parser("pdf", help="print the page to PDF", parents=[_JSON_PARENT])
    ppdf.add_argument("profile")
    _tab_arg(ppdf)
    ppdf.add_argument("--landscape", action="store_true")
    ppdf.add_argument("--background", action="store_true")
    ppdf.add_argument("--out", default=None)
    ppdf.set_defaults(func=cmd_pdf)

    pdl = sub.add_parser("download", help="click a ref and save the download", parents=[_JSON_PARENT])
    pdl.add_argument("profile")
    pdl.add_argument("--ref", required=True)
    _tab_arg(pdl)
    pdl.add_argument("--out-dir", required=True)
    pdl.add_argument("--timeout", type=float, default=30.0)
    pdl.set_defaults(func=cmd_download)

    pul = sub.add_parser("upload", help="set file input files", parents=[_JSON_PARENT])
    pul.add_argument("profile")
    pul.add_argument("--ref", default=None)
    pul.add_argument("--selector", default=None, help="CSS selector (no snapshot ref needed)")
    pul.add_argument("--file", action="append", required=True)
    _tab_arg(pul)
    pul.set_defaults(func=cmd_upload)

    prun = sub.add_parser("run", help="evaluate possibly-async JS (awaitPromise)", parents=[_JSON_PARENT])
    prun.add_argument("profile")
    prun.add_argument("code")
    _tab_arg(prun)
    prun.add_argument("--timeout", type=float, default=30.0)
    prun.set_defaults(func=cmd_run)

    ph = sub.add_parser("history", help="read-only profile history", parents=[_JSON_PARENT])
    ph.add_argument("profile")
    ph.add_argument("--limit", type=int, default=20)
    ph.add_argument("--query", default=None)
    ph.set_defaults(func=cmd_history)

    pses = sub.add_parser("session", help="label the current session", parents=[_JSON_PARENT])
    pses.add_argument("profile")
    pses.add_argument("label")
    pses.add_argument("--summary", default=None)
    pses.add_argument("--category", default=None)
    pses.set_defaults(func=cmd_session)

    paud = sub.add_parser("audit", help="per-profile command trail", parents=[_JSON_PARENT])
    paud.add_argument("profile")
    paud.add_argument("--limit", type=int, default=20)
    paud.set_defaults(func=cmd_audit)

    psk = sub.add_parser("skill", help="saved skill macros (save/list/show/run/rm/rename/log-run)", parents=[_JSON_PARENT])
    psksub = psk.add_subparsers(dest="skill_cmd", required=True)
    psks = psksub.add_parser("save", parents=[_JSON_PARENT])
    psks.add_argument("name")
    psks.add_argument("--description", default=None)
    psks.add_argument("--site", default=None)
    psks.add_argument("--steps", default=None, help="JSON list of {cmd:[...]} steps")
    psks.add_argument("--notes", default=None)
    psksub.add_parser("list", parents=[_JSON_PARENT])
    pskh = psksub.add_parser("show", parents=[_JSON_PARENT])
    pskh.add_argument("name")
    pskr = psksub.add_parser("run", parents=[_JSON_PARENT])
    pskr.add_argument("name")
    pskr.add_argument("--timeout", type=float, default=None,
                      help="wall-clock seconds over the whole replay")
    pskrm = psksub.add_parser("rm", parents=[_JSON_PARENT])
    pskrm.add_argument("name")
    pskrn = psksub.add_parser("rename", parents=[_JSON_PARENT])
    pskrn.add_argument("name")
    pskrn.add_argument("new")
    pskl = psksub.add_parser("log-run", parents=[_JSON_PARENT])
    pskl.add_argument("name")
    pskl.add_argument("--failed", action="store_true")
    pskl.add_argument("--note", default=None)
    psks_ = psksub.add_parser("search", parents=[_JSON_PARENT])
    psks_.add_argument("text")
    pskp = psksub.add_parser("promote", parents=[_JSON_PARENT])
    pskp.add_argument("name")
    pskp.add_argument("--to", default=None, help="workflow name (default: <skill>-promoted)")
    psk.set_defaults(func=cmd_skill)

    pwf = sub.add_parser("wf", help="workflow modules (save/list/show/search/run/export/rm/rename/runs/prune/log-run)", parents=[_JSON_PARENT])
    pwfsub = pwf.add_subparsers(dest="wf_cmd", required=True)
    pwfs = pwfsub.add_parser("save", parents=[_JSON_PARENT])
    pwfs.add_argument("name")
    pwfs.add_argument("--file", default=None, help="module path, or - for stdin (exactly one of --file/--code)")
    pwfs.add_argument("--code", default=None, help="inline module source (agents: no filesystem needed)")
    pwfs.add_argument("--description", default=None)
    pwfs.add_argument("--site", default=None)
    pwfs.add_argument("--inputs", default=None, help="JSON inputs spec")
    pwfs.add_argument("--depends", default=None, help="comma-separated depends_on")
    pwfs.add_argument("--recursive", action="store_true")
    pwfs.add_argument("--max-depth", type=int, default=None)
    pwfs.add_argument("--max-steps", type=int, default=None)
    pwfsub.add_parser("list", parents=[_JSON_PARENT])
    pwfh = pwfsub.add_parser("show", parents=[_JSON_PARENT])
    pwfh.add_argument("name")
    pwfh.add_argument("--source", action="store_true", help="include module.py source")
    pwfrm = pwfsub.add_parser("rm", parents=[_JSON_PARENT])
    pwfrm.add_argument("name")
    pwfrn = pwfsub.add_parser("rename", parents=[_JSON_PARENT])
    pwfrn.add_argument("name")
    pwfrn.add_argument("new")
    pwfrs = pwfsub.add_parser("runs", parents=[_JSON_PARENT])
    pwfrs.add_argument("name")
    pwfrs.add_argument("--limit", type=int, default=20)
    pwfrs.add_argument("--output", default=None,
                       help="stamp prefix: print that run's spilled outputs")
    pwfp = pwfsub.add_parser("prune", parents=[_JSON_PARENT])
    pwfp.add_argument("name")
    pwfp.add_argument("--keep", type=int, default=50)
    pwfq = pwfsub.add_parser("search", parents=[_JSON_PARENT])
    pwfq.add_argument("text")
    pwfr = pwfsub.add_parser("run", parents=[_JSON_PARENT])
    pwfr.add_argument("name")
    pwfr.add_argument("--profile", required=True)
    pwfr.add_argument("--tab", default=None)
    pwfr.add_argument("--input", default=None, help="JSON inputs")
    pwfr.add_argument("--set", action="append", default=None, help="k=v input (repeatable)")
    pwfr.add_argument("--timeout", type=float, default=None, help="wall-clock seconds (kills runaway modules)")
    pwfr.add_argument("--new-tab", action="store_true", help="run in a fresh tab, closed afterwards (parallel-safe)")
    pwfe = pwfsub.add_parser("export", parents=[_JSON_PARENT])
    pwfe.add_argument("name")
    pwfe.add_argument("--out", default=None)
    pwfl = pwfsub.add_parser("log-run", parents=[_JSON_PARENT])
    pwfl.add_argument("name")
    pwfl.add_argument("--failed", action="store_true")
    pwfl.add_argument("--note", default=None)
    pwf.set_defaults(func=cmd_wf)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "json_"):
        args.json_ = False  # SUPPRESS default — no --json given either position
    global _OUT_TOON
    _OUT_TOON = bool(getattr(args, "toon_", False)) and not args.json_
    if not getattr(args, "cmd", None):
        return _home_view()  # AXI §8: no args shows live state, not a manual
    t0 = time.monotonic()
    verb = getattr(getattr(args, "func", None), "__name__", "?")
    verb = verb[4:] if verb.startswith("cmd_") else verb
    prof = getattr(args, "profile", None) or getattr(args, "name", "-")
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        browser.audit_log(prof, verb, 130,
                          round((time.monotonic() - t0) * 1000))
        return 130
    except Exception as exc:
        browser.audit_log(prof, verb, 1,
                          round((time.monotonic() - t0) * 1000))
        if getattr(args, "json_", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    browser.audit_log(prof, verb, rc,
                      round((time.monotonic() - t0) * 1000))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
