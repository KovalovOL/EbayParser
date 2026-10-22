"""One long-polling process. Bot token is never placed in logs."""
import asyncio
import html
import logging
import secrets
import time
from urllib.parse import urlsplit

from app.db import init_db, session_scope
from app.config import get_settings
from app.models import LoginChallenge, ServiceState
from app.runtime import configure_logging, heartbeat, process_lock
from app.security import digest
from app.telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)


async def handle_update(update: dict, telegram: TelegramClient):
    message = update.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        return
    command = message.get("text", "").split()
    if not command or command[0].split("@")[0] != "/start" or len(command) != 2 or not command[1].startswith("login_"):
        public_url = get_settings().public_url
        website = (f'<a href="{html.escape(public_url, quote=True)}">Open the website</a>'
                   if urlsplit(public_url).hostname not in {"localhost", "127.0.0.1", "::1"}
                   else "Open the Mira Tarel0chn1tsa website")
        await telegram.send(chat["id"],
            '<b>Mira Tarel0chn1tsa</b>\n\n'
            f'1. {website} and enter your Telegram @username.\n'
            '2. Follow the sign-in link on the website and press Start here.\n'
            '3. Enter the six-digit code on the website to finish signing in.\n\n'
            'Your account is created automatically. Add a search on the website to receive new eBay matches here.\n'
            'The first check saves results without alerts. Later checks alert once per new title + price + location combination.\n\n'
            'To pause notifications, pause the search on the website. Use /help to show these instructions again.')
        return
    challenge_id = command[1][6:]
    with session_scope() as db:
        challenge = db.get(LoginChallenge, challenge_id)
        if not challenge or challenge.used or challenge.expires_at < time.time() or challenge.attempts >= 5:
            error = "This sign-in link has expired or is no longer valid. Request a new code on the website."
        elif not sender.get("username") or sender["username"].lower() != challenge.username:
            error = "Your Telegram username does not match the one entered on the website. Check your @username and request a new code."
        elif challenge.code_hash:
            error = "Your code was already sent above. Enter it on the website. If it has expired, request a new code there."
        else:
            error = None
            code = f"{secrets.randbelow(1_000_000):06d}"
            # Keep the transaction open until delivery succeeds; failed delivery does not consume the challenge.
            await telegram.send(chat["id"], f"<b>Mira Tarel0chn1tsa — sign-in code</b>\n\n<code>{code}</code>\n\n"
                f"For @{html.escape(sender['username'])}. Return to the website and enter these six digits.\n"
                "Expires 10 minutes after your request on the website. Do not share this code.")
            challenge.code_hash = digest(challenge.id + ":" + code)
            challenge.telegram_id = sender["id"]
            challenge.chat_id = chat["id"]
    if error:
        await telegram.send(chat["id"], error)


async def run():
    init_db()
    lock = process_lock("bot")
    telegram = TelegramClient()
    with session_scope() as db:
        state = db.get(ServiceState, "bot")
        offset = state.value if state else 0
    try:
        while True:
            try:
                updates = await telegram.call("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
                for update in updates:
                    await handle_update(update, telegram)
                    offset = update["update_id"] + 1
                    heartbeat("bot", value=offset)
                heartbeat("bot", value=offset)
            except TelegramError as exc:
                heartbeat("bot", str(exc))
                log.warning("%s", exc)
                await asyncio.sleep(10)
            except Exception:
                heartbeat("bot", "The Telegram bot encountered an internal error. Check the bot logs.")
                log.exception("Bot update processing failed")
                await asyncio.sleep(10)
    finally:
        await telegram.close()
        lock.close()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(run())

