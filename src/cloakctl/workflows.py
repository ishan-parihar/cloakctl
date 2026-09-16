"""Tier-3 workflow registry: versioned, composable Python browser modules.

Layout:  ~/.cloakctl/workflows/<name>/{module.py, manifest.json}
Builtins ship in `cloakctl.wf_lib` (read-only, META dict per module) and
resolve when no user workflow of the same name exists.

Composition is ONLY via `ctx.call(name, inputs)` (+ explicit `depends_on`).
`wf save` refuses cyclic graphs; runs enforce depth + step budgets and a
call-stack cycle fuse (see sdk.RunState).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import paths
from .sdk import (Context, RunState, WfBudgetError, WfCycleError, WfError,
                   WfInputError, WfTimeoutError)
from .util import atomic_write_json, atomic_write_text, now_iso, strict_name

MAX_RUNS = 200  # run-history cap per manifest (prune, don't grow)

try:
    import signal as _signal
    _HAS_ALARM = hasattr(_signal, "setitimer")
except Exception:
    _signal = None  # type: ignore[assignment]
    _HAS_ALARM = False

REDACTED = "\u22efredacted\u22ef"  # ⋯redacted⋯, greppable + JSON-safe


def _redact(inputs: dict, spec: dict) -> dict:
    """Copy inputs with `secret: true` spec entries redacted.

    Declared secrets never appear in the echoed callTree, run documents,
    or error strings — only the module itself sees the real value."""
    out = dict(inputs)
    for k, decl in (spec or {}).items():
        if isinstance(decl, dict) and decl.get("secret") and k in out:
            out[k] = REDACTED
    return out

INPUT_TYPES = {"str": str, "int": int, "float": float, "bool": bool,
               "list": list, "dict": dict}

STALE_DAYS = 30


def _wf_root() -> Path:
    d = paths.HOME_STATE / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _safe(name: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in name).strip("-")[:64]
    if not safe:
        raise ValueError("workflow: empty name")
    return safe


def _wf_dir(name: str) -> Path:
    return _wf_root() / _safe(name)


def _builtin_lib() -> Path:
    return Path(__file__).parent / "wf_lib"


def _read_meta(path: Path) -> dict:
    """Read a module-level META dict via AST (no import needed)."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "META" for t in node.targets):
            return dict(ast.literal_eval(node.value))
    raise WfError(f"{path}: module must define a META dict")


