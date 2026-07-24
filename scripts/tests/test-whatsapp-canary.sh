#!/usr/bin/env bash
# Unit tests for the pure functions in whatsapp-canary.sh (sourced, not
# dispatched — the main-guard keeps dispatch from firing).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../whatsapp-canary.sh"

fails=0
check() {
  local desc="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    echo "ok   - $desc"
  else
    echo "FAIL - $desc: got '$got', want '$want'"
    fails=$((fails + 1))
  fi
}

# canary_parse_number
check "full device JID"      "$(canary_parse_number '61494559126:4@s.whatsapp.net')" "61494559126"
check "JID without device"   "$(canary_parse_number '61494559126@s.whatsapp.net')"   "61494559126"
check "empty input"          "$(canary_parse_number '')"                              ""

# canary_decide — CANARY_REALERT_HOURS=6 → 21600s
CANARY_REALERT_HOURS=6
now=1000000
check "first failure alerts"            "$(canary_decide '' fail "$now")"                              "alert"
check "ok to fail alerts"               "$(canary_decide "ok 999000" fail "$now")"                     "alert"
check "still failing, within window"    "$(canary_decide "fail 900000 $((now - 21599))" fail "$now")"  "none"
check "still failing, window elapsed"   "$(canary_decide "fail 900000 $((now - 21600))" fail "$now")"  "realert"
check "recovery sends all-clear"        "$(canary_decide "fail 900000 990000" ok "$now")"              "allclear"
check "steady ok is quiet"              "$(canary_decide "ok 999000" ok "$now")"                       "none"
check "first ever run is quiet"         "$(canary_decide '' ok "$now")"                                "none"

if [ "$fails" -gt 0 ]; then
  echo "$fails test(s) failed" >&2
  exit 1
fi
echo "all tests passed"
