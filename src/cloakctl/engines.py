"""Engine detection and shared engine constants.

cloakctl drives one of two engines per profile:

- **obscura** (default): a standalone stealth headless browser with its own
  CDP server (`obscura serve`). One page per server; page state lives inside
  the long-lived keeper session (see keeper.py), cookies persist in the
  profile's storage dir.
- **cloakbrowser** (opt-in): a Chromium-family binary launched with
  `--user-data-dir` + `--remote-debugging-port=0` — the original engine.

Resolution order (sticky per profile once set):
1. explicit `--engine` on `cloakctl open`
2. `CLOAKCTL_ENGINE` env
3. profile meta (`engine` key)
4. obscura when an `obscura` binary is found, else cloakbrowser
"""

from __future__ import annotations

import os
import shutil

ENGINE_OBScura = "obscura"
ENGINE_CB = "cloakbrowser"
ENGINES = (ENGINE_OBScura, ENGINE_CB)


def find_obscura() -> str | None:
    """Path to the obscura binary (PATH first, then ~/.local/bin)."""
    p = shutil.which("obscura")
    if p:
        return p
    home = os.path.expanduser("~/.local/bin/obscura")
    if os.path.isfile(home) and os.access(home, os.X_OK):
        return home
    return None


def find_cloakbrowser() -> str | None:
    """The opt-in Chromium-family launcher (cloakbrowser or any Chromium)."""
    for b in ("cloakbrowser", "chromium", "chrome", "google-chrome",
              "brave", "brave-browser", "brave-origin", "microsoft-edge"):
        p = shutil.which(b)
        if p:
            return p
    home = os.path.expanduser("~/.cloakbrowser")
    if os.path.isdir(home):
        for root, _dirs, files in os.walk(home):
            if "chrome" in files:
                p = os.path.join(root, "chrome")
                if os.access(p, os.X_OK):
                    return p
    return None


def resolve_engine(explicit: str | None = None,
                   profile_meta: dict | None = None) -> str:
    """Engine resolution order; raises on an impossible request."""
    explicit = (explicit or os.environ.get("CLOAKCTL_ENGINE") or "").strip().lower()
    if explicit in ENGINES:
        return explicit
    if explicit:
        raise ValueError(f"unknown engine {explicit!r}; want one of {list(ENGINES)}")
    meta_engine = ((profile_meta or {}).get("engine") or "").strip().lower()
    if meta_engine in ENGINES:
        return meta_engine
    return ENGINE_OBScura if find_obscura() else ENGINE_CB


def engine_info() -> dict:
    """What's installed — for doctor and for the auto-detect decision."""
    return {
        "obscura": find_obscura(),
        "cloakbrowser": find_cloakbrowser(),
    }
