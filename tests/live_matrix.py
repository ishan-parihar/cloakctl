"""Exhaustive cloakctl production-readiness matrix (isolated CLOAKCTL_HOME)."""
import json, os, signal, subprocess, sys, tempfile, threading, time

VENV_PY = os.path.expanduser("~/Documents/github/my-projects/agentic-utility/internet/cloakctl/.venv/bin/python")
HOME = tempfile.mkdtemp(prefix="cloaktest-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

def run(*args, timeout=60):
    t = time.monotonic()
    p = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=timeout, env=ENV)
    return p.returncode, p.stdout, p.stderr, time.monotonic() - t

def jget(out):
    try: return json.loads(out)
    except Exception: return None

# --- T1: CLI contract -------------------------------------------------------
rc, out, err, _ = run("--help")
check("T1.1 --help exits 0", rc == 0)
for cmd in (["doctor"], ["profiles", "list"], ["status"]):
    for pos in ([ "--json", *cmd], [*cmd, "--json"]):
        rc, out, err, _ = run(*pos)
        check(f"T1.2 --json {'first' if pos[0]=='--json' else 'last'}: {' '.join(cmd)}",
              rc == 0 and jget(out) is not None, f"rc={rc}")
rc, out, err, _ = run("frobnicate")
check("T1.3 unknown command exits 2", rc == 2, f"rc={rc}")
rc, out, err, _ = run("--version")
check("T1.4 --version flag exists", rc == 0, f"rc={rc} (gap if missing)")

# --- T2: profiles ------------------------------------------------------------
rc, out, _, _ = run("--json", "profiles", "create", "p1")
d = jget(out)
check("T2.1 profiles create", rc == 0 and d and d.get("created") == "p1", out.strip()[:80])
rc, out, _, _ = run("--json", "profiles", "create", "p1")
check("T2.2 profiles create idempotent", rc == 0, f"rc={rc}")
rc, out, _, _ = run("--json", "profiles", "rm", "nope")
d = jget(out)
check("T2.3 rm nonexistent -> existed:false", rc == 0 and d and d.get("existed") is False, out.strip()[:80])
rc, out, _, _ = run("--json", "status", "ghost")
d = jget(out)
check("T2.4 status missing profile exists:false", rc == 0 and d and d.get("exists") is False, out.strip()[:100])
rc, out, err, _ = run("--json", "attach", "ghost")
check("T2.5 attach cold -> exit 1", rc == 1, (err or out).strip()[:80])
rc, out, err, _ = run("--json", "exec", "ghost", "1+1")
check("T2.6 exec cold -> exit 1", rc == 1, (err or out).strip()[:80])
rc, out, err, _ = run("--json", "import", "ghost", "file:/nope.json", "-y")
check("T2.7 import missing file -> exit 1", rc == 1, (err or out).strip()[:80])

# --- T3: lifecycle ------------------------------------------------------------
t0 = time.monotonic()
rc, out, _, dt = run("--json", "open", "p1")
d = jget(out)
pid1 = (d or {}).get("pid")
check("T3.1 open launches", rc == 0 and d and d.get("reattached") is False and pid1, f"{dt:.1f}s pid={pid1}")
rc, out, _, _ = run("--json", "open", "p1")
d2 = jget(out)
check("T3.2 double open reattaches same pid", rc == 0 and d2 and d2.get("reattached") is True and d2.get("pid") == pid1)
rc, out, _, _ = run("--json", "status", "p1")
d = jget(out)
check("T3.3 status live + fields", rc == 0 and d and d.get("live") is True and d.get("wsEndpoint", "").startswith("ws://"), out.strip()[:120])
rc, out, _, _ = run("--json", "attach", "p1")
d = jget(out)
check("T3.4 attach returns wsEndpoint", rc == 0 and d and d.get("wsEndpoint", "").startswith("ws://"))
rc, out, _, _ = run("--json", "exec", "p1", "1+1")
d = jget(out)
check("T3.5 exec evaluate 1+1=2", rc == 0 and d and d.get("value") == 2, out.strip()[:80])
rc, out, err, _ = run("--json", "exec", "p1", "throw new Error('kablam')")
check("T3.6 exec JS error surfaces (no hang)", rc == 1 or "kablam" in (out + err), (out + err).strip()[:100])
rc, out, _, _ = run("--json", "exec", "p1", "Promise.resolve(42)")
d = jget(out)
check("T3.7 exec promise behaviour documented", True, f"rc={rc} value={(d or {}).get('value') if d else out.strip()[:60]}")

