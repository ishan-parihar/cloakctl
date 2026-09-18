"""cloakctl live obscura — full engine matrix for the DEFAULT obscura engine.

Local HTTP fixture (no external network): main page with form/links/list/
script block, second page, download link. Covers the whole verb surface with
obscura honesty checks: single-page tabs semantics, download/history
fail-fasts, upload gating (--allow-file-access), private-network SSRF guard.

Usage: python tests/live_obscura.py   (needs the obscura binary; exit 0 = green)
"""
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import functools
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
_VENV_BIN = os.path.join(REPO, ".venv", "bin", "cloakctl")
BIN = _VENV_BIN if os.access(_VENV_BIN, os.X_OK) else "cloakctl"
HOME = tempfile.mkdtemp(prefix="cloakobscura-")
ENV = dict(os.environ, CLOAKCTL_HOME=HOME)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)


def run(*args, timeout=120):
    p = subprocess.run([BIN, *args, "--json"], capture_output=True,
                       text=True, timeout=timeout, env=ENV)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, {"_raw": (p.stdout + p.stderr)[:200]}


# --- local fixture site ------------------------------------------------------

HTML_MAIN = """<html><head><title>T1 Main</title></head><body>
<h1>Main Page</h1>
<form onsubmit="event.preventDefault();document.getElementById('out').textContent='Clicked!'">
  <input id="user" placeholder="user"><input id="pw" type="password"><button id="go">Go</button>
</form>
<a href="two.html" id="to2">Page Two</a>
<a href="file.bin" id="dl">download me</a>
<input type="file" id="fu">
<ul><li>alpha</li><li>beta</li><li>gamma</li></ul>
<script>console.log("boot ok");</script>
<div id="out"></div>
</body></html>"""

HTML_TWO = "<html><head><title>T1 Two</title></head><body><p>two content</p></body></html>"

HTTP_PORT = 8792  # fixed: the engine needs --allow-private-network either way


