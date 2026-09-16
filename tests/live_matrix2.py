"""Live matrix part 2: parity verbs (snapshot/act/navigate/tabs/read/...)."""
import json, os, subprocess, sys, tempfile, time

HOME = tempfile.mkdtemp(prefix="cloaktest2-home-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"  # repo venv, else PATH

results = []
def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

def run(*args, timeout=60):
    p = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=timeout, env=ENV)
    try: return p.returncode, json.loads(p.stdout), p.stderr
    except Exception: return p.returncode, {"_raw": (p.stdout + p.stderr)[:200]}, p.stderr

PAGE = ("data:text/html,<h1>Probe</h1><p id=delay></p>"
        "<input id=q value=''><button id=b>Go</button><p id=r></p>"
        "<a id=dl href='data:text/plain,hello-download' download='hi.txt'>get</a>"
        "<input id=f type=file><select id=s><option value=a>A</option><option value=b>B</option></select>"
        "<script>setTimeout(()=>delay.textContent='late-text',800);"
        "b.onclick=()=>{r.textContent='hi '+q.value}</scr" + "ipt>")

run("--json", "profiles", "create", "w")
rc, d, _ = run("--json", "open", "w")
check("N0 open", rc == 0 and d.get("pid"), str(d.get("pid")))

# navigate ---------------------------------------------------------------
rc, d, _ = run("--json", "navigate", "w", "--url", PAGE)
check("N1 navigate url", rc == 0 and d.get("title") == "", f"url-ok={bool(d.get('url'))}")
rc, d, _ = run("--json", "navigate", "w", "--url", "https://example.com/", timeout=60)
check("N2 navigate live", rc == 0 and d.get("title") == "Example Domain", d.get("title"))
rc, d, _ = run("--json", "navigate", "w", "--back")
check("N3 navigate back", rc == 0, d.get("url", "")[:60])
rc, d, _ = run("--json", "navigate", "w", "--forward")
check("N4 navigate forward", rc == 0 and "example.com" in d.get("url", ""), d.get("url", "")[:60])
rc, d, _ = run("--json", "navigate", "w", "--reload")
check("N5 navigate reload", rc == 0, str(d.get("action")))

# tabs -------------------------------------------------------------------
run("--json", "navigate", "w", "--url", PAGE)
rc, d, _ = run("--json", "tabs", "w", "list")
n0 = len(d.get("tabs", []))
check("T1 tabs list", rc == 0 and n0 >= 1, f"tabs={n0} windows={d.get('windows')}")
rc, d, _ = run("--json", "tabs", "w", "new", "--url", "about:blank", "--background")
tid = d.get("targetId", "")
check("T2 tabs new background", rc == 0 and tid, d.get("id"))
rc, d, _ = run("--json", "tabs", "w", "list")
check("T3 tab count +1", len(d.get("tabs", [])) == n0 + 1, f"{n0}->{len(d.get('tabs', []))}")
rc, d, _ = run("--json", "tabs", "w", "activate", tid[:8])
check("T4 tabs activate", rc == 0, str(d))
rc, d, _ = run("--json", "tabs", "w", "close", tid[:8])
check("T5 tabs close", rc == 0, str(d))
rc, d, _ = run("--json", "windows", "w", "list")
check("T6 windows list", rc == 0 and d.get("windows"), str(d.get("windows"))[:80])
rc, d, _ = run("--json", "groups", "w", "list")
check("T7 groups list empty", rc == 0, str(d))

# snapshot / diff ----------------------------------------------------------
# Re-anchor on PAGE: tab focus after close is browser-defined, so an agent
# always navigates to a known page before snapshotting.
rc, d, _ = run("--json", "navigate", "w", "--url", PAGE)
check("S0 re-anchor PAGE", rc == 0)
rc, d, _ = run("--json", "snapshot", "w")
refs = d.get("refs", 0)
check("S1 snapshot refs", rc == 0 and refs >= 4, f"refs={refs}")
check("S2 trust markers", "UNTRUSTED_PAGE_CONTENT" in d.get("snapshot", ""))
rc, d2, _ = run("--json", "snapshot", "w")
check("S3 re-snapshot stable", rc == 0 and d2.get("changedSinceLast") is False)
rc, d, _ = run("--json", "diff", "w")
check("S4 diff clean", rc == 0 and d.get("changeCount") == 0, str(d.get("changeCount")))
rc, d, _ = run("--json", "exec", "w", "document.getElementById('r').textContent='mutated'")
rc, d, _ = run("--json", "diff", "w")
check("S5 diff detects mutation", rc == 0 and d.get("changeCount", 0) > 0, str(d.get("changeCount")))
rc, d, _ = run("--json", "snapshot", "w", "--mode", "text")
check("S6 snapshot text mode", rc == 0 and "Probe" in d.get("snapshot", ""))

