#!/usr/bin/env bash
set -euo pipefail

env_file=/etc/popcorn.env
jellyfin_db=/var/lib/jellyfin/data/jellyfin.db

if [[ ! -f "$env_file" || ! -f "$jellyfin_db" ]]; then
  echo "Popcorn environment or Jellyfin database is missing." >&2
  exit 1
fi

api_key="$(sqlite3 "$jellyfin_db" "SELECT AccessToken FROM ApiKeys WHERE Name='Popcorn' ORDER BY Id DESC LIMIT 1;")"
if [[ -z "$api_key" ]]; then
  api_key="$(openssl rand -hex 16)"
  timestamp="$(date -u +'%Y-%m-%d %H:%M:%S.0000000+00:00')"
  sqlite3 "$jellyfin_db" \
    "INSERT INTO ApiKeys(DateCreated, DateLastActivity, Name, AccessToken) VALUES('$timestamp', '$timestamp', 'Popcorn', '$api_key');"
fi

temporary="$(mktemp /etc/popcorn.env.XXXXXX)"
trap 'rm -f "$temporary"' EXIT
grep -v '^POPCORN_JELLYFIN_API_KEY=' "$env_file" > "$temporary" || true
printf 'POPCORN_JELLYFIN_API_KEY=%s\n' "$api_key" >> "$temporary"
chown --reference="$env_file" "$temporary"
chmod --reference="$env_file" "$temporary"
mv "$temporary" "$env_file"
trap - EXIT

echo "Provisioned the Popcorn Jellyfin API key."
