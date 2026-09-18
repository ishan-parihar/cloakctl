#!/usr/bin/env bash
# cloakctl smoke — end-to-end check on an isolated state dir.
# Verifies the installed CLI: binary, doctor, engine detection, profile
# create/open/exec/close, teardown. Uses a temp CLOAKCTL_HOME — never touches
# the operator's ~/.cloakctl. Works for both engines: obscura (default) and
# cloakbrowser (opt-in Chromium). Without any browser binary, reports
# doctor-only green.
# Usage: scripts/smoke.sh   (exit 0 = green)
set -uo pipefail

BIN="${CLOAKCTL_BIN:-$(command -v cloakctl || true)}"
[ -n "$BIN" ] || { echo '{"error": "cloakctl not on PATH (run ./install.sh)"}'; exit 1; }

TMP="$(mktemp -d /tmp/cloakctl-smoke-XXXXXX)"
export CLOAKCTL_HOME="$TMP/state"
PASS=0; FAIL=0

check() { # check <name> <rc>
  if [ "$2" = "0" ]; then PASS=$((PASS+1)); echo "[PASS] $1"
  else FAIL=$((FAIL+1)); echo "[FAIL] $1"; fi
}

# 1. binary + version
"$BIN" --version >/dev/null 2>&1; check "binary responds (--version)" "$?"

# 2. doctor (JSON) — tolerates a missing engine binary
DOCTOR="$("$BIN" doctor --json 2>/dev/null)"
check "doctor --json clean" "$?"
echo "$DOCTOR" | grep -q '"defaultEngine": *"obscura"'; check "default engine is obscura" "$?"

# 3. profile lifecycle on the isolated HOME (needs an engine binary)
OBSCURA_BIN="$(command -v obscura || true)"
CHROMIUM_BIN="$(command -v cloakbrowser || command -v chromium || command -v chrome || command -v brave || command -v edge || true)"
if [ -n "$OBSCURA_BIN" ] || [ -n "$CHROMIUM_BIN" ]; then
  "$BIN" profiles create smoke --json >/dev/null 2>&1; check "profiles create" "$?"
  OPEN="$("$BIN" open smoke --json 2>/dev/null)"
  echo "$OPEN" | grep -q '"engine": *"obscura"'; check "open reports engine" "$?"
  echo "$OPEN" | grep -q '"cdpPort": *[0-9]'; check "open (prints cdpPort)" "$?"
  "$BIN" exec smoke "location.href" --json >/dev/null 2>&1; check "exec round-trip" "$?"
  "$BIN" navigate smoke --url "about:blank" --json >/dev/null 2>&1; check "navigate" "$?"
  "$BIN" status smoke --json | grep -q '"live": true'; check "status live" "$?"
  "$BIN" close smoke --json >/dev/null 2>&1; check "close" "$?"
  sleep 1
  LEFT=$(pgrep -af "cloakctl-smoke" | grep -v pgrep | grep -cv "grep" || true)
  [ "${LEFT:-0}" = "0" ]; check "teardown clean (no strays)" "$?"
else
  echo "[SKIP] engine lifecycle (no obscura and no Chromium found — run ./install.sh, then re-run)"
fi

rm -rf "$TMP"
echo "---- smoke: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
