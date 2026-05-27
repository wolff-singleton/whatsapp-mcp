#!/usr/bin/env bash
# SessionEnd hook: dump the full raw session transcript to conversations/.
# Claude Code passes a JSON payload on stdin with .transcript_path and .cwd.
set -euo pipefail

payload="$(cat)"
transcript_path="$(printf '%s' "$payload" | jq -r '.transcript_path // empty')"
cwd="$(printf '%s' "$payload" | jq -r '.cwd // empty')"

if [[ -z "$transcript_path" || ! -f "$transcript_path" ]]; then
  echo "save-session: no transcript_path in payload" >&2
  exit 0
fi

project_dir="${cwd:-$PWD}"
out_dir="$project_dir/conversations"
mkdir -p "$out_dir"

session_id="$(basename "$transcript_path" .jsonl)"
short_id="${session_id:0:8}"
stamp="$(date +%Y-%m-%d-%H%M%S)"

ai_title="$(jq -r 'select(.type == "ai-title") | .aiTitle' "$transcript_path" 2>/dev/null | tail -n 1)"
slug="$(printf '%s' "${ai_title:-}" \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' \
  | cut -c1-50 \
  | sed -E 's/-+$//')"

if [[ -z "$ai_title" ]]; then
  echo "save-session: no aiTitle — skipping transcript for session $short_id" >&2
  exit 0
fi

out_file="$out_dir/${stamp}-${slug}.txt"

jq -r '
  select(.type == "user" or .type == "assistant")
  | "\n\n===== " + (.type | ascii_upcase) + " [" + (.timestamp // "") + "] =====\n\n"
    + (.message.content
       | if type == "string" then .
         else (map(
           if .type == "text" then .text
           elif .type == "tool_use" then "[tool_use: " + .name + "]\n" + (.input | tostring)
           elif .type == "tool_result" then "[tool_result]\n" + (if (.content|type)=="string" then .content else (.content|tostring) end)
           elif .type == "thinking" then "[thinking]\n" + .thinking
           else "[" + .type + "]" end
         ) | join("\n"))
       end)
' "$transcript_path" > "$out_file"

echo "save-session: wrote $out_file" >&2
