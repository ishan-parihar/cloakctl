"""cloakctl live matrix 3 — adversarial / resilience / production-grade.

Each R-case asserts the CLI never leaves residue, never prints tracebacks,
and always emits parseable JSON. Destructive. Uses its own CLOAKCTL_HOME.
Exit 0 + 'N/N PASS' on success, 1 with FAIL lines otherwise.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HOME = tempfile.mkdtemp(prefix="cloaktest3-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
B = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def run(*args, env_extra=None, timeout=90):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([B, *args], capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def jdoc(stdout):
    try:
        return json.loads(stdout)
    except Exception:
        return None


def procs(fragment):
    p = subprocess.run(["pgrep", "-f", fragment], capture_output=True, text=True)
    return [l for l in p.stdout.splitlines() if l.strip()]


DP = "user-data-dir=" + HOME  # matches all browsers under this HOME

# --- setup: one live profile ---
run("profiles", "create", "adv")
rc, out, _ = run("--json", "open", "adv")
check("R0 open adv", rc == 0 and jdoc(out) and jdoc(out).get("pid"), out[:120])
run("navigate", "adv", "--url", "data:text/html,<html><body><button id=b>Hi</button><input id=q><a href='https://example.com'>x</a></body></html>")

# R1: SIGKILL main mid-life -> status stale, open reclaims, close leaves 0
rc, out, _ = run("--json", "status", "adv")
assert jdoc(out) and jdoc(out).get("live"), out[:150]
pid = jdoc(out)["pid"]
subprocess.run(["kill", "-9", str(pid)], check=False)
time.sleep(1)
left = procs(DP)
rc, out, _ = run("--json", "status", "adv")
st = jdoc(out)
check("R1 status sees stale", rc == 0 and st and st.get("live") is False, f"orphans={len(left)}")
rc, out, _ = run("--json", "open", "adv")
check("R1 open reclaims", rc == 0 and jdoc(out), out[:120])
run("close", "adv")
time.sleep(1)
check("R1 zero residue after reclaim+close", len(procs(DP)) == 0, f"left={len(procs(DP))}")
# R1b: kill -9 then CLOSE (no reopen) must still reap orphaned renderers
run("--json", "open", "adv")
rc, out, _ = run("--json", "status", "adv")
subprocess.run(["kill", "-9", str(jdoc(out)["pid"])], check=False)
time.sleep(1)
run("--json", "close", "adv")
time.sleep(1)
check("R1b close reaps dead-main orphans", len(procs(DP)) == 0, f"left={len(procs(DP))}")

# R2: corrupt lock file -> open treats as stale, succeeds
run("--json", "open", "adv")
lockp = os.path.join(HOME, "runtime", "lock.adv.json")
with open(lockp, "w") as f:
    f.write("{garbage!!!")
rc, out, _ = run("--json", "open", "adv")
# old browser still alive but lock unreadable: open must either reclaim or clean-error, never traceback
d = jdoc(out)
check("R2 corrupt lock safe", d is not None and "Traceback" not in out, f"rc={rc} {out[:100]}")
run("close", "adv")

# R3: dead-pid lock -> reclaim
rc, out, _ = run("--json", "open", "adv")
assert jdoc(out) and jdoc(out).get("pid"), out[:150]
pid = jdoc(out)["pid"]
subprocess.run(["kill", "-9", str(pid)], check=False)
time.sleep(1)
rc, out, _ = run("--json", "open", "adv")
check("R3 dead-pid reclaim", rc == 0 and jdoc(out), out[:100])

# R4/R5: double close + unknown close
rc, out, _ = run("--json", "close", "adv")
check("R4 first close", rc == 0 and jdoc(out) is not None, out[:100])
rc, out, err = run("--json", "close", "adv")
check("R4 double close clean", rc == 0 and jdoc(out) is not None and "Traceback" not in err, f"rc={rc}")
rc, out, err = run("--json", "close", "no-such-profile")
check("R5 unknown close idempotent", rc == 0 and (jdoc(out) or {}).get("closed") is False and "Traceback" not in err, f"rc={rc} {out[:100]}")

# R6/R7: exec on closed, bogus binary
rc, out, err = run("--json", "exec", "adv", "1+1")
check("R6 exec on closed clean", rc != 0 and jdoc(out) is not None and "Traceback" not in err, f"rc={rc}")
rc, out, err = run("--json", "open", "adv", timeout=60, env_extra={"CLOAKCTL_BROWSER": "/nonexistent/chrome"})
# open may attach to existing? adv is closed, so it launches -> must fail cleanly
check("R7 bogus binary clean", rc != 0 and "Traceback" not in err, f"rc={rc} {err[:120]}")

# R8: rm live without force refuses; with force closes (0 procs)
run("--json", "open", "adv")
rc, out, _ = run("--json", "profiles", "rm", "adv")
check("R8 rm live refuses", rc != 0 and jdoc(out) is not None, f"rc={rc}")
rc, out, _ = run("--json", "profiles", "rm", "adv", "--force")
time.sleep(1)
check("R8 rm --force closes", rc == 0 and len(procs(DP)) == 0, f"rc={rc} left={len(procs(DP))}")
run("profiles", "create", "adv")
run("--json", "open", "adv")
run("navigate", "adv", "--url", "data:text/html,<html><body><button id=b>Hi</button></body></html>")

# R9: 10-way race -> exactly one browser family, losers clean, close -> 0
kids = [subprocess.Popen([B, "--json", "open", "adv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV) for _ in range(10)]
outs = [k.communicate(timeout=120) for k in kids]
ok = sum(1 for _, o, _ in [(0, o, e) for o, e in outs] if jdoc(o) and (jdoc(o).get("pid") or jdoc(o).get("reattached")))
check("R9 race has winners", ok >= 1, f"winners={ok}")
loser_clean = all(("Traceback" not in e) for _, e in outs)
check("R9 losers traceback-free", loser_clean)
time.sleep(1)
n = len(procs(DP))
check("R9 single browser family", 1 <= n <= 16, f"procs={n}")
run("close", "adv")
time.sleep(1)
check("R9 zero after close", len(procs(DP)) == 0, f"left={len(procs(DP))}")
run("--json", "open", "adv")
run("navigate", "adv", "--url", "data:text/html,<html><body><button id=b>Hi</button><input id=q></body></html>")

# R10: chrome:// page must not hang
t0 = time.monotonic()
try:
    rc, out, err = run("--json", "navigate", "adv", "--url", "chrome://version", timeout=40)
    nav_ok = jdoc(out) is not None
except subprocess.TimeoutExpired:
    nav_ok = False
dt = time.monotonic() - t0
check("R10 chrome:// no hang", nav_ok and dt < 35, f"{dt:.1f}s rc={rc}")
run("navigate", "adv", "--url", "data:text/html,<html><body>back</body></html>")

# R11: garbage URL -> clean JSON either way
rc, out, err = run("--json", "navigate", "adv", "--url", "ht!tp://[bad")
check("R11 bad url clean", jdoc(out) is not None and "Traceback" not in err, f"rc={rc} {out[:100]}")

# R12: wait timeout -> rc!=0 inside budget
t0 = time.monotonic()
rc, out, _ = run("--json", "wait", "adv", "--text", "never-appears-zz", "--timeout", "4", timeout=40)
dt = time.monotonic() - t0
check("R12 wait timeout bounded", rc != 0 and jdoc(out) is not None and dt < 24, f"{dt:.1f}s")

# R13/R14/R15/R16: error verbs clean
rc, out, err = run("--json", "exec", "adv", "throw new Error('boom')")
check("R13 js throw exits 1", rc == 1 and jdoc(out) is not None, f"rc={rc}")
rc, out, err = run("--json", "download", "adv", "--ref", "e999", "--out-dir", "/tmp")
check("R14 bad download ref", rc != 0 and jdoc(out) is not None and "Traceback" not in err, f"rc={rc}")
rc, out, err = run("--json", "upload", "adv", "--ref", "e1", "--file", "/nonexistent.bin")
check("R15 missing upload file", rc != 0 and jdoc(out) is not None and "Traceback" not in err, f"rc={rc}")
rc, out, err = run("--json", "skill", "run", "no-such-skill")
check("R16 unknown skill run", rc != 0 and jdoc(out) is not None and "Traceback" not in err, f"rc={rc}")

# R17: --json sweep over all verbs (errors must still be JSON)
rc, out, _ = run("--json", "snapshot", "adv", "--mode", "ax", "--depth", "4")
snap = jdoc(out) or {}
import re as _re
m = _re.search(r"^\s*- button .*\[ref=(e\d+)\]", snap.get("snapshot", ""), _re.M)
btn = m.group(1) if m else None
img = os.path.join(HOME, "s.png")
pdfp = os.path.join(HOME, "p.pdf")
sweep = [
    ["profiles", "list"], ["status", "adv"], ["attach", "adv"],
    ["navigate", "adv", "--reload"], ["snapshot", "adv"], ["snapshot", "adv", "--mode", "text"],
    ["diff", "adv"], ["read", "adv"], ["read", "adv", "--format", "links"],
    ["grep", "adv", "Hi"], ["screenshot", "adv", "--out", img], ["pdf", "adv", "--out", pdfp],
    ["exec", "adv", "location.href"], ["run", "adv", "return 7*6"],
    ["tabs", "adv", "list"], ["windows", "adv", "list"], ["groups", "adv", "list"],
    ["history", "adv", "--limit", "5"], ["session", "adv", "r17"], ["skill", "list"],
    ["doctor"], ["wait", "adv", "--text", "Hi", "--timeout", "5"],
]
if btn:
    sweep.append(["act", "adv", "click", "--ref", btn])
bad = []
for args in sweep:
    try:
        rc, out, err = run("--json", *args, timeout=60)
    except subprocess.TimeoutExpired:
        bad.append(("TIMEOUT", args))
        continue
    d = jdoc(out)
    if d is None or "Traceback" in err:
        bad.append((rc, args, (out + err)[:150]))
check("R17 json sweep", not bad, f"bad={bad[:4]}")

# R18: re-save preserves runs; R19: failed runs logged; R20: telemetry
run("--json", "skill", "save", "rskill", "--description", "d",
    "--steps", json.dumps([{"cmd": ["exec", "adv", "1+1"]}, {"cmd": ["exec", "adv", "2+2"] }]))
run("--json", "skill", "run", "rskill")
run("--json", "skill", "save", "rskill", "--description", "d2",
    "--steps", json.dumps([{"cmd": ["exec", "adv", "3+3"] }]))
rc, out, _ = run("--json", "skill", "show", "rskill")
d = jdoc(out)
check("R18 resave keeps runs", d and len(d.get("runs", [])) == 1 and len(d.get("steps")) == 1, out[:150])
run("--json", "skill", "save", "rfail", "--description", "d",
    "--steps", json.dumps([{"cmd": ["exec", "adv", "1+1"]}, {"cmd": ["exec", "adv", "throw 1"]}, {"cmd": ["exec", "adv", "9+9"] }]))
rc, out, _ = run("--json", "skill", "run", "rfail")
d = jdoc(out)
check("R19 failed run stops+reports", rc != 0 and d and d.get("failedStep") == 1 and len(d.get("results", [])) == 2, out[:150])
rc, out, _ = run("--json", "skill", "show", "rfail")
d = jdoc(out)
lastrun = (d.get("runs") or [{}])[-1]
check("R19 failed run recorded", lastrun.get("ok") is False and lastrun.get("failedStep") == 1, str(lastrun)[:150])
check("R20 telemetry present", isinstance(lastrun.get("durationMs"), int) and isinstance(d and d.get("runs"), list), str(lastrun)[:150])

# --- teardown ---
run("close", "adv")
time.sleep(1)
check("R21 teardown clean", len(procs(DP)) == 0, f"left={len(procs(DP))}")

print(f"==== {len(PASS)}/{len(PASS) + len(FAIL)} PASS ====")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
