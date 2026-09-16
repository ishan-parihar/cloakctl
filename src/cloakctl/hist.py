"""Read-only browser history for a profile (mirrors the cookie pipeline).

Copies the History SQLite (+WAL/SHM) to tmp and opens mode=ro; the live
profile is never touched.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from . import paths

_WEBKIT_EPOCH_OFFSET = 11644473600


def _history_db(name: str) -> Path:
    # Chromium nests the real profile one level down (<user-data-dir>/Default/).
    cdir = paths.chrome_dir(name)
    direct = cdir / "History"
    if direct.exists():
        return direct
    nested = sorted(cdir.glob("*/History"), key=lambda p: p.stat().st_mtime, reverse=True)
    if nested:
        return nested[0]
    raise FileNotFoundError(f"no History db for profile {name!r} yet (browse first)")


def read_history(name: str, limit: int = 20, query: str | None = None) -> dict[str, Any]:
    db = _history_db(name)
    tmp = Path(tempfile.mkdtemp(prefix="cloakctl-history-"))
    try:
        local = tmp / "History"
        shutil.copy2(db, local)
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(str(db) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(local) + suffix))
        conn = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
        try:
            sql = ("SELECT url, title, visit_count, last_visit_time FROM urls ")
            params: list[Any] = []
            if query:
                sql += "WHERE url LIKE ? OR title LIKE ? "
                params = [f"%{query}%", f"%{query}%"]
            sql += "ORDER BY last_visit_time DESC LIMIT ?"
            rows = conn.execute(sql, (*params, limit)).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    entries = [{"url": u, "title": t or "", "visits": v,
                "lastVisit": (lv / 1_000_000 - _WEBKIT_EPOCH_OFFSET) if lv else None}
               for u, t, v, lv in rows]
    return {"profile": name, "entries": entries, "count": len(entries)}
