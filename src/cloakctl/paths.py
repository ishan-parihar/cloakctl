"""Filesystem layout for cloakctl.

~/.cloakctl/
  profiles/<name>/chrome/     <- cloakbrowser persistent user-data-dir
  profiles/<name>/meta.json   <- created, lastUsed, mintedUA, importHistory[]
  runtime/lock.<name>.json    <- {pid, startedAt, cdpPort} while live
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

HOME_STATE = Path(os.environ.get("CLOAKCTL_HOME", Path.home() / ".cloakctl"))
PROFILES_DIR = HOME_STATE / "profiles"
RUNTIME_DIR = HOME_STATE / "runtime"

PROFILE_META_VERSION = 1


def check_name(name: str) -> str:
    """Every path builder routes through here: profile names can never
    escape the state dir (`audit ../../x` reads nothing outside), and must
    be shell/tab-completion friendly (strict charset, bounded length)."""
    import re as _re
    if not name or not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError(
            f"invalid profile name: {name!r} (use 1-64 chars of "
            "[A-Za-z0-9_-], starting with a letter or digit)")
    return name


def profile_dir(name: str) -> Path:
    return PROFILES_DIR / check_name(name)


def chrome_dir(name: str) -> Path:
    return profile_dir(name) / "chrome"


def meta_path(name: str) -> Path:
    return profile_dir(name) / "meta.json"


def lock_path(name: str) -> Path:
    return RUNTIME_DIR / f"lock.{check_name(name)}.json"


def ensure_layout() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Profiles hold cookie stores; keep them private.
    os.chmod(PROFILES_DIR, 0o700)
    os.chmod(RUNTIME_DIR, 0o700)


def load_meta(name: str) -> dict:
    p = meta_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {
        "version": PROFILE_META_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastUsed": None,
        "mintedUA": None,
        "importHistory": [],
    }


def save_meta(name: str, meta: dict) -> None:
    from .util import atomic_write_json

    p = meta_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, meta)


def touch_last_used(name: str) -> None:
    meta = load_meta(name)
    meta["lastUsed"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_meta(name, meta)
