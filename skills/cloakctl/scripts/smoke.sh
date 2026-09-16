#!/usr/bin/env bash
# cloakctl smoke — end-to-end check on an isolated state dir.
# Verifies the installed CLI: binary, doctor, profile create/open/exec/close,
# teardown. Uses a temp CLOAKCTL_HOME — never touches the operator's ~/.cloakctl.
# Needs a Chromium for the browser half; without one, reports doctor-only green.
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

# 2. doctor (JSON) — tolerates a missing browser
"$BIN" doctor --json >/dev/null 2>&1; check "doctor --json clean" "$?"

# 3. profile lifecycle on the isolated HOME (needs a browser)
if command -v cloakbrowser >/dev/null 2>&1 || [ -n "$(command -v chromium || command -v chrome || command -v brave || command -v edge)" ]; then
  "$BIN" profiles create smoke --json >/dev/null 2>&1; check "profiles create" "$?"
  OPEN=$("$BIN" open smoke --json 2>/dev/null)
  echo "$OPEN" | grep -q '"live": true\|"wsEndpoint"' ; check "open (prints wsEndpoint)" "$?"
  "$BIN" exec smoke "location.href" --json >/dev/null 2>&1; check "exec round-trip" "$?"
  "$BIN" status smoke --json | grep -q '"live": true'; check "status live" "$?"
  "$BIN" close smoke --json >/dev/null 2>&1; check "close" "$?"
  sleep 1
  LEFT=$(pgrep -af "cloakctl-smoke" | grep -v pgrep | grep -cv "grep" || true)
  [ "${LEFT:-0}" = "0" ]; check "teardown clean (no strays)" "$?"
else
  echo "[SKIP] browser lifecycle (no Chromium found — install one, then re-run)"
fi

rm -rf "$TMP"
echo "---- smoke: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
