"""Small shared helpers: cookie jar fingerprinting, formatting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def jar_fingerprint(cookies: list[dict]) -> str:
    """Stable short fingerprint over (name, domain, value). No values are logged."""
    material = "\n".join(
        f"{c.get('name','')}={c.get('value','')}@{c.get('domain','')}"
        for c in sorted(cookies, key=lambda c: (c.get("name", ""), c.get("domain", "")))
    )
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_text(path: Path, text: str) -> None:
    """Crash-safe write: temp file in the same dir + os.replace.

    Concurrent readers never see half-written JSON (registry manifests,
    skill docs, meta.json, ref files are all read by parallel runs)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2))


@contextmanager
def interprocess_lock(path: Path):
    """Serialize read-modify-write cycles across parallel CLI processes.

    Atomic writes alone stop corruption but not lost updates (two `wf
    log-run` racing both read N entries and both write N+1). The lock is a
    sibling `<path>.lock` file held only for the integer-millisecond
    registry mutation — never across browser I/O. POSIX flock; elsewhere
    (or on error) it degrades to best-effort rather than failing."""
    lockp = Path(str(path) + ".lock")
    try:
        lockp.parent.mkdir(parents=True, exist_ok=True)
        with open(lockp, "w") as f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass  # non-POSIX: unlocked, still correct single-writer
            yield
    except OSError:
        yield  # lock file itself unwritable: proceed unlocked, never break


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-_]{0,63}$")


def strict_name(kind: str, name: str) -> str:
    """Registry names are strict at save time so two raw names can never
    collide on one sanitized slot ("a/b" vs "a-b"). Read paths stay
    lenient; only saves are gated."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"{kind} name {name!r}: use 1-64 chars of [A-Za-z0-9-_], "
            "leading alnum")
    return name


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
