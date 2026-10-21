import asyncio
import html
import logging

import httpx

from app.config import get_settings

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else get_settings().telegram_bot_token
        self.client = httpx.AsyncClient(timeout=40)

    async def close(self):
        await self.client.aclose()

    async def call(self, method: str, **payload):
        if not self.token:
            raise TelegramError("Не задано TELEGRAM_BOT_TOKEN (або tg) у .env")
        try:
            response = await self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTP exception messages include the bot token in their URL. Never propagate/log them.
            raise TelegramError("Telegram API недоступний; перевірте підключення") from None
        if not data.get("ok"):
            code = data.get("error_code", response.status_code)
            if code == 409:
                raise TelegramError("Telegram 409: зупиніть інший polling-процес або вимкніть webhook цього бота")
            if code == 429:
                await asyncio.sleep(min(data.get("parameters", {}).get("retry_after", 5), 30))
            raise TelegramError(f"Telegram API відхилив запит (код {code})")
        return data["result"]

    async def send(self, chat_id: int, message: str):
        return await self.call("sendMessage", chat_id=chat_id, text=message, parse_mode="HTML",
                               link_preview_options={"is_disabled": True})


def item_message(watch_name: str, data: dict) -> str:
    esc = lambda value: html.escape(str(value or ""))
    price = data.get("price_text") or f'{data.get("price", "—")} {data.get("currency", "")}'
    attrs = list(data.get("attributes", {}).items())[:6]
    lines = ["🔔 <b>Новий товар на eBay</b>", "", f'<b>{esc(data.get("title", "")[:300])}</b>',
             f'💰 {esc(price)}', f'🚚 {esc(data.get("shipping_text") or "Доставка не вказана")}',
             f'📍 {esc(data.get("location") or "Локація не вказана")}',
             f'Стан: {esc(data.get("condition"))}', f'Пошук: {esc(watch_name)}']
    lines += [f"{esc(k)[:100]}: {esc(str(v)[:200])}" for k, v in attrs]
    lines += ["", f'<a href="{esc(data["url"])}">Відкрити на eBay ↗</a>']
    return "\n".join(lines)

