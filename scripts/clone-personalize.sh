#!/bin/bash
# ── jTAK MeshCore clone personalizer ──────────────────────────────────────────
# Turns a freshly-cloned NVME (an exact copy of another MeshCore hub) into a
# unique hub. Run ONCE, as root, on the NEW hardware after the clone boots.
#
#   sudo /opt/jtak/scripts/clone-personalize.sh <hub-number> [options]
#   sudo /opt/jtak/scripts/clone-personalize.sh 2
#
# From <hub-number> N it derives:  name=jTAK-MCoreN  hostname=mcoreN
#                                  hub_id=mcoreN     guid=mcore000N
# Override any of those with the flags below.
#
# Options:
#   --name NAME        mesh/dashboard name        (default jTAK-MCore<N>)
#   --hostname HOST    system hostname            (default mcore<N>)
#   --hub-id ID        HQ hub id                  (default mcore<N>)
#   --guid GUID        dashboard guid             (default mcore000<N>)
#   --keep-db          keep jtak.db + RF logs (default: wiped for a clean hub)
#   --yes              don't prompt for confirmation
#
# WHY (MeshCore vs Meshtastic): MeshCore's identity is an Ed25519 seed FILE on
# the disk (meshcore.seed_file). A clone shares it -> both hubs get the SAME mesh
# public key = same node = advert/ACK/routing collisions. This script deletes the
# seed so the service mints a fresh unique identity on next start. It also bumps
# the name/guid/hub_id, regenerates host identity (hostname, SSH host keys,
# machine-id), and wipes learned state. The CHANNEL SECRET and radio config are
# left untouched (the fleet shares one private mesh).
set -euo pipefail

JTAK=/opt/jtak
YAML=$JTAK/config/jtak.yaml
IDENT=$JTAK/config/jtak.identity.yaml
OWNER=sdg          # owner of /opt/jtak + the pymc files
PYMC_USER=sdg

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)."
[ -f "$YAML" ] || die "$YAML not found — is this a jTAK hub?"

# ── Parse args ────────────────────────────────────────────────────────────────
[ $# -ge 1 ] || die "usage: $0 <hub-number> [--name .. --hostname .. --hub-id .. --guid .. --keep-db --yes]"
N="$1"; shift
[[ "$N" =~ ^[0-9]+$ ]] || die "hub-number must be an integer (got '$N')."

NAME="jTAK-MCore${N}"
HOST="mcore${N}"
HUB_ID="mcore${N}"
GUID="$(printf 'mcore%04d' "$N")"
KEEP_DB=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name)     NAME="$2"; shift 2;;
    --hostname) HOST="$2"; shift 2;;
    --hub-id)   HUB_ID="$2"; shift 2;;
    --guid)     GUID="$2"; shift 2;;
    --keep-db)  KEEP_DB=1; shift;;
    --yes)      ASSUME_YES=1; shift;;
    *) die "unknown option: $1";;
  esac
done

# ── Read paths + current identity from config ─────────────────────────────────
val() { grep -m1 -E "^\s*$1:" "$YAML" | sed -E "s/.*$1:\s*//; s/\s*#.*//; s/^[\"']//; s/[\"']$//"; }
SEED_FILE="$(val seed_file)";      SEED_FILE="${SEED_FILE:-/home/sdg/pymc/hub_identity.seed}"
CONTACTS_FILE="$(val contacts_file)"; CONTACTS_FILE="${CONTACTS_FILE:-/home/sdg/pymc/hub_contacts.json}"
DB_PATH="$(val path)";             DB_PATH="${DB_PATH:-/opt/jtak/data/jtak.db}"
LOG_DIR="$(val base_path)";        LOG_DIR="${LOG_DIR:-/opt/jtak/logs/rf}"
GPS_STATUS=/opt/jtak/data/meshcore-gps.json
HQ_WM=/opt/jtak/data/hq_watermarks.json
OLD_NAME="$(val node_name)"; [ -n "$OLD_NAME" ] || die "could not read current node_name from $YAML"
SECRET_BEFORE="$(grep -c "secret:" "$YAML" || true)"

