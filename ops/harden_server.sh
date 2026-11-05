#!/bin/bash
# One-time (and safe to re-run) hardening of the Kauli server. Run as root, after `git pull` has brought /root/kauli/ops/ in:
#       bash /root/kauli/ops/harden_server.sh
# KEEP YOUR CURRENT SSH WINDOW OPEN while it runs, and open a second window afterwards to confirm your key still logs in.
#
# What it does, in order (each step says what it did and rolls itself back if its own test fails):
#   1. SSH: key login only (no passwords, root only with a key), 3 tries, no X11/agent forwarding.
#      Today thousands of password guesses a day hit this server (journalctl -u ssh | grep -c "Failed password").
#   2. fail2ban: bans an address that keeps guessing (1 hour, growing to 30 days for repeat offenders).
#   3. Firewall (ufw): everything inbound denied except SSH. The Cloudflare tunnel only makes outbound connections.
#   4. Kauli's port 8000 moves from "the whole internet" to this machine only (ops/deploy.sh), so Cloudflare's protection
#      cannot be skipped by calling the server's IP directly. Tested through Cloudflare; rolled back if the site stops answering.
#   5. Backups: the failure-alert / verification scripts are scheduled, logs are rotated, the settings-file copy is explained.
#   6. Housekeeping: .env backup files made private; Docker's 50 GB of old build cache trimmed (keeps the last 7 days).
set -u
cd /root/kauli || exit 1
say() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
grep -qE '^(ssh-|ecdsa-|sk-)' /root/.ssh/authorized_keys 2>/dev/null || { echo "STOP: /root/.ssh/authorized_keys has no key, turning passwords off would lock you out."; exit 1; }

say "1. SSH: keys only"
cat > /etc/ssh/sshd_config.d/00-kauli-hardening.conf <<'EOF'
# Key-only access: password guessing cannot work. (00- so it is read before any cloud-init drop-in.)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
if sshd -t; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  sshd -T | grep -E '^(passwordauthentication|permitrootlogin|maxauthtries) '
else
  rm -f /etc/ssh/sshd_config.d/00-kauli-hardening.conf; echo "sshd config invalid - change removed, nothing applied"
fi

say "2. fail2ban"
if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban >/dev/null 2>&1; then
  cat > /etc/fail2ban/jail.d/kauli-sshd.local <<'EOF'
[DEFAULT]
bantime = 1h
bantime.increment = true
bantime.factor = 4
bantime.maxtime = 30d
findtime = 10m
maxretry = 4
ignoreip = 127.0.0.1/8 ::1

[sshd]
enabled = true
backend = systemd
mode = aggressive
EOF
  systemctl enable --now fail2ban >/dev/null 2>&1; systemctl restart fail2ban; sleep 3
  systemctl is-active fail2ban; fail2ban-client status sshd 2>&1 | sed -n 2,4p
else
  echo "could not install fail2ban (skipped)"
fi

say "3. Firewall"
apt-get install -y -qq ufw >/dev/null 2>&1
if command -v ufw >/dev/null; then
  ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
  ufw allow 22/tcp comment ssh >/dev/null
  ufw --force enable >/dev/null
  # test: containers still reach the internet and the public site still answers; otherwise switch the firewall off again
  if docker exec kauli python3 -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=10)" >/dev/null 2>&1 \
     && curl -fsS -o /dev/null --max-time 15 "https://kauli-forgemedia.com/robots.txt?cb=$RANDOM"; then
    ufw status | sed -n 1,6p
  else
    ufw disable; echo "WARNING: the firewall broke networking, so it was switched off again. Nothing else changed."
  fi
fi

say "4. Kauli's port: this machine only"
if [ -f ops/deploy.sh ]; then
  cp -a deploy.sh "deploy.sh.before-hardening" 2>/dev/null || true
  cp ops/deploy.sh deploy.sh && chmod +x deploy.sh
  if bash deploy.sh; then
    sleep 5
    if curl -fsS -o /dev/null --max-time 20 "https://kauli-forgemedia.com/robots.txt?cb=$RANDOM"; then
      echo "OK: the site answers through Cloudflare, and the port is published on:"; docker port kauli
    else
      echo "WARNING: the site does not answer through Cloudflare with the port closed to the internet."
      echo "         The tunnel must point at http://localhost:8000 (Cloudflare dashboard > Zero Trust > Networks > Tunnels). Reverting."
      cp deploy.sh.before-hardening deploy.sh && bash deploy.sh
    fi
  fi
else
  echo "ops/deploy.sh is not on the server yet (run the deploy first)"
fi

say "5. Backups"
chmod +x /root/kauli/backup_to_r2.sh /root/kauli/ops/verify_backup.sh 2>/dev/null
( crontab -l 2>/dev/null | grep -v 'ops/verify_backup.sh'; echo '30 7 * * * /root/kauli/ops/verify_backup.sh >> /var/log/kauli-backup-verify.log 2>&1' ) | crontab -
cat > /etc/logrotate.d/kauli <<'EOF'
/var/log/kauli-*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
EOF
crontab -l | grep -E 'kauli/(backup_to_r2|ops/verify_backup)'
if [ -s /root/.kauli-backup-passphrase ]; then echo "settings-file backup: passphrase present, the nightly backup will copy .env ENCRYPTED"
else echo "settings-file backup: NOT ON. To switch it on (once):  head -c 32 /dev/urandom | base64 > /root/.kauli-backup-passphrase && chmod 600 /root/.kauli-backup-passphrase"
     echo "                      then copy that file's one line into your password manager - without it the .env copy cannot be opened."; fi

say "6. Housekeeping"
chmod 600 /root/kauli/.env /root/kauli/.env.bak* 2>/dev/null
docker builder prune -f --filter until=168h 2>&1 | tail -1
df -h / | tail -1

say "done - now open a SECOND ssh window and confirm your key still logs in before closing this one"
