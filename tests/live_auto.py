"""cloakctl live automation stress — save / re-run / chain / join macros.

Spins a local fixture server, saves realistic multi-step skills, replays them
on fresh profiles, chains (nested skill run), and joins (parallel profiles).
Exit 0 + 'N/N PASS' on success.
"""
import functools
import http.server
import json
import os
import re
import time
import subprocess
import sys
import tempfile
import threading

HOME = tempfile.mkdtemp(prefix="cloakauto-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
B = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def run(*args, timeout=90):
    p = subprocess.run([B, *args], capture_output=True, text=True, timeout=timeout, env=ENV)
    return p.returncode, p.stdout, p.stderr


def jdoc(stdout):
    try:
        return json.loads(stdout)
    except Exception:
        return None


# ---------- fixture server ----------
FORM = """<html><body><h1>Order</h1>
<form action="/done.html" method="get">
<input id="q" name="q" type="text"><input id="c" type="checkbox" name="c">
<select id="s" name="s"><option value="a">A</option><option value="b">B</option></select>
<button id="go" type="submit">Go</button></form></body></html>"""
SLOW = """<html><body><div id="early">start</div><script>
setTimeout(()=>{const d=document.createElement('div');d.id='late';d.textContent='ARRIVED-42';document.body.appendChild(d);},2500);</script></body></html>"""
LINKS = "<html><body>" + "".join(f"<a href='/p{i}.html'>link-{i}</a>" for i in range(30)) + "</body></html>"
UP = """<html><body><input id="f" type="file"><div id="nm"></div><script>
document.getElementById('f').addEventListener('change',e=>{document.getElementById('nm').textContent='GOT:'+e.target.files[0].name;});</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = FORM if self.path.startswith("/form") else SLOW if self.path.startswith("/slow") \
            else LINKS if self.path.startswith("/links") else UP if self.path.startswith("/upload") \
            else "<html><body><h1>Done marker ORDER-OK</h1></body></html>" if self.path.startswith("/done") \
            else ("<html><body>file-bytes</body></html>" if self.path.startswith("/file.bin") else "<html><body>page</body></html>")
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
U = lambda p: f"http://127.0.0.1:{PORT}{p}"  # noqa: E731

UPFILE = os.path.join(HOME, "up.bin")
open(UPFILE, "wb").write(b"x" * 64)

# ---------- A0: profile + discover live refs for the form ----------
run("profiles", "create", "auto")
run("--json", "open", "auto")
run("navigate", "auto", "--url", U("/form.html"))
rc, out, _ = run("--json", "snapshot", "auto", "--depth", "6")
snaptext = (jdoc(out) or {}).get("snapshot", "")
m_q = re.search(r"^\s*- textbox .*\[ref=(e\d+)\]", snaptext, re.M)
m_go = re.search(r"^\s*- button .*\[ref=(e\d+)\]", snaptext, re.M)
qref, goref = (m_q.group(1) if m_q else None), (m_go.group(1) if m_go else None)
check("A0 refs discovered", qref and goref, f"q={qref} go={goref}")

# ---------- A1: save order-form macro, replay on FRESH profile ----------
steps = [
    {"cmd": ["navigate", "auto2", "--url", U("/form.html")]},
    {"cmd": ["snapshot", "auto2", "--depth", "6"]},
    {"cmd": ["act", "auto2", "type", "--ref", qref, "--text", "widget"]},
    {"cmd": ["act", "auto2", "click", "--ref", goref]},
    {"cmd": ["wait", "auto2", "--text", "ORDER-OK", "--timeout", "15"]},
    {"cmd": ["read", "auto2", "--format", "text"]},
]
run("profiles", "create", "auto2")
rc, out, _ = run("--json", "skill", "save", "order-form", "--description", "fill order form", "--site", "fixture",
                 "--steps", json.dumps(steps), "--notes", "refs stable per page")
check("A1 save macro", rc == 0, out[:100])
rc, out, _ = run("--json", "open", "auto2")
rc, out, _ = run("--json", "skill", "run", "order-form")
d = jdoc(out)
last = (d.get("results") or [{}])[-1].get("output", "") if d else ""
check("A1 replay reaches done", rc == 0 and d and d.get("ok") and "ORDER-OK" in last, f"rc={rc} steps={d and d.get('steps')}")

# ---------- A2: nested skill run (chain) ----------
run("--json", "skill", "save", "auto-inner", "--description", "inner probe",
    "--steps", json.dumps([{"cmd": ["exec", "auto2", "document.title"]}, {"cmd": ["tabs", "auto2", "list"] }]))
run("--json", "skill", "save", "auto-outer", "--description", "outer chain",
    "--steps", json.dumps([{"cmd": ["skill", "run", "auto-inner"]}, {"cmd": ["exec", "auto2", "1+1"] }]))
rc, out, _ = run("--json", "skill", "run", "auto-outer")
d = jdoc(out)
check("A2 nested skill run", rc == 0 and d and d.get("ok") and d.get("steps") == 2, out[:160])

# ---------- A3: multi-tab workflow macro (active-tab flow, no ids needed) ----------
steps3 = [
    {"cmd": ["tabs", "auto2", "new", "--url", U("/links.html")]},
    {"cmd": ["wait", "auto2", "--text", "link-29", "--timeout", "10"]},
    {"cmd": ["snapshot", "auto2", "--tab", "active", "--depth", "3"]},
    {"cmd": ["read", "auto2", "--tab", "active", "--format", "links"]},
    {"cmd": ["tabs", "auto2", "close", "active"]},
]
run("--json", "skill", "save", "tab-sweep", "--description", "open read close",
    "--steps", json.dumps(steps3))
rc, out, _ = run("--json", "skill", "run", "tab-sweep")
d = jdoc(out)
res = (d or {}).get("results", [])
links_out = res[3].get("output", "") if len(res) > 3 else ""
dbg = [(r.get("cmd", ["?"])[1], r.get("rc"), len(r.get("output", ""))) for r in res]
check("A3 tab macro extracts+closes", rc == 0 and d and d.get("ok") and "link-2" in links_out, f"rc={rc} steps={dbg}")
# A3b: 10KB step output must spill to disk with truncated inline doc
run("--json", "skill", "save", "big-out", "--description", "spill",
    "--steps", json.dumps([{"cmd": ["exec", "auto2", "'z'.repeat(10000)"] }]))
rc, out, _ = run("--json", "skill", "run", "big-out")
d = jdoc(out)
r0 = (d.get("results") or [{}])[0] if d else {}
import os as _os
spill = r0.get("outputPath")
spill_ok = (rc == 0 and r0.get("truncated") is True and len(r0.get("output", "")) == 4000
            and bool(spill and _os.path.exists(spill)) and len(open(spill).read()) > 10000)
check("A3b long output spilled to file", spill_ok, f"spill={spill}")

# ---------- A5: wait-in-macro on slow page ----------
run("--json", "skill", "save", "slow-catch", "--description", "catch late div",
    "--steps", json.dumps([
        {"cmd": ["navigate", "auto2", "--url", U("/slow.html")]},
        {"cmd": ["wait", "auto2", "--text", "ARRIVED-42", "--timeout", "15"]},
        {"cmd": ["grep", "auto2", "ARRIVED-42"]}]))
rc, out, _ = run("--json", "skill", "run", "slow-catch")
d = jdoc(out)
check("A5 slow macro", rc == 0 and d and d.get("ok"), out[:120])

# ---------- A6: download+upload roundtrip macro ----------
run("navigate", "auto2", "--url", U("/upload.html"))
rc, out, _ = run("--json", "snapshot", "auto2", "--depth", "5")
snaptext = (jdoc(out) or {}).get("snapshot", "")
allrefs = re.findall(r"\[ref=(e\d+)\]", snaptext)
fref = allrefs[0] if allrefs else None
run("--json", "skill", "save", "up-round", "--description", "upload file",
    "--steps", json.dumps([
        {"cmd": ["navigate", "auto2", "--url", U("/upload.html")]},
        {"cmd": ["upload", "auto2", "--selector", "#f", "--file", UPFILE]},
        {"cmd": ["wait", "auto2", "--text", "GOT:up.bin", "--timeout", "10"]}]))
rc, out, _ = run("--json", "skill", "run", "up-round")
d = jdoc(out)
check("A6 upload macro", rc == 0 and d and d.get("ok"), f"fref={fref} {out[:140]}")

# ---------- A7/A8: log-run + determinism ----------
rc, out, _ = run("--json", "skill", "log-run", "order-form", "--note", "manual check")
rc, out, _ = run("--json", "skill", "show", "order-form")
d = jdoc(out)
check("A7 log-run recorded", d and len(d.get("runs", [])) >= 2, f"runs={d and len(d.get('runs'))}")
oks = 0
for _ in range(3):
    rc, _, _ = run("--json", "skill", "run", "order-form", timeout=120)
    oks += (rc == 0)
check("A8 3x deterministic", oks == 3, f"{oks}/3")

# ---------- A9: failing macro stops at right step ----------
run("--json", "skill", "save", "bad-macro", "--description", "fails",
    "--steps", json.dumps([
        {"cmd": ["exec", "auto2", "1+1"]},
        {"cmd": ["exec", "auto2", "throw new Error('nope')"]},
        {"cmd": ["exec", "auto2", "9+9"]}]))
rc, out, _ = run("--json", "skill", "run", "bad-macro")
d = jdoc(out)
check("A9 fail stops, no step3", rc != 0 and d and d.get("failedStep") == 1 and len(d.get("results", [])) == 2, out[:140])

# ---------- A4: literal-step variant (documents no-templating limit) ----------
steps_v = [dict(s, cmd=[a.replace("widget", "gadget") if isinstance(a, str) else a for a in s["cmd"]]) for s in steps]
run("--json", "skill", "save", "order-form-gadget", "--description", "variant",
    "--steps", json.dumps(steps_v))
rc, out, _ = run("--json", "skill", "run", "order-form-gadget", timeout=120)
check("A4 variant replays", rc == 0 and (jdoc(out) or {}).get("ok"), f"rc={rc}")

# ---------- A11: resave preserves history, updates steps ----------
rc, out, _ = run("--json", "skill", "show", "order-form")
before = len((jdoc(out) or {}).get("runs", []))
run("--json", "skill", "save", "order-form", "--description", "v2",
    "--steps", json.dumps([{"cmd": ["exec", "auto2", "7*7"] }]))
rc, out, _ = run("--json", "skill", "show", "order-form")
d = jdoc(out)
check("A11 resave keeps runs+updates", d and len(d.get("runs", [])) == before and len(d.get("steps")) == 1, f"runs={d and len(d.get('runs'))}")

# ---------- A10: join — 5 parallel profiles, macros concurrently ----------
for i in range(5):
    run("profiles", "create", f"join{i}")
    run("--json", "open", f"join{i}")
    run("--json", "skill", "save", f"join-macro-{i}", "--description", "parallel probe",
        "--steps", json.dumps([
            {"cmd": ["navigate", f"join{i}", "--url", U("/slow.html")]},
            {"cmd": ["wait", f"join{i}", "--text", "ARRIVED-42", "--timeout", "15"]},
            {"cmd": ["exec", f"join{i}", f"'MARK-{i}-'+document.getElementById('late').textContent"]}]))
kids = [subprocess.Popen([B, "--json", "skill", "run", f"join-macro-{i}"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV) for i in range(5)]
outs = [k.communicate(timeout=180) for k in kids]
ok_all, no_xtalk = True, True
for i, (o, e) in enumerate(outs):
    d = jdoc(o)
    last = (d.get("results") or [{}])[-1].get("output", "") if d else ""
    if not (d and d.get("ok") and f"MARK-{i}-ARRIVED-42" in last):
        ok_all = False
    others = [f"MARK-{k}-" for k in range(5) if k != i]
    if any(m in last for m in others):
        no_xtalk = False
check("A10 parallel join 5/5", ok_all, f"{sum(1 for o,e in outs if (jdoc(o) or {}).get('ok'))}/5")
check("A10 no crosstalk", no_xtalk)
for i in range(5):
    run("close", f"join{i}")

# ---------- teardown ----------
run("close", "auto")
run("close", "auto2")
time.sleep(1)
left = subprocess.run(["pgrep", "-f", "user-data-dir=" + HOME], capture_output=True, text=True).stdout.strip()
check("A12 teardown clean", not left, left[:100])

print(f"==== {len(PASS)}/{len(PASS) + len(FAIL)} PASS ====")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
