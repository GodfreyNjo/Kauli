#!/bin/bash
# Nightly backup of the orders database and order/upload files to Cloudflare
# R2 - same bucket/credentials already configured for client-upload staging
# (see webapp/r2_uploads.py), just different prefixes, so no new secrets.
#
# Prompted by a real gap: Contabo's own "Auto Backup" was never enabled on
# this VPS, and the one-off rclone commands run by hand on 2026-08-25/26
# (visible under r2:kauli-backups/{db,output,uploads}/ already) were never
# wired into anything recurring - a single missed restart away from losing
# every client's uploaded file, transcript and delivered output with no
# copy anywhere else. This script is the actual recurring job; see the
# crontab entry that runs it nightly (`crontab -l` on the VM).
#
# Requires: rclone with an `r2` remote already configured (see
# ~/.config/rclone/rclone.conf on the VM - same R2_ACCOUNT_ID/keys as .env).
set -Eeuo pipefail

DATA_DIR="/root/kauli/webapp/data"
DB_PATH="$DATA_DIR/kauli_demo.db"
REMOTE="r2:kauli-backups"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TMP_DB="/tmp/kauli_demo_${STAMP}.db"
LOG_PREFIX="[kauli-backup ${STAMP}]"

# A backup that fails quietly is how data gets lost: any failure below emails the staff list (KAULI_STAFF_EMAILS)
# through the app's own mailer, and leaves a marker file that a monitor can look for.
alert() {
  echo "${LOG_PREFIX} ALERT: $1"
  docker exec kauli python3 -c "import sys; from webapp import notifications; notifications.notify_staff(sys.argv[1], sys.argv[2])" \
    "Kauli backup FAILED" "$1" >/dev/null 2>&1 || true
  touch /var/lib/kauli-backup.failed
}
trap 'rm -f "$TMP_DB"; alert "The nightly backup stopped at line $LINENO. See /var/log/kauli-backup.log on the server. Last night'"'"'s copy is still in R2."' ERR

echo "${LOG_PREFIX} started $(date -u --iso-8601=seconds)"

# 1. Hot-copy the live SQLite DB via sqlite3's own backup API, not `cp` - a
#    plain file copy of a database the app is actively writing to can grab a
#    torn, inconsistent set of pages. sqlite3.Connection.backup() is the
#    real, safe way to snapshot a live db.
python3 - "$DB_PATH" "$TMP_DB" <<'PYEOF'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close()
dst.close()
PYEOF
# Never upload a copy that is damaged or suspiciously small: SQLite's own integrity check, and at least half the size
# of the newest copy already in R2 (a database that suddenly shrinks that much is a problem, not a quiet night).
[ "$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0])" "$TMP_DB")" = "ok" ] \
  || { echo "${LOG_PREFIX} snapshot failed SQLite's integrity check"; false; }
prev_name="$(rclone lsf "${REMOTE}/db/" | sort | tail -n 1)"
remote_size() { rclone lsf --format sp --separator " " --files-only "${REMOTE}/db/" | awk -v n="$1" '$2==n{print $1}'; }
prev_size="$(remote_size "$prev_name")"; prev_size="${prev_size:-0}"
new_size="$(stat -c %s "$TMP_DB")"
if [ "$prev_size" -gt 0 ] && [ $(( new_size * 2 )) -lt "$prev_size" ]; then
  echo "${LOG_PREFIX} snapshot is ${new_size} bytes but the last one was ${prev_size}"; false
fi
rclone copyto "$TMP_DB" "${REMOTE}/db/kauli_demo_${STAMP}.db" --checksum
[ "$(remote_size "kauli_demo_${STAMP}.db")" = "$new_size" ] || { echo "${LOG_PREFIX} uploaded size differs"; false; }
rm -f "$TMP_DB"
echo "${LOG_PREFIX} DB snapshot uploaded and verified (kauli_demo_${STAMP}.db, ${new_size} bytes)"

# 1b. The settings file (.env: every API key and the session secret). Without it a rebuilt server cannot start the
#     app, and putting it in a bucket unprotected would hand out every key, so it is only copied ENCRYPTED, with a
#     passphrase kept in /root/.kauli-backup-passphrase (create it once; keep a second copy in a password manager -
#     without the passphrase the copy cannot be opened). No passphrase file = nothing is copied.
if [ -s /root/.kauli-backup-passphrase ] && command -v gpg >/dev/null; then
  gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --passphrase-file /root/.kauli-backup-passphrase \
      -o "/tmp/kauli_env_${STAMP}.gpg" /root/kauli/.env
  rclone copyto "/tmp/kauli_env_${STAMP}.gpg" "${REMOTE}/secrets/kauli_env_${STAMP}.gpg"
  rm -f "/tmp/kauli_env_${STAMP}.gpg"
  rclone lsf "${REMOTE}/secrets/" | sort | head -n -10 | while read -r old; do [ -n "$old" ] && rclone deletefile "${REMOTE}/secrets/${old}"; done
  echo "${LOG_PREFIX} encrypted settings copy uploaded"
else
  echo "${LOG_PREFIX} no /root/.kauli-backup-passphrase - settings (.env) NOT backed up"
fi

# 2. Mirror order deliverables and other real user data - incremental
#    (rclone only transfers what actually changed), with a dated
#    --backup-dir so a local delete/overwrite doesn't just silently vanish
#    from the backup too; it lands under versions/<stamp>/ instead.
for sub in output uploads avatars blog_covers; do
  if [ -d "${DATA_DIR}/${sub}" ]; then
    rclone sync "${DATA_DIR}/${sub}" "${REMOTE}/${sub}" \
      --backup-dir "${REMOTE}/versions/${STAMP}/${sub}" \
      --fast-list --checksum
    echo "${LOG_PREFIX} synced ${sub}/"
  fi
done

# 3. Retention - without limit, both the DB snapshots and the versions/
#    archive from step 2 grow forever. Keep 30 days of each; this is a
#    simple safety net, not point-in-time recovery for longer than that.
rclone lsf "${REMOTE}/db/" | sort | head -n -30 | while read -r old; do
  [ -n "$old" ] && rclone deletefile "${REMOTE}/db/${old}" && echo "${LOG_PREFIX} pruned old snapshot ${old}"
done

CUTOFF="$(date -u -d '30 days ago' +%Y%m%d-%H%M%S)"
rclone lsf "${REMOTE}/versions/" --dirs-only | sed 's#/$##' | while read -r d; do
  if [[ -n "$d" && "$d" < "$CUTOFF" ]]; then
    rclone purge "${REMOTE}/versions/${d}" && echo "${LOG_PREFIX} pruned old version snapshot ${d}"
  fi
done

echo "${LOG_PREFIX} finished $(date -u --iso-8601=seconds)"

rm -f /var/lib/kauli-backup.failed
