set -u
cd "$(dirname "$0")/.."
T=/tmp/bk; rm -rf $T; mkdir -p $T/bin $T/r2/db $T/kauli/webapp/data/uploads $T/kauli/webapp/data/output
export PATH="$T/bin:$PATH"

# --- fake rclone: r2:kauli-backups/... lives in $T/r2/...
cat > $T/bin/rclone <<'EOS'
#!/bin/bash
R=/tmp/bk/r2
map() { case "$1" in r2:kauli-backups*) echo "$R/${1#r2:kauli-backups}";; *) echo "$1";; esac; }
cmd=$1; shift
case $cmd in
  lsf) fmt=""; sep=";"; dirs=0; args=()
       while [ $# -gt 0 ]; do case $1 in --format) fmt=$2; shift 2;; --separator) sep=$2; shift 2;; --dirs-only) dirs=1; shift;; --files-only|--fast-list) shift;; --max-depth) shift 2;; *) args+=("$1"); shift;; esac; done
       p=$(map "${args[0]}"); [ -d "$p" ] || exit 0
       if [ $dirs = 1 ]; then for d in "$p"/*/; do [ -d "$d" ] && echo "$(basename "$d")/"; done; exit 0; fi
       for f in "$p"/*; do [ -f "$f" ] || continue; if [ "$fmt" = sp ]; then echo "$(stat -c %s "$f")${sep}$(basename "$f")"; else basename "$f"; fi; done;;
  copyto) src=$1; dst=$(map "$2"); [ "${src#r2:}" != "$src" ] && src=$(map "$src"); mkdir -p "$(dirname "$dst")"; cp "$src" "$dst";;
  sync) mkdir -p "$(map "$2")"; cp -r "$1"/. "$(map "$2")"/;;
  deletefile) rm -f "$(map "$1")";;
  purge) rm -rf "$(map "$1")";;
  *) echo "stub rclone: unsupported $cmd" >&2; exit 2;;
esac
EOS
# --- fake docker: records alerts, answers the live-count query
cat > $T/bin/docker <<'EOS'
#!/bin/bash
if [[ "$*" == *notify_staff* ]]; then echo "ALERT: ${@: -2:1} | ${@: -1}" >> /tmp/bk/alerts.log; exit 0; fi
if [[ "$*" == *"select count"* ]]; then echo "${LIVE_COUNTS:-3 4}"; exit 0; fi
exit 0
EOS
chmod +x $T/bin/rclone $T/bin/docker

# --- relocated copies of the real scripts
reloc() { sed -e "s#/root/kauli#$T/kauli#g" -e "s#/var/lib/kauli-backup.failed#$T/failed#g" -e "s#/var/lib/kauli-backup-verify.failed#$T/vfailed#g" -e "s#/root/.kauli-backup-passphrase#$T/pass#g" "$1" > "$2"; chmod +x "$2"; }
reloc backup_to_r2.sh $T/backup.sh; reloc ops/verify_backup.sh $T/verify.sh

mkdb() { rm -f "$1"; python3 - "$1" "$2" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("create table users(id)"); c.execute("create table orders(id)")
n = int(sys.argv[2])
c.executemany("insert into users values(?)", [(i,) for i in range(3)]); c.executemany("insert into orders values(?)", [(i,) for i in range(4)])
c.execute("create table pad(x)"); c.executemany("insert into pad values(?)", [("x"*500,)] * n); c.commit()
EOF
}
fail=0; ok() { echo "PASS: $1"; }; bad() { echo "FAIL: $1"; fail=1; }
DB=$T/kauli/webapp/data/kauli_demo.db

echo "--- 1 healthy night"
mkdb $DB 400; touch $T/failed
bash $T/backup.sh > $T/o1.txt 2>&1; rc=$?
[ $rc = 0 ] && ok "backup exits 0" || { bad "backup rc=$rc"; cat $T/o1.txt; }
[ "$(ls $T/r2/db | wc -l)" = 1 ] && ok "one snapshot uploaded" || bad "no snapshot"
[ ! -e $T/failed ] && ok "failure marker cleared on success" || bad "marker left"
grep -q "uploaded and verified" $T/o1.txt && ok "upload verified" || bad "no verify line"
grep -q "NOT backed up" $T/o1.txt && ok "no passphrase -> settings skipped, clearly logged" || bad "env skip not logged"

echo "--- 2 encrypted settings copy"
echo "s3cret-passphrase" > $T/pass; echo "KEY=value" > $T/kauli/.env; sleep 1
bash $T/backup.sh > $T/o2.txt 2>&1 && ok "backup with passphrase exits 0" || { bad "rc"; cat $T/o2.txt; }
f=$(ls $T/r2/secrets/*.gpg 2>/dev/null | head -1)
if [ -n "$f" ]; then
  grep -q "KEY=value" "$f" && bad "env copy is NOT encrypted" || ok "env copy is not readable as plain text"
  gpg --batch --quiet --passphrase-file $T/pass -d "$f" 2>/dev/null | grep -q "KEY=value" && ok "env copy decrypts with the passphrase" || bad "cannot decrypt"
else bad "no encrypted copy"; fi

echo "--- 3 snapshot much smaller than yesterday is refused and reported"
rm -f $T/alerts.log; dd if=/dev/zero of=$T/r2/db/kauli_demo_29990101-000000.db bs=1M count=5 2>/dev/null
n_before=$(ls $T/r2/db | wc -l)
bash $T/backup.sh > $T/o3.txt 2>&1 && bad "should have failed" || ok "backup exits non-zero"
[ "$(ls $T/r2/db | wc -l)" = "$n_before" ] && ok "the suspicious copy was not uploaded" || bad "uploaded anyway"
grep -q "Kauli backup FAILED" $T/alerts.log && ok "staff alert sent" || bad "no alert"; [ -e $T/failed ] && ok "failure marker written" || bad "no marker"
rm -f $T/r2/db/kauli_demo_29990101-000000.db

echo "--- 4 damaged database is refused and reported"
rm -f $T/alerts.log $T/failed; head -c 3000 /dev/urandom > $DB
bash $T/backup.sh > $T/o4.txt 2>&1 && bad "should have failed" || ok "damaged DB -> non-zero"
grep -q "Kauli backup FAILED" $T/alerts.log && ok "staff alert sent" || bad "no alert"

echo "--- 5 verification: healthy snapshot passes"
rm -rf $T/r2/db/*; mkdb $DB 400; bash $T/backup.sh >/dev/null 2>&1
rm -f $T/alerts.log; bash $T/verify.sh > $T/v1.txt 2>&1 && ok "verify passes" || { bad "verify failed"; cat $T/v1.txt; }
grep -q "is intact" $T/v1.txt && ok "reports intact" || bad "no OK line"; [ ! -s $T/alerts.log ] && ok "no alert" || bad "unexpected alert"

echo "--- 6 verification: backup missing data vs live"
rm -f $T/alerts.log; LIVE_COUNTS="100 400" bash $T/verify.sh > $T/v2.txt 2>&1 && bad "should fail" || ok "verify fails"
grep -q "missing data" $T/alerts.log && ok "alert says data is missing" || bad "no alert"

echo "--- 7 verification: stale backup"
rm -rf $T/r2/db/*; cp $DB $T/r2/db/kauli_demo_20200101-010000.db; rm -f $T/alerts.log
bash $T/verify.sh > $T/v3.txt 2>&1 && bad "should fail" || ok "stale -> fails"; grep -q "hours old" $T/alerts.log && ok "alert says it is old" || bad "no alert"

echo "--- 8 verification: damaged snapshot"
rm -rf $T/r2/db/*; head -c 4000 /dev/urandom > $T/r2/db/kauli_demo_$(date -u +%Y%m%d-%H%M%S).db; rm -f $T/alerts.log
bash $T/verify.sh > $T/v4.txt 2>&1 && bad "should fail" || ok "damaged -> fails"; grep -q "damaged\|unexpectedly" $T/alerts.log && ok "alert sent" || bad "no alert"
[ $fail = 0 ] && echo ALL-OK || echo SOME-FAILED
