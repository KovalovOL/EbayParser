#!/usr/bin/env bash
# Runs on the Linux VPS, from an uploaded release directory.
set -Eeuo pipefail
umask 077

deploy_root=/opt/mira-tarel0chn1tsa
release_id=${1:?Usage: bash scripts/deploy.sh RELEASE_ID}
[[ $release_id =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]] || { echo 'Invalid release ID'; exit 1; }
release_dir="$deploy_root/releases/$release_id"
[[ $(pwd -P) == "$release_dir" ]] || { echo 'Run from the matching release directory'; exit 1; }
[[ -s "$deploy_root/.env" ]] || { echo "Create $deploy_root/.env before deploying"; exit 1; }
exec 9>"$deploy_root/deploy.lock"
flock -n 9 || { echo 'Another deployment is already running'; exit 1; }
ln -sfn "$deploy_root/.env" "$release_dir/.env"
chmod 600 "$deploy_root/.env"
export RELEASE_ID="$release_id"
compose=(docker compose --project-name mira-tarel0chn1tsa --env-file "$deploy_root/.env"
  -f compose.yaml -f compose.traefik.yaml)

docker network inspect traefik-public >/dev/null
"${compose[@]}" config --quiet
# Build before touching the running release. Tag images by release for manual rollback.
"${compose[@]}" build web worker

# sqlite3.backup includes committed WAL data. Backups remain in the private data volume.
if docker volume inspect mira-tarel0chn1tsa_signal-data >/dev/null 2>&1; then
  docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true \
    --mount type=volume,src=mira-tarel0chn1tsa_signal-data,dst=/data \
    "mira-tarel0chn1tsa-app:$release_id" python -c '
import os, sqlite3, sys
from pathlib import Path
os.umask(0o077)
if Path("/data/ebay.db").exists():
    Path("/data/backups").mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect("file:/data/ebay.db?mode=ro", uri=True) as source:
        with sqlite3.connect("/data/backups/before-" + sys.argv[1] + ".sqlite3") as target:
            source.backup(target)
' "$release_id"
fi

# Stop consumers before web applies schema migrations. Named volumes are preserved.
"${compose[@]}" stop worker bot
"${compose[@]}" up -d --no-build --wait --wait-timeout 180

# Verify DNS, Traefik routing and the certificate, as well as container health.
"${compose[@]}" exec -T web python - <<'PY'
import json
import time
import urllib.error
import urllib.request
from app.config import get_settings
url = get_settings().public_url.rstrip("/") + "/api/health"
for attempt in range(12):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200 and json.load(response) == {"status": "ok"}
        break
    except (urllib.error.URLError, TimeoutError, AssertionError):
        if attempt == 11:
            raise SystemExit("Public HTTPS health check failed. Check DNS and Traefik.") from None
        time.sleep(5)
PY

if [[ -L "$deploy_root/current" ]]; then
  ln -sfn "$(readlink "$deploy_root/current")" "$deploy_root/previous"
fi
ln -sfn "$release_dir" "$deploy_root/current.next"
mv -Tf "$deploy_root/current.next" "$deploy_root/current"
"${compose[@]}" ps
echo "Deployed $release_id"
