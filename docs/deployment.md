# VPS deployment

The app runs at **miratarelochnitsa.store** behind the VPS's existing Traefik. It uses the existing `traefik-public` Docker network and publishes no host ports.

## GitHub secrets

Add these secrets in repository settings, or in the `production` environment:

| Secret | Value |
|---|---|
| `VPS_HOST` | VPS IP address or hostname, without `https://` |
| `VPS_USER` | SSH user with Docker access, for example `root` |
| `VPS_SSH_KEY` | Private SSH key whose public key is in that user's `authorized_keys` |
| `APP_ENV` | The complete multiline contents of the app's `.env` file |

Set `APP_ENV` as a **secret** because it contains the Telegram bot token. For example:

```dotenv
APP_DOMAIN=miratarelochnitsa.store
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_BOT_USERNAME=your_bot_username
```

The workflow writes `APP_ENV` to `.env`, uploads it with the code, and sets file permissions to `600` on the VPS. You do not need to create `.env` on the VPS manually.

## Deploy

Pull requests run the Python tests. A push to `main` runs the tests and then deploys over SSH. The workflow sends the committed source and creates `.env` from `APP_ENV`, then runs `docker compose up -d --build` with the Traefik configuration. The database and browser profiles use persistent Docker volumes.

The VPS needs Docker Compose, the existing `traefik-public` network, and the domain's DNS pointed at the server.

To check the services on the VPS:

```bash
cd /opt/mira-tarel0chn1tsa
docker compose -p mira-tarel0chn1tsa -f compose.yaml -f compose.traefik.yaml ps
docker compose -p mira-tarel0chn1tsa -f compose.yaml -f compose.traefik.yaml logs --tail=100 worker
```

Stop the local bot before running the VPS bot with the same Telegram token; Telegram long polling should run in one place at a time.
