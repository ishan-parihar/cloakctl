#!/usr/bin/env bash
# cloakctl installer — idempotent, re-runnable.
#   ./install.sh              # pipx if present, else an isolated venv
#   ./install.sh --venv       # force the venv install
#   ./install.sh --check-only # verify an existing install, install nothing
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/.local/share/cloakctl/venv}"
BIN_LINK="$HOME/.local/bin/cloakctl"
MODE="auto"

for arg in "$@"; do
  case "$arg" in
    --venv) MODE="venv" ;;
    --check-only) MODE="check" ;;
    -h|--help)
      echo "usage: ./install.sh [--venv] [--check-only]"
      echo "  default: pipx when available, isolated venv otherwise."
      exit 0 ;;
    *) echo "unknown flag: $arg (try --help)" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }

# --- 1. interpreter ---------------------------------------------------------
need python3 "install python3 (>=3.10) first"
PY_OK="$(python3 -c 'import sys; print("yes" if sys.version_info >= (3, 10) else "no")')"
[ "$PY_OK" = "yes" ] || { echo "python3 must be >= 3.10" >&2; exit 1; }

# --- 2. install -------------------------------------------------------------
if [ "$MODE" = "check" ]; then
  :
elif [ "$MODE" = "venv" ] || ! command -v pipx >/dev/null 2>&1; then
  [ "$MODE" = "auto" ] && echo "pipx not found — using an isolated venv at $VENV_DIR"
  need python3 "venv needs python3"
  python3 -m venv "$VENV_DIR" 2>/dev/null || python3 -m venv --without-pip "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip 2>/dev/null || true
  "$VENV_DIR/bin/pip" install --quiet "$ROOT"
  mkdir -p "$(dirname "$BIN_LINK")"
  ln -sf "$VENV_DIR/bin/cloakctl" "$BIN_LINK"
  echo "linked: $BIN_LINK -> $VENV_DIR/bin/cloakctl"
else
  pipx install "$ROOT" 2>/dev/null || pipx install --force "$ROOT"
fi

export PATH="$HOME/.local/bin:$PATH"
BIN="$(command -v cloakctl || true)"
[ -n "$BIN" ] || { echo "install ok but cloakctl not on PATH; add ~/.local/bin to PATH" >&2; exit 1; }

# --- 3. verify --------------------------------------------------------------
echo "cloakctl: $($BIN --version)"
echo "--- doctor (browser may be absent on a fresh box; install it next) ---"
"$BIN" doctor || true
case "$PATH" in *"$HOME/.local/bin"*) ;; *) echo "NOTE: ~/.local/bin is not on your PATH yet" ;; esac
echo "---"
echo "next: install a Chromium (cloakbrowser, chromium, chrome, brave or edge),"
echo "then: cloakctl profiles create <name> && cloakctl open <name>"
