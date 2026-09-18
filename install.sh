#!/usr/bin/env bash
# cloakctl installer — idempotent, re-runnable.
#
#   ./install.sh                       # cloakctl + obscura (default engine)
#   ./install.sh --engine cloakbrowser # opt-in: skip obscura, need a Chromium
#   ./install.sh --obscura-only        # just fetch the obscura binary
#   ./install.sh --obscura-build <flavor>  # stealth|no-render-stealth|plain|no-render (default: stealth)
#   ./install.sh --venv                # force the venv install for cloakctl
#   ./install.sh --check-only          # verify an existing install, install nothing
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$HOME/.local/share/cloakctl/venv}"
BIN_LINK="$HOME/.local/bin/cloakctl"
MODE="auto"
ENGINE="obscura"           # default engine: obscura (cloakbrowser is opt-in)
OBSCURA_BUILD="stealth"
OBSCURA_ONLY=0
PINNED_TAG="v0.2.2"        # fallback when the GitHub API is unreachable

usage() {
  echo "usage: ./install.sh [--engine obscura|cloakbrowser] [--obscura-only]"
  echo "                    [--obscura-build stealth|no-render-stealth|plain|no-render]"
  echo "                    [--venv] [--check-only]"
  echo "  default: installs cloakctl AND the obscura binary (cloakctl's default engine)."
  echo "  --engine cloakbrowser opts out of obscura and expects a Chromium-family browser."
  exit 0
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --venv) MODE="venv" ;;
    --check-only) MODE="check" ;;
    --obscura-only) OBSCURA_ONLY=1 ;;
    --engine=*) ENGINE="${1#*=}" ;;
    --engine) ENGINE="${2:-}"; shift ;;
    --obscura-build=*) OBSCURA_BUILD="${1#*=}" ;;
    --obscura-build) OBSCURA_BUILD="${2:-}"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

case "$ENGINE" in
  obscura|cloakbrowser) ;;
  *) echo "--engine must be 'obscura' (default) or 'cloakbrowser'" >&2; exit 2 ;;
esac
case "$OBSCURA_BUILD" in
  stealth|no-render-stealth|plain|no-render) ;;
  *) echo "--obscura-build must be stealth|no-render-stealth|plain|no-render" >&2; exit 2 ;;
esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }

fetch() { # fetch <url> <out>
  if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 300 "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qT 300 -O "$2" "$1"
  else return 1; fi
}

# --- 0. obscura binary (the default engine) ---------------------------------
install_obscura() {
  local os arch flavor asset url tag tmp
  case "$(uname -s)" in
    Linux) os="linux" ;;
    Darwin) os="macos" ;;
    MINGW*|MSYS*|CYGWIN*) os="windows" ;;
    *) echo "obscura: unsupported OS $(uname -s) — install it manually (github.com/h4ckf0r0day/obscura)" >&2; return 1 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) echo "obscura: unsupported arch $(uname -m)" >&2; return 1 ;;
  esac
  if [ "$os" = "windows" ]; then asset="obscura-${arch}-${os}-${OBSCURA_BUILD}.zip"
  else asset="obscura-${arch}-${os}-${OBSCURA_BUILD}.tar.gz"; fi
  url="https://github.com/h4ckf0r0day/obscura/releases/download/${PINNED_TAG}/${asset}"
  # latest tag when the API is reachable (asset names are stable across tags)
  tag="$(fetch https://api.github.com/repos/h4ckf0r0day/obscura/releases/latest /dev/stdout 2>/dev/null \
        | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  if [ -n "${tag:-}" ]; then url="${url/$PINNED_TAG/$tag}"; fi

  echo "obscura: downloading ${asset}${tag:+ ($tag)}..."
  tmp="$(mktemp -d)"
  if ! fetch "$url" "$tmp/obscura-archive"; then
    echo "obscura: download failed — check your network or grab it manually:" >&2
    echo "  $url" >&2
    rm -rf "$tmp"; return 1
  fi
  mkdir -p "$HOME/.local/bin"
  if [ "$os" = "windows" ]; then
    unzip -o -q "$tmp/obscura-archive" -d "$HOME/.local/bin"
  else
    tar -xzf "$tmp/obscura-archive" -C "$tmp"
    for bin in "$tmp"/obscura*; do
      [ -f "$bin" ] || continue
      install -m 0755 "$bin" "$HOME/.local/bin/$(basename "$bin")"
    done
  fi
  rm -rf "$tmp"
  command -v obscura >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
  if command -v obscura >/dev/null 2>&1; then
    echo "obscura: installed -> $(command -v obscura)"
  else
    echo "obscura: installed to $HOME/.local/bin (add it to PATH)" >&2
  fi
}

