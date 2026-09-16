#!/bin/bash
# Keeps the Supabase project that backs Kauli's login/signup/password-reset/
# MFA (see webapp/supabase_auth.py) from being auto-paused by Supabase's
# free-tier inactivity policy - a real notice received 2026-09-16: "Free
# projects that have no API requests in a 7-day period are paused."
# Kauli's own real traffic doesn't reliably generate enough Supabase API
# calls on its own at this stage (most requests never touch auth at all -
# only login/signup/password-change/MFA do), so this is a real, deliberate
# keep-alive, not decorative: one lightweight, side-effect-free GET to
# GoTrue's own /auth/v1/health endpoint, on a schedule comfortably inside
# the 7-day window (see the crontab entry - runs every 2 days) so a single
# missed run from a VM restart or blip never risks the real 7-day cutoff.
#
# This buys time, not a permanent fix - see the real fix (upgrading to
# Supabase Pro, which removes the auto-pause policy entirely) discussed
# with Godfrey; worth doing once Kauli's real usage justifies the cost.
set -euo pipefail

SUPABASE_URL=$(grep -E '^SUPABASE_URL=' /root/kauli/.env | head -1 | cut -d= -f2-)
SUPABASE_ANON_KEY=$(grep -E '^SUPABASE_ANON_KEY=' /root/kauli/.env | head -1 | cut -d= -f2-)

if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_ANON_KEY" ]; then
  echo "[supabase-keepalive] SUPABASE_URL/SUPABASE_ANON_KEY not found in .env - skipping"
  exit 1
fi

code=$(curl -s -o /dev/null -w '%{http_code}' -H "apikey: ${SUPABASE_ANON_KEY}" "${SUPABASE_URL}/auth/v1/health")
echo "[supabase-keepalive] $(date -u --iso-8601=seconds) GET /auth/v1/health -> HTTP ${code}"
if [ "$code" != "200" ]; then
  exit 1
fi