def _all_manifests(extra: dict | None = None) -> dict[str, dict]:
    """All known manifests (user + builtin), optionally overlaying one."""
    out: dict[str, dict] = {}
    lib = _builtin_lib()
    if lib.is_dir():
        for f in sorted(lib.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                m = _read_meta(f)
                m = {"name": f.stem, "builtin": True, "runs": [], **m}
                out[m["name"]] = m
            except Exception:
                continue
    for f in sorted(_wf_root().glob("*/manifest.json")):
        try:
            m = json.loads(f.read_text())
            m["builtin"] = False
            out[m["name"]] = m
        except Exception:
            continue
    if extra:
        out[extra["name"]] = extra
    return out


def _called_names(module_path: Path) -> set[str]:
    """Workflow names referenced via ctx.call('name') / .call('name')."""
    try:
        tree = ast.parse(module_path.read_text())
    except Exception:
        return set()
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.add(node.args[0].value)
    return names


def _check_cycles(manifests: dict[str, dict],
                  module_for: dict[str, Path | None]) -> None:
    """Toposort the dependency graph; raise WfCycleError with the path."""
    edges: dict[str, set[str]] = {}
    for name, m in manifests.items():
        deps = set(m.get("depends_on") or [])
        mod = module_for.get(name)
        if mod is not None and mod.exists():
            deps |= _called_names(mod)
        edges[name] = {d for d in deps if d in manifests}
        if m.get("recursive") and name in edges[name]:
            edges[name].discard(name)  # declared bounded self-recursion
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    path: list[str] = []

    def visit(n: str) -> None:
        color[n] = GRAY
        path.append(n)
        for d in sorted(edges.get(n, ())):
            if color[d] == GRAY:
                i = path.index(d)
                raise WfCycleError(
                    f"cycle refused: {' -> '.join([*path[i:], d])}")
            if color[d] == WHITE:
                visit(d)
        path.pop()
        color[n] = BLACK

    for n in sorted(edges):
        if color[n] == WHITE:
            visit(n)


def validate_inputs(spec: dict, inputs: dict) -> dict:
    """Apply defaults, enforce required + types. Unknown keys rejected."""
    spec = spec or {}
    unknown = sorted(set(inputs) - set(spec))
    if unknown:
        raise WfInputError(f"unknown inputs: {unknown}; spec: {sorted(spec)}")
    out: dict[str, Any] = {}
    for key, decl in spec.items():
        if not isinstance(decl, dict) or "type" not in decl:
            raise WfInputError(f"inputs spec[{key!r}]: need {{type, ...}}")
        tname = decl["type"]
        if tname not in INPUT_TYPES:
            raise WfInputError(f"inputs spec[{key!r}]: bad type {tname!r}")
        if key in inputs:
            val = inputs[key]
        elif "default" in decl:
            val = decl["default"]
        elif decl.get("required", True):
            raise WfInputError(f"missing required input: {key!r}")
        else:
            continue
        want = INPUT_TYPES[tname]
        if tname == "float" and isinstance(val, int) and not isinstance(val, bool):
            val = float(val)
        if not isinstance(val, want) or (want is bool and not isinstance(val, bool)):
            raise WfInputError(
                f"input {key!r}: want {tname}, got {type(val).__name__}")
        out[key] = val
    return out


def _resolve(name: str) -> tuple[str, Path, dict]:
    """-> (kind, module_path, manifest). User registry shadows builtins."""
    safe = _safe(name)
    d = _wf_root() / safe
    mf = d / "manifest.json"
    mod = d / "module.py"
    if mf.exists() and mod.exists():
        m = json.loads(mf.read_text())
        m["builtin"] = False
        return "user", mod, m
    libmod = _builtin_lib() / f"{safe}.py"
    if libmod.exists():
        m = {"name": safe, "builtin": True, "runs": [], **_read_meta(libmod)}
        return "builtin", libmod, m
    raise FileNotFoundError(f"no workflow {name!r}")


def save_workflow(name: str, module_file: str | None = None,
                  description: str = "",
                  site: str = "", inputs: dict | None = None,
                  depends_on: list[str] | None = None,
                  recursive: bool = False,
                  max_depth: int | None = None,
                  max_steps: int | None = None,
                  module_source: str | None = None) -> dict[str, Any]:
    """Save a workflow module. Source comes from `module_file` (path),
    `module_source` (inline code), or stdin when module_file is `-`.
    Exactly one source is required."""
    if (module_file is None) == (module_source is None):
        raise WfError("wf save: give exactly one of --file / --code")
    if module_source is not None:
        source = module_source
        origin = "--code"
    elif module_file == "-":
        source = sys.stdin.read()
        origin = "stdin"
    else:
        src = Path(module_file)
        if not src.is_file():
            raise FileNotFoundError(f"module file not found: {module_file}")
        source = src.read_text()
        origin = str(src)
    if not source.strip():
        raise WfError(f"wf save: empty module from {origin}")
    strict_name("workflow", name)
    safe = _safe(name)
    tree = ast.parse(source)  # syntax gate before anything else
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run"
               for n in tree.body):
        raise WfError(f"{origin}: module must define run(ctx, inputs)")
    # META is self-description: CLI flags win, META fills the rest, so a
    # bare `--code` module compounds without extra flags.
    meta: dict[str, Any] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "META" for t in n.targets):
            try:
                v = ast.literal_eval(n.value)
                if isinstance(v, dict):
                    meta = v
            except Exception:
                meta = {}
            break
    if description == "":
        description = str(meta.get("description", "") or "")
    if site == "":
        site = str(meta.get("site", "") or "")
    if inputs is None:
        inputs = meta.get("inputs", {}) or {}
    if depends_on is None:
        depends_on = list(meta.get("depends_on", []) or [])
    if recursive is False:
        recursive = bool(meta.get("recursive", False))
    if max_depth is None:
        max_depth = meta.get("max_depth")
    if max_steps is None:
        max_steps = meta.get("max_steps")
    version = str(meta.get("version", "") or "1.0.0")
    inputs = inputs or {}
    for k, decl in inputs.items():
        if not isinstance(decl, dict) or decl.get("type") not in INPUT_TYPES:
            raise WfInputError(f"inputs[{k!r}]: need {{type: ...}} "
                               f"(one of {sorted(INPUT_TYPES)})")
    d = _wf_root() / safe
    prior_runs: list = []
    mf = d / "manifest.json"
    from .util import interprocess_lock
    with interprocess_lock(mf):
        if mf.exists():
            try:
                prior_runs = json.loads(mf.read_text()).get("runs", []) or []
            except Exception:
                prior_runs = []
        manifest = {"name": safe, "version": version,
                    "description": description, "site": site,
                    "inputs": inputs, "depends_on": depends_on or [],
                    "recursive": recursive,
                    "max_depth": max_depth or 8, "max_steps": max_steps or 500,
                    "runs": prior_runs, "updated": now_iso()}
        mods = _all_manifests(manifest)
        mod_for = {n: (_wf_root() / _safe(n) / "module.py"
                       if (m.get("builtin") is False) else
                       (_builtin_lib() / f"{_safe(n)}.py"))
                   for n, m in mods.items()}
        import tempfile as _tf2
        with _tf2.NamedTemporaryFile("w", suffix=".py",
                                     delete=False) as _f:
            _f.write(source)
            _tmp = _f.name
        try:
            mod_for[safe] = Path(_tmp)
            _check_cycles(mods, mod_for)
        finally:
            try:
                os.unlink(_tmp)
            except Exception:
                pass
        for dep in manifest["depends_on"]:
            if dep not in mods:
                raise WfError(f"depends_on {dep!r}: unknown workflow "
                              f"(have: {sorted(mods)})")
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / "module.py", source)
        atomic_write_json(mf, manifest)
        return {"workflow": safe, "saved": True,
                "depends_on": manifest["depends_on"]}