if [ "$MODE" != "check" ] && [ "$ENGINE" = "obscura" ]; then
  if command -v obscura >/dev/null 2>&1 || [ -x "$HOME/.local/bin/obscura" ]; then
    echo "obscura: already present ($(command -v obscura || echo "$HOME/.local/bin/obscura"))"
  else
    install_obscura || echo "continuing without obscura — cloakctl will fail to open profiles until it is installed" >&2
  fi
fi

# --- 1. interpreter ---------------------------------------------------------
if [ "$OBSCURA_ONLY" = "1" ]; then
  echo "obscura-only install complete."
  exit 0
fi

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
  ln -sf "$VENV_DIR/bin/cloakctl-mcp" "$BIN_LINK-mcp"
  echo "linked: $BIN_LINK -> $VENV_DIR/bin/cloakctl"
else
  pipx install "$ROOT" 2>/dev/null || pipx install --force "$ROOT"
fi

export PATH="$HOME/.local/bin:$PATH"
BIN="$(command -v cloakctl || true)"
[ -n "$BIN" ] || { echo "install ok but cloakctl not on PATH; add ~/.local/bin to PATH" >&2; exit 1; }
BINMCP="$(command -v cloakctl-mcp || true)"
[ -n "$BINMCP" ] || BINMCP="$VENV_DIR/bin/cloakctl-mcp"

# --- 3. verify --------------------------------------------------------------
echo "cloakctl: $($BIN --version)"
echo "mcp server: $BINMCP (stdio; register with your agent harness)"
echo "--- doctor ---"
"$BIN" doctor || true
case "$PATH" in *"$HOME/.local/bin"*) ;; *) echo "NOTE: ~/.local/bin is not on your PATH yet" ;; esac
echo "---"
# --- 4. agent-harness integrations (best-effort, never fatal) ----------------
# Each supported harness gets its plugin/registration if that harness is
# present. Nothing here can fail the install.
echo "--- agent integrations ---"
if [ -d "$HOME/.hermes/hermes-agent/plugins/browser" ]; then
  DEST="$HOME/.hermes/hermes-agent/plugins/browser/cloakctl"
  mkdir -p "$DEST"
  cp -f "$ROOT/integrations/hermes/browser-cloakctl/"*.py "$DEST/" 2>/dev/null || true
  cp -f "$ROOT/integrations/hermes/browser-cloakctl/plugin.yaml" "$DEST/" 2>/dev/null || true
  echo "hermes: plugin synced to $DEST (browser.cloud_provider: cloakctl)"
else
  echo "hermes: not detected — install later with:"
  echo "  cp -r integrations/hermes/browser-cloakctl ~/.hermes/hermes-agent/plugins/browser/cloakctl"
fi
if [ -f "$HOME/.config/opencode/opencode.json" ]; then
  echo "opencode: add the MCP server (merge into ~/.config/opencode/opencode.json):"
  echo '  {"mcp": {"cloakctl": {"type": "local", "command": ["cloakctl-mcp"]}}} '
fi
if [ -f "$HOME/.omp/agent.toml" ] || [ -d "$HOME/.omp" ]; then
  echo "omp: register the MCP server in ~/.omp/agent.toml:"
  echo '  [mcpServers.cloakctl]'
  echo '  command = "cloakctl-mcp"'
fi
if [ "${CODEX_HOME:-}" ] || [ -d "$HOME/.codex" ]; then
  echo "codex: register the MCP server in ~/.codex/config.toml:"
  echo '  [mcp_servers.cloakctl]'
  echo '  command = "cloakctl-mcp"'
fi
if [ -f "$HOME/.claude.json" ] || [ -d "$HOME/.claude" ]; then
  echo "claude code: register the MCP server:"
  echo '  claude mcp add cloakctl -- cloakctl-mcp'
fi

echo "--- done ---"
if [ "$ENGINE" = "obscura" ]; then
  echo "default engine: obscura — just: cloakctl profiles create <name> && cloakctl open <name>"
  echo "opt into the Chromium engine any time: cloakctl open <name> --engine cloakbrowser"
else
  echo "engine: cloakbrowser (opt-in). Install a Chromium-family browser"
  echo "(cloakbrowser, chromium, chrome, brave or edge) if doctor shows none,"
  echo "then: cloakctl profiles create <name> && cloakctl open <name> --engine cloakbrowser"
fi
