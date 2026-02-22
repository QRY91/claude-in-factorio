#!/usr/bin/env bash
# Push a telemetry event to the bore relay.
# Usage: ./relay_push.sh <type> <json_data>
# Example: ./relay_push.sh chat '{"role":"agent","message":"Mining iron"}'
# Example: ./relay_push.sh tool_call '{"tool":"walk_to","input":{"x":42,"y":-21}}'

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | grep -v '^$' | xargs)
fi

TYPE="${1:?Usage: relay_push.sh <type> <json_data>}"
DATA="${2:?Usage: relay_push.sh <type> <json_data>}"
AGENT="${3:-BORE-01}"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%S)"

if [ -z "$RELAY_URL" ]; then echo "Error: RELAY_URL not set (check bridge/.env)"; exit 1; fi
if [ -z "$RELAY_TOKEN" ]; then echo "Error: RELAY_TOKEN not set (check bridge/.env)"; exit 1; fi

curl -s -X POST "${RELAY_URL}/ingest" \
  -H "Authorization: Bearer ${RELAY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "[{\"type\":\"${TYPE}\",\"agent\":\"${AGENT}\",\"timestamp\":\"${TIMESTAMP}\",\"data\":${DATA}}]"