def rm_workflow(name: str) -> dict[str, Any]:
    """Delete a user workflow (registry dir + run spill). No FS needed."""
    kind, _mod, _m = _resolve(name)
    if kind == "builtin":
        raise WfError(f"cannot remove builtin workflow {name!r}")
    import shutil as _sh
    d = _wf_root() / _safe(name)
    _sh.rmtree(d, ignore_errors=True)
    return {"workflow": _safe(name), "removed": True}


def rename_workflow(name: str, new: str) -> dict[str, Any]:
    """Rename a user workflow, preserving manifest, source, run history.

    Callers holding the old name get a clean `no workflow` error (JSON,
    rc=1) — rename is explicit, never silent."""
    strict_name("workflow", new)
    kind, _mod, m = _resolve(name)
    if kind == "builtin":
        raise WfError(f"cannot rename builtin workflow {name!r}")
    target = _wf_root() / _safe(new)
    if (_wf_root() / _safe(new) / "manifest.json").exists() or \
            (_builtin_lib() / f"{_safe(new)}.py").exists():
        raise WfError(f"cannot rename to {new!r}: name taken")
    m["name"] = _safe(new)
    m["updated"] = now_iso()
    src = _wf_root() / _safe(name)
    atomic_write_json(src / "manifest.json", m)
    src.rename(target)
    return {"workflow": _safe(new), "renamedFrom": _safe(name),
            "saved": True}


