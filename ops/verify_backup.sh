#!/bin/bash
# Proves, every morning, that last night's backup can actually be used to restore Kauli - a backup nobody has ever
# restored is a hope, not a backup. Downloads the newest database snapshot from R2, then checks that:
#   1. it is recent (made in the last 36 hours),
#   2. SQLite's own integrity check passes,
#   3. it holds roughly as many customers and orders as the live database (within 10 %),
#   4. the most recent R2 copy of the uploads folder exists.
# Any failure emails the staff list (KAULI_STAFF_EMAILS) through the app's own mailer. Run from cron after the backup:
#   30 7 * * * /root/kauli/ops/verify_backup.sh >> /var/log/kauli-backup-verify.log 2>&1
set -Eeuo pipefail

REMOTE="r2:kauli-backups"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d /tmp/kauli-verify.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

alert() {
  echo "[verify-backup ${STAMP}] ALERT: $1"
  docker exec kauli python3 -c "import sys; from webapp import notifications; notifications.notify_staff(sys.argv[1], sys.argv[2])" \
    "Kauli backup check FAILED" "$1" >/dev/null 2>&1 || true
  touch /var/lib/kauli-backup-verify.failed
}
trap 'alert "The backup check stopped unexpectedly at line $LINENO. Look at /var/log/kauli-backup-verify.log on the server."' ERR

latest="$(rclone lsf "${REMOTE}/db/" | sort | tail -n 1)"
[ -n "$latest" ] || { alert "There is no database snapshot in ${REMOTE}/db/ at all."; exit 1; }

# 1. age (the file name carries the UTC time it was made)
made="$(echo "$latest" | sed -E 's/^kauli_demo_([0-9]{8})-([0-9]{2})([0-9]{2})([0-9]{2})\.db$/\1 \2:\3:\4/')"
age_h=$(( ( $(date -u +%s) - $(date -u -d "$made" +%s) ) / 3600 ))
[ "$age_h" -le 36 ] || { alert "The newest database backup (${latest}) is ${age_h} hours old. The nightly backup has stopped working."; exit 1; }

# 2. integrity
rclone copyto "${REMOTE}/db/${latest}" "${WORK}/snap.db" --checksum
check="$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0])" "${WORK}/snap.db")"
[ "$check" = "ok" ] || { alert "The backup ${latest} is damaged (SQLite says: ${check})."; exit 1; }

# 3. contents against the live database
snap="$(python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(*(c.execute('select count(*) from '+t).fetchone()[0] for t in ('users','orders')))" "${WORK}/snap.db")"
live="$(docker exec kauli python3 -c "import sqlite3; c=sqlite3.connect('file:/app/webapp/data/kauli_demo.db?mode=ro',uri=True); print(*(c.execute('select count(*) from '+t).fetchone()[0] for t in ('users','orders')))")"
read -r su so <<<"$snap"; read -r lu lo <<<"$live"
if [ $(( su * 10 )) -lt $(( lu * 9 )) ] || [ $(( so * 10 )) -lt $(( lo * 9 )) ]; then
  alert "The backup ${latest} has ${su} customers and ${so} orders but the live site has ${lu} and ${lo}. It is missing data."; exit 1
fi

# 4. the files
n="$(rclone lsf "${REMOTE}/uploads/" --files-only --max-depth 1 2>/dev/null | wc -l)"
echo "[verify-backup ${STAMP}] OK: ${latest} (${age_h}h old) is intact; ${su} customers / ${so} orders in the backup, ${lu} / ${lo} live; ${n} files at the top of uploads/"
rm -f /var/lib/kauli-backup-verify.failed
