"""Active-browser cookie extraction + import + in-context validation.

Extraction is read-only: the source browser's Cookies SQLite is copied to a
temp dir (along with WAL/SHM) and decrypted there; the source DB is never
opened for write. Decryption covers the Chromium-on-Linux schemes:

- v10/v11: AES-128-CBC, key = PBKDF2-HMAC-SHA1("peanuts", "saltysalt", 1, 16),
  IV = 16 spaces.
- v20: AES-256-GCM with the browser's "Safe Storage" key from the OS keyring
  (secretstorage), 12-byte nonce after the 3-byte version prefix.

Import is same-context by construction: cookies are written via CDP into the
*already-running* persistent browser for the target profile, then validated by
navigating inside that browser. No cookie bytes ever leave the machine.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .cdp import CdpClient
from .util import jar_fingerprint, now_iso

# --- source browser discovery -------------------------------------------------

BROWSER_ROOTS = {
    "brave": Path.home() / ".config" / "BraveSoftware" / "Brave-Browser",
    "chrome": Path.home() / ".config" / "google-chrome",
    "chromium": Path.home() / ".config" / "chromium",
    "edge": Path.home() / ".config" / "microsoft-edge",
}


def discover_cookie_db(browser: str, profile: str | None = None) -> Path:
    """Locate a `Cookies` SQLite file. Prefers the most recently modified profile."""
    root = BROWSER_ROOTS.get(browser)
    if root is None and browser.startswith("file:"):
        p = Path(browser[len("file:") :])
        if not p.exists():
            raise FileNotFoundError(f"cookie file not found: {p}")
        return p
    if root is None or not root.exists():
        raise FileNotFoundError(f"no {browser} config dir found under ~/.config")
    if profile:
        cand = root / profile / "Cookies"
        if not cand.exists():
            raise FileNotFoundError(f"no Cookies db for {browser} profile {profile!r}")
        return cand
    # Channel dirs nest either <root>/<channel>/Cookies or
    # <root>/<channel>/<Default|Profile N>/Cookies (Brave-Origin style).
    for pattern in ("*/Cookies", "*/*/Cookies"):
        candidates = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    raise FileNotFoundError(f"no Cookies db found under {root}")


# --- keyring + decryption -----------------------------------------------------

_SAFE_STORAGE_LABELS = (
    "Chrome Safe Storage",
    "Chromium Safe Storage",
    "Brave Safe Storage",
)


def _safe_storage_password(browser: str) -> bytes | None:
    try:
        import secretstorage  # type: ignore

        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()
        for item in collection.get_all_items():
            if item.get_label() in _SAFE_STORAGE_LABELS:
                return bytes(item.get_secret())
    except Exception:
        return None
    return None


def _derive_key_v10() -> bytes:
    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=SHA1(), length=16, salt=b"saltysalt", iterations=1)
    return kdf.derive(b"peanuts")


def _derive_key_v20(browser: str) -> bytes | None:
    password = _safe_storage_password(browser)
    if password is None:
        return None
    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=SHA1(), length=32, salt=b"saltysalt", iterations=1)
    return kdf.derive(password)


def _decrypt_v10(blob: bytes) -> bytes:
    # NOTE: AES lives in primitives on every cryptography version; decrepit
    # only holds legacy algorithms (and lost AES in v50) — never import from there.
    from cryptography.hazmat.primitives.ciphers.algorithms import AES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    ct = blob[3:]  # strip the b'v10'/b'v11' version prefix
    cipher = Cipher(AES(_derive_key_v10()), modes.CBC(b" " * 16))
    dec = cipher.decryptor()
    padded = dec.update(ct) + dec.finalize()
    return padded[: -padded[-1]] if padded and 1 <= padded[-1] <= 16 else padded


def _decrypt_v20(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce, ct = blob[3:15], blob[15:]
    return AESGCM(key).decrypt(nonce, ct, None)


def decrypt_value(prefix: str, blob: bytes, browser: str) -> str | None:
    try:
        if prefix in ("v10", "v11"):
            return _decrypt_v10(blob).decode("utf-8", "replace")
        if prefix == "v20":
            key = _derive_key_v20(browser)
            if key is None:
                return None
            return _decrypt_v20(blob, key).decode("utf-8", "replace")
    except Exception:
        return None
    return None


# --- extraction -----------------------------------------------------------------

_WEBKIT_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01
_SAMESITE = {0: None, 1: "Lax", 2: "Strict", -1: "None"}


def _chrome_time_to_unix(micros: int | None) -> float | None:
    if not micros:
        return None
    return micros / 1_000_000 - _WEBKIT_EPOCH_OFFSET


def extract_cookies(browser: str, profile: str | None = None) -> list[dict[str, Any]]:
    """Read-only extraction from a Chromium-family browser. Returns raw rows."""
    db = discover_cookie_db(browser, profile)
    tmp = Path(tempfile.mkdtemp(prefix="cloakctl-cookies-"))
    try:
        local = tmp / "Cookies"
        shutil.copy2(db, local)
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(local) + suffix))
        conn = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value, value, path, expires_utc, "
                "is_secure, is_httponly, samesite, priority FROM cookies"
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out: list[dict[str, Any]] = []
    undecrypted = 0
    for host, name, enc, plain, path, exp, secure, httponly, samesite, priority in rows:
        value = plain
        if not value and enc:
            prefix = enc[:3].decode("ascii", "replace") if len(enc) >= 3 else ""
            value = decrypt_value(prefix, enc, browser)
            if not value:
                undecrypted += 1
        expires = _chrome_time_to_unix(exp)
        out.append(
            {
                "domain": host,
                "name": name,
                "value": value or "",
                "path": path or "/",
                "expires": expires,
                "secure": bool(secure),
                "httpOnly": bool(httponly),
                "sameSite": _SAMESITE.get(samesite),
                "priority": ["Low", "Medium", "High"][priority] if priority in (0, 1, 2) else "Medium",
            }
        )
    extract_cookies.stats = {"rows": len(rows), "undecrypted": undecrypted}  # type: ignore[attr-defined]
    return out


# --- CDP mapping -------------------------------------------------------------------


def to_cdp_cookies(rows: list[dict[str, Any]], domains: list[str] | None = None) -> list[dict[str, Any]]:
    """Map extracted rows to CDP Storage.setCookies dicts; filter + drop expired."""
    import time as _time

    now = _time.time()
    allowed = tuple(domains) if domains else None
    out = []
    for r in rows:
        dom = (r.get("domain") or "").lstrip(".")
        if allowed and not any(dom == d or dom.endswith("." + d) for d in allowed):
            continue
        exp = r.get("expires")
        if exp is not None and exp > 0 and exp < now:
            continue  # expired
        c: dict[str, Any] = {
            "name": r["name"],
            "value": r["value"],
            "domain": r["domain"],
            "path": r.get("path") or "/",
            "secure": bool(r.get("secure")),
            "httpOnly": bool(r.get("httpOnly")),
        }
        if exp is not None and exp > 0:
            c["expires"] = float(exp)  # session cookies (<=0) stay session
        if r.get("sameSite") in ("Lax", "Strict", "None"):
            c["sameSite"] = r["sameSite"]
        if r.get("priority") in ("Low", "Medium", "High"):
            c["priority"] = r["priority"]
        out.append(c)
    return out


# --- import + validation ------------------------------------------------------------


def import_to_profile(
    name: str,
    source: str,
    *,
    domains: list[str] | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> dict:
    """Import the active browser jar into a live profile (same-context)."""
    from . import browser as browser_mod

    if source.startswith("file:"):
        raw = json.loads(Path(source[len("file:") :]).read_text())
        rows = raw.get("cookies", raw) if isinstance(raw, dict) else raw
    else:
        b, _, prof = source.partition(":")
        rows = extract_cookies(b, prof or None)

    cdp_cookies = to_cdp_cookies(rows, domains)
    stats = getattr(extract_cookies, "stats", {})
    summary = {
        "source": source,
        "domains": domains or [],
        "extracted": len(rows),
        "undecrypted": stats.get("undecrypted", 0) if source.startswith("file:") is False else 0,
        "wouldImport": len(cdp_cookies),
        "jarFingerprint": jar_fingerprint(cdp_cookies),
        "dryRun": dry_run,
        "imported": 0,
        "validation": None,
    }

    if dry_run:
        return summary

    # The profile browser must be live: same-context guarantee.
    st = browser_mod.status_profile(name)
    if not st.get("live"):
        raise RuntimeError(
            f"profile {name!r} is not live; run `cloakctl open {name}` first "
            "(import writes into the running browser — never into a cold dir)"
    )
    if not assume_yes and not _confirm(summary):
        summary["aborted"] = True
        return summary

    from .cdp import open_client

    with open_client(name) as cdp:
        cdp.set_cookies(cdp_cookies)
        jar_after = cdp.get_cookies()
    summary["imported"] = len(cdp_cookies)
    summary["profileCookieCount"] = len(jar_after)

    meta_path_update(name, {"importHistory": {"ts": now_iso(), "source": source, "domains": domains or [], "count": len(cdp_cookies), "fingerprint": summary["jarFingerprint"]}})
    return summary


def _confirm(summary: dict) -> bool:
    print(
        f"import {summary['wouldImport']} cookies from {summary['source']} "
        f"(fingerprint {summary['jarFingerprint']}) into the live profile? [y/N]"
    )
    return input().strip().lower() in ("y", "yes")


def meta_path_update(name: str, patch: dict) -> None:
    from . import paths

    meta = paths.load_meta(name)
    for k, v in patch.items():
        if k == "importHistory":
            hist = meta.setdefault("importHistory", [])
            hist.append(v)
            del hist[:-50]  # bounded: history, not a log
        else:
            meta[k] = v
    paths.save_meta(name, meta)


BURN_MARKERS = ("delete me", "delete-me")


def classify_validation(final_url: str, body_text: str, jar: list[dict]) -> dict:
    """Classify an in-browser validation navigation. LinkedIn-aware, generic-safe."""
    low_url = (final_url or "").lower()
    low_body = (body_text or "").lower()

    burn = any(c.get("name") == "li_at" and any(m in str(c.get("value", "")).lower() for m in BURN_MARKERS) for c in jar)
    burn = burn or "li_at=delete" in low_body or "clear-site-data" in low_body
    verdict = "alive"
    if burn:
        verdict = "burn_signature"
    elif "authwall" in low_url or "/checkpoint" in low_url or "challenge" in low_url:
        verdict = "challenged"
    elif "/login" in low_url or "session-redirect" in low_url:
        verdict = "anonymous"
    else:
        logged_in_markers = ("feed-identity-module", "data-test-feed-item", "global-nav__me")
        if any(m in low_body for m in logged_in_markers):
            verdict = "logged_in"
    return {"url": final_url, "verdict": verdict}


VALIDATION_PROBE_URL = os.environ.get("CLOAKCTL_PROBE_URL", "https://www.linkedin.com/feed/")


def validate_context(name: str, probe_url: str | None = None) -> dict:
    """Navigate the live profile browser to the probe and classify the session."""
    from .cdp import open_client

    url = probe_url or VALIDATION_PROBE_URL
    with open_client(name) as cdp:
        cdp.navigate(url, load_timeout=30.0)
        final_url = cdp.evaluate("location.href") or url
        body = cdp.evaluate("document.body ? document.body.innerText.slice(0, 20000) : ''") or ""
        jar = cdp.get_cookies()
    result = {"profile": name, "probe": url, **classify_validation(final_url, body, jar)}
    meta_path_update(name, {"lastValidation": {"ts": now_iso(), **result}})
    return result
