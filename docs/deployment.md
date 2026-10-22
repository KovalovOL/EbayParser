# VPS deployment

Repository: [KovalovOL/EbayParser](https://github.com/KovalovOL/EbayParser). Domain: **miratarelochnitsa.store**.

The production stack reuses the existing Traefik instance. It publishes **no host ports**: Traefik connects to `web:8000` on `traefik-public`. The other applications' ports, including 80, 443, 8080, 8081 and 5432, remain available to their existing containers. Only `web` joins the public proxy network; `bot` and `worker` use the private Compose network.

## One-time VPS setup

1. Set the domain's DNS A record to the VPS IPv4 address. Set an AAAA record only if IPv6 is configured on this VPS.
2. Verify Docker Compose **2.24.4 or newer**, the existing network, and Traefik settings:

   ```bash
   docker compose version
   docker network inspect traefik-public
   ```

   Traefik must use the Docker provider, entrypoints `web` (80) and `websecure` (443), and the existing certificate resolver `myresolver`. The override also redirects HTTP to HTTPS.

3. Create the application directory and its private configuration on the VPS:

   ```bash
   install -d -m 700 /opt/mira-tarel0chn1tsa
   touch /opt/mira-tarel0chn1tsa/.env
   chmod 600 /opt/mira-tarel0chn1tsa/.env
   nano /opt/mira-tarel0chn1tsa/.env
   ```

   Configuration:

   ```dotenv
   APP_DOMAIN=miratarelochnitsa.store
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_BOT_USERNAME=your_bot_username
   EBAY_HTTP_CLIENT=playwright
   EBAY_BROWSER_HEADLESS=false
   EBAY_FIREFOX_FALLBACK=true
   REQUEST_DELAY_SECONDS=10
   REQUEST_DELAY_JITTER_SECONDS=10
   EBAY_BLOCK_COOLDOWN_SECONDS=900
   ```

   The existing `tg=...` token alias also works. Production Compose automatically sets `PUBLIC_URL=https://miratarelochnitsa.store`, secure refresh cookies and SQLite paths. A signing key is created in the persistent data volume unless `SECRET_KEY` is supplied. Never commit `.env`.

4. Allow a dedicated SSH key to log in to the deploy account using its `~/.ssh/authorized_keys`. That account needs write access to `/opt/mira-tarel0chn1tsa`, Docker access, Bash, `tar`, and `flock` (normally included on Ubuntu). `VPS_USER=root` works with the server layout shown in this project.

## GitHub configuration

In [repository settings](https://github.com/KovalovOL/EbayParser/settings/environments), create the **production** environment, allow deployments from the default branch, and add these Actions secrets there:

| Secret | Value |
|---|---|
| `VPS_HOST` | VPS IPv4 address or SSH hostname, without a scheme |
| `VPS_USER` | SSH account, for example `root` |
| `VPS_SSH_KEY` | Private deployment key, including its BEGIN/END lines |

The workflow connects over SSH on the default port `22` and accepts the server's SSH host key on first connection. The bot token belongs only in the server `.env`.

## Deployments

Push to the repository's default branch (`main`), or run **Test and deploy** from [Actions](https://github.com/KovalovOL/EbayParser/actions). Pull requests run CI only. The workflow tests Python, validates JavaScript, shell syntax and the production Compose model, and runs actual Chromium/Firefox fallback checks in Docker with intercepted pages. It deploys only after those checks pass.

Each deployment:

1. Uploads an archive of the exact checked-out commit to `/opt/mira-tarel0chn1tsa/releases/COMMIT-RUN-ATTEMPT` over SSH. No GitHub credentials are needed on the VPS.
2. Builds the application and Chromium/Firefox worker images on the VPS while the previous version runs. No container registry subscription or registry token is needed.
3. Backs up existing SQLite data, stops the old consumers, and recreates the three services using the same named volumes. Web applies schema migrations before bot/worker start.
4. Waits for service health, verifies the public HTTPS health endpoint, and updates `current` and `previous` symlinks. Concurrent deployments are serialized in Actions and with a VPS lock.

The first deployment creates fresh server volumes. Local searches, accounts and browser cookies are **not copied automatically**. If you need to retain local data, transfer a consistent SQLite backup and `/data/secret.key` into the server data volume before starting consumers; keep UID `10001` ownership. Browser profiles are separate from SQLite and need not be transferred; a new VPS also has a different IP address.

Before enabling the VPS bot with the same Telegram token, stop the local bot and worker:

```bash
docker compose stop bot worker
```

Telegram long polling must run in only one installation for this token. The website can remain available locally for saved results.

## Operations

From the VPS, use the last successful release:

```bash
cd /opt/mira-tarel0chn1tsa/current
export RELEASE_ID=$(basename "$(pwd -P)")
docker compose -p mira-tarel0chn1tsa -f compose.yaml -f compose.traefik.yaml ps
docker compose -p mira-tarel0chn1tsa -f compose.yaml -f compose.traefik.yaml logs --tail=100 worker
```

Persistent volumes are `mira-tarel0chn1tsa_signal-data` (SQLite, signing key, backups) and `mira-tarel0chn1tsa_browser-data` (separate Chromium and Firefox profiles). Backups are `/data/backups/before-RELEASE_ID.sqlite3` inside the data volume. Keep these volumes when updating; `down -v` deletes them. Releases and images remain available for manual rollback and can be cleaned up later when no longer needed.

If a deployment fails, Actions fails too. `current` still points to the last successful release; the containers may already contain the attempted update. Inspect the failed release's Compose status before retrying. A failed build does not stop the running version.

To roll back application images when the previous code supports the current database schema:

```bash
cd /opt/mira-tarel0chn1tsa/previous
export RELEASE_ID=$(basename "$(pwd -P)")
docker compose -p mira-tarel0chn1tsa -f compose.yaml -f compose.traefik.yaml up -d --no-build --wait
```

The workflow does not automatically restore an older database because that would discard data saved since the backup. An incompatible schema change requires a deliberate database restore while consumers are stopped.

An eBay 403 does not make the worker container unhealthy: it is an upstream pause, shown with its retry time in the application. Container health checks measure process liveness. Every check starts with persistent **Chromium**, with one **Firefox** fallback attempt for access/browser failures. Only one browser stays open at a time. Both use separate persistent profiles. HTTP 429 pauses all searches immediately; when both browsers are blocked, the shared retry deadline also applies to the failed searches without an additional hourly backoff.

Configuration references: [Traefik Docker routing](https://doc.traefik.io/traefik/v3.6/reference/routing-configuration/other-providers/docker/), [Compose merge and reset](https://docs.docker.com/reference/compose-file/merge/), [GitHub deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments).
