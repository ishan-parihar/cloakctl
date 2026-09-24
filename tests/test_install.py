"""Installer integration tests for the managed Obscura binary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'obscura {version}'\n")
    path.chmod(0o755)


def _make_release(home: Path) -> tuple[Path, str]:
    """Create a fake GitHub release and return its archive and SHA-256."""
    payload = home / "payload" / "obscura"
    archive = home / "obscura.tar.gz"
    _write_executable(payload, "9.9.9")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="obscura")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    stub_dir = home / "stub-bin"
    stub_dir.mkdir(parents=True)
    curl = stub_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys

args = sys.argv[1:]
url = next((arg for arg in args if arg.startswith("http")), "")
out = args[args.index("-o") + 1] if "-o" in args else ""
release = {
    "tag_name": "v9.9.9",
    "assets": [{
        "name": "obscura-x86_64-linux-stealth.tar.gz",
        "digest": "sha256:" + os.environ["FAKE_DIGEST"],
    }],
}
if url.endswith("/releases/latest"):
    payload = json.dumps(release)
    if out == "/dev/stdout":
        sys.stdout.write(payload)
    else:
        with open(out, "w", encoding="utf-8") as stream:
            stream.write(payload)
else:
    shutil.copyfile(os.environ["FAKE_ARCHIVE"], out)
"""
    )
    curl.chmod(0o755)
    return archive, digest


def _run_installer(
    home: Path,
    archive: Path,
    digest: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "FAKE_ARCHIVE": str(archive),
            "FAKE_DIGEST": digest,
            "HOME": str(home),
            "PATH": f"{home / 'stub-bin'}:{env['PATH']}",
        }
    )
    return subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--obscura-only"],
        check=False,
        capture_output=True,
        cwd=ROOT,
        env=env,
        text=True,
        timeout=30,
    )


def _installed_version(home: Path) -> str:
    result = subprocess.run(
        [str(home / ".local" / "bin" / "obscura")],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def test_installer_replaces_existing_obscura_with_latest_verified_release(
    tmp_path: Path,
) -> None:
    # Given: an older managed binary already exists.
    _write_executable(tmp_path / ".local" / "bin" / "obscura", "0.0.1")
    archive, digest = _make_release(tmp_path)

    # When: the installer checks and updates the managed release.
    run = _run_installer(tmp_path, archive, digest)

    # Then: the verified latest binary replaces the older one.
    assert run.returncode == 0, run.stdout + run.stderr
    assert _installed_version(tmp_path) == "obscura 9.9.9"


def test_installer_rejects_checksum_mismatch_without_replacing_binary(
    tmp_path: Path,
) -> None:
    # Given: an older managed binary and a release digest that does not match.
    _write_executable(tmp_path / ".local" / "bin" / "obscura", "0.0.1")
    archive, _ = _make_release(tmp_path)

    # When: the installer attempts the update.
    run = _run_installer(tmp_path, archive, "0" * 64)

    # Then: verification fails and the working binary remains untouched.
    assert run.returncode != 0
    assert _installed_version(tmp_path) == "obscura 0.0.1"
    assert "sha256" in (run.stdout + run.stderr).lower()
