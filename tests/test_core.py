"""Unit tests for cloakctl core: paths, locks, cookie mapping/classification, CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cloakctl import paths, util  # noqa: E402
from cloakctl.cookies import classify_validation, to_cdp_cookies  # noqa: E402
from cloakctl.locks import (  # noqa: E402
    LockInfo,
    ProfileInUseError,
    acquire,
    find_stale,
    is_live,
    read_lock,
    release,
)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOAKCTL_HOME", str(tmp_path / "state"))
    # paths.py binds HOME_STATE at import; patch the module attrs too.
    monkeypatch.setattr(paths, "HOME_STATE", tmp_path / "state")
    monkeypatch.setattr(paths, "PROFILES_DIR", tmp_path / "state" / "profiles")
    monkeypatch.setattr(paths, "RUNTIME_DIR", tmp_path / "state" / "runtime")
    yield tmp_path / "state"


# --- paths -----------------------------------------------------------------


def test_profile_name_validation(state):
    with pytest.raises(ValueError):
        paths.profile_dir("../evil")
    with pytest.raises(ValueError):
        paths.profile_dir("")
    assert paths.profile_dir("ok").name == "ok"


def test_meta_roundtrip(state):
    paths.ensure_layout()
    meta = paths.load_meta("p1")
    assert meta["version"] == 1
    meta["mintedUA"] = "TestUA/1.0"
    paths.save_meta("p1", meta)
    assert paths.load_meta("p1")["mintedUA"] == "TestUA/1.0"


# --- locks -------------------------------------------------------------------


def test_lock_acquire_read_release(state):
    paths.ensure_layout()
    acquire("p1", pid=os.getpid(), cdp_port=9222, ws_endpoint="ws://x")
    info = read_lock("p1")
    assert info is not None and info.pid == os.getpid() and info.cdp_port == 9222
    assert is_live(info)  # our own pid, real start time recorded
    assert release("p1", expect_pid=os.getpid())
    assert read_lock("p1") is None


def test_lock_dead_pid_is_stale(state):
    paths.ensure_layout()
    # pid 1 always exists; use a pid that cannot exist.
    dead = 4_000_000
    acquire("p2", pid=dead, cdp_port=9223, ws_endpoint=None)
    info = read_lock("p2")
    assert not is_live(info)
    assert find_stale() == ["p2"]


def test_lock_acquire_refuses_live(state):
    paths.ensure_layout()
    acquire("p3", pid=os.getpid(), cdp_port=9224, ws_endpoint=None)
    with pytest.raises(ProfileInUseError):
        acquire("p3", pid=os.getpid(), cdp_port=9999, ws_endpoint=None)


def test_lock_acquire_overwrites_stale(state):
    paths.ensure_layout()
    acquire("p4", pid=4_000_001, cdp_port=9225, ws_endpoint=None)
    acquire("p4", pid=os.getpid(), cdp_port=9226, ws_endpoint=None)
    assert read_lock("p4").cdp_port == 9226


# --- cookie mapping -------------------------------------------------------------


def test_to_cdp_cookies_filters_expired_and_domains():
    now = util.now_iso()  # touch import
    rows = [
        {"name": "a", "value": "1", "domain": ".linkedin.com", "path": "/", "expires": 9999999999, "secure": True, "httpOnly": True, "sameSite": "Lax", "priority": "High"},
        {"name": "old", "value": "x", "domain": ".linkedin.com", "path": "/", "expires": 1000, "secure": False, "httpOnly": False},
        {"name": "sess", "value": "s", "domain": ".example.com", "path": "/", "expires": 0, "secure": False, "httpOnly": False},
        {"name": "other", "value": "o", "domain": ".github.com", "path": "/", "expires": 9999999999, "secure": False, "httpOnly": False},
    ]
    out = to_cdp_cookies(rows, domains=["linkedin.com"])
    names = [c["name"] for c in out]
    assert names == ["a"]  # expired dropped, non-matching domain dropped
    assert out[0]["expires"] == 9999999999.0
    assert out[0]["sameSite"] == "Lax"

    sess = to_cdp_cookies([rows[2]])
    assert "expires" not in sess[0]  # session cookies stay session


# --- validation classification ----------------------------------------------------


def test_classify_burn_signature():
    jar = [{"name": "li_at", "value": "delete me", "domain": ".linkedin.com"}]
    assert classify_validation("https://www.linkedin.com/", "", jar)["verdict"] == "burn_signature"


def test_classify_challenged():
    assert (
        classify_validation("https://www.linkedin.com/authwall?trk=/x", "", [])[ "verdict" ]
        == "challenged"
    )


def test_classify_anonymous():
    assert classify_validation("https://www.linkedin.com/login", "", [])["verdict"] == "anonymous"


def test_classify_logged_in():
    assert (
        classify_validation("https://www.linkedin.com/feed/", "…global-nav__me…", [])["verdict"]
        == "logged_in"
    )


# --- fingerprint -------------------------------------------------------------------


def test_jar_fingerprint_stable_and_value_sensitive():
    a = [{"name": "x", "value": "1", "domain": ".d"}]
    b = [{"name": "x", "value": "2", "domain": ".d"}]
    assert util.jar_fingerprint(a) == util.jar_fingerprint(a)
    assert util.jar_fingerprint(a) != util.jar_fingerprint(b)
    assert len(util.jar_fingerprint(a)) == 12


# --- CLI smoke (parser wiring) -------------------------------------------------------


def test_cli_help_exits_zero(capsys):
    from cloakctl.cli import main

    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0


def test_cli_status_json(state, capsys):
    from cloakctl.cli import main

    rc = main(["--json", "status"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "profiles" in data


# --- snapshots -----------------------------------------------------------------


def _ax_nodes():
    return [
        {"nodeId": "1", "role": {"value": "RootWebArea"}},
        {"nodeId": "2", "parentId": "1", "role": {"value": "button"},
         "name": {"value": "Go"}, "backendDOMNodeId": 42},
        {"nodeId": "3", "parentId": "1", "role": {"value": "textbox"},
         "name": {"value": "q"}, "backendDOMNodeId": 43},
        {"nodeId": "4", "parentId": "1", "role": {"value": "StaticText"},
         "name": {"value": "hi"}},
    ]


def test_snapshot_refs_stable_and_backend_mapped():
    from cloakctl.snap import render_snapshot

    text, refmap = render_snapshot(_ax_nodes(), "https://x/")
    assert "[ref=e1]" in text and "[ref=e2]" in text
    assert refmap == {"e1": 42, "e2": 43}
    assert "StaticText" in text


def test_trust_wrap_markers_paired():
    from cloakctl.snap import trust_wrap

    w = trust_wrap("click me", "https://evil.test/")
    nonce = w.split("nonce=")[1].split(" ")[0].rstrip("]")
    assert "[END_UNTRUSTED_PAGE_CONTENT nonce=" + nonce + "]" in w
    assert "click me" in w and "https://evil.test/" in w


def test_grep_lines_case_insensitive_limit():
    from cloakctl.snap import grep_lines

    assert grep_lines("a\nAb\nAB\nc", "ab", limit=2) == ["Ab", "AB"]


def test_diff_snapshots_detects_change(state):
    from cloakctl import snap

    _, txt = snap._ref_paths("p", "t1")
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("line1\nline2")
    out = snap.diff_snapshots("p", "t1", "line1\nline2-changed")
    assert "-line2" in out and "+line2-changed" in out
    assert snap.diff_snapshots("p", "t1", "line1\nline2") == ""


def test_load_ref_requires_snapshot(state):
    from cloakctl import snap

    with pytest.raises(KeyError):
        snap.load_ref("nosuch", "notab", "e1")


# --- skills --------------------------------------------------------------------


def test_skill_roundtrip(state):
    from cloakctl import skills

    r = skills.save_skill("s1", "desc", site="x", steps=[{"cmd": ["--json", "status"]}], notes="n")
    assert r["saved"] is True and r["steps"] == 1
    shown = skills.show_skill("s1")
    assert shown["description"] == "desc" and len(shown["steps"]) == 1
    listed = skills.list_skills()
    assert any(s["skill"] == "s1" for s in listed["skills"])
    assert skills.log_run("s1", ok=True, note="t")["runs"] == 1
    with pytest.raises(FileNotFoundError):
        skills.show_skill("missing")
    with pytest.raises(ValueError):
        skills.save_skill("s2", "d", steps=[{"nope": 1}])


def test_name_session(state):
    from cloakctl import skills
    from cloakctl import paths as _paths

    r = skills.name_session("prof", "my label", "sum")
    assert r["label"] == "my label"
    assert _paths.load_meta("prof")["sessionLabel"]["label"] == "my label"


# --- history -------------------------------------------------------------------


def test_history_missing_db(state):
    from cloakctl import hist

    with pytest.raises(FileNotFoundError):
        hist.read_history("empty-prof")


# --- new CLI verbs parse -------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["snapshot", "p"], ["diff", "p"], ["navigate", "p", "--url", "https://x/"],
    ["act", "p", "click", "--ref", "e1"], ["wait", "p", "--text", "x"],
    ["tabs", "p", "list"], ["windows", "p", "list"], ["groups", "p", "list"],
    ["read", "p"], ["grep", "p", "x"], ["screenshot", "p"], ["pdf", "p"],
    ["run", "p", "1+1"], ["history", "p"], ["session", "p", "lbl"],
    ["skill", "list"],
])
def test_new_verbs_parse(state, argv):
    from cloakctl.cli import build_parser

    ns = build_parser().parse_args(argv)
    assert callable(getattr(ns, "func", None))


def test_cli_each_verb_accepts_json_after(state):
    from cloakctl.cli import build_parser

    for argv in (["snapshot", "p"], ["act", "p", "click"], ["tabs", "p", "list"],
                 ["read", "p"], ["skill", "list"]):
        ns = build_parser().parse_args([*argv, "--json"])
        assert ns.json_ is True


# --- workflow registry units (no browser) --------------------------------------


def test_validate_inputs_ok_and_defaults(state):
    from cloakctl.workflows import validate_inputs

    spec = {"url": {"type": "str", "required": True},
            "n": {"type": "int", "required": False, "default": 3},
            "f": {"type": "float", "required": False}}
    out = validate_inputs(spec, {"url": "http://x/", "f": 2})
    assert out == {"url": "http://x/", "n": 3, "f": 2.0}


def test_validate_inputs_reject(state):
    import pytest as _p

    from cloakctl.workflows import validate_inputs

    spec = {"url": {"type": "str", "required": True},
            "n": {"type": "int", "required": False}}
    with _p.raises(Exception, match="missing required"):
        validate_inputs(spec, {})
    with _p.raises(Exception, match="want int"):
        validate_inputs(spec, {"url": "u", "n": "abc"})
    with _p.raises(Exception, match="unknown inputs"):
        validate_inputs(spec, {"url": "u", "bogus": 1})


def test_called_names_extraction(state, tmp_path):
    from cloakctl.workflows import _called_names

    p = tmp_path / "m.py"
    p.write_text('def run(ctx, i):\n    a = ctx.call("alpha", {})\n'
                 '    return ctx.call("beta", {})')
    assert _called_names(p) == {"alpha", "beta"}


def test_cycle_detection(state, tmp_path):
    import pytest as _p

    from cloakctl.workflows import _check_cycles

    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    a.write_text('def run(ctx, i):\n    return ctx.call("b", {})')
    b.write_text('def run(ctx, i):\n    return {"ok": True}')
    c.write_text('def run(ctx, i):\n    return ctx.call("c", {})')
    manifests = {"a": {"name": "a"}, "b": {"name": "b"}}
    _check_cycles(manifests, {"a": a, "b": b})  # acyclic ok
    manifests = {"a": {"name": "a"}, "b": {"name": "b", "depends_on": ["a"]}}
    with _p.raises(Exception, match="cycle refused: a -> b -> a"):
        _check_cycles(manifests, {"a": a, "b": b})
    # undeclared self-loop refused; declared bounded recursion allowed
    with _p.raises(Exception, match="cycle refused: c -> c"):
        _check_cycles({"c": {"name": "c"}}, {"c": c})
    _check_cycles({"c": {"name": "c", "recursive": True}}, {"c": c})


def test_wf_save_run_resave_preserves_runs(state, tmp_path):
    from cloakctl import workflows as _wf

    m = tmp_path / "leaf.py"
    m.write_text('META = {"description": "leaf"}\n'
                 "def run(ctx, inputs):\n"
                 '    return {"echo": inputs.get("v", "")}\n')
    _wf.save_workflow("leaf", str(m), inputs={"v": {"type": "str", "required": False}})
    res = _wf.run_workflow("leaf", {"v": "hi"}, profile="nobody")
    assert res["ok"] is True and res["outputs"] == {"echo": "hi"}
    assert res["steps"] == 0  # no primitives touched
    _wf.save_workflow("leaf", str(m))
    assert _wf.show_workflow("leaf")["runs"] == 1


def test_wf_run_rejects_bad_module(state, tmp_path):
    import pytest as _p

    from cloakctl import workflows as _wf

    m = tmp_path / "bad.py"
    m.write_text("def run(ctx, inputs):\n    return [1, 2]\n")
    _wf.save_workflow("badret", str(m))
    res = _wf.run_workflow("badret", {}, profile="nobody")
    assert res["ok"] is False and "must return a dict" in res["error"]
    m2 = tmp_path / "norun.py"
    m2.write_text("x = 1\n")
    with _p.raises(Exception, match="must define run"):
        _wf.save_workflow("norun", str(m2))


def test_skill_stats_and_stale(state):
    from cloakctl import skills as _s

    _s.save_skill("st", "desc", steps=[{"cmd": ["status"]}])
    lst = _s.list_skills()
    assert lst["skills"][0]["runs"] == 0
    assert _s.show_skill("st")["stats"]["stale"] is False  # fresh, not stale
    old = _s.show_skill("st")
    old["updated"] = "2020-01-01T00:00:00Z"
    _s._skill_path("st").write_text(json.dumps(old))
    assert _s.show_skill("st")["stats"]["stale"] is True
    assert "st" in [h["skill"] for h in _s.search_skills("desc")["skills"]]


def test_cli_wf_skill_verbs_parse(state):
    from cloakctl.cli import build_parser

    for argv in (["wf", "list"], ["wf", "show", "x"], ["wf", "search", "x"],
                 ["wf", "run", "x", "--profile", "p"],
                 ["wf", "save", "x", "--file", "m.py"],
                 ["wf", "export", "x"], ["wf", "log-run", "x"],
                 ["skill", "search", "x"], ["skill", "promote", "x"]):
        ns = build_parser().parse_args(argv)
        assert callable(getattr(ns, "func", None))
    ns = build_parser().parse_args(["wf", "run", "x", "--profile", "p", "--json"])
    assert ns.json_ is True
