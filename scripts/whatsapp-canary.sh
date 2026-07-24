#!/usr/bin/env bash
# whatsapp-canary.sh — end-to-end canary for the WhatsApp bridge.
#
# Sends a timestamped message from the bridge's own account to itself via
# POST /api/send. The bridge only reports success after WhatsApp's server
# acks the message, so one probe proves the whole delivery path: REST API up,
# bearer token valid, session authenticated, server round-trip OK. This
# catches the states a websocket-only health check misses (logged-out with
# the container up; unlinked QR-limbo where the socket is open but no
# session exists).
#
# On failure it emails an alert (deduped: once on the ok→fail transition,
# then every CANARY_REALERT_HOURS while still failing, all-clear on
# recovery). On success it pings CANARY_HEARTBEAT_URL if set, so an external
# dead-man's switch (e.g. healthchecks.io) alerts when this canary itself
# stops running.
#
# Scheduled by scripts/systemd/whatsapp-canary.timer (every 30 min).
# Dispatch: whatsapp-canary.sh [run|send|self-number]   (default: run)
set -euo pipefail

# gmail-pro-cli-pp-cli lives in ~/go/bin; systemd user units start with a
# minimal PATH that omits it.
PATH="$HOME/go/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

CANARY_CONFIG="${CANARY_CONFIG:-$HOME/.config/whatsapp-canary/config.env}"
# shellcheck disable=SC1090
[ -f "$CANARY_CONFIG" ] && source "$CANARY_CONFIG"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANARY_STORE_DIR="${CANARY_STORE_DIR:-$SCRIPT_DIR/../whatsapp-bridge/store}"
CANARY_API_URL="${CANARY_API_URL:-http://localhost:8080/api}"
CANARY_EMAIL_TO="${CANARY_EMAIL_TO:-nathaniel@nathanielwolff.com}"
CANARY_EMAIL_FROM="${CANARY_EMAIL_FROM:-robot-arn@nathanielwolff.com}"
CANARY_HEARTBEAT_URL="${CANARY_HEARTBEAT_URL:-}"
CANARY_REALERT_HOURS="${CANARY_REALERT_HOURS:-6}"
CANARY_STATE_DIR="${CANARY_STATE_DIR:-$HOME/.local/state/whatsapp-canary}"
STATE_FILE="$CANARY_STATE_DIR/state"
LOG_FILE="$CANARY_STATE_DIR/canary.log"

canary_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_FILE"
}

# --- self-number: which account is paired right now -------------------------

# Pure: "61494559126:4@s.whatsapp.net" → "61494559126". Empty in, empty out.
canary_parse_number() {
  local jid="$1"
  jid="${jid%%@*}"
  printf '%s' "${jid%%:*}"
}

# Reads the paired JID from the whatsmeow device store (read-only). Empty
# output means no account is linked — itself a failure the caller reports.
canary_self_number() {
  local jid
  jid="$(sqlite3 "file:$CANARY_STORE_DIR/whatsapp.db?mode=ro" \
    'SELECT jid FROM whatsmeow_device LIMIT 1;' 2>/dev/null || true)"
  canary_parse_number "$jid"
}

canary_token() {
  local token_file="$CANARY_STORE_DIR/.bridge-token"
  [ -f "$token_file" ] && tr -d '\r\n' < "$token_file" || true
}

# --- probe -------------------------------------------------------------------

# Sends "[canary] <UTC timestamp>" to the bridge's own account. Prints a
# failure reason on stdout and returns non-zero if anything in the path is
# broken.
canary_send() {
  local self token auth=() payload response
  self="$(canary_self_number)"
  if [ -z "$self" ]; then
    printf 'no account paired (whatsmeow_device is empty)'
    return 1
  fi
  token="$(canary_token)"
  [ -n "$token" ] && auth=(-H "Authorization: Bearer $token")
  payload="$(jq -nc --arg r "$self" \
    --arg m "[canary] $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{recipient:$r, message:$m}')"
  if ! response="$(curl -fsS -m 25 -X POST "$CANARY_API_URL/send" \
      -H "Content-Type: application/json" "${auth[@]}" -d "$payload" 2>&1)"; then
    printf 'send request failed: %s' "$response"
    return 1
  fi
  if [ "$(printf '%s' "$response" | jq -r '.success' 2>/dev/null)" != "true" ]; then
    printf 'bridge rejected send: %s' "$response"
    return 1
  fi
  return 0
}

