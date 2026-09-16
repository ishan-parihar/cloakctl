"""cloakctl live matrix5 — workflow-module infrastructure (S1-S4+S6).

Covers the WORKFLOWS-PLAN acceptance set: registry CRUD, input validation,
static cycle refusal, dynamic fuses (ancestor re-entry, depth, step budget),
3-deep composition with call trees, the seed arsenal on fixtures, skill
promote, search/export, doctor evolution hints. Exit 0 + 'N/N PASS'.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

HOME = tempfile.mkdtemp(prefix="cloakwf-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
B = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)


def run(*args, timeout=120):
    p = subprocess.run([B, *args], capture_output=True, text=True, timeout=timeout, env=ENV)
    return p.returncode, p.stdout, p.stderr


def jdoc(stdout):
    try:
        return json.loads(stdout)
    except Exception:
        return None


# ---------- fixtures ----------
FORM = """<html><body><h1>Order</h1>
<form action="/done.html" method="get">
<input id="q" name="q" type="text"><input id="c" name="c" type="checkbox">
<select id="s" name="s"><option value="a">A</option><option value="b">B</option></select>
<button id="go" type="submit">Go</button></form></body></html>"""
TABLE = """<html><body><table><tr><th>Name</th><th>Price</th></tr>
<tr><td>alpha</td><td>10</td></tr><tr><td>beta</td><td>20</td></tr>
<tr><td>gamma</td><td>30</td></tr></table></body></html>"""
DL = """<html><body><a href="/file.bin">Get file</a></body></html>"""


def items_page(p):
    links = "".join(f"<a class='item' href='/i{p}-{i}.html'>item-{p}-{i}</a>" for i in range(5))
    nxt = f"<a class='next' href='/items?p={p + 1}'>Next</a>" if p < 3 else ""
    return f"<html><body>{links}{nxt}</body></html>"


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/form"):
            body, ctype = FORM, "text/html"
        elif self.path.startswith("/table"):
            body, ctype = TABLE, "text/html"
        elif self.path.startswith("/items"):
            import urllib.parse as _u
            q = _u.urlparse(self.path).query
            p = int((_u.parse_qs(q).get("p", ["1"])[0]))
            body, ctype = items_page(p), "text/html"
        elif self.path.startswith("/dl"):
            body, ctype = DL, "text/html"
        elif self.path.startswith("/file.bin"):
            body, ctype = b"file-bytes-1234", "application/octet-stream"
        elif self.path.startswith("/done"):
            body, ctype = "<html><body><h1>Done marker ORDER-OK</h1></body></html>", "text/html"
        else:
            body, ctype = "<html><body>page</body></html>", "text/html"
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if self.path.startswith("/file.bin"):
            self.send_header("Content-Disposition", "attachment; filename=file.bin")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
U = lambda p: f"http://127.0.0.1:{PORT}{p}"  # noqa: E731

MODS = os.path.join(HOME, "mods")
os.makedirs(MODS, exist_ok=True)


def mod(name, code):
    p = os.path.join(MODS, name + ".py")
    open(p, "w").write(code)
    return p


# ---------- 0: profile up ----------
rc, out, _ = run("profiles", "create", "m5", "--json")
check("m5.0 profile create", rc == 0)
rc, out, _ = run("open", "m5", "--json")
check("m5.1 open live", rc == 0 and (jdoc(out) or {}).get("pid"))

# ---------- 1: registry CRUD ----------
LEAF = mod("leaf", 'META = {"description": "leaf"}\ndef run(ctx, inputs):\n    return {"echo": inputs.get("v", "")}\n')
rc, out, _ = run("wf", "save", "leaf", "--file", LEAF, "--description", "leaf",
                 "--inputs", '{"v": {"type": "str", "required": false}}', "--json")
check("m5.2 wf save", rc == 0 and jdoc(out).get("saved") is True)
rc, out, _ = run("wf", "show", "leaf", "--json")
d = jdoc(out) or {}
check("m5.3 wf show", d.get("name") == "leaf" and d.get("runs") == 0)
rc, out, _ = run("wf", "run", "leaf", "--profile", "m5", "--set", "v=hi", "--json")
d = jdoc(out) or {}
check("m5.4 wf run ok", rc == 0 and d.get("ok") is True and d.get("outputs", {}).get("echo") == "hi",
      f"steps={d.get('steps')}")
rc, out, _ = run("wf", "show", "leaf", "--json")
check("m5.5 run logged", (jdoc(out) or {}).get("runs") == 1)
rc, out, _ = run("wf", "save", "leaf", "--file", LEAF, "--json")
rc, out, _ = run("wf", "show", "leaf", "--json")
check("m5.6 resave preserves runs", (jdoc(out) or {}).get("runs") == 1)

# ---------- 2: input validation ----------
rc, out, err = run("wf", "run", "scrape_list", "--profile", "m5",
                   "--input", '{"url": "http://x/"}', "--json")
d = jdoc(out) or {}
check("m5.7 missing required rejected", rc != 0 and "missing required input" in (d.get("error") or err))
rc, out, err = run("wf", "run", "paginate_collect", "--profile", "m5",
                   "--input", json.dumps({"url": U("/items?p=1"), "selector": "a.item",
                                           "next_selector": ".next", "max_pages": "abc"}), "--json")
d = jdoc(out) or {}
check("m5.8 wrong type rejected", rc != 0 and "want int" in (d.get("error") or err))
rc, out, err = run("wf", "run", "leaf", "--profile", "m5", "--set", "bogus=1", "--json")
d = jdoc(out) or {}
check("m5.9 unknown input rejected", rc != 0 and "unknown inputs" in (d.get("error") or err))

# ---------- 3: static cycle refusal ----------
A = mod("cyc_a", 'def run(ctx, inputs):\n    return ctx.call("cyc_b", {})\n')
CB = mod("cyc_b", 'def run(ctx, inputs):\n    return ctx.call("cyc_a", {})\n')
rc, out, _ = run("wf", "save", "cyc_a", "--file", A, "--json")
check("m5.10 first half saves (dangling call ok)", rc == 0)
rc, out, err = run("wf", "save", "cyc_b", "--file", CB, "--json")
d = jdoc(out) or {}
check("m5.11 cycle refused with path", rc != 0 and "cyc_a -> cyc_b -> cyc_a" in (d.get("error") or err), (d.get("error") or err)[:120])
S = mod("selfy", 'def run(ctx, inputs):\n    return ctx.call("selfy", {})\n')
rc, out, err = run("wf", "save", "selfy", "--file", S, "--json")
check("m5.12 undeclared self-loop refused", rc != 0 and "cycle refused" in ((jdoc(out) or {}).get("error") or err))
rc, out, _ = run("wf", "save", "selfy", "--file", S, "--recursive", "--json")
check("m5.13 declared recursion saves", rc == 0)

# ---------- 4: dynamic fuses ----------
rc, out, _ = run("wf", "run", "selfy", "--profile", "m5", "--json")
d = jdoc(out) or {}
check("m5.14 ancestor re-entry aborts", d.get("ok") is False and "cycle" in (d.get("error") or ""))
D4 = mod("d4", 'def run(ctx, inputs):\n    return {"leaf": True}\n')
D3 = mod("d3", 'def run(ctx, inputs):\n    return ctx.call("d4", {})\n')
D2 = mod("d2", 'def run(ctx, inputs):\n    return ctx.call("d3", {})\n')
D1 = mod("d1", 'def run(ctx, inputs):\n    return ctx.call("d2", {})\n')
for n in ("d4", "d3", "d2"):
    run("wf", "save", n, "--file", {"d4": D4, "d3": D3, "d2": D2}[n], "--json")
rc, out, _ = run("wf", "save", "d1", "--file", D1, "--max-depth", "2", "--json")
rc, out, _ = run("wf", "run", "d1", "--profile", "m5", "--json")
d = jdoc(out) or {}
check("m5.15 max-depth aborts", d.get("ok") is False and "max depth" in (d.get("error") or ""))
BG = mod("hungry", 'def run(ctx, inputs):\n    ctx.navigate("http://127.0.0.1:%d/plain")\n'
                    '    ctx.navigate("http://127.0.0.1:%d/plain")\n'
                    '    ctx.snapshot()\n    return {"ok": True}\n' % (PORT, PORT))
run("wf", "save", "hungry", "--file", BG, "--max-steps", "2", "--json")
rc, out, _ = run("wf", "run", "hungry", "--profile", "m5", "--json")
d = jdoc(out) or {}
check("m5.16 step budget aborts", d.get("ok") is False and "step budget" in (d.get("error") or ""))

# ---------- 5: 3-deep composition + call tree ----------
rc, out, _ = run("wf", "save", "d1", "--file", D1, "--json")  # default depth 8
rc, out, _ = run("wf", "run", "d1", "--profile", "m5", "--json")
d = jdoc(out) or {}
tree = d.get("callTree", [])
ok_tree = (d.get("ok") is True and len(tree) == 1 and tree[0]["wf"] == "d2"
           and len(tree[0].get("children", [])) == 1
           and tree[0]["children"][0]["wf"] == "d3"
           and len(tree[0]["children"][0].get("children", [])) == 1
           and tree[0]["children"][0]["children"][0]["wf"] == "d4")
check("m5.17 3-deep composition + call tree", ok_tree, json.dumps(tree)[:200])

# ---------- 6: seed arsenal on fixtures ----------
rc, out, _ = run("wf", "run", "form_fill", "--profile", "m5", "--input",
                 json.dumps({"url": U("/form"), "fields": {"q": "hello"},
                             "submit_name": "Go", "expect_text": "ORDER-OK"}), "--json")
d = jdoc(out) or {}
o = d.get("outputs", {})
check("m5.18 form_fill e2e", d.get("ok") is True and o.get("filled") == ["q"]
      and o.get("waitMatched") == "text:ORDER-OK", str(o)[:160])
rc, out, _ = run("wf", "run", "scrape_table", "--profile", "m5",
                 "--input", json.dumps({"url": U("/table")}), "--json")
d = jdoc(out) or {}
o = d.get("outputs", {})
check("m5.19 scrape_table e2e", d.get("ok") is True and o.get("headers") == ["Name", "Price"]
      and o.get("count") == 3, str(o)[:160])
rc, out, _ = run("wf", "run", "scrape_list", "--profile", "m5",
                 "--input", json.dumps({"url": U("/items?p=1"), "selector": "a.item"}), "--json")
d = jdoc(out) or {}
check("m5.20 scrape_list e2e", d.get("ok") is True and d.get("outputs", {}).get("count") == 5)
rc, out, _ = run("wf", "run", "paginate_collect", "--profile", "m5",
                 "--input", json.dumps({"url": U("/items?p=1"), "selector": "a.item",
                                         "next_selector": ".next", "max_pages": 5}), "--json",
                 timeout=180)
d = jdoc(out) or {}
o = d.get("outputs", {})
kids = d.get("callTree", [])
check("m5.21 paginate_collect composes scrape_list", d.get("ok") is True and o.get("pages") == 3
      and o.get("count") == 15 and any(k.get("wf") == "scrape_list" for k in kids),
      f"pages={o.get('pages')} count={o.get('count')}")
DLDIR = os.path.join(HOME, "dl")
rc, out, _ = run("wf", "run", "download_collect", "--profile", "m5",
                 "--input", json.dumps({"url": U("/dl"), "link_text": "Get file",
                                         "out_dir": DLDIR}), "--json")
d = jdoc(out) or {}
o = d.get("outputs", {})
check("m5.22 download_collect e2e", d.get("ok") is True and (o.get("bytes") or 0) > 0
      and os.path.isfile(o.get("path") or ""), str(o)[:160])
rc, out, _ = run("wf", "run", "login_check", "--profile", "m5",
                 "--input", json.dumps({"url": U("/plain")}), "--json")
d = jdoc(out) or {}
check("m5.23 login_check classifies", d.get("ok") is True
      and d.get("outputs", {}).get("verdict") in ("anonymous", "alive")
      and d.get("outputs", {}).get("verdict") != "logged_in",
      str(d.get("outputs"))[:120])

# ---------- 7: promote + search + export ----------
rc, out, _ = run("skill", "save", "m5macro", "--description", "navigate snap click",
                 "--steps", json.dumps([{"cmd": ["navigate", "m5", "--url", U("/form")]},
                                        {"cmd": ["snapshot", "m5"]},
                                        {"cmd": ["act", "m5", "click", "--ref", "e1"]}]), "--json")
check("m5.24 skill save", rc == 0)
rc, out, _ = run("skill", "promote", "m5macro", "--json")
d = jdoc(out) or {}
check("m5.25 promote scaffolds", rc == 0 and d.get("saved") is True and d.get("todos") == 1,
      str(d)[:120])
rc, out, _ = run("wf", "show", "m5macro-promoted", "--json")
d = jdoc(out) or {}
rc2, out2, _ = run("wf", "export", "m5macro-promoted")
check("m5.26 promoted draft registered", "DRAFT" in d.get("description", "")
      and "TODO(semantic)" in out2 and "ctx.navigate" in out2)
rc, out, _ = run("skill", "search", "navigate snap", "--json")
check("m5.27 skill search", len((jdoc(out) or {}).get("skills", [])) == 1)
rc, out, _ = run("wf", "search", "paginat", "--json")
check("m5.28 wf search", any(w["workflow"] == "paginate_collect" for w in (jdoc(out) or {}).get("workflows", [])))
rc, out, _ = run("wf", "export", "scrape_list", "--json")
check("m5.29 wf export renders", rc == 0 and "## Inputs" in out and "ctx.call" in out)

# ---------- 8: evolution loop ----------
rc, out, _ = run("doctor", "--json")
d = (jdoc(out) or {}).get("workflows", {})
check("m5.30 doctor workflow section", d.get("workflows", 0) >= 10 and d.get("hint") == "",
      f"workflows={d.get('workflows')}")
HOME2 = tempfile.mkdtemp(prefix="cloakwf-hint-")
ENV2 = dict(os.environ, CLOAKCTL_HOME=HOME2)
p2 = subprocess.run([B, "profiles", "create", "h", "--json"], capture_output=True, text=True, env=ENV2)
audit = os.path.join(HOME2, "runtime", "audit.h.jsonl")
os.makedirs(os.path.dirname(audit), exist_ok=True)
import time as _t
open(audit, "w").write("\n".join(
    json.dumps({"ts": "2026-09-16T00:00:00Z", "cmd": c, "rc": 0, "ms": 1})
    for c in ["snapshot", "read", "grep", "act", "snapshot", "read"]) + "\n")
p2 = subprocess.run([B, "doctor", "--json"], capture_output=True, text=True, env=ENV2)
d2 = (jdoc(p2.stdout) or {}).get("workflows", {})
check("m5.31 doctor nudge fires", "skill promote" in (d2.get("hint") or ""),
      (d2.get("hint") or "")[:100])

# ---------- teardown: zero residue ----------
rc, out, _ = run("close", "m5", "--json")
check("m5.32 close clean", rc == 0 and jdoc(out).get("closed") is True)
rc, out, _ = run("status", "m5", "--json")
check("m5.33 not live after close", (jdoc(out) or {}).get("live") is False)
import shutil as _sh
left = subprocess.run(["pgrep", "-f", f"user-data-dir=[^ ]*{HOME}/profiles/m5"],
                      capture_output=True, text=True)
check("m5.34 no stray browser procs", left.returncode != 0, left.stdout.strip()[:80])

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} PASS")
sys.exit(1 if FAIL else 0)