def stats(data: dict) -> dict[str, Any]:
    runs = data.get("runs", []) or []
    clean = [r for r in runs if r.get("ok")]
    last = runs[-1] if runs else None
    try:
        age = (time.time() - time.mktime(time.strptime(
            data.get("updated", "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except Exception:
        age = 0.0
    return {"runs": len(runs), "cleanRuns": len(clean),
            "lastRun": last.get("ts") if last else None,
            "lastOk": last.get("ok") if last else None,
            "ageDays": round(age, 1),
            "stale": len(clean) == 0 and age > STALE_DAYS}


def list_workflows(include_builtin: bool = True) -> dict[str, Any]:
    out = []
    for name, m in sorted(_all_manifests().items()):
        if m.get("builtin") and not include_builtin:
            continue
        out.append({"workflow": name, "description": m.get("description", ""),
                    "site": m.get("site", ""),
                    "builtin": bool(m.get("builtin")),
                    "version": m.get("version", ""),
                    **stats(m)})
    return {"workflows": out}


def show_workflow(name: str, include_source: bool = False) -> dict[str, Any]:
    kind, mod, m = _resolve(name)
    doc: dict[str, Any] = {"kind": "workflow", **m, **stats(m)}
    if include_source:
        try:
            doc["source"] = Path(mod).read_text()
        except Exception as e:
            doc["source_error"] = f"{type(e).__name__}: {e}"
    return doc


def search_workflows(text: str) -> dict[str, Any]:
    q = text.lower()
    hits = []
    for name, m in sorted(_all_manifests().items()):
        blob = " ".join([name, m.get("description", ""), m.get("site", ""),
                         json.dumps(m.get("inputs", {}))]).lower()
        if q in blob:
            hits.append({"workflow": name,
                         "description": m.get("description", ""),
                         "site": m.get("site", ""),
                         "builtin": bool(m.get("builtin"))})
    return {"query": text, "workflows": hits}


def log_run(name: str, ok: bool = True, note: str = "",
            extra: dict | None = None) -> dict[str, Any]:
    mf = _wf_root() / _safe(name) / "manifest.json"
    from .util import interprocess_lock
    with interprocess_lock(mf):  # appends must not lose races (r4)
        kind, _mod, m = _resolve(name)
        if kind == "builtin":
            raise WfError(f"cannot log runs on builtin {name!r}")
        entry = {"ts": now_iso(), "ok": ok, "note": note}
        if extra:
            entry.update(extra)
        runs = m.setdefault("runs", [])
        runs.append(entry)
        del runs[:-MAX_RUNS]  # bounded: compounding must not grow manifests
        atomic_write_json(mf, m)
        return {"workflow": name, "runs": len(m["runs"])}


def _load_module(safe: str, path: Path):
    modname = f"cloakctl_wf_{safe}"
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise WfError(f"cannot load module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(modname, None)
        raise
    return mod


def _run_inner(name: str, inputs: dict, profile: str, tab: str | None,
               state: RunState, node: dict) -> Any:
    kind, mod_path, manifest = _resolve(name)
    validated = validate_inputs(manifest.get("inputs", {}), inputs)
    node["version"] = manifest.get("version", "")
    mod = _load_module(_safe(name), mod_path)
    run = getattr(mod, "run", None)
    if not callable(run):
        raise WfError(f"workflow {name!r}: no callable run(ctx, inputs)")
    ctx = Context(profile, tab, state, node)
    out = run(ctx, validated)
    if not isinstance(out, dict):
        raise WfError(f"workflow {name!r}: run() must return a dict, "
                      f"got {type(out).__name__}")
    json.dumps(out)  # serializability gate
    return out


def _arm_timeout(timeout: float | None):
    """Process wall-clock via ITIMER_REAL (posix). Returns disarmer."""
    if not timeout or timeout <= 0:
        return lambda: None
    if not _HAS_ALARM or _signal is None:
        raise WfError("wf run --timeout needs POSIX setitimer "
                      "(this platform lacks it)")
    prev_handler = _signal.getsignal(_signal.SIGALRM)
    try:
        prev_timer = _signal.getitimer(_signal.ITIMER_REAL)
    except Exception:
        prev_timer = (0.0, 0.0)

    def _fire(_signum, _frame):
        raise WfTimeoutError(f"wall-clock timeout after {timeout:g}s")

    _signal.signal(_signal.SIGALRM, _fire)
    _signal.setitimer(_signal.ITIMER_REAL, timeout)

    def _disarm():
        try:
            _signal.setitimer(_signal.ITIMER_REAL, 0.0)
        finally:
            _signal.signal(_signal.SIGALRM, prev_handler)
            try:
                if prev_timer[0] > 0:
                    _signal.setitimer(_signal.ITIMER_REAL, *prev_timer[:2])
            except Exception:
                pass
    return _disarm


def run_workflow(name: str, inputs: dict, profile: str,
                 tab: str | None = None, timeout: float | None = None,
                 new_tab: bool = False) -> dict[str, Any]:
    _kind, _mod, manifest = _resolve(name)
    deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None
    state = RunState(name, manifest.get("max_steps", 500),
                     manifest.get("max_depth", 8), deadline=deadline)
    spec = manifest.get("inputs", {})
    node: dict[str, Any] = {"wf": name,
                            "inputs": _redact(inputs, spec),
                            "version": manifest.get("version", ""),
                            "children": []}
    t0 = time.monotonic()
    stamp = now_iso().replace(":", "-")
    owned_tab: str | None = None
    run_tab = tab
    disarm = _arm_timeout(timeout)
    try:
        if new_tab:
            from . import tabs as _tabs
            owned_tab = _tabs.new_tab(profile).get("targetId", "")
            run_tab = owned_tab or None
            node["tab"] = (run_tab or "")[:12]
        outputs = _run_inner(name, inputs, profile, run_tab, state, node)
        ok, err = True, ""
    except Exception as e:
        outputs, ok, err = {}, False, f"{type(e).__name__}: {e}"
    finally:
        try:
            disarm()
        except Exception:
            pass
        if owned_tab:
            try:
                from . import tabs as _tabs2
                _tabs2.close_tab(profile, owned_tab)
            except Exception:
                pass
    dur = round((time.monotonic() - t0) * 1000)
    entry: dict[str, Any] = {"ts": now_iso(), "ok": ok, "durationMs": dur,
                             "steps": state.steps, "profile": profile}
    if err:
        entry["error"] = err[:300]
    if manifest.get("builtin") is not True:
        try:
            log_run(name, ok=ok, note=f"wf run ({state.steps} steps)",
                    extra={"durationMs": dur, "steps": state.steps,
                           "stamp": stamp,
                           **({"error": entry["error"]} if err else {})})
        except Exception:
            pass
    doc: dict[str, Any] = {"workflow": name, "ok": ok, "steps": state.steps,
                            "durationMs": dur,
                            "call": {"wf": name,
                                     "inputs": _redact(inputs, spec),
                                     "version": manifest.get("version", "")},
                            "callTree": node["children"]}
    if err:
        doc["error"] = err
    else:
        blob = json.dumps(outputs, default=str)
        if len(blob) > 4000:
            try:
                logdir = _wf_root() / _safe(name) / "runs" / stamp
                logdir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(logdir / "outputs.json", blob)
                doc["outputs"] = {"truncated": True,
                                  "outputPath": str(logdir / "outputs.json"),
                                  "stamp": stamp, "chars": len(blob)}
            except Exception:
                doc["outputs"] = {"truncated": True, "chars": len(blob)}
        else:
            doc["outputs"] = outputs
    return doc


def runs_workflow(name: str, limit: int = 20,
                  output: str | None = None) -> dict[str, Any]:
    """Run history for debugging (errors, durations, spill pointers).

    With `output` (a stamp prefix), print that run's spilled outputs.json
    inline — capped and trust-wrapped, so the agent never needs FS access."""
    _kind, _mod, m = _resolve(name)
    runs = m.get("runs", []) or []
    if output:
        for e in reversed(runs):
            st = str(e.get("stamp", ""))
            if st and st.startswith(output):
                p = _wf_root() / _safe(name) / "runs" / st / "outputs.json"
                if not p.is_file():
                    raise WfError(f"run {st}: no spilled outputs")
                from . import snap as _snap
                blob = p.read_text()[:20000]
                return {"workflow": name, "stamp": st,
                        "outputs": _snap.trust_wrap(blob, f"wf:{name}:{st}"),
                        "truncated": len(p.read_text()) > 20000}
        raise WfError(f"run {output!r}: no matching stamp "
                      f"(see `wf runs {name}`)")
    tail = runs[-limit:]
    return {"workflow": name, "runs": tail, "shown": len(tail),
            "total": len(runs)}


def prune_workflow(name: str, keep: int = 50) -> dict[str, Any]:
    """Trim run history to the newest `keep` entries and drop orphaned
    spill dirs. Native cleanup so long-lived registries stay small."""
    import shutil as _sh
    mf = _wf_root() / _safe(name) / "manifest.json"
    from .util import interprocess_lock
    with interprocess_lock(mf):
        kind, _mod, m = _resolve(name)
        if kind == "builtin":
            raise WfError(f"cannot prune builtin workflow {name!r}")
        runs = m.get("runs", []) or []
        kept = runs[-max(keep, 0):] if keep > 0 else []
        stamps = {str(e.get("stamp", "")) for e in kept if e.get("stamp")}
        spill = _wf_root() / _safe(name) / "runs"
        dropped_dirs = 0
        if spill.is_dir():
            for d in spill.iterdir():
                if d.is_dir() and d.name not in stamps:
                    _sh.rmtree(d, ignore_errors=True)
                    dropped_dirs += 1
        m["runs"] = kept
        atomic_write_json(mf, m)
        return {"workflow": name, "kept": len(kept),
                "droppedRuns": len(runs) - len(kept),
                "droppedSpills": dropped_dirs}


def export_workflow(name: str) -> dict[str, Any]:
    _kind, mod_path, m = _resolve(name)
    lines = [f"# {name}", "",
             m.get("description", ""), "",
             f"Version: {m.get('version', '')} · "
             f"Builtin: {bool(m.get('builtin'))} · "
             f"Depends on: {', '.join(m.get('depends_on', [])) or '—'}", "",
             "## Inputs", ""]
    for k, decl in (m.get("inputs", {}) or {}).items():
        req = "required" if decl.get("required", True) else "optional"
        dflt = f", default={decl['default']!r}" if "default" in decl else ""
        lines.append(f"- `{k}` ({decl.get('type')}, {req}{dflt}): "
                     f"{decl.get('description', '')}")
    lines += ["", "## Call forms", "",
              f"CLI: `cloakctl wf run {name} --profile P "
              "--input '{...}'`",
              f"Python: `ctx.call({name!r}, {{...}})`",
              f"Flow step: `cloakctl wf run {name} --profile P --json`", "",
              "## Source", "", "```python",
              mod_path.read_text().rstrip(), "```"]
    return {"workflow": name, "markdown": "\n".join(lines)}