def start_site(tmp):
    with open(os.path.join(tmp, "index.html"), "w") as f:
        f.write(HTML_MAIN)
    with open(os.path.join(tmp, "two.html"), "w") as f:
        f.write(HTML_TWO)
    with open(os.path.join(tmp, "file.bin"), "wb") as f:
        f.write(b"BINDATA" * 64)

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", HTTP_PORT),
                                   functools.partial(H, directory=tmp))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def wait_port(port, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="cloakobscura-site-")
    httpd = start_site(tmp)
    dl_dir = tempfile.mkdtemp(prefix="cloakobscura-dl-")
    upfile = os.path.join(tmp, "file.bin")
    BASE = f"http://127.0.0.1:{HTTP_PORT}"
    try:
        assert wait_port(HTTP_PORT), "fixture site failed to start"

        # r0: open (obscura default) + engine fields
        run("profiles", "create", "ob")
        rc, d = run("open", "ob", "--browser-arg=--allow-private-network",
                    "--browser-arg=--allow-file-access", "--stealth")
        check("r0 open obscura", rc == 0 and d.get("engine") == "obscura"
              and d.get("cdpPort", 0) > 0 and d.get("stealth") is True, f"rc={rc}")

        # r1: navigate + wait
        rc, d = run("navigate", "ob", "--url", BASE)
        check("r1 navigate", rc == 0 and d.get("url") == BASE + "/"
              and d.get("title") == "T1 Main", f"rc={rc}")
        rc, d = run("wait", "ob", "--text", "Main Page", "--timeout", "5")
        check("r1b wait text", rc == 0, f"rc={rc}")

        # r2: read text/links/console
        rc, d = run("read", "ob", "--format", "text")
        check("r2 read text", rc == 0 and "Main Page" in d.get("content", "")
              and "UNTRUSTED_PAGE_CONTENT" in d.get("content", ""), f"rc={rc}")
        rc, d = run("read", "ob", "--format", "links")
        links = d.get("links", [])
        check("r2b read links", rc == 0 and
              any("two.html" in str(l) for l in links) and
              any("file.bin" in str(l) for l in links), f"n={len(links)}")
        rc, d = run("read", "ob", "--format", "console")
        check("r2c console capture", rc == 0 and d.get("errors") == [], f"rc={rc}")

        # r3: grep over ax tree
        rc, d = run("grep", "ob", "beta")
        check("r3 grep ax", rc == 0 and d.get("count", 0) >= 1, f"rc={rc}")

        # r4: snapshot -> refs
        rc, snap = run("snapshot", "ob")
        check("r4 snapshot", rc == 0 and snap.get("refs", 0) >= 5, f"rc={rc}")

        # r5: tabs semantics (single page)
        rc, d = run("tabs", "ob", "list")
        tabs = d.get("tabs", [])
        check("r5 tabs list single page", rc == 0 and len(tabs) == 1
              and tabs[0].get("id") == "page-1"
              and tabs[0].get("title") == "T1 Main", f"tabs={tabs}")
        rc, d = run("tabs", "ob", "new", "--url", f"{BASE}/two.html")
        check("r5b tabs new navigates", rc == 0 and d.get("navigated") is True
              and d.get("url", "").endswith("/two.html"), f"rc={rc}")
        rc, d = run("read", "ob", "--format", "text")
        check("r5c page moved", rc == 0 and "two content" in d.get("content", ""))

        # r6: history/back/forward/reload
        rc, d = run("navigate", "ob", "--back")
        check("r6 back", rc == 0 and d.get("url") == BASE + "/", f"rc={rc}")
        rc, d = run("navigate", "ob", "--forward")
        check("r6b forward", rc == 0 and d.get("url", "").endswith("/two.html"))
        rc, d = run("navigate", "ob", "--reload")
        check("r6c reload", rc == 0, f"rc={rc}")

        # r7: obscura honesty — history fail-fast with hint
        rc, d = run("history", "ob")
        check("r7 history fail-fast", rc != 0 and "History db" in str(d), f"rc={rc}")

        # r8: validate probe
        rc, d = run("validate", "ob", "--url", BASE)
        check("r8 validate alive", rc in (0, 2) and d.get("verdict") == "alive",
              f"rc={rc} verdict={d.get('verdict')}")

        # r9: download fail-fast with remediation
        rc, d = run("download", "ob", "--ref", "e5", "--out-dir", dl_dir)
        check("r9 download fail-fast", rc != 0 and "not supported" in str(d)
              and "cloakbrowser" in str(d), f"rc={rc}")

        # r10: screenshot + pdf bytes land
        shot = os.path.join(tmp, "shot.png")
        rc, d = run("screenshot", "ob", "--out", shot)
        check("r10 screenshot", rc == 0 and os.path.getsize(shot) > 1000, f"rc={rc}")
        pdf = os.path.join(tmp, "page.pdf")
        rc, d = run("pdf", "ob", "--out", pdf)
        check("r10b pdf", rc == 0 and os.path.getsize(pdf) > 1000, f"rc={rc}")

        # r11: exec + run (async)
        rc, d = run("exec", "ob", "1+1")
        check("r11 exec", rc == 0 and d.get("value") == 2, f"rc={rc}")
        rc, d = run("run", "ob", "(async () => 42)()")
        check("r11b run awaitPromise", rc == 0 and d.get("value") == 42, f"rc={rc}")

        # r12: act by ref (snapshot refs) — fill + click the form button
        run("navigate", "ob", "--url", BASE)
        run("wait", "ob", "--text", "Main Page", "--timeout", "5")
        run("snapshot", "ob")
        rc, d = run("act", "ob", "fill", "--field", "e1=hello")
        check("r12 act fill", rc == 0 and d.get("filled") == ["e1"], f"rc={rc}")
        rc, d = run("act", "ob", "click", "--ref", "e3")
        check("r12b act click", rc == 0 and d.get("clicked") == "e3", f"rc={rc}")
        rc, d = run("wait", "ob", "--text", "Clicked!", "--timeout", "3")
        check("r12c form reacted", rc == 0, f"rc={rc}")

        # r13: upload with --allow-file-access granted at open
        rc, d = run("upload", "ob", "--selector", "#fu", "--file", upfile)
        check("r13 upload allowed", rc == 0, f"rc={rc} {d}")
        # r13b: non-file inputs are rejected on BOTH engines (same error shape)
        rc, d = run("upload", "ob", "--selector", "#user", "--file", upfile)
        check("r13b upload rejects non-file input", rc != 0
              and "not a file input" in str(d), f"rc={rc} {d}")

        # r14: status reflects engine + keeper
        rc, d = run("status", "ob")
        check("r14 status", rc == 0 and d.get("engine") == "obscura"
              and d.get("live") is True and d.get("pid", 0) > 0, f"rc={rc}")

        # r15: close + teardown
        rc, d = run("close", "ob")
        check("r15 close", rc == 0 and d.get("closed") is True, f"rc={rc}")
        time.sleep(1.5)
        left = subprocess.run(["pgrep", "-af", "cloakobscura-"],
                              capture_output=True, text=True).stdout
        left = [l for l in left.splitlines() if "pgrep" not in l]
        check("r15b teardown clean", not left, str(left)[:120])

        print(f"\n---- live_obscura: {len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            print("FAILURES:", FAIL)
        return 1 if FAIL else 0
    finally:
        httpd.shutdown()
        subprocess.run([BIN, "close", "ob", "--json"], env=ENV,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(dl_dir, ignore_errors=True)
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
