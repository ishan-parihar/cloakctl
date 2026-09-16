"""cloakctl live matrix 4 — post-contrast ports (act kinds, selector upload,
active-tab alias, audit trail, session category, JSON errors). data: URLs only.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HOME = tempfile.mkdtemp(prefix="cloaktest4-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
B = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def run(*args, timeout=60):
    p = subprocess.run([B, *args], capture_output=True, text=True, timeout=timeout, env=ENV)
    return p.returncode, p.stdout, p.stderr


def jdoc(s):
    try:
        return json.loads(s)
    except Exception:
        return None


run("profiles", "create", "m4")
run("--json", "open", "m4")
PAGE = "data:text/html,<html><body><form><input id=q type=text><input id=c type=checkbox><button id=go>Go</button></form><input id=f type=file></body></html>"
run("navigate", "m4", "--url", PAGE)
rc, out, _ = run("--json", "snapshot", "m4", "--depth", "5")
refs = re.findall(r"\[ref=(e\d+)\]", (jdoc(out) or {}).get("snapshot", ""))
check("F0 refs", len(refs) >= 4, str(refs))
q, c = refs[0], refs[1]

# F1/F2: fill batch ground truth
rc, out, _ = run("--json", "act", "m4", "fill", "--field", f"{q}=alpha", "--field", f"{q}=beta")
d = jdoc(out)
rc2, out2, _ = run("--json", "exec", "m4", "document.getElementById('q').value")
check("F1 fill batch", rc == 0 and d.get("failed") == [] and (jdoc(out2) or {}).get("value") == "beta", out[:120])
# F2/F3: check / uncheck ground truth
run("--json", "act", "m4", "check", "--ref", c)
_, out2, _ = run("--json", "exec", "m4", "document.getElementById('c').checked")
run("--json", "act", "m4", "uncheck", "--ref", c)
_, out3, _ = run("--json", "exec", "m4", "document.getElementById('c').checked")
check("F2 check/uncheck", (jdoc(out2) or {}).get("value") is True and (jdoc(out3) or {}).get("value") is False)
# F4: drag coordinates
rc, out, _ = run("--json", "act", "m4", "drag", "--x", "50", "--y", "60", "--dx", "30", "--dy", "40")
check("F4 drag", rc == 0 and (jdoc(out) or {}).get("drag") == [50, 60, 80, 100], out[:100])
# F5: upload --selector, no snapshot needed
upf = os.path.join(HOME, "u.bin")
open(upf, "wb").write(b"z" * 32)
rc, out, _ = run("--json", "upload", "m4", "--selector", "#f", "--file", upf)
check("F5 upload selector", rc == 0 and (jdoc(out) or {}).get("uploaded") == "#f", out[:120])
# F6: tabs new foreground becomes default + close active
rc, out, _ = run("--json", "tabs", "m4", "new", "--url", "data:text/html,NEW-9")
nid = (jdoc(out) or {}).get("id")
run("--json", "wait", "m4", "--text", "NEW-9", "--timeout", "10")
rc, out, _ = run("--json", "exec", "m4", "document.body.innerText")
check("F6 new tab is default", rc == 0 and "NEW-9" in (jdoc(out) or {}).get("value", ""), out[:100])
rc, out, _ = run("--json", "tabs", "m4", "close", "active")
check("F6 close active", rc == 0, out[:100])
# F7: skill run rc + audit trail + session category
run("--json", "skill", "save", "m4s", "--description", "d",
    "--steps", json.dumps([{"cmd": ["exec", "m4", "1+1"]}, {"cmd": ["exec", "m4", "throw 1"]}]))
rc, out, _ = run("--json", "skill", "run", "m4s")
check("F7 skill fail rc=1", rc == 1 and (jdoc(out) or {}).get("failedStep") == 1, f"rc={rc}")
rc, out, _ = run("--json", "session", "m4", "probe-task", "--summary", "s", "--category", "testing")
check("F7 session category", rc == 0 and (jdoc(out) or {}).get("category") == "testing", out[:100])
rc, out, _ = run("--json", "audit", "m4", "--limit", "50")
d = jdoc(out)
cmds = [c["cmd"] for c in (d or {}).get("commands", [])]
check("F7 audit trail", rc == 0 and "snapshot" in cmds and "upload" in cmds and all("ms" in c for c in d["commands"]), str(cmds[-6:]))
# F8: JSON errors, no tracebacks, no argv secrets in audit
rc, out, err = run("--json", "exec", "dead-prof", "1")
check("F8 json error shape", rc == 1 and (jdoc(out) or {}).get("error") and "Traceback" not in err, f"rc={rc} {out[:80]}")
audit_raw = open(os.path.join(HOME, "runtime", "audit.m4.jsonl")).read()
check("F8 audit secret-safe", "--field" not in audit_raw and "beta" not in audit_raw, f"{len(audit_raw)} bytes")

run("close", "m4")
time.sleep(1)
left = subprocess.run(["pgrep", "-f", "user-data-dir=" + HOME], capture_output=True, text=True).stdout.strip()
check("F9 teardown", not left, left[:80])

print(f"==== {len(PASS)}/{len(PASS) + len(FAIL)} PASS ====")
sys.exit(1 if FAIL else 0)
