"""cloakctl live matrix8 — remote-endpoint mode (VPS-side CLI, browser elsewhere).

Remote mode is a cloakbrowser (Chromium-family) feature: obscura's CDP is
per-connection isolated, so a remote attach would see an empty session.
The host browser is therefore launched with `--engine cloakbrowser`.

Two-HOME simulation: HOME_A is the browser host (local launch), HOME_B is
the VPS side (only `open --endpoint` / CLOAKCTL_CDP_URL, never a launch).
Covers attach, env fallback, verbs, wf runs, detach semantics, dead-endpoint
errors, browser-side download/upload honesty, doctor rss guard — plus the
obscura honesty contract: an empty --endpoint must never fall through to a
local launch, and obscura's engine string is refused at `attach`.
Exit 0.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"
HOME_A = tempfile.mkdtemp(prefix="cloaktest8-host-")
HOME_B = tempfile.mkdtemp(prefix="cloaktest8-vps-")
ENV_A = dict(os.environ, CLOAKCTL_HOME=HOME_A)
ENV_B = dict(os.environ, CLOAKCTL_HOME=HOME_B)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)


def runA(*args, timeout=120):
    p = subprocess.run([BIN, *args, "--json"], capture_output=True,
                       text=True, timeout=timeout, env=ENV_A)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, {"_raw": (p.stdout + p.stderr)[:200]}


def runB(*args, timeout=120, env=None):
    p = subprocess.run([BIN, *args, "--json"], capture_output=True,
                       text=True, timeout=timeout, env=env or ENV_B)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, {"_raw": (p.stdout + p.stderr)[:200]}


# host side: launch the browser that the "VPS" will drive.
# cloakbrowser is REQUIRED for remote mode (obscura CDP is per-connection).
runA("profiles", "create", "m8")
rc, d = runA("open", "m8", "--engine", "cloakbrowser")
WS = (d.get("wsEndpoint") or "")
check("r0 host browser launched", rc == 0 and WS.startswith("ws://"),
      WS[:40])

# r1: attach-only open from the VPS side
rc, d = runB("open", "m8", "--endpoint", WS)
check("r1 remote attach", rc == 0 and d.get("remote") is True
      and d.get("wsEndpoint") == WS and "browser" in d, f"rc={rc}")

# r2: CLOAKCTL_CDP_URL fallback (no --endpoint flag)
env_url = dict(ENV_B, CLOAKCTL_CDP_URL=WS)
rc, d = runB("open", "m8b", env=env_url)
check("r2 env endpoint fallback", rc == 0 and d.get("remote") is True,
      f"rc={rc}")

# r3: verbs over the remote path
rc, d = runB("exec", "m8", "location.href")
rc2, d2 = runB("tabs", "m8", "new", "--url", "about:blank")
tid = (d2.get("targetId") or "")
rc3, d3 = runB("snapshot", "m8", "--tab", tid[:8])
check("r3 remote exec/tabs/snapshot", rc == 0 and rc2 == 0 and rc3 == 0
      and isinstance(d3.get("snapshot"), str), f"{rc} {rc2} {rc3}")

# r4: wf run --new-tab over remote; owned tab reaped remotely
MOD = ('META = {"version": "1.0.0", "description": "r", "inputs": {}, '
       '"depends_on": []}\n'
       "def run(ctx, inputs):\n"
       "    return {'t': ctx.exec('document.title')}\n")
runB("wf", "save", "m8w", "--code", MOD)
_, before = runB("tabs", "m8", "list")
rc, d = runB("wf", "run", "m8w", "--profile", "m8", "--new-tab")
_, after = runB("tabs", "m8", "list")
check("r4 remote wf run + tab reaped", rc == 0 and d.get("ok") is True
      and len(after.get("tabs", [])) == len(before.get("tabs", [])),
      f"rc={rc}")

# r5: status/doctor tell the truth (no pid, rssMB null not machine-sum)
rc, d = runB("status", "m8")
rc2, d2 = runB("doctor")
lp = (d2.get("memory", {}).get("liveProfiles", []) or [])
me = [e for e in lp if e.get("profile") == "m8"]
check("r5 remote status/doctor honest", rc == 0 and d.get("remote") is True
      and "pid" not in d and "jarFingerprint" in d
      and me and me[0].get("rssMB") is None and me[0].get("remote") is True,
      f"{d.get('remote')} {me}")

# r6: dead endpoint is a JSON error, not a traceback
rc, d = runB("open", "dead", "--endpoint", "ws://127.0.0.1:9/nope")
check("r6 dead endpoint JSON error", rc == 1
      and "unreachable" in (d.get("error", "") + d.get("_raw", "")))

# r6b: an EMPTY endpoint must never fall through to a local launch —
# the VPS side asked for remote mode; silently launching a browser there
# is exactly the bug class remote mode exists to prevent.
rc, d = runB("open", "m8-empty", "--endpoint", "")
rc2, d2 = runB("status", "m8-empty")
check("r6b empty endpoint refuses local launch", rc == 1
      and rc2 == 0 and d2.get("live") is False and d2.get("exists") is False,
      f"rc={rc} live={d2.get('live')}")

# r6c: obscura cannot be remote-attached honestly: attach refuses with a
# pointer to cloakbrowser (the engine string is only reported for obscura
# profiles; a cloakbrowser profile prints the ws endpoint instead).
runA("profiles", "create", "m8ob")
runA("open", "m8ob")
rc, d = runA("attach", "m8ob")
check("r6c attach refuses obscura", rc == 1
      and "per-connection" in (d.get("error", "") + d.get("_raw", "")))
runA("close", "m8ob")

# r7: close detaches; host browser survives; VPS lock released
rc, d = runB("close", "m8")
rc2, d2 = runA("status", "m8")
lock_gone = not os.path.exists(os.path.join(HOME_B, "runtime", "lock.m8.json"))
check("r7 detach keeps host alive", rc == 0 and d.get("detached") is True
      and rc2 == 0 and d2.get("live") is True and lock_gone)

# re-attach for file-semantics probes
runB("open", "m8", "--endpoint", WS)

# r8: remote download resolves via events, lands browser-side
PAGE = ("data:text/html,<a href='data:text/plain,hello-remote' "
        "download='hi.txt' id='dl'>get it</a>")
runB("navigate", "m8", "--url", PAGE)
_, snap = runB("snapshot", "m8")
ref = ""
for line in (snap.get("snapshot", "") or "").splitlines():
    if "[ref=" in line and ("get it" in line or "link" in line):
        ref = line.split("[ref=")[1].split("]")[0]
        break
if ref:
    # verb deadline (15s) MUST sit under the subprocess kill (25s) so a slow
    # download surfaces as the verb's own JSON timeout, never a test kill.
    rc, d = runB("download", "m8", "--ref", ref, "--out-dir", "/tmp",
                 "--timeout", "15", timeout=25)
    check("r8 remote download guid", rc == 0 and d.get("remote") is True
          and bool(d.get("guid")), f"{rc} {d}")
else:
    check("r8 remote download guid", False, "no ref found")

# r9: upload names the browser host in the error
rc, d = runB("upload", "m8", "--selector", "#nope",
             "--file", "/tmp/does-not-exist-xyz")
check("r9 upload error names browser host", rc != 0
      and "BROWSER host" in (d.get("error", "") + d.get("_raw", "")))

# r10: host death reads as not-live JSON on the VPS side, no traceback
runA("close", "m8")
rc, d = runB("status", "m8")
rc2, d2 = runB("exec", "m8", "1+1")
check("r10 host death is clean JSON", rc == 0 and d.get("live") is False
      and rc2 == 1 and isinstance(d2.get("error", d2.get("_raw")), str))

# teardown: zero strays belonging to THIS run's state dirs on either side
# (other cloaktest8 matches may predate this run; only ours can be ours)
runB("close", "m8b")
runB("close", "m8")
time.sleep(1.5)
left = subprocess.run(["pgrep", "-af", HOME_A], capture_output=True,
                      text=True).stdout.strip()
left += subprocess.run(["pgrep", "-af", HOME_B], capture_output=True,
                       text=True).stdout.strip()
check("r11 teardown clean", left == "", left[:200])

print(f"==== {len(PASS)}/{len(PASS) + len(FAIL)} PASS ====  (A={HOME_A} B={HOME_B})")
runA("close", "m8")
runA("close", "m8ob")
sys.exit(0 if not FAIL else 1)
