#!/bin/bash
# ── jTAK Deploy Script ────────────────────────────────────────────────────────
# Syncs code, DB migrations, and nginx config from TAK-2 to a remote hub.
#
# Usage:
#   ./deploy.sh tak3
#   ./deploy.sh tak4
#   ./deploy.sh tak1
#   ./deploy.sh all          # push to all reachable peers
#   ./deploy.sh 172.24.x.x   # push to a specific IP

set -euo pipefail

JTAK=/opt/jtak
DB=/opt/jtak/data/jtak.db
USER=sdg

# ── Hub registry ──────────────────────────────────────────────────────────────
declare -A HUB_IPS=(
  [tak1]="172.24.29.220"
  [tak2]="172.24.236.22"
  [tak3]="172.24.136.116"
  [tak4]="172.24.94.122"
  [tak5]="172.24.13.14"
)
declare -A HUB_HOSTS=(
  [tak1]="jtak1.local"
  [tak2]="jtak2.local"
  [tak3]="jtak3.local"
  [tak4]="jtak4.local"
  [tak5]="jtak5.local"
)

# ── Argument parsing ──────────────────────────────────────────────────────────
if [ $# -eq 0 ]; then
  echo "Usage: $0 <tak1|tak2|tak3|tak4|all|IP>"
  exit 1
fi

TARGETS=()
if [ "$1" = "all" ]; then
  for hub in tak1 tak3 tak4; do TARGETS+=("$hub"); done
else
  TARGETS=("$1")
fi

# ── Deploy function ───────────────────────────────────────────────────────────
deploy_to() {
  local target="$1"
  local ip name

  if [[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ip="$target"
    name="$target"
  else
    ip="${HUB_IPS[$target]:-}"
    name="$target"
    if [ -z "$ip" ]; then echo "Unknown hub: $target"; return 1; fi
  fi

  echo ""
  echo "══════════════════════════════════════════════"
  echo "  Deploying to $name ($ip)"
  echo "══════════════════════════════════════════════"

  # ── Connectivity check ───────────────────────────────────────────────────
  if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$USER@$ip" true 2>/dev/null; then
    echo "  ✗ Cannot reach $ip — skipping"
    return 1
  fi

  # ── 1. Sync code ─────────────────────────────────────────────────────────
  echo "  → Syncing code..."
  # Ensure db/ directory exists on remote (may be root-owned dirs)
  ssh "$USER@$ip" "sudo mkdir -p $JTAK/db && sudo chown $USER $JTAK/db"

  # --rsync-path="sudo rsync" lets us write to root-owned dirs (led/, scripts/, ingest/)
  rsync -rl --checksum \
    --no-perms --no-owner --no-group \
    --rsync-path="sudo rsync" \
    --exclude='config/' \
    --exclude='data/' \
    --exclude='venv/' \
    --exclude='logs/' \
    --exclude='ots-plugin/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    "$JTAK/" "$USER@$ip:$JTAK/"

  # ── 2. DB migrations ─────────────────────────────────────────────────────
  echo "  → Running DB migrations..."
  # Use python3 (sqlite3 CLI not always installed); errors on existing tables/columns are safe
  ssh "$USER@$ip" "python3 - $DB" << 'PYEOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA journal_mode=WAL")
with open("/opt/jtak/db/migrations.sql") as f:
    sql = f.read()
for stmt in sql.split(";"):
    # Strip comment lines so a leading comment doesn't skip the whole chunk
    lines = [l for l in stmt.splitlines() if not l.strip().startswith("--")]
    stmt = "\n".join(lines).strip()
    if not stmt:
        continue
    try:
        db.execute(stmt)
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "duplicate column" in msg or "already exists" in msg:
            pass  # migration already applied
        else:
            print(f"  WARN: {msg} — [{stmt[:60]}]")
db.commit()
print("  migrations ok")
PYEOF

  # ── 3. Nginx config ───────────────────────────────────────────────────────
  echo "  → Installing nginx config..."
  local hostname cert key
  hostname="${HUB_HOSTS[$name]:-}"

  if [ -n "$hostname" ]; then
    # Detect the actual cert name on the remote hub
    cert=$(ssh "$USER@$ip" "ls /etc/nginx/${name}-local.crt /etc/nginx/jtak-local.crt 2>/dev/null | head -1 || echo /etc/nginx/${name}-local.crt")
    key="${cert%.crt}.key"

    sed -e "s|__HOSTNAME__|$hostname|g" \
        -e "s|__CERT__|$cert|g" \
        -e "s|__KEY__|$key|g" \
        "$JTAK/config/nginx/jtak-local.conf" \
      | ssh "$USER@$ip" "sudo tee /etc/nginx/sites-enabled/jtak-local > /dev/null"

    # Remove OTS nginx configs if still present
    ssh "$USER@$ip" "
      for f in ots_http ots_https ots_certificate_enrollment; do
        if [ -f /etc/nginx/sites-enabled/\$f ]; then
          sudo rm /etc/nginx/sites-enabled/\$f
          echo '     Removed OTS nginx: '\$f
        fi
      done
    "

    if ssh "$USER@$ip" "sudo nginx -t 2>/dev/null"; then
      ssh "$USER@$ip" "sudo systemctl reload nginx"
      echo "     nginx reloaded"
    else
      echo "     WARN: nginx config test failed — skipping reload"
    fi
  else
    echo "     (skipping nginx — no hostname for $name)"
  fi

  # ── 4. Config additions ───────────────────────────────────────────────────
  echo "  → Checking jtak.yaml..."
  ssh "$USER@$ip" "
    if ! grep -q '^\s*- waypoint' /opt/jtak/config/jtak.yaml; then
      sudo sed -i '/^  - led_ctrl/i\\  - waypoint' /opt/jtak/config/jtak.yaml
      echo '     Added waypoint to hud.chips'
    fi
    if ! grep -q '^sounds:' /opt/jtak/config/jtak.yaml; then
      printf '\nsounds:\n  enabled: true\n  volume: 0.6\n  direct_message: chime\n  channel_message: ping\n  waypoint: drop\n' | sudo tee -a /opt/jtak/config/jtak.yaml > /dev/null
      echo '     Added sounds config'
    fi
  "

  # ── 5. Python packages ────────────────────────────────────────────────────
  ssh "$USER@$ip" "
    if ! /opt/jtak/venv/bin/python3 -c 'import qrcode' 2>/dev/null; then
      sudo /opt/jtak/venv/bin/pip install qrcode --quiet
      echo '     Installed qrcode'
    fi
  "

  # ── 6. Restart services ───────────────────────────────────────────────────
  echo "  → Restarting services..."
  ssh "$USER@$ip" "sudo systemctl restart jtak-api tactical_monitor && sleep 2 && systemctl is-active jtak-api tactical_monitor"

  echo "  ✓ $name done"
}

# ── Run ───────────────────────────────────────────────────────────────────────
FAILED=()
for t in "${TARGETS[@]}"; do
  deploy_to "$t" || FAILED+=("$t")
done

echo ""
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "✓ All deployments successful"
else
  echo "✗ Failed: ${FAILED[*]}"
  exit 1
fi
