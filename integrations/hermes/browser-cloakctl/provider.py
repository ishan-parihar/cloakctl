"""cloakctl browser provider — local persistent stealth browser (obscura by default).

Implements :class:`agent.browser_provider.BrowserProvider` on top of the
`cloakctl` CLI. Unlike cloud providers (Browserbase/Browser Use/Firecrawl)
which create ephemeral cloud sessions per task, cloakctl keeps ONE persistent
browser per profile on this host. The browser outlives the CLI (setsid) and
is reachable over CDP.

Engine contract (cloakctl 0.3.1+):
  - default engine is **obscura** (~65MB RAM, stealth, tracker-blocking).
    The profile's keeper daemon hosts a loopback CDP **bridge** over its
    master connection: the endpoint returned by this provider serves the
    profile's TRUE session (same page, same cookies) — not an empty one.
    No Chromium download, no cloakbrowser dependency, fully self-contained.
  - cloakbrowser (Chromium-family) remains available via config
    (``browser.cloakctl_engine: cloakbrowser``) for users who want it; the
    provider then uses the engine's native browser-level ws endpoint.

Config keys::

    browser:
      cloud_provider: "cloakctl"
      cloakctl_profile: "hermes"          # optional, default "hermes"
      cloakctl_bin: "/path/to/cloakctl"  # optional, default auto-discover
      cloakctl_engine: "obscura"         # optional, default "obscura"

CLI discovery order:
  1. ``browser.cloakctl_bin`` in config.yaml
  2. ``CLOAKCTL_BIN`` env var
  3. ``cloakctl`` on PATH (install.sh links it to ~/.local/bin)
  4. ``python -m cloakctl`` via the hermes venv
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from agent.browser_provider import BrowserProvider

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE = "hermes"
_DEFAULT_ENGINE = "obscura"


def _discover_cloakctl_bin() -> Optional[str]:
    """Return a runnable cloakctl invocation, or None if not installed."""
    # 1. Explicit config path
    try:
        from hermes_cli.config import read_raw_config, cfg_get

        cfg = read_raw_config()
        val = cfg_get(cfg, "browser", "cloakctl_bin")
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        pass

    # 2. Env override
    env_bin = os.environ.get("CLOAKCTL_BIN", "").strip()
    if env_bin:
        return env_bin

    # 3. On PATH
    found = shutil.which("cloakctl")
    if found:
        return found

    # 4. Common install locations ($HOME-respecting — no expanduser literals)
    home = os.environ.get("HOME") or str(Path.home())
    candidates = [
        Path(home) / ".local/bin/cloakctl",
        Path(home) / "Documents/github/my-projects/agentic-utility/internet/cloakctl/.venv/bin/cloakctl",
        Path(home) / "agentic-utility/internet/cloakctl/.venv/bin/cloakctl",
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)

    # 5. python -m cloakctl (hermes venv may have it)
    try:
        import importlib.util

        if importlib.util.find_spec("cloakctl") is not None:
            return f"{sys.executable} -m cloakctl"
    except Exception:
        pass

    return None


def _get(key: str, default: str) -> str:
    try:
        from hermes_cli.config import read_raw_config, cfg_get

        cfg = read_raw_config()
        val = cfg_get(cfg, "browser", key)
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get("CLOAKCTL_" + key.upper().replace("CLOAKCTL_", ""),
                          default).strip() or default


def _get_profile() -> str:
    return _get("cloakctl_profile", _DEFAULT_PROFILE)


def _get_engine() -> str:
    eng = _get("cloakctl_engine", _DEFAULT_ENGINE)
    return eng if eng in ("obscura", "cloakbrowser") else _DEFAULT_ENGINE


def _run_cloakctl(args: list[str], timeout: float = 30,
                  extra_env: Dict[str, str] | None = None) -> Dict[str, Any]:
    """Run cloakctl and return parsed JSON output (--json machine contract).

    Raises RuntimeError on non-zero exit with the CLI's error text.
    """
    bin_cmd = _discover_cloakctl_bin()
    if not bin_cmd:
        raise RuntimeError(
            "cloakctl not found. Install it: "
            "git clone https://github.com/ishan-parihar/cloakctl && cd cloakctl && ./install.sh  "
            "or set browser.cloakctl_bin in config.yaml"
        )

    if " -m " in bin_cmd:
        parts = bin_cmd.split()
        cmd = [*parts, "--json", *args]
    else:
        cmd = [bin_cmd, "--json", *args]

    env = None
    if extra_env:
        env = {**os.environ, **extra_env}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            env=env)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise RuntimeError(f"cloakctl {' '.join(args)} failed: {err}")

    out = (result.stdout or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def _http_json(url: str, timeout: float = 3) -> Optional[Dict[str, Any]]:
    """Fetch a DevTools HTTP endpoint (best-effort)."""
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read())
    except Exception:
        return None


class CloakctlBrowserProvider(BrowserProvider):
    """Local persistent stealth browser via cloakctl.

    Each ``task_id`` maps to the same persistent profile — the browser is
    shared across tasks (single-writer via cloakctl's lockfile). ``bb_session_id``
    is the profile name; ``close_session`` is a no-op (the browser persists).
    """

    @property
    def name(self) -> str:
        return "cloakctl"

    @property
    def display_name(self) -> str:
        return "Cloakctl"

    def is_available(self) -> bool:
        return _discover_cloakctl_bin() is not None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, task_id: str) -> Dict[str, object]:
        profile = _get_profile()
        engine = _get_engine()
        if not _discover_cloakctl_bin():
            raise ValueError(
                "cloakctl not found. Install: "
                "git clone https://github.com/ishan-parihar/cloakctl && cd cloakctl && ./install.sh  "
                "or set browser.cloakctl_bin in config.yaml."
            )

        # Ensure profile exists (idempotent).
        try:
            _run_cloakctl(["profiles", "create", profile], timeout=10)
        except RuntimeError as exc:
            if "already" not in str(exc).lower():
                logger.debug("cloakctl profiles create %s: %s", profile, exc)

        # Open (or idempotently re-attach). obscura is the default: the
        # keeper + CDP bridge serve the profile's true session. Extra args
        # for obscura: --stealth is a first-class flag; private-network
        # access for localhost test targets is granted here because hermes
        # commonly drives localhost URLs.
        open_args = ["open", profile]
        if engine == "obscura":
            open_args += ["--browser-arg=--allow-private-network"]
        else:
            open_args += ["--engine", "cloakbrowser"]
        # Hermes has explicitly chosen cloakctl as its browser backend, on
        # hosts that are often small VPS boxes: relax the launch RAM floor
        # (obscura idles ~60MB; the default 400MB floor rejects healthy
        # launches on 1-2GB machines). Operators can still raise it via
        # CLOAKCTL_MIN_MEM_MB in hermes' environment.
        open_env = {"CLOAKCTL_MIN_MEM_MB": "150"}
        data = _run_cloakctl(open_args, timeout=120, extra_env=open_env)

        got_engine = data.get("engine")
        pid = data.get("pid")
        cdp_port = data.get("cdpPort")
        ws_endpoint = data.get("wsEndpoint") or ""

        if engine == "cloakbrowser" and got_engine == "obscura":
            # cloakbrowser requested but fell back (no Chromium binary).
            raise RuntimeError(
                "cloakctl could not use cloakbrowser (no Chromium-family "
                "browser on PATH). Either install one, or use the default "
                "self-contained obscura engine (browser.cloakctl_engine: "
                "obscura). Check: cloakctl doctor"
            )

        # obscura: wsEndpoint (when present) is the keeper BRIDGE — the true
        # session. Derive fallbacks from cdpPort if needed.
        if got_engine == "obscura" and not ws_endpoint and cdp_port:
            # Older keeper without bridge support: surface a typed error.
            raise RuntimeError(
                "cloakctl keeper did not report a CDP bridge endpoint. "
                "Upgrade cloakctl (git pull + ./install.sh) — obscura "
                "attach requires the bridge. Check: cloakctl doctor"
            )

        if not ws_endpoint and cdp_port:
            ws_endpoint = f"ws://127.0.0.1:{cdp_port}/devtools/browser"
            info = _http_json(f"http://127.0.0.1:{cdp_port}/json/version", timeout=3)
            if info and info.get("webSocketDebuggerUrl"):
                ws_endpoint = info["webSocketDebuggerUrl"]

        if not ws_endpoint:
            raise RuntimeError(
                f"cloakctl open {profile} did not return a CDP endpoint. "
                f"Output: {data}"
            )

        session_name = f"cloakctl_{profile}_{task_id}_{uuid.uuid4().hex[:6]}"
        reattached = bool(data.get("reattached"))

        logger.info(
            "cloakctl session %s (profile=%s engine=%s reattached=%s pid=%s bridge=%s)",
            session_name, profile, got_engine, reattached, pid,
            data.get("bridgePort"),
        )

        return {
            "session_name": session_name,
            "bb_session_id": profile,  # close/emergency use the profile name
            "cdp_url": ws_endpoint,
            "expires_at": None,  # persistent — no expiry
            "features": {
                "cloakctl": True,
                "persistent": True,
                "engine": got_engine,
                "bridge": bool(data.get("bridgePort")),
                "reattached": reattached,
                "pid": pid,
                "cdpPort": cdp_port,
            },
            "external_call_id": None,
        }

    def close_session(self, session_id: str) -> bool:
        """No-op for the persistent model — the browser outlives tasks.

        We deliberately do NOT close the browser here. The persistent profile
        is the session; closing it would discard cookies and force a fresh
        launch on the next turn. Operators close explicitly via
        `cloakctl close <profile>` or `hermes browser close`.

        Return True so the dispatcher's cleanup loop treats this as success.
        """
        logger.debug("cloakctl close_session(%s) — persistent profile, no close", session_id)
        return True

    def emergency_cleanup(self, session_id: str) -> None:
        # Same rationale as close_session — never kill the persistent browser
        # from an atexit handler. Best-effort: only log.
        logger.debug("cloakctl emergency_cleanup(%s) — no-op (persistent profile)", session_id)

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "cloakctl",
            "badge": "local",
            "tag": ("Persistent stealth browser (obscura, ~65MB) — per-profile "
                    "cookie persistence, self-contained, no cloud"),
            "env_vars": [],
            "post_setup": "",
        }


# ------------------------------------------------------------------
# Optional: direct cleanup helper for operators who WANT to close
# ------------------------------------------------------------------

def close_cloakctl_profile(profile: Optional[str] = None) -> bool:
    """Explicitly close a cloakctl profile browser. For operator tooling."""
    name = profile or _get_profile()
    try:
        _run_cloakctl(["close", name], timeout=30)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to close cloakctl profile %s: %s", name, exc)
        return False