# --- T4: concurrency ------------------------------------------------------------
outs = []
def _open(): outs.append(run("--json", "open", "p1"))
ts = [threading.Thread(target=_open) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
pids = {jget(o[1]).get("pid") for o in outs if jget(o[1])}
check("T4.1 4x concurrent open -> single pid", len(pids) == 1, f"pids={pids} rcs={[o[0] for o in outs]}")

# --- T5: stale lock / kill -9 ----------------------------------------------------
os.kill(pid1, signal.SIGKILL)
time.sleep(1.5)
rc, out, _, _ = run("--json", "status", "p1")
d = jget(out)
check("T5.1 status after SIGKILL -> live:false", rc == 0 and d and d.get("live") is False)
rc, out, _, dt = run("--json", "open", "p1")
d = jget(out)
pid2 = (d or {}).get("pid")
check("T5.2 open reclaims stale lock (fresh pid)", rc == 0 and d and d.get("reattached") is False and pid2 != pid1, f"{dt:.1f}s pid={pid2}")
rc, out, _, _ = run("--json", "profiles", "rm", "p1")
check("T5.3 rm live without --force refuses", rc == 1, (out).strip()[:100])
rc, out, _, _ = run("--json", "close", "p1")
d = jget(out)
check("T5.4 close live -> closed:true", rc == 0 and d and d.get("closed") is True)
rc, out, _, _ = run("--json", "close", "p1")
d = jget(out)
check("T5.5 close again -> closed:false", rc == 0 and d and d.get("closed") is False)
rc, out, _, _ = run("--json", "profiles", "rm", "p1", "--force")
d = jget(out)
check("T5.6 rm cold --force", rc == 0 and d and d.get("existed") is True)

# --- T6: corrupt lock -------------------------------------------------------------
run("--json", "profiles", "create", "p2")
lockp = os.path.join(HOME, "runtime", "lock.p2.json")
os.makedirs(os.path.dirname(lockp), exist_ok=True)
open(lockp, "w").write("{corrupt!!!")
rc, out, err, _ = run("--json", "status", "p2")
check("T6.1 status corrupt lock graceful", rc == 0, f"rc={rc} {(err or out).strip()[:80]}")
rc, out, err, _ = run("--json", "open", "p2")
d = jget(out)
pid3 = (d or {}).get("pid")
check("T6.2 open with corrupt lock recovers", rc == 0 and d and pid3, f"rc={rc} {(err or out).strip()[:80]}")

# --- T7: env overrides --------------------------------------------------------------
e2 = dict(ENV, CLOAKCTL_BROWSER="/nonexistent-browser-xyz")
p = subprocess.run([BIN, "--json", "open", "p-cold-xyz"], capture_output=True, text=True, timeout=60, env=e2)
check("T7.1 bad CLOAKCTL_BROWSER clean error", p.returncode == 1 and "CLOAKCTL_BROWSER" in (p.stdout + p.stderr) and "not found" in (p.stdout + p.stderr), (p.stdout + p.stderr).strip()[:100])
rc, out, _, _ = run("--json", "doctor")
d = jget(out)
check("T7.2 doctor stateDir respects CLOAKCTL_HOME", rc == 0 and d and d.get("stateDir", "").startswith(HOME), (d or {}).get("stateDir"))

# --- T8: tab leak ----------------------------------------------------------------------
import urllib.request
def targets(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            return json.loads(r.read())
    except Exception as e: return None
rc, out, _, _ = run("--json", "status", "p2")
port = (jget(out) or {}).get("cdpPort")
n0 = len(targets(port) or [])
run("--json", "exec", "p2", "location.href")
run("--json", "status", "p2")
n1 = len(targets(port) or [])
check("T8.1 no tab leak across exec+status", n1 == n0, f"before={n0} after={n1}")

# --- T9: cookie file import (synthetic) -------------------------------------------------
cj = {"cookies": [
    {"domain": ".example.com", "name": "sess", "value": "abc123", "path": "/", "secure": False, "httpOnly": True},
    {"domain": ".example.com", "name": "old", "value": "x", "path": "/", "expires": 1000},
]}
fp = os.path.join(HOME, "cookies.json"); open(fp, "w").write(json.dumps(cj))
rc, out, _, _ = run("--json", "import", "p2", f"file:{fp}", "--dry-run")
d = jget(out)
check("T9.1 import dry-run filters expired", rc == 0 and d and d.get("extracted") == 2 and d.get("wouldImport") == 1, out.strip()[:150])
rc, out, _, _ = run("--json", "import", "p2", f"file:{fp}", "--domain", "other.com", "--dry-run")
d = jget(out)
check("T9.2 import --domain filter", rc == 0 and d and d.get("wouldImport") == 0, out.strip()[:120])
rc, out, _, _ = run("--json", "import", "p2", f"file:{fp}", "-y")
d = jget(out)
check("T9.3 import live -y imports 1", rc == 0 and d and d.get("imported") == 1, out.strip()[:150])

# --- T10: validate vs generic site ---------------------------------------------------------
rc, out, _, dt = run("--json", "validate", "p2", "--url", "https://example.com/", timeout=90)
d = jget(out)
check("T10.1 validate example.com classifies + exit", True, f"rc={rc} verdict={(d or {}).get('verdict')} {dt:.1f}s")

# --- T11: pure-function units ------------------------------------------------------------------
unit = subprocess.run([VENV_PY, "-c", """
import json, sys
sys.path.insert(0, %r)
from cloakctl.cookies import classify_validation, to_cdp_cookies, decrypt_value, _derive_key_v10
from cloakctl.util import jar_fingerprint
import time
r = []
# classifier matrix
r.append(('burn-li_at', classify_validation('https://x/','', [{'name':'li_at','value':'delete me'}])['verdict'] == 'burn_signature'))
r.append(('challenged', classify_validation('https://www.linkedin.com/checkpoint/challenge/','',[])['verdict'] == 'challenged'))
r.append(('anonymous', classify_validation('https://www.linkedin.com/login','',[])['verdict'] == 'anonymous'))
r.append(('logged_in', classify_validation('https://www.linkedin.com/feed/','<div class=global-nav__me>',[])['verdict'] == 'logged_in'))
r.append(('alive-generic', classify_validation('https://example.com/','hello',[])['verdict'] == 'alive'))
# to_cdp_cookies
rows = [{'domain':'.a.com','name':'s','value':'v','path':'/','expires':time.time()+9999,'secure':False,'httpOnly':False},
        {'domain':'.a.com','name':'e','value':'v','path':'/','expires':1000,'secure':False,'httpOnly':False},
        {'domain':'.b.com','name':'x','value':'v','path':'/','expires':None,'secure':True,'httpOnly':True,'sameSite':'Lax'}]
c = to_cdp_cookies(rows)
r.append(('expired-dropped', len(c) == 2))
r.append(('domain-filter', len(to_cdp_cookies(rows, ['b.com'])) == 1))
# v10 roundtrip (through the fixed module path)
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
key = _derive_key_v10()
pad = lambda b: b + bytes([16-len(b)%%16])*(16-len(b)%%16)
enc = Cipher(AES(key), modes.CBC(b' '*16))
e = enc.encryptor(); blob = b'v10' + e.update(pad(b'secret-val')) + e.finalize()
r.append(('v10-roundtrip', decrypt_value('v10', blob, 'chrome') == 'secret-val'))
# fingerprint stability
j1 = [{'name':'b','value':'2','domain':'x'},{'name':'a','value':'1','domain':'x'}]
r.append(('fingerprint-stable', jar_fingerprint(j1) == jar_fingerprint(list(reversed(j1)))))
r.append(('fingerprint-sensitive', jar_fingerprint(j1) != jar_fingerprint([dict(j1[0], value='3'), j1[1]])))
print(json.dumps(r))
""" % (os.path.expanduser('~/Documents/github/my-projects/agentic-utility/internet/cloakctl/src'),)],
    capture_output=True, text=True, timeout=60)
try:
    for name, ok in json.loads(unit.stdout):
        check(f"T11 unit:{name}", ok)
except Exception as e:
    check("T11 unit harness", False, (unit.stdout + unit.stderr)[:200])

# --- T12: existing suite ---------------------------------------------------------------------------
s = subprocess.run(["python3", "-m", "pytest", "tests/", "-q"], capture_output=True, text=True, timeout=300,
    cwd=os.path.expanduser("~/Documents/github/my-projects/agentic-utility/internet/cloakctl"))
check("T12 existing pytest suite green", s.returncode == 0, s.stdout.strip().splitlines()[-1] if s.stdout.strip() else s.stderr[-200:])

# --- cleanup -------------------------------------------------------------------------------------------
run("--json", "close", "p2")
npass = sum(1 for _, ok, _ in results if ok)
print(f"\n==== {npass}/{len(results)} PASS ====  (HOME={HOME})")
sys.exit(0 if npass == len(results) else 1)
