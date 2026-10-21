"""One long-polling process. Bot token is never placed in logs."""
import asyncio
import html
import logging
import secrets
import time

from app.db import init_db, session_scope
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
    if not command or command[0].split("@")[0] != "/start":
        return
    if len(command) != 2 or not command[1].startswith("login_"):
        await telegram.send(chat["id"], "Вітаю! Відкрийте сайт eBay Signal, вкажіть свій @username і перейдіть за посиланням входу. Тут надходитимуть нові товари з ваших пошуків.")
        return
    challenge_id = command[1][6:]
    with session_scope() as db:
        challenge = db.get(LoginChallenge, challenge_id)
        if not challenge or challenge.used or challenge.expires_at < time.time():
            error = "Посилання входу закінчилося. Створіть новий запит на сайті."
        elif not sender.get("username") or sender["username"].lower() != challenge.username:
            error = "Username не збігається з указаним на сайті. Перевірте @username та створіть новий запит."
        elif challenge.code_hash:
            error = "Код уже надіслано вище. Введіть його на сайті або створіть новий запит."
        else:
            error = None
            code = f"{secrets.randbelow(1_000_000):06d}"
            # Keep the transaction open until delivery succeeds; failed delivery does not consume the challenge.
            await telegram.send(chat["id"], f"🔐 <b>Ваш код входу в eBay Signal</b>\n\n<code>{code}</code>\n\nДіє 10 хвилин від запиту на сайті. Введіть його лише на вашому сайті eBay Signal.\n@{html.escape(sender['username'])}")
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
                heartbeat("bot", "Внутрішня помилка бота; дивіться логи")
                log.exception("Bot update processing failed")
                await asyncio.sleep(10)
    finally:
        await telegram.close()
        lock.close()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(run())

