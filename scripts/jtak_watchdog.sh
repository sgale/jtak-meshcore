#!/bin/bash
# jTAK Watchdog — checks meshtasticd packet flow and jtak-api health
# Restarts services if stale; reboots hub if restart fails
# Runs every 5 min via systemd timer

LOG=/opt/jtak/logs/watchdog.log
DB=/opt/jtak/data/jtak.db
STALE_MIN=10        # minutes without a packet before restart
REBOOT_MIN=15       # minutes without a packet after restart before reboot
API_URL="http://127.0.0.1:8420/jtak/api/api/status"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [watchdog] $*" | tee -a "$LOG"; }

mkdir -p "$(dirname $LOG)"

# ── meshtasticd packet flow check ────────────────────────────────────────────
last_packet=$(python3 -c "
import sqlite3, datetime
try:
    db = sqlite3.connect('$DB')
    row = db.execute("SELECT timestamp FROM rf_metrics WHERE direct_or_relay != 'SELF' ORDER BY rowid DESC LIMIT 1").fetchone()
    if row:
        ts = datetime.datetime.fromisoformat(row[0].replace('Z','+00:00'))
        age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60
        print(f'{age:.1f}')
    else:
        print('999')
except Exception as e:
    print('999')
" 2>/dev/null)

stale=$(echo "$last_packet > $STALE_MIN" | bc -l 2>/dev/null)

if [ "$stale" = "1" ]; then
    log "No packets for ${last_packet}m (threshold ${STALE_MIN}m) — restarting meshtasticd"

    # Kill any zombie processes holding GPIOs
    pkill -9 -x meshtasticd 2>/dev/null
    sleep 2

    # Ensure gpsnmea-pty (GPS PTY bridge) is up first — meshtasticd requires it
    if systemctl cat gpsnmea-pty &>/dev/null && ! systemctl is-active --quiet gpsnmea-pty; then
        log "gpsnmea-pty is down — restarting it first"
        systemctl restart gpsnmea-pty
        sleep 3
    fi

    systemctl restart meshtasticd
    log "meshtasticd restarted"

    # Wait and recheck
    sleep 90
    last_after=$(python3 -c "
import sqlite3, datetime
try:
    db = sqlite3.connect('$DB')
    row = db.execute("SELECT timestamp FROM rf_metrics WHERE direct_or_relay != 'SELF' ORDER BY rowid DESC LIMIT 1").fetchone()
    if row:
        ts = datetime.datetime.fromisoformat(row[0].replace('Z','+00:00'))
        age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60
        print(f'{age:.1f}')
    else:
        print('999')
except:
    print('999')
" 2>/dev/null)

    still_stale=$(echo "$last_after > $REBOOT_MIN" | bc -l 2>/dev/null)
    if [ "$still_stale" = "1" ]; then
        log "Still no packets after restart (${last_after}m stale) — rebooting hub"
        systemctl reboot
    else
        log "Packets flowing again after restart (${last_after}m ago)"
    fi
else
    : # healthy — last packet ${last_packet}m ago
fi

# ── jtak-api health check ─────────────────────────────────────────────────────
api_ok=$(curl -sf --max-time 5 "$API_URL" > /dev/null 2>&1; echo $?)
if [ "$api_ok" != "0" ]; then
    log "jtak-api not responding — restarting"
    systemctl restart jtak-api
    sleep 30
    api_ok2=$(curl -sf --max-time 5 "$API_URL" > /dev/null 2>&1; echo $?)
    if [ "$api_ok2" = "0" ]; then
        log "jtak-api recovered after restart"
    else
        log "jtak-api still down after restart — rebooting hub"
        systemctl reboot
    fi
fi
