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
set -euo pipefail

DATA_DIR="/root/kauli/webapp/data"
DB_PATH="$DATA_DIR/kauli_demo.db"
REMOTE="r2:kauli-backups"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TMP_DB="/tmp/kauli_demo_${STAMP}.db"
LOG_PREFIX="[kauli-backup ${STAMP}]"

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
rclone copyto "$TMP_DB" "${REMOTE}/db/kauli_demo_${STAMP}.db" --checksum
rm -f "$TMP_DB"
echo "${LOG_PREFIX} DB snapshot uploaded (kauli_demo_${STAMP}.db)"

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
