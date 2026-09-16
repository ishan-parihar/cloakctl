"""cloakctl load/resource characterization (measurement, not PASS/FAIL).

Measures on THIS host so operators can size deployments (including VPS):
  L1  per-profile browser RSS: idle, 1 tab, 5 tabs (data: URLs, no network)
  L2  multi-profile scale: 1/2/4 live profiles, total RSS + launch time
  L3  verb latency: snapshot / exec / act round-trips (local CDP)
  L4  parallel wf runs: 4x --new-tab on one profile, wall time + peak RSS
  L5  teardown: zero strays

Prints a markdown table. Exit 0 unless strays remain.
Usage: python tests/live_load.py  (needs a Chromium; ~2GB free for L2)
"""
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"
HOME = tempfile.mkdtemp(prefix="cloakload-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)

from cloakctl import browser as _b  # noqa: E402


def run(*args, timeout=120):
    if "--json" not in args:
        args = (*args, "--json")  # measurement parses stdout as JSON
    p = subprocess.run([BIN, *args], capture_output=True, text=True,
                       timeout=timeout, env=ENV)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, {"_raw": (p.stdout + p.stderr)[:200]}


def rss(pid):
    try:
        return round(_b._rss_tree_mb(pid), 1)
    except Exception:
        return 0.0


def live_pid(profile):
    d = run("status", profile)[1]
    return d.get("pid", 0)


print(f"# cloakctl load — {time.strftime('%Y-%m-%d')} "
      f"(host avail {(_b._mem_available_mb() or 0):.0f}MB)")
rows = []

# L1: one profile, tab scaling (data: URLs — no network in the measurement)
run("profiles", "create", "load1")
t0 = time.monotonic()
run("open", "load1")
launch_s = round(time.monotonic() - t0, 1)
pid = live_pid("load1")
time.sleep(2)
rows.append(("L1 browser launch (cold)", f"{launch_s}s", f"{rss(pid)} MB RSS"))
rows.append(("L1 idle, 1 blank tab", "—", f"{rss(pid)} MB RSS"))
PAGE = ("data:text/html,<h1>T</h1><ul>" + "<li><a href='#'>item</a></li>" * 200
        + "</ul>")
for i in range(4):
    run("tabs", "load1", "new", "--url", PAGE, "--background")
time.sleep(2)
rows.append(("L1 same browser, 5 heavy tabs", "—", f"{rss(pid)} MB RSS"))

# L3: verb latency on the loaded tab (median of 5)
def med(fn, n=5):
    ts = []
    for _ in range(n):
        t = time.monotonic()
        fn()
        ts.append((time.monotonic() - t) * 1000)
    return round(sorted(ts)[n // 2])

snap_ms = med(lambda: run("snapshot", "load1"))
exec_ms = med(lambda: run("exec", "load1", "document.title"))
snap_n = run("snapshot", "load1")[1].get("refs", 0)
rows.append((f"L3 snapshot ({snap_n} refs, 5-tab browser)", f"{snap_ms} ms", "—"))
rows.append(("L3 exec round-trip", f"{exec_ms} ms", "—"))

# L2: multi-profile scale
pids = [pid]
for i in range(2, 5):
    name = f"load{i}"
    run("profiles", "create", name)
    t = time.monotonic()
    run("open", name)
    pids.append(live_pid(name))
    rows.append((f"L2 profile {i} launch", f"{round(time.monotonic() - t, 1)}s",
                 f"{rss(pids[-1])} MB RSS"))
time.sleep(2)
total = round(sum(rss(p) for p in pids), 1)
rows.append(("L2 4 live profiles total", "—", f"{total} MB RSS"))

# L4: 4 parallel wf runs, one profile, owned tabs
MOD = ('META = {"version": "1.0.0", "description": "load probe", '
       '"inputs": {"tag": {"type": "str"}}, "depends_on": []}\n'
       "def run(ctx, inputs):\n"
       "    s = ctx.snapshot()\n"
       "    return {'tag': inputs['tag'], 'refs': s.get('refs', 0)}\n")
run("wf", "save", "load_probe", "--code", MOD)
t = time.monotonic()
procs = [subprocess.Popen(
    [BIN, "wf", "run", "load_probe", "--profile", "load1", "--new-tab",
     "--set", f"tag=P{i}", "--json"], stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, env=ENV) for i in range(4)]
peak_now = sum(rss(p) for p in pids)
while True:  # main-thread polling: no sampler thread to starve
    if all(p.poll() is not None for p in procs):
        break
    peak_now = max(peak_now, sum(rss(p) for p in pids))
    time.sleep(0.05)
outs = [p.communicate(timeout=180) for p in procs]
wall = round(time.monotonic() - t, 1)
oks = sum(1 for o, _ in outs if '"ok": true' in o)
rows.append((f"L4 4 parallel wf runs ({oks}/4 ok)", f"{wall}s wall",
             f"{round(peak_now, 1)} MB peak RSS"))

# L6: disk per profile (chrome user-data-dir)
import pathlib as _pl
sizes = []
for i in range(1, 5):
    d = _pl.Path(HOME) / "profiles" / f"load{i}" / "chrome"
    sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e6
    sizes.append(round(sz, 1))
rows.append(("L6 disk per profile (fresh)", "—",
             f"{min(sizes):.0f}-{max(sizes):.0f} MB"))

# L5: teardown
for i in range(1, 5):
    run("close", f"load{i}")
left = subprocess.run(["pgrep", "-af", "cloakload"], capture_output=True,
                      text=True).stdout.strip()

print("\n| probe | latency | memory |")
print("|---|---|---|")
for a, b, c in rows:
    print(f"| {a} | {b} | {c} |")
print(f"\nteardown strays: {'NONE' if not left else left[:200]}")
sys.exit(0 if not left else 1)
