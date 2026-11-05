#!/bin/bash
# The ONLY way this app should ever be redeployed - a plain `docker run` typed by hand is exactly how the
# webapp/data volume mount gets dropped by accident (every blog cover, avatar and order upload lives ONLY in
# that mount). Run this after every `git pull` + `docker build`. Install: cp ops/deploy.sh /root/kauli/deploy.sh
#
# Hardening (2026-11):
#  * the port is published on the server's loopback only (127.0.0.1:8000). The Cloudflare tunnel runs on this
#    machine and reaches it there; nobody on the internet can reach the app directly, so Cloudflare's firewall and
#    rate limits cannot be skipped and a visitor cannot invent the cf-connecting-ip header the rate limiter trusts.
#  * no-new-privileges and no Linux capabilities: the app runs as a normal user and needs neither.
# If the hardened container does not answer within two minutes, the script falls back to the same container without
# the two capability flags (the loopback-only port stays), so a deploy never ends with the site down.
set -euo pipefail
cd /root/kauli

start_kauli() {          # $@ = extra docker flags
  docker stop kauli 2>/dev/null || true
  docker rm kauli 2>/dev/null || true
  docker run -d --name kauli --network kauli_net --restart unless-stopped \
    -p 127.0.0.1:8000:8000 \
    -v /root/kauli/webapp/data:/app/webapp/data \
    --env-file /root/kauli/.env \
    "$@" \
    kauli:latest >/dev/null
}

answers() {              # wait up to $1 seconds for the app to serve a page
  for _ in $(seq 1 "$1"); do
    curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:8000/robots.txt && return 0
    sleep 1
  done
  return 1
}

start_kauli --security-opt no-new-privileges:true --cap-drop ALL
echo "Deployed. Waiting for startup..."
if ! answers 120; then
  echo "WARNING: the hardened container did not answer in 120s - restarting without the capability flags"
  docker logs --tail 20 kauli || true
  start_kauli
  answers 120 || { echo "ERROR: the app does not answer"; docker logs --tail 40 kauli; exit 1; }
fi
docker logs --tail 15 kauli
echo "---"
echo "Verifying persisted data survived:"
docker exec kauli ls /app/webapp/data/blog_covers/ | wc -l | xargs echo "blog cover files:"
docker exec kauli ls /app/webapp/data/avatars/ | wc -l | xargs echo "avatar files:"
echo "Published on:"; docker port kauli
