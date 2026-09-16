"""Session labels + saved skill macros.

`session name` labels the current profile session (who/what, like neo's
name_session). `skill save` stores a named, replayable sequence of cloakctl
steps (agent-declared, like neo's save_skill); `skill run` replays them;
`skill log-run` records a run for later review (mark_skill_run parity).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import paths
from .util import atomic_write_json, now_iso, strict_name

MAX_RUNS = 200  # run-history cap per skill doc (prune, don't grow)


def _skills_dir() -> Path:
    d = paths.HOME_STATE / "skills"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def name_session(name: str, label: str, summary: str = "",
                 category: str = "") -> dict[str, Any]:
    meta = paths.load_meta(name)
    meta["sessionLabel"] = {"label": label, "summary": summary,
                              "category": category, "ts": now_iso()}
    paths.save_meta(name, meta)
    return {"profile": name, "label": label, "summary": summary,
            "category": category}


def _skill_path(skill: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in skill).strip("-")[:64]
    if not safe:
        raise ValueError("skill: empty name")
    return _skills_dir() / f"{safe}.json"


def save_skill(skill: str, description: str, site: str = "",
                steps: list[dict] | None = None, notes: str = "") -> dict[str, Any]:
    strict_name("skill", skill)
    steps = steps or []
    for s in steps:
        if not isinstance(s, dict) or "cmd" not in s:
            raise ValueError("skill steps must be [{cmd: [...], ...}]")
    from .util import interprocess_lock
    p = _skill_path(skill)
    with interprocess_lock(p):
        prior_runs: list = []
        if p.exists():
            try:
                prior_runs = json.loads(p.read_text()).get("runs", []) or []
            except Exception:
                prior_runs = []
        data = {"name": skill, "description": description, "site": site,
                "steps": steps, "learnedNotes": notes, "runs": prior_runs,
                "updated": now_iso()}
        atomic_write_json(p, data)
    return {"skill": skill, "steps": len(steps), "saved": True}


def skill_stats(data: dict[str, Any]) -> dict[str, Any]:
    """Run health + staleness for a skill doc (evolution loop, S6)."""
    runs = data.get("runs", []) or []
    clean = [r for r in runs if r.get("ok")]
    last = runs[-1] if runs else None
    try:
        import time as _t
        age = (_t.time() - _t.mktime(_t.strptime(
            data.get("updated", "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except Exception:
        age = 0.0
    return {"runs": len(runs), "cleanRuns": len(clean),
            "lastRun": last.get("ts") if last else None,
            "lastOk": last.get("ok") if last else None,
            "ageDays": round(age, 1),
            "stale": len(clean) == 0 and age > 30}


def search_skills(text: str) -> dict[str, Any]:
    q = text.lower()
    hits = []
    for f in sorted(_skills_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        blob = " ".join([data.get("name", ""), data.get("description", ""),
                           data.get("site", ""), data.get("learnedNotes", "")]).lower()
        if q in blob:
            hits.append({"skill": data.get("name", f.stem),
                         "description": data.get("description", ""),
                         "site": data.get("site", ""),
                         "steps": len(data.get("steps", []))})
    return {"query": text, "skills": hits}


def list_skills() -> dict[str, Any]:
    d = _skills_dir()
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        out.append({"skill": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "site": data.get("site", ""),
                    "steps": len(data.get("steps", [])),
                    **skill_stats(data)})
    return {"skills": out}


def show_skill(skill: str) -> dict[str, Any]:
    p = _skill_path(skill)
    if not p.exists():
        raise FileNotFoundError(f"no skill {skill!r}")
    data = json.loads(p.read_text())
    data["kind"] = "skill"  # agent-owned context + static replay, never code
    data["stats"] = skill_stats(data)
    return data


def log_run(skill: str, ok: bool = True, note: str = "",
            extra: dict | None = None) -> dict[str, Any]:
    from .util import interprocess_lock
    p = _skill_path(skill)
    with interprocess_lock(p):
        data = show_skill(skill)
        data.pop("stats", None)  # computed view only; never persisted
        entry = {"ts": now_iso(), "ok": ok, "note": note}
        if extra:
            entry.update(extra)
        runs = data.setdefault("runs", [])
        runs.append(entry)
        del runs[:-MAX_RUNS]  # bounded: compounding must not grow docs
        atomic_write_json(p, data)
        return {"skill": skill, "runs": len(data["runs"])}


def run_skill(skill: str, timeout: float | None = None) -> dict[str, Any]:
    """Replay a saved skill's steps in-process. Steps are argv lists.

    Step stdout is captured (not printed) so `skill run --json` emits a
    single summary document; per-step output is kept truncated in results.
    `timeout` is a wall-clock over the whole replay (a hanging `wait`
    step must not hang the replay).
    """
    import contextlib
    import io as _io

    from .cli import main as cli_main

    from .workflows import _arm_timeout

    data = show_skill(skill)
    disarm = _arm_timeout(timeout)
    try:
        results = []
        t0 = time.monotonic()
        stamp = now_iso().replace(":", "-")
        # Sanitized stem: the registry name is strict at save, but resolve the
        # spill dir from the same mapping as the doc so raw/odd names can
        # never escape the skills dir.
        logdir = _skills_dir() / f"{_skill_path(skill).stem}.steps" / stamp
        for i, step in enumerate(data.get("steps", [])):
            argv = [str(a) for a in step.get("cmd", [])]
            buf = _io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = cli_main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:
                dur = round((time.monotonic() - t0) * 1000)
                log_run(skill, ok=False, note=f"replay crashed at step {i}: {e}",
                        extra={"failedStep": i, "steps": len(results),
                               "durationMs": dur})
                return {"skill": skill, "ok": False, "failedStep": i,
                        "error": str(e), "results": results, "durationMs": dur}
            full = buf.getvalue()
            entry: dict[str, Any] = {"step": i, "cmd": argv, "rc": rc,
                                     "output": full[:4000]}
            if len(full) > 4000:  # spill full evidence to disk, keep doc small
                try:
                    logdir.mkdir(parents=True, exist_ok=True)
                    p = logdir / f"step-{i}.log"
                    p.write_text(full)
                    entry["outputPath"] = str(p)
                    entry["truncated"] = True
                except Exception:
                    pass
            results.append(entry)
            if rc != 0:
                dur = round((time.monotonic() - t0) * 1000)
                log_run(skill, ok=False, note=f"replay failed at step {i} rc={rc}",
                        extra={"failedStep": i, "steps": len(results),
                               "durationMs": dur})
                return {"skill": skill, "ok": False, "failedStep": i,
                        "results": results, "durationMs": dur}
        dur = round((time.monotonic() - t0) * 1000)
        log_run(skill, ok=True, note=f"replay {len(results)} steps",
                extra={"steps": len(results), "durationMs": dur})
        return {"skill": skill, "ok": True, "steps": len(results),
                "results": results, "durationMs": dur}
    finally:
        try:
            disarm()
        except Exception:
            pass


def rm_skill(skill: str) -> dict[str, Any]:
    """Delete a saved skill (doc + step spill). No FS needed."""
    p = _skill_path(skill)
    if not p.exists():
        raise FileNotFoundError(f"no skill {skill!r}")
    import shutil as _sh
    p.unlink(missing_ok=True)
    _sh.rmtree(_skills_dir() / f"{p.stem}.steps", ignore_errors=True)
    return {"skill": skill, "removed": True}


def rename_skill(skill: str, new: str) -> dict[str, Any]:
    """Rename a skill, preserving description/steps/notes/run history."""
    strict_name("skill", new)
    data = show_skill(skill)
    data.pop("stats", None)
    data["name"] = new
    data["updated"] = now_iso()
    atomic_write_json(_skill_path(new), data)
    rm_skill(skill)
    return {"skill": new, "renamedFrom": skill, "saved": True}


def promote_skill(skill: str, workflow: str | None = None) -> dict:
    """Scaffold a Tier-3 workflow module from a recorded macro.

    Known verbs become ctx calls; snapshot refs (brittle across pages)
    become TODO(semantic) markers for the agent to replace with
    find_ref/fill_form/selectors. Saved as a user workflow draft.
    """
    from . import workflows as _wf

    data = show_skill(skill)
    target = workflow or f"{skill}-promoted"
    out = [
        '"""Promoted from skill %r. Replace TODO(semantic) markers' % skill,
        "with semantic addressing (ctx.find_ref / ctx.fill_form / selectors).",
        '"""',
        "",
        "META = {",
        '    "version": "0.1.0",',
        f'    "description": "Promoted from skill {skill}. DRAFT - '
        "harden refs before production use.\",",
        f'    "site": {data.get("site", "")!r},',
        '    "inputs": {"url": {"type": "str", "required": False,',
        '                        "description": "Starting page URL."}},',
        '    "depends_on": [],',
        "}",
        "",
        "",
        "def run(ctx, inputs):",
        '    if inputs.get("url"):',
        '        ctx.navigate(inputs["url"])',
    ]
    todos = 0
    for step in data.get("steps", []):
        argv = [str(a) for a in step.get("cmd", [])]
        line, todo = _promote_argv(argv)
        out.append(f"    {line}")
        todos += todo
    out.append('    return {"ok": True}')
    code = "\n".join(out) + "\n"
    import os as _os
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        saved = _wf.save_workflow(
            target, tmp,
            description=(f"Promoted from skill {skill} "
                         f"(DRAFT, {todos} semantic TODOs)."),
            site=data.get("site", ""))
    finally:
        try:
            _os.unlink(tmp)
        except Exception:
            pass
    saved["todos"] = todos
    saved["scaffold"] = code  # inline: read/edit/resave via `wf save --code`
    saved["next"] = (f"review scaffold, replace TODO(semantic) markers, "
                     f"then `wf save {target} --code '...' "
                     f"--description ... --inputs ...`")
    return saved


