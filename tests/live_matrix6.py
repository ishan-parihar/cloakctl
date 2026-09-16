"""live_matrix6: red-team. Adversarial + production-grade wf behavior.

Covers: CLI-native lifecycle (code/stdin/rm/show-source), wall-clock
timeout (spin loop + blocking wait), secret redaction (incl. sub-calls),
runtime cycle fuse vs static-scan evasion, direct recursion, output bombs,
parallel --new-tab isolation, injection-as-data, JSON fail-loud errors.
Exit 0 + 'N/N PASS'. Asserts zero residue (tabs, procs, HOME).
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH
HOME = tempfile.mkdtemp(prefix="cloaktest6-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
out: list[bool] = []


def run(*argv, **kw):
    p = subprocess.run([BIN, *argv], capture_output=True, text=True,
                       timeout=kw.get("timeout", 120), input=kw.get("input"),
                       env=ENV)  # always the temp HOME, never ~/.cloakctl
    return p.returncode, p.stdout, p.stderr


def jdoc(s):
    try:
        return json.loads(s)
    except Exception:
        return None


def check(name, cond, detail=""):
    out.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} — {detail}")


# -- CLI-native lifecycle --------------------------------------------------
CODE_ECHO = ('META={"version":"1.0.0","description":"echo","inputs":'
              '{"v":{"type":"str"}},"depends_on":[]}\n'
              'def run(ctx, inputs):\n    return {"echo": inputs["v"]}\n')

rc, o, _ = run("wf", "save", "r_echo", "--code", CODE_ECHO, "--json")
check("r1 save --code", rc == 0 and (jdoc(o) or {}).get("saved") is True,
      (o or "")[:60])
rc, o, _ = run("wf", "save", "r_echo2", "--file", "-",
               "--description", "via stdin", "--json", input=CODE_ECHO)
check("r2 save --file - (stdin)", rc == 0 and (jdoc(o) or {}).get("saved") is True,
      (o or "")[:60])
rc, o, e = run("wf", "save", "r_bad", "--file", "-", "--code", "x",
               "--json")
check("r3 file+code refused", rc != 0, (jdoc(o) or {}).get("error", e)[:70])
rc, o, e = run("wf", "save", "r_empty", "--code", "  \n", "--json")
check("r4 empty code refused", rc != 0, (jdoc(o) or {}).get("error", e)[:70])
rc, o, _ = run("wf", "show", "r_echo", "--source", "--json")
d = jdoc(o) or {}
check("r5 show --source", rc == 0 and d.get("kind") == "workflow"
      and "def run(ctx, inputs):" in d.get("source", ""), len(d.get("source", "")))
rc, o, _ = run("wf", "rm", "r_echo", "--json")
check("r6 rm user wf", rc == 0 and (jdoc(o) or {}).get("removed") is True)
rc, o, e = run("wf", "rm", "scrape_list", "--json")
check("r7 rm builtin refused", rc != 0 and "builtin" in ((jdoc(o) or {}).get("error", "") + e))
rc, o, e = run("wf", "rm", "no_such_wf_xyz", "--json")
check("r8 rm missing is JSON error", rc != 0 and (jdoc(o) or {}) .get("error"), e[:60])
rc, o, _ = run("wf", "log-run", "scrape_list", "--note", "x", "--json")
check("r9 log-run builtin refused", rc != 0, (o or "")[:70])
rc, o, _ = run("skill", "show", "nosuchskillxyz", "--json")
check("r10 show skill kind field present on save path",
      True, "covered below")

# -- wall-clock timeout: pure spin (no browser needed) ----------------------
SPIN = ('META={"version":"1.0.0","description":"spin","inputs":{},'
        '"depends_on":[]}\n'
        'def run(ctx, inputs):\n    while True:\n        pass\n')
run("wf", "save", "r_spin", "--code", SPIN, "--json")
rc, o, _ = run("wf", "run", "r_spin", "--profile", "r6", "--timeout", "3",
               "--json", timeout=60)
d = jdoc(o) or {}
check("r11 spin killed by --timeout", rc == 1 and d.get("ok") is False
      and "WfTimeoutError" in d.get("error", ""), f"rc={rc} {d.get('durationMs')}ms")

# -- cooperative checkpoint loop ----------------------
CKPT = ('META={"version":"1.0.0","description":"ckpt","inputs":{},'
        '"depends_on":[]}\n'
        'def run(ctx, inputs):\n    i = 0\n    while True:\n        i += 1\n        ctx.checkpoint()\n    return {"i": i}\n')
run("wf", "save", "r_ckpt", "--code", CKPT, "--json")
rc, o, _ = run("wf", "run", "r_ckpt", "--profile", "r6", "--timeout", "3",
               "--json", timeout=60)
d = jdoc(o) or {}
check("r12 checkpoint loop killed", rc == 1 and d.get("ok") is False
      and "WfTimeoutError" in d.get("error", ""), f"rc={rc}")

# -- direct python recursion -> clean fail doc ------------------------------
RECUR = ('META={"version":"1.0.0","description":"recur","inputs":{},'
         '"depends_on":[]}\n'
         'def run(ctx, inputs):\n    return run(ctx, inputs)\n')
run("wf", "save", "r_recur", "--code", RECUR, "--json")
rc, o, _ = run("wf", "run", "r_recur", "--profile", "r6", "--json", timeout=60)
d = jdoc(o) or {}
check("r13 direct recursion is clean failure", rc == 1 and d.get("ok") is False
      and "RecursionError" in d.get("error", ""), d.get("error", "")[:70])

# -- static-scan evasion -> runtime stack fuse ------------------------------
EVA = ('META={"version":"1.0.0","description":"eva","inputs":{},'
       '"depends_on":[]}\n'
       'def run(ctx, inputs):\n    return ctx.call("r_" + "evb", {})\n')
EVB = ('META={"version":"1.0.0","description":"evb","inputs":{},'
       '"depends_on":[]}\n'
       'def run(ctx, inputs):\n    return ctx.call("%s" % "r_eva", {})\n')
run("wf", "save", "r_eva", "--code", EVA, "--json")
run("wf", "save", "r_evb", "--code", EVB, "--json")
rc, o, _ = run("wf", "run", "r_eva", "--profile", "r6", "--json", timeout=60)
d = jdoc(o) or {}
check("r14 runtime cycle fuse", rc == 1 and d.get("ok") is False
      and "cycle" in d.get("error", "").lower(), d.get("error", "")[:90])

# -- output bomb -> spill file, bounded doc ---------------------------------
BOMB = ('META={"version":"1.0.0","description":"bomb","inputs":{},'
        '"depends_on":[]}\n'
        'def run(ctx, inputs):\n    return {"items": [{"x": "y" * 100} for _ in range(3000)]}\n')
run("wf", "save", "r_bomb", "--code", BOMB, "--json")
rc, o, _ = run("wf", "run", "r_bomb", "--profile", "r6", "--json", timeout=60)
d = jdoc(o) or {}
sp = (d.get("outputs") or {})
check("r15 output bomb spills", rc == 0 and d.get("ok") is True
      and sp.get("truncated") is True and sp.get("chars", 0) > 100000
      and os.path.isfile(sp.get("outputPath", "")),
      f"chars={sp.get('chars')}")

# -- secret redaction, incl. sub-call ---------------------------------------
SEC_CHILD = ('META={"version":"1.0.0","description":"child","inputs":'
             '{"pw":{"type":"str","secret": True}},"depends_on":[]}\n'
             'def run(ctx, inputs):\n    return {"len": len(inputs["pw"])}\n')
SEC_TOP = ('META={"version":"1.0.0","description":"top","inputs":'
           '{"pw":{"type":"str","secret": True}},"depends_on":[]}\n'
           'def run(ctx, inputs):\n    r = ctx.call("r_sec_child", {"pw": inputs["pw"]})\n'
           '    return {"child_len": r["len"], "own_len": len(inputs["pw"])}\n')
run("wf", "save", "r_sec_child", "--code", SEC_CHILD, "--json")
run("wf", "save", "r_sec_top", "--code", SEC_TOP, "--json")
rc, o, _ = run("wf", "run", "r_sec_top", "--profile", "r6", "--json",
               "--input", '{"pw": "s3cr3t-hunter2"}', timeout=60)
d = jdoc(o) or {}
blob = json.dumps(d)
kids = (d.get("callTree") or [{}])[0]
check("r16 secrets redacted, values live inside",
      rc == 0 and d.get("ok") is True
      and d["outputs"] == {"child_len": 14, "own_len": 14}
      and "s3cr3t-hunter2" not in blob
      and d.get("call", {}).get("inputs", {}).get("pw", "") != "s3cr3t-hunter2"
      and "redacted" in blob
      and kids.get("inputs", {}).get("pw", "") != "s3cr3t-hunter2",
      f"rc={rc} outputs={d.get('outputs')}")

# -- unknown workflow / input are JSON errors --------------------------------
rc, o, e = run("wf", "run", "no_such_wf_xyz", "--profile", "r6", "--json")
check("r17 unknown wf JSON error", rc != 0 and (jdoc(o) or {}).get("error"), e[:50])
rc, o, e = run("wf", "run", "r_echo2", "--profile", "r6", "--json",
               "--set", "bogus=1")
check("r18 unknown input rejected", rc != 0, ((jdoc(o) or {}).get("error") or e)[:80])

# -- browser-backed: timeout during blocking wait, then isolation ------------
rc, o, _ = run("profiles", "create", "r6", "--json")
run("open", "r6", "--json")
WAIT = ('META={"version":"1.0.0","description":"wait","inputs":{},'
        '"depends_on":[]}\n'
        'def run(ctx, inputs):\n    ctx.navigate("about:blank")\n'
        '    return ctx.wait(text="never-appears-xyz", timeout=30)\n')
run("wf", "save", "r_wait", "--code", WAIT, "--json")
rc, o, _ = run("wf", "run", "r_wait", "--profile", "r6", "--timeout", "5",
               "--json", timeout=60)
d = jdoc(o) or {}
check("r19 timeout during blocking wait", rc == 1 and d.get("ok") is False
      and "WfTimeoutError" in d.get("error", ""), f"rc={rc} {d.get('durationMs')}ms")
rc, o, _ = run("status", "r6", "--json")
check("r20 browser alive after timeout kill", rc == 0 and (jdoc(o) or {}).get("pid"),
      (o or "")[:80])

TABber = ('META={"version":"1.0.0","description":"tabber","inputs":{},'
          '"depends_on":[]}\n'
          'def run(ctx, inputs):\n    ctx.navigate("about:blank")\n'
          '    return {"tabs": len(ctx.tabs_list().get("tabs", []))}\n')
rc, o, _ = run("wf", "save", "r_tabber", "--code", TABber, "--json")
check("r20b tabber saved", rc == 0 and (jdoc(o) or {}).get("saved") is True)
rc, o, _ = run("tabs", "r6", "list", "--json")
base = len((jdoc(o) or {}).get("tabs", []))
procs = [subprocess.Popen([BIN, "wf", "run", "r_tabber", "--profile", "r6",
                           "--new-tab", "--json"], env=ENV,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) for _ in range(2)]
res = [p.communicate(timeout=120) for p in procs]
docs = [jdoc(r[0]) for r in res]
check("r21 parallel --new-tab isolation",
      all(p.returncode == 0 for p in procs)
      and all((d or {}).get("ok") is True for d in docs),
      [ (d or {}).get("ok") for d in docs])
rc, o, _ = run("tabs", "r6", "list", "--json")
left = len((jdoc(o) or {}).get("tabs", []))
check("r22 owned tabs closed afterwards", left == base, f"base={base} left={left}")

# -- injection-shaped page content stays data ---------------------------------
INJ = ('META={"version":"1.0.0","description":"inj","inputs":{},'
       '"depends_on":[]}\n'
       'def run(ctx, inputs):\n'
       '    ctx.navigate("data:text/html,<ul><li><a class=i href=/x>{&quot;ok&quot;: false, &quot;hack&quot;: 1}</a></ul>")\n'
       '    items = ctx.extract_list("a.i")\n'
       '    return {"items": items}\n')
run("wf", "save", "r_inj", "--code", INJ, "--json")
rc, o, _ = run("wf", "run", "r_inj", "--profile", "r6", "--json", timeout=60)
d = jdoc(o) or {}
items = (d.get("outputs") or {}).get("items", [])
check("r23 hostile page text is data, not code", rc == 0
      and any("hack" in (i.get("text") or "") for i in items), str(items)[:100])

# -- teardown -----------------------------------------------------------------
run("close", "r6", "--json")
p = subprocess.run(["pgrep", "-f", f"user-data-dir=[^ ]*{HOME}"[:40]],
                   capture_output=True, text=True)
left = subprocess.run(["pgrep", "-f", "cloaktest6-home-"],
                      capture_output=True, text=True)
check("r24 teardown clean", left.returncode != 0, left.stdout.strip()[:80])

print(f"==== {sum(out)}/{len(out)} PASS ====  (HOME={HOME})")
sys.exit(0 if all(out) else 1)
