"""cloakctl live matrix7 — scale, conflicts, lifecycle (S7 hardening).

Registry races (parallel saves/log-runs stay valid + bounded), strict names,
traversal refusal, rename/rm/runs/prune/output-fetch verbs, export --json,
skill replay timeout, 3-way parallel --new-tab isolation on ONE profile,
close_window group cleanup. Exit 0 + 'N/N PASS'.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"
HOME = tempfile.mkdtemp(prefix="cloaktest7-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)


def run(*args, timeout=120):
    p = subprocess.run([BIN, *args], capture_output=True, text=True,
                       timeout=timeout, env=ENV)
    return p.returncode, p.stdout, p.stderr


def jdoc(stdout):
    try:
        return json.loads(stdout)
    except Exception:
        return None


MOD = ('META = {"version": "1.0.0", "description": "m7", "inputs": {}, '
       '"depends_on": []}\n'
       "def run(ctx, inputs):\n"
       "    return {'v': TAG}\n")
BIG = ('META = {"version": "1.0.0", "description": "big", "inputs": {}, '
       '"depends_on": []}\n'
       "def run(ctx, inputs):\n"
       "    return {'blob': 'x' * 5000}\n")  # no browser touched

# r1: strict workflow names ("-lead" is an argparse-level refusal, covered
# for strict_name itself in test_wf_prod.py)
for bad in ["bad/name", "", "x" * 65, "sp ace"]:
    rc, o, _ = run("wf", "save", bad, "--code", MOD.replace("TAG", "1"),
                   "--json")
    check(f"r1 wf save refuses {bad!r}", rc != 0 and jdoc(o) is not None
          and "error" in (jdoc(o) or {}), f"rc={rc}")

# r2: strict skill names
rc, o, _ = run("skill", "save", "bad/name", "--description", "d",
               "--steps", "[]", "--json")
check("r2 skill save refuses slash", rc != 0 and "error" in (jdoc(o) or {}),
      f"rc={rc}")

# r3: 8 parallel saves of one name — registry never half-written
procs = [subprocess.Popen(
    [BIN, "wf", "save", "r_race", "--code",
     MOD.replace("TAG", str(i)), "--json"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
    for i in range(8)]
for p in procs:
    p.communicate(timeout=120)
rc, o, _ = run("wf", "show", "r_race", "--json")
d = jdoc(o) or {}
check("r3 parallel saves converge valid", rc == 0 and d.get("name") == "r_race"
      and isinstance(d.get("version"), str), f"rc={rc}")
mf = os.path.join(HOME, "workflows", "r_race", "manifest.json")
try:
    json.load(open(mf))
    valid = True
except Exception:
    valid = False
check("r3 manifest parses after storm", valid)

# r4: 20 parallel log-runs — history valid + bounded
run("wf", "save", "r_logstorm", "--code", MOD.replace("TAG", "1"), "--json")
procs = [subprocess.Popen(
    [BIN, "wf", "log-run", "r_logstorm", "--note", f"n{i}"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
    for i in range(20)]
for p in procs:
    p.communicate(timeout=120)
rc, o, _ = run("wf", "runs", "r_logstorm", "--json")
d = jdoc(o) or {}
check("r4 log storm valid + bounded", rc == 0 and d.get("total", 0) <= 200
      and d.get("total", 0) >= 20, f"total={(d.get('total'))}")

# r5: rename preserves runs; old name dies loudly
run("wf", "log-run", "r_race", "--note", "keepme")
rc, o, _ = run("wf", "rename", "r_race", "r_renamed", "--json")
d = jdoc(o) or {}
rc2, o2, _ = run("wf", "runs", "r_renamed", "--json")
kept = any(e.get("note") == "keepme" for e in (jdoc(o2) or {}).get("runs", []))
rc3, o3, _ = run("wf", "show", "r_race", "--json")
check("r5 rename preserves runs, old dies",
      rc == 0 and d.get("renamedFrom") == "r_race" and kept
      and rc3 != 0 and "no workflow" in (jdoc(o3) or {}).get("error", ""))

# r6: rm builtin refused; rm user ok; rm missing is JSON error
rc, o, _ = run("wf", "rm", "scrape_list", "--json")
check("r6 rm builtin refused", rc != 0 and "builtin" in (jdoc(o) or {}).get("error", ""))
rc, o, _ = run("wf", "rm", "r_logstorm", "--json")
rc2, o2, _ = run("wf", "rm", "r_logstorm", "--json")
check("r6 rm user ok, twice dies loud",
      rc == 0 and rc2 != 0 and "no workflow" in (jdoc(o2) or {}).get("error", ""))

# r7: skill rename keeps runs; rm removes; traversal refused
run("skill", "save", "r_sk", "--description", "d", "--steps", "[]", "--json")
run("skill", "log-run", "r_sk", "--note", "sk-keep")
rc, o, _ = run("skill", "rename", "r_sk", "r_sk2", "--json")
rc2, o2, _ = run("skill", "show", "r_sk2", "--json")
kept = any(e.get("note") == "sk-keep" for e in (jdoc(o2) or {}).get("runs", []))
rc3, _, _ = run("skill", "rm", "r_sk2", "--json")
rc4, o4, _ = run("skill", "rm", "r_sk2", "--json")
check("r7 skill rename/rm lifecycle", rc == 0 and kept and rc3 == 0 and rc4 != 0)
rc, o, _ = run("audit", "../../etc", "--json")
check("r7 traversal refused JSON", rc != 0 and "invalid profile" in (jdoc(o) or {}).get("error", ""))

# r8+r9: big-output run spills; runs --output fetches without FS
run("wf", "save", "r_big", "--code", BIG, "--json")
rc, o, _ = run("wf", "run", "r_big", "--profile", "nobody", "--json")
d = jdoc(o) or {}
out = (d.get("outputs") or {})
stamp = out.get("stamp", "")
check("r8 big output spills with stamp", rc == 0 and out.get("truncated") is True
      and bool(stamp) and bool(out.get("outputPath")), f"rc={rc}")
rc, o, _ = run("wf", "runs", "r_big", "--output", stamp[:13], "--json")
d = jdoc(o) or {}
check("r9 spilled outputs fetched via CLI", rc == 0 and "xxxx" in (d.get("outputs") or ""))

# r10: prune keeps N, drops spills
rc, o, _ = run("wf", "prune", "r_big", "--keep", "1", "--json")
d = jdoc(o) or {}
spilldir = os.path.join(HOME, "workflows", "r_big", "runs")
left = os.listdir(spilldir) if os.path.isdir(spilldir) else []
check("r10 prune keeps newest spill only",
      rc == 0 and d.get("kept") == 1 and left == [stamp], f"left={left}")

# r11: export --json is a JSON doc, not a raw print
rc, o, _ = run("wf", "export", "r_big", "--json")
d = jdoc(o) or {}
check("r11 export --json is JSON", rc == 0 and "## Source" in d.get("markdown", ""))

# r12: 3-way parallel --new-tab runs on ONE profile stay isolated
run("profiles", "create", "r7", "--json")
run("open", "r7", "--json")
TABMOD = ('META = {"version": "1.0.0", "description": "t", "inputs": '
          '{"tag": {"type": "str"}}, "depends_on": []}\n'
          "def run(ctx, inputs):\n"
          "    u = ctx.exec(\"location.href\")\n"
          "    return {'tag': inputs['tag'], 'url': u}\n")
run("wf", "save", "r_iso", "--code", TABMOD, "--json")
procs = [subprocess.Popen(
    [BIN, "wf", "run", "r_iso", "--profile", "r7", "--new-tab",
     "--set", f"tag=T{i}", "--json"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
    for i in range(3)]
res = [p.communicate(timeout=120) for p in procs]
docs = [jdoc(r[0]) for r in res]
tags = sorted((d or {}).get("outputs", {}).get("tag", "?") for d in docs)
check("r12 3-way parallel isolation", tags == ["T0", "T1", "T2"]
      and all((d or {}).get("ok") for d in docs), f"{tags}")
rc, o, _ = run("tabs", "r7", "list", "--json")
n = len((jdoc(o) or {}).get("tabs", []))
check("r12 owned tabs closed afterwards", n == 1, f"left={n}")

# r13: skill replay timeout kills a hanging wait step
run("skill", "save", "r_hang", "--description", "d",
    "--steps", '[{"cmd": ["wait", "r7", "--text", "never-appears-xyz", "--timeout", "25"]}]',
    "--json")
import time as _t
t0 = _t.monotonic()
rc, o, _ = run("skill", "run", "r_hang", "--timeout", "4", "--json")
dt = _t.monotonic() - t0
d = jdoc(o) or {}
check("r13 replay wall-clock enforced", rc != 0 and dt < 15
      and "timeout" in json.dumps(d).lower(), f"{dt:.1f}s rc={rc}")

# r14: groups list self-heals labels of dead tabs (worst case: the whole
# browser died, labels must not linger on the resurrected profile)
rc, o, _ = run("tabs", "r7", "new", "--url", "about:blank", "--json")
tid = (jdoc(o) or {}).get("targetId", "")
run("groups", "r7", "group", "deadgrp", tid[:8], "--json")
run("close", "r7", "--json")
run("open", "r7", "--json")
rc, o, _ = run("groups", "r7", "list", "--json")
d = jdoc(o) or {}
check("r14 dead group labels healed", d.get("groups", {}) == {}
      and d.get("droppedStale", 0) >= 1, f"{d}")

# teardown: zero strays
run("close", "r7", "--json")
import subprocess as _sp
left = _sp.run(["pgrep", "-af", "cloaktest7"], capture_output=True,
               text=True).stdout.strip()
check("r15 teardown clean", left == "", left[:200])

print(f"==== {len(PASS)}/{len(PASS) + len(FAIL)} PASS ====  (HOME={HOME})")
sys.exit(0 if not FAIL else 1)