def _promote_opt(argv: list, flag: str):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _promote_argv(argv: list) -> tuple:
    """Translate one macro argv into a scaffold line. Returns (line, todos)."""
    if not argv:
        return "pass", 0
    verb, rest = argv[0], argv[1:]
    if verb in ("open", "close", "profiles", "status", "attach",
                "import", "doctor", "session", "audit", "history"):
        return (f"# manual: {argv} (session management stays "
                "outside workflows)"), 0
    if verb == "navigate":
        url = _promote_opt(rest, "--url")
        if url:
            return f"ctx.navigate({url!r})", 0
        for f in ("--back", "--forward", "--reload"):
            if f in rest:
                return f"ctx.{f[2:]}()", 0
        return f"# TODO(manual): {argv}", 1
    if verb == "snapshot":
        return "snap = ctx.snapshot()", 0
    if verb == "diff":
        return "changes = ctx.diff()", 0
    if verb == "act":
        kind = rest[1] if len(rest) > 1 else "click"
        ref = _promote_opt(rest, "--ref")
        text = _promote_opt(rest, "--text")
        key = _promote_opt(rest, "--key")
        if ref:
            extra = ""
            if text is not None:
                extra += f", text={text!r}"
            if key is not None:
                extra += f", key={key!r}"
            return ((f"ctx.act({kind!r}, ref={ref!r}{extra})  "
                     "# TODO(semantic): replace ref with ctx.find_ref()"), 1)
        if text is not None:
            return f"ctx.act({kind!r}, text={text!r})", 0
        if key is not None:
            return f"ctx.act({kind!r}, key={key!r})", 0
        return f"ctx.act({kind!r})", 0
    if verb == "wait":
        text, sel = _promote_opt(rest, "--text"), _promote_opt(rest, "--selector")
        if text is not None:
            return f"ctx.wait(text={text!r})", 0
        if sel is not None:
            return f"ctx.wait(selector={sel!r})", 0
        return f"# TODO(manual): {argv}", 1
    if verb == "read":
        return "content = ctx.read()", 0
    if verb == "grep":
        pat = rest[1] if len(rest) > 1 else ""
        return f"matches = ctx.grep({pat!r})", 0
    if verb in ("run", "exec"):
        code = rest[1] if len(rest) > 1 else ""
        meth = "run_js" if verb == "run" else "exec"
        return f"value = ctx.{meth}({code!r})", 0
    if verb == "validate":
        return "verdict = ctx.validate()", 0
    if verb in ("tabs", "windows", "groups", "screenshot", "pdf",
                "download", "upload", "skill"):
        return f"# TODO(manual): {argv}", 1
    return f"# TODO(manual): {argv}", 1
