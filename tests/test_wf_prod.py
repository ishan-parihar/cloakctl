"""Production-grade wf units: CLI-native lifecycle, redaction, budgets.

No browser needed. Uses the same CLOAKCTL_HOME isolation pattern as
test_core.state.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cloakctl import paths  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOAKCTL_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(paths, "HOME_STATE", tmp_path / "state")
    monkeypatch.setattr(paths, "PROFILES_DIR", tmp_path / "state" / "profiles")
    monkeypatch.setattr(paths, "RUNTIME_DIR", tmp_path / "state" / "runtime")
    yield tmp_path / "state"


CODE_OK = ('META = {"version": "3.0.0", "description": "inline", '
           '"site": "example.com", '
           '"inputs": {"v": {"type": "str", "required": False}}, '
           '"depends_on": []}\n'
           "def run(ctx, inputs):\n"
           '    return {"echo": inputs.get("v", "")}\n')


def test_save_code_and_stdin_and_meta_merge(state, monkeypatch):
    from cloakctl import workflows as _wf

    assert _wf.save_workflow("inline1", module_source=CODE_OK)["saved"] is True
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE_OK))
    assert _wf.save_workflow("inline2", "-")["saved"] is True
    m = _wf.show_workflow("inline1")
    assert (m["version"], m["description"], m["site"]) == (
        "3.0.0", "inline", "example.com")
    assert m["inputs"]["v"]["type"] == "str"  # META inputs adopted
    assert m["kind"] == "workflow"
    assert "def run(ctx, inputs):" in _wf.show_workflow(
        "inline1", include_source=True)["source"]


def test_save_source_mutex_and_empty(state, tmp_path):
    from cloakctl import workflows as _wf

    with pytest.raises(Exception, match="exactly one"):
        _wf.save_workflow("bad", str(tmp_path / "x.py"),
                          module_source=CODE_OK)
    with pytest.raises(Exception, match="empty module"):
        _wf.save_workflow("empty", module_source="  \n")
    with pytest.raises(Exception, match="must define run"):
        _wf.save_workflow("norun", module_source="x = 1\n")


def test_rm_and_builtin_refusals(state):
    from cloakctl import workflows as _wf

    _wf.save_workflow("gone", module_source=CODE_OK)
    assert _wf.rm_workflow("gone")["removed"] is True
    with pytest.raises(Exception, match="cannot remove builtin"):
        _wf.rm_workflow("scrape_list")
    with pytest.raises(Exception, match="no workflow"):
        _wf.rm_workflow("no_such_wf_xyz")
    with pytest.raises(Exception, match="cannot log runs on builtin"):
        _wf.log_run("scrape_list", note="x")


def test_secret_redaction(state):
    from cloakctl.workflows import REDACTED, _redact

    spec = {"pw": {"type": "str", "secret": True},
            "u": {"type": "str"}}
    assert _redact({"pw": "hunter2", "u": "bob"},
                   spec) == {"pw": REDACTED, "u": "bob"}


def test_deadline_and_checkpoint(state):
    import time as _t

    from cloakctl.sdk import Context, RunState, WfTimeoutError

    with pytest.raises(WfTimeoutError):
        RunState("x", deadline=_t.monotonic() - 1).bump()
    with pytest.raises(WfTimeoutError):
        Context("p", state=RunState(
            "x", deadline=_t.monotonic() - 1)).checkpoint()
    Context("p").checkpoint()  # no deadline: no-op


def test_run_timeout_and_redacted_doc(state):
    from cloakctl import workflows as _wf

    _wf.save_workflow("spin", module_source='META = {"description": "s"}\n'
                     "def run(ctx, inputs):\n    while True:\n        pass\n")
    res = _wf.run_workflow("spin", {}, profile="nobody", timeout=1)
    assert res["ok"] is False and "WfTimeoutError" in res["error"]
    _wf.save_workflow("secdoc", module_source='META = {"description": "s"}\n'
                     "def run(ctx, inputs):\n    return {\"ok\": True}\n",
                     inputs={"pw": {"type": "str", "secret": True}})
    res = _wf.run_workflow("secdoc", {"pw": "hunter2"}, profile="nobody")
    assert res["ok"] is True
    blob = json.dumps(res)
    assert "hunter2" not in blob
    assert res["call"]["inputs"] == {"pw": _wf.REDACTED}


def test_skill_kind_and_promote_inline(state):
    from cloakctl import skills as _s

    _s.save_skill("kk", "desc", steps=[{"cmd": ["status", "--json"]}])
    assert _s.show_skill("kk")["kind"] == "skill"
    saved = _s.promote_skill("kk", workflow="kk-promoted")
    assert "def run(ctx, inputs):" in saved["scaffold"]
    assert saved["next"].startswith("review scaffold")


def test_new_parsers(state):
    from cloakctl.cli import build_parser

    for argv in (["wf", "save", "x", "--code", "y"],
                 ["wf", "rm", "x"], ["wf", "show", "x", "--source"],
                 ["wf", "rename", "x", "y"], ["wf", "runs", "x"],
                 ["wf", "prune", "x"],
                 ["wf", "run", "x", "--profile", "p",
                  "--timeout", "5", "--new-tab"],
                 ["skill", "rm", "x"], ["skill", "rename", "x", "y"],
                 ["skill", "run", "x", "--timeout", "4"]):
        ns = build_parser().parse_args(argv)
        assert callable(getattr(ns, "func", None))
    ns = build_parser().parse_args(["wf", "save", "x", "--file", "-"])
    assert ns.file == "-"


def test_strict_names_and_caps(state):
    from cloakctl import skills as _s
    from cloakctl import workflows as _wf

    for bad in ["a/b", "-x", "x" * 65, ""]:
        with pytest.raises(Exception):
            _wf.save_workflow(bad, module_source=CODE_OK)
        with pytest.raises(Exception):
            _s.save_skill(bad, "d", steps=[])
    _wf.save_workflow("cap", module_source=CODE_OK)
    for i in range(205):
        _wf.log_run("cap", note=f"n{i}")
    assert _wf.show_workflow("cap")["runs"] == _wf.MAX_RUNS
    assert len(_wf.runs_workflow("cap", limit=500)["runs"]) == _wf.MAX_RUNS
    _s.save_skill("capsk", "d", steps=[])
    for i in range(205):
        _s.log_run("capsk", note=f"n{i}")
    assert len(_s.show_skill("capsk")["runs"]) == _s.MAX_RUNS
    renamed = _wf.rename_workflow("cap", "cap2")
    assert renamed["renamedFrom"] == "cap"
    assert _wf.show_workflow("cap2")["runs"] == _wf.MAX_RUNS
    with pytest.raises(Exception, match="no workflow"):
        _wf.show_workflow("cap")
    pruned = _wf.prune_workflow("cap2", keep=10)
    assert pruned["kept"] == 10
    assert len(_wf.runs_workflow("cap2")["runs"]) == 10


def test_atomic_manifest_roundtrip(state):
    from cloakctl import workflows as _wf
    from cloakctl.util import atomic_write_json

    _wf.save_workflow("atom", module_source=CODE_OK)
    mf = state / "workflows" / "atom" / "manifest.json"
    atomic_write_json(mf, json.loads(mf.read_text()))
    assert _wf.show_workflow("atom")["name"] == "atom"


def test_import_history_bounded(state):
    from cloakctl.cookies import meta_path_update
    from cloakctl import paths

    paths.profile_dir("h").mkdir(parents=True, exist_ok=True)
    for i in range(60):
        meta_path_update("h", {"importHistory": {"ts": str(i)}})
    assert len(paths.load_meta("h")["importHistory"]) == 50
