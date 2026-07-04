#!/bin/bash
# Watch the agent message channel in real-time
# Usage: ./watch_messages.sh [tak-2|hq]
AGENT="${1:-tak-2}"
KEY="jtak-0C_b030bJrlgaQYHrjtW-C-XQ2bybIffLkb08C9qheY"
URL="https://hq.jtak.club/api/v1/agent/messages"
LAST_ID=0

echo "── jTAK Agent Channel [$AGENT inbox] ─────────────────────────────"
echo "   Polling every 10s. Ctrl-C to stop."
echo ""

while true; do
  MSGS=$(curl -s -H "X-jTAK-Key: $KEY" "$URL?to_agent=$AGENT&since_id=$LAST_ID" 2>/dev/null)
  COUNT=$(echo "$MSGS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null)
  if [ "$COUNT" -gt "0" ] 2>/dev/null; then
    echo "$MSGS" | python3 -c "
import sys, json
msgs = json.load(sys.stdin)
for m in msgs:
    print(f'[{m[\"created_at\"][:19]}] {m[\"from_agent\"]} → {m[\"to_agent\"]}  #{m[\"id\"]}')
    print(f'  Subject: {m[\"subject\"]}')
    for line in m['body'].split('\n')[:6]:
        print(f'  {line}')
    if len(m['body'].split(chr(10))) > 6:
        print(f'  ... [{len(m[\"body\"])} chars total]')
    print()
"
    LAST_ID=$(echo "$MSGS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(max(m['id'] for m in d))" 2>/dev/null)
  fi
  sleep 10
done
