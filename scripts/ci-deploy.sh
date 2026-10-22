#!/usr/bin/env bash
# Runs on the GitHub runner. The bot token and application .env stay on the VPS.
set -Eeuo pipefail

: "${VPS_HOST:?Set the VPS_HOST GitHub secret}"
: "${VPS_USER:?Set the VPS_USER GitHub secret}"
: "${VPS_SSH_KEY:?Set the VPS_SSH_KEY GitHub secret}"
[[ $VPS_HOST =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'VPS_HOST must be a hostname or IPv4 address'; exit 1; }
[[ $VPS_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo 'Invalid VPS_USER'; exit 1; }
release_id="${GITHUB_SHA:?}-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}"
[[ $release_id =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]] || exit 1
deploy_root=/opt/mira-tarel0chn1tsa
remote_release="$deploy_root/releases/$release_id"
ssh_dir=$(mktemp -d)
trap 'rm -rf -- "$ssh_dir"' EXIT
chmod 700 "$ssh_dir"
printf '%s\n' "$VPS_SSH_KEY" > "$ssh_dir/key"
chmod 600 "$ssh_dir/key"
ssh_options=(-i "$ssh_dir/key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15 -o ServerAliveInterval=30)
destination="$VPS_USER@$VPS_HOST"

# Only committed files are packaged. Never upload a local .env or .git directory.
git archive --format=tar.gz --output="$ssh_dir/source.tar.gz" HEAD
ssh "${ssh_options[@]}" "$destination" "mkdir -p '$remote_release'"
scp "${ssh_options[@]}" "$ssh_dir/source.tar.gz" "$destination:$remote_release/source.tar.gz"
ssh "${ssh_options[@]}" "$destination" \
  "cd '$remote_release' && tar -xzf source.tar.gz && rm source.tar.gz && bash scripts/deploy.sh '$release_id'"