# act -----------------------------------------------------------------------
def refmap():
    rc, d, _ = run("--json", "snapshot", "w")
    import re
    return {m: m for m in re.findall(r"\[ref=(e\d+)\]", d.get("snapshot", ""))}, d.get("snapshot", "")
refs_found, snaptext = refmap()
# find button/input refs via grep
rc, d, _ = run("--json", "grep", "w", "button|textbox", "--over", "ax")
check("G1 grep ax", rc == 0 and d.get("count", 0) >= 2, f"count={d.get('count')}")
import re
# NOTE: anchor regexes at line start (MULTILINE) — the trust-marker header
# echoes the page URL, which may itself contain tag text like '<button'.
m_btn = re.search(r"^\s*- button .*\[ref=(e\d+)\]", snaptext, re.M)
m_inp = re.search(r"^\s*- textbox .*\[ref=(e\d+)\]", snaptext, re.M)
check("G2 refs present", bool(m_btn and m_inp), f"btn={m_btn.group(1) if m_btn else None} inp={m_inp.group(1) if m_inp else None}")
if m_inp and m_btn:
    b, i = m_btn.group(1), m_inp.group(1)
    rc, d, _ = run("--json", "act", "w", "type", "--ref", i, "--text", "parity")
    check("A1 act type", rc == 0, str(d))
    rc, d, _ = run("--json", "act", "w", "click", "--ref", b)
    check("A2 act click", rc == 0, str(d))
    rc, d, _ = run("--json", "exec", "w", "document.getElementById('r').textContent")
    check("A3 click effect", d.get("value") == "hi parity", repr(d.get("value"))[:40])
    rc, d, _ = run("--json", "act", "w", "clear", "--ref", i)
    rc, d, _ = run("--json", "exec", "w", "document.getElementById('q').value")
    check("A4 act clear", d.get("value") == "", repr(d.get("value"))[:20])
    rc, d, _ = run("--json", "act", "w", "key", "--key", "Enter")
    check("A5 act key", rc == 0, str(d)[:60])
    rc, d, _ = run("--json", "act", "w", "scroll", "--dy", "200")
    check("A6 act scroll", rc == 0, str(d)[:60])
    rc, d, _ = run("--json", "act", "w", "hover", "--ref", b)
    check("A7 act hover", rc == 0, str(d)[:60])
    m_sel = re.search(r"^\s*- combobox .*\[ref=(e\d+)\]", snaptext, re.M)
    if m_sel:
        rc, d, _ = run("--json", "act", "w", "select", "--ref", m_sel.group(1), "--value", "b")
        check("A8 act select", rc == 0 and d.get("ok") is True, str(d)[:80])
    rc, d, _ = run("--json", "act", "w", "click", "--ref", "e9999")
    check("A9 stale ref errors", rc == 1, str(d)[:80])

# wait -------------------------------------------------------------------------
rc, d, _ = run("--json", "navigate", "w", "--url", PAGE)
rc, d, _ = run("--json", "wait", "w", "--text", "late-text", "--timeout", "10")
check("W1 wait text", rc == 0, str(d))
rc, d, _ = run("--json", "wait", "w", "--selector", "#r", "--timeout", "5")
check("W2 wait selector", rc == 0, str(d))
rc, d, _ = run("--json", "wait", "w", "--text", "never-appears-xyz", "--timeout", "2")
check("W3 wait timeout errors", rc == 1, str(d)[:80])

# read ----------------------------------------------------------------------------
rc, d, _ = run("--json", "read", "w", "--format", "markdown")
check("R1 read markdown", rc == 0 and "# Probe" in d.get("content", ""), d.get("content", "")[:60])
rc, d, _ = run("--json", "read", "w", "--format", "text")
check("R2 read text", rc == 0 and "Probe" in d.get("content", ""))
rc, d, _ = run("--json", "read", "w", "--format", "links")
check("R3 read links", rc == 0 and isinstance(d.get("links"), list))
rc, d, _ = run("--json", "read", "w", "--format", "console")
check("R4 read console", rc == 0 and isinstance(d.get("errors"), list), str(d.get("errors"))[:80])
rc, d, _ = run("--json", "grep", "w", "Probe", "--over", "text")
check("R5 grep text", rc == 0 and d.get("count", 0) >= 1)

