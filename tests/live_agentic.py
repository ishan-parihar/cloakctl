"""Agentic task e2e using ONLY cloakctl primitives (offline page + example.com)."""
import base64, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from cloakctl.cdp import CdpClient
from cloakctl import browser

HOME = tempfile.mkdtemp(prefix="cloakagent-")
os.environ["CLOAKCTL_HOME"] = HOME
out = []
def check(name, cond, detail=""):
    out.append(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

t0 = time.monotonic()
info = browser.open_profile("agent")
print(f"open: {time.monotonic()-t0:.1f}s pid={info['pid']}")

FORM = "data:text/html,<form><input id=q value=''><button id=b>Go</button><p id=r></p><script>b.onclick=(e)=>{e.preventDefault();r.textContent='hi '+q.value}</scr" + "ipt>"
with CdpClient(info["wsEndpoint"]) as cdp:
    # 1. navigate offline page
    t = time.monotonic(); cdp.navigate(FORM, load_timeout=10); dt_nav = time.monotonic() - t
    check("A1 navigate data: URL", True, f"{dt_nav:.1f}s")
    # 2. form fill + click entirely via evaluate (what an agent must hand-roll)
    cdp.evaluate("document.getElementById('q').value='agent-fill'; document.getElementById('q').dispatchEvent(new Event('input',{bubbles:true}))")
    cdp.evaluate("document.getElementById('b').click()")
    time.sleep(0.5)
    r = cdp.evaluate("document.getElementById('r').textContent")
    check("A2 fill+click+read via JS", r == "hi agent-fill", repr(r))
    # 3. screenshot via raw CDP call
    t = time.monotonic()
    shot = cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
    raw = base64.b64decode(shot["data"])
    sp = os.path.join(HOME, "shot.jpg"); open(sp, "wb").write(raw)
    check("A3 screenshot via raw CDP", len(raw) > 5000, f"{len(raw)} bytes {time.monotonic()-t:.1f}s")
    # 4. pdf via raw CDP call
    t = time.monotonic()
    pdf = cdp.call("Page.printToPDF", {"printBackground": False})
    praw = base64.b64decode(pdf["data"])
    pp = os.path.join(HOME, "page.pdf"); open(pp, "wb").write(praw)
    check("A4 pdf via raw CDP", len(praw) > 1000, f"{len(praw)} bytes {time.monotonic()-t:.1f}s")
    # 5. live network read
    try:
        t = time.monotonic()
        cdp.navigate("https://example.com/", load_timeout=25)
        title = cdp.evaluate("document.title")
        body = cdp.evaluate("document.body ? document.body.innerText.slice(0,500) : ''")
        check("A5 live navigate+read example.com", title == "Example Domain" and "Example" in body, f"{time.monotonic()-t:.1f}s title={title!r}")
    except Exception as e:
        check("A5 live navigate+read example.com", False, f"SKIP/FAIL net? {str(e)[:100]}")

browser.close_profile("agent")
print(f"AGENTIC: {sum(out)}/{len(out)}  (HOME={HOME})")