cat <<EOF

  jTAK MeshCore clone personalizer
  ────────────────────────────────
  new name .......... $OLD_NAME  ->  $NAME   (x3 in jtak.yaml)
  hostname .......... $(hostname)  ->  $HOST
  guid / hub_id ..... $GUID / $HUB_ID   (jtak.identity.yaml)
  new mesh identity . delete $SEED_FILE  -> fresh Ed25519 key on restart
  host identity ..... regenerate SSH host keys + machine-id
  wipe learned ...... contacts, gps-status, hq watermarks$([ $KEEP_DB -eq 0 ] && echo ", jtak.db, RF logs")
  KEEP (shared) ..... channel secret, radio config, hq url
EOF

if [ "$ASSUME_YES" -ne 1 ]; then
  read -rp $'\n  Proceed? [y/N] ' ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted."; exit 0; }
fi

echo; echo "==> stopping services"
systemctl stop jtak-meshcore.service jtak-api.service jtak-led.service 2>/dev/null || true
systemctl stop jtak-push.service 2>/dev/null || true

echo "==> backing up config"
cp -a "$YAML" "$YAML.bak.$(date +%s)"
[ -f "$IDENT" ] && cp -a "$IDENT" "$IDENT.bak.$(date +%s)" || true

echo "==> [CRITICAL] deleting MeshCore identity seed (forces a fresh unique pubkey)"
rm -f "$SEED_FILE"

echo "==> renaming hub in jtak.yaml ($OLD_NAME -> $NAME)"
# Only the 3 identity keys hold this exact string; the channel secret is never touched.
sed -i "s|${OLD_NAME}|${NAME}|g" "$YAML"
SECRET_AFTER="$(grep -c "secret:" "$YAML" || true)"
[ "$SECRET_BEFORE" = "$SECRET_AFTER" ] || die "channel secret line count changed — aborting, restore $YAML.bak.*"

echo "==> writing fresh jtak.identity.yaml (guid=$GUID hub_id=$HUB_ID)"
cat > "$IDENT" <<EOF
guid: $GUID
hub_id: $HUB_ID
provisioned: '$(date -u +%Y-%m-%dT%H:%M:%S+00:00)'
EOF
chown "$OWNER":"$OWNER" "$IDENT"

echo "==> wiping learned/runtime state"
rm -f "$CONTACTS_FILE" "$GPS_STATUS" "$HQ_WM"
if [ "$KEEP_DB" -eq 0 ]; then
  rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"     # schema is recreated on API start
  rm -f "$LOG_DIR"/rf_log_*.csv 2>/dev/null || true
  echo "    wiped jtak.db + RF logs (fresh history)"
else
  echo "    kept jtak.db + RF logs (--keep-db)"
fi

echo "==> setting hostname ($HOST)"
hostnamectl set-hostname "$HOST"
if grep -qE '^\s*127\.0\.1\.1\s' /etc/hosts; then
  sed -i -E "s|^(\s*127\.0\.1\.1\s+).*|\1$HOST|" /etc/hosts
else
  echo "127.0.1.1	$HOST" >> /etc/hosts
fi

echo "==> regenerating SSH host keys + machine-id (network-distinct from the source)"
rm -f /etc/ssh/ssh_host_*  && ssh-keygen -A >/dev/null
rm -f /etc/machine-id /var/lib/dbus/machine-id
systemd-machine-id-setup >/dev/null
[ -d /var/lib/dbus ] && ln -sf /etc/machine-id /var/lib/dbus/machine-id || true

echo "==> starting services"
systemctl daemon-reload
systemctl start jtak-led.service jtak-api.service jtak-meshcore.service
systemctl start jtak-push.service 2>/dev/null || true

echo "==> waiting for the new MeshCore identity to mint..."
NEWKEY=""
for _ in $(seq 1 15); do
  sleep 2
  NEWKEY="$(journalctl -u jtak-meshcore.service --since '30 sec ago' 2>/dev/null \
            | grep -oE 'identity [0-9a-f]{12}' | tail -1 | awk '{print $2}')"
  [ -n "$NEWKEY" ] && break
done

cat <<EOF

  ✅ Done — this hub is now '$NAME' ($HOST).
     new MeshCore pubkey: ${NEWKEY:-<check: journalctl -u jtak-meshcore -e | grep identity>}…
     guid/hub_id: $GUID / $HUB_ID

  Next:
   • Re-advert from the field radios (this hub's contact book was wiped).
   • Verify on the dashboard: header shows $NAME, NODES list is clean.
   • SSH host key changed — clear the old entry on your client if it warns:
       ssh-keygen -R $HOST   (and/or the hub's IP)
   • A reboot is recommended so the new hostname/machine-id fully settle.
EOF
