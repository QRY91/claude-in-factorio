#!/bin/bash
# Detached bridge launcher. Arg: agent spec, e.g. "--agents doug-nauvis"
cd "$HOME/projects/claude-in-factorio" || exit 1
mkdir -p logs
pkill -f "bridge/pipe.py" 2>/dev/null
sleep 1
rm -f bridge/.session-*.json
# Secrets from the pass vault (decrypted at launch; no plaintext in .env).
# os.environ wins over bridge/.env in pipe.py, so these take effect.
export RELAY_TOKEN="$(gpg -d --quiet "$HOME/.password-store/relay/token.gpg" 2>/dev/null)"
export RELAY_URL="$(gpg -d --quiet "$HOME/.password-store/relay/url.gpg" 2>/dev/null)"
[ -z "$RELAY_TOKEN" ] && echo "WARN: RELAY_TOKEN empty (vault decrypt failed?) — telemetry relay disabled"
# Shared "doug" Anthropic key (same vault). See ~/.config/doug/env.sh.
export ANTHROPIC_API_KEY="$(gpg -d --quiet "$HOME/.password-store/anthropic/doug.gpg" 2>/dev/null)"
TS=$(date +%Y%m%d-%H%M%S)
LOG="logs/bridge-$TS.log"
ln -sf "bridge-$TS.log" logs/bridge.log
setsid bash -lc "exec python3 -u bridge/pipe.py $* >> '$LOG' 2>&1" < /dev/null &
echo "bridge launched: pipe.py $* (history: $LOG | live: logs/bridge.log)"