# --- alerting ----------------------------------------------------------------

canary_email() {
  local subject="$1" body="$2"
  command -v gmail-pro-cli-pp-cli >/dev/null 2>&1 || {
    canary_log "EMAIL SKIPPED (no gmail CLI): $subject"
    return 1
  }
  printf 'From: %s\r\nTo: %s\r\nSubject: %s\r\n\r\n%s\r\n' \
    "$CANARY_EMAIL_FROM" "$CANARY_EMAIL_TO" "$subject" "$body" \
    | gmail-pro-cli-pp-cli messages send --message-file - --agent \
      >> "$LOG_FILE" 2>&1
}

# Pure decision: given previous state line ("ok <epoch>" / "fail <first>
# <last_alert>" / empty), the outcome ("ok"/"fail") and now-epoch, prints
# the action to take: alert | realert | allclear | none.
canary_decide() {
  local prev="$1" outcome="$2" now="$3"
  local prev_status last_alert
  prev_status="${prev%% *}"
  if [ "$outcome" = "fail" ]; then
    if [ "$prev_status" != "fail" ]; then
      printf 'alert'
    else
      last_alert="${prev##* }"
      if [ $((now - last_alert)) -ge $((CANARY_REALERT_HOURS * 3600)) ]; then
        printf 'realert'
      else
        printf 'none'
      fi
    fi
  else
    if [ "$prev_status" = "fail" ]; then printf 'allclear'; else printf 'none'; fi
  fi
}

# --- orchestration -------------------------------------------------------------

canary_run() {
  mkdir -p "$CANARY_STATE_DIR"
  local now prev="" outcome reason="" action first_fail
  now="$(date +%s)"
  [ -f "$STATE_FILE" ] && prev="$(cat "$STATE_FILE")"

  if reason="$(canary_send)"; then
    outcome=ok
  else
    outcome=fail
  fi

  action="$(canary_decide "$prev" "$outcome" "$now")"
  case "$action" in
    alert)
      canary_email "🔴 WhatsApp bridge DOWN" \
        "Canary self-message failed on $(hostname): $reason. Autopilot WhatsApp pings will silently fail until the bridge is re-linked (see whatsapp-mcp repo)." || true
      printf 'fail %s %s\n' "$now" "$now" > "$STATE_FILE"
      ;;
    realert)
      first_fail="$(printf '%s' "$prev" | awk '{print $2}')"
      canary_email "🔴 WhatsApp bridge STILL DOWN" \
        "Canary has been failing since $(date -d "@$first_fail" -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '%s' "$first_fail"). Latest: $reason" || true
      printf 'fail %s %s\n' "$first_fail" "$now" > "$STATE_FILE"
      ;;
    allclear)
      canary_email "🟢 WhatsApp bridge recovered" \
        "Canary self-message delivering again on $(hostname)." || true
      printf 'ok %s\n' "$now" > "$STATE_FILE"
      ;;
    none)
      if [ "$outcome" = "ok" ]; then
        printf 'ok %s\n' "$now" > "$STATE_FILE"
      else
        printf '%s\n' "$prev" > "$STATE_FILE"
      fi
      ;;
  esac

  if [ "$outcome" = "ok" ]; then
    canary_log "ok (action=$action)"
    [ -n "$CANARY_HEARTBEAT_URL" ] && curl -fsS -m 10 "$CANARY_HEARTBEAT_URL" >/dev/null 2>&1 || true
    return 0
  fi
  canary_log "FAIL (action=$action): $reason"
  return 1
}

# --- dispatch ------------------------------------------------------------------

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-run}" in
    run)         canary_run ;;
    send)        canary_send && echo "sent ok" ;;
    self-number) canary_self_number; echo ;;
    *) echo "usage: $0 [run|send|self-number]" >&2; exit 2 ;;
  esac
fi