# screenshot / pdf ------------------------------------------------------------------
rc, d, _ = run("--json", "screenshot", "w")
import pathlib
ok = rc == 0 and pathlib.Path(d.get("path", "")).is_file()
check("C1 screenshot file", ok, f"{d.get('bytes')}b {d.get('path')}")
rc, d, _ = run("--json", "screenshot", "w", "--full", "--format", "png")
check("C2 screenshot full png", rc == 0 and d.get("path", "").endswith(".png"))
rc, d, _ = run("--json", "pdf", "w")
check("C3 pdf file", rc == 0 and pathlib.Path(d.get("path", "")).is_file(), f"{d.get('bytes')}b")

# download / upload ---------------------------------------------------------------------
outdir = os.path.join(HOME, "dl")
rc, d, _ = run("--json", "snapshot", "w")
m_dl = re.search(r"^\s*- link .*\[ref=(e\d+)\]", d.get("snapshot", ""), re.M)
if m_dl:
    rc, d, _ = run("--json", "download", "w", "--ref", m_dl.group(1), "--out-dir", outdir, timeout=60)
    ok = rc == 0 and pathlib.Path(d.get("path", "")).is_file()
    check("D1 download data-url", ok, str(d)[:120])
# file input ref: a ref line that is not button/link/textbox/combobox
m_f = None
for l in d.get("snapshot", "").splitlines():
    mm = re.search(r"\[ref=(e\d+)\]", l)
    if mm and not any(k in l for k in ("button", "link", "textbox", "combobox")):
        m_f = mm; break
upl = os.path.join(HOME, "up.txt"); open(upl, "w").write("upload-me")
if m_f:
    rc, d, _ = run("--json", "upload", "w", "--ref", m_f.group(1), "--file", upl)
    check("D2 upload file", rc == 0, str(d)[:100])
    rc, d, _ = run("--json", "exec", "w", "document.getElementById('f').files.length")
    check("D3 upload effect", d.get("value") == 1, str(d.get("value")))

# run / history / session / skill -------------------------------------------------------------
rc, d, _ = run("--json", "run", "w", "await new Promise(r=>setTimeout(()=>r('async-ok'),300))", "--timeout", "10")
check("P1 run async", rc == 0 and d.get("value") == "async-ok", str(d)[:80])
rc, d, _ = run("--json", "run", "w", "await Promise.reject(new Error('nope'))", "--timeout", "10")
check("P2 run rejection errors", rc == 1, str(d)[:100])
# History flushes to SQLite on clean browser exit — cycle the browser first.
run("--json", "close", "w")
run("--json", "open", "w")
rc, d, _ = run("--json", "history", "w", "--limit", "5")
check("H1 history", rc == 0 and d.get("count", 0) >= 1, f"count={d.get('count')}")
rc, d, _ = run("--json", "history", "w", "--query", "example.com")
check("H2 history query", rc == 0, f"count={d.get('count')}")
rc, d, _ = run("--json", "session", "w", "parity-probe", "--summary", "matrix run")
check("S7 session label", rc == 0 and d.get("label") == "parity-probe")
rc, d, _ = run("--json", "skill", "save", "probe-skill", "--description", "probe", "--site", "data:",
               "--steps", json.dumps([{"cmd": ["--json", "exec", "w", "1+1"]}, {"cmd": ["--json", "read", "w", "--format", "text"]}]))
check("K1 skill save", rc == 0, str(d))
rc, d, _ = run("--json", "skill", "list")
check("K2 skill list", rc == 0 and any(s["skill"] == "probe-skill" for s in d.get("skills", [])))
rc, d, _ = run("--json", "skill", "show", "probe-skill")
check("K3 skill show", rc == 0 and len(d.get("steps", [])) == 2)
rc, d, _ = run("--json", "skill", "run", "probe-skill", timeout=90)
check("K4 skill run replays", rc == 0 and d.get("ok") is True, str(d)[:120])
rc, d, _ = run("--json", "skill", "log-run", "probe-skill", "--note", "matrix")
check("K5 skill log-run", rc == 0 and d.get("runs", 0) >= 1)
rc, d, _ = run("--json", "doctor")
check("DOC doctor memory section", rc == 0 and "memory" in d, str(d.get("memory"))[:120])

run("--json", "close", "w")
run("--json", "profiles", "rm", "w", "--force")
npass = sum(results)
print(f"\n==== {npass}/{len(results)} PASS ====  (HOME={HOME})")
sys.exit(0 if npass == len(results) else 1)
