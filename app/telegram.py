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
            raise TelegramError("The Telegram bot is not configured. Contact the administrator.")
        try:
            response = await self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTP exception messages include the bot token in their URL. Never propagate/log them.
            raise TelegramError("Telegram is unavailable. Check the network connection.") from None
        if not data.get("ok"):
            code = data.get("error_code", response.status_code)
            if code == 409:
                raise TelegramError("Telegram 409: stop the other bot process or disable this bot's webhook.")
            if code == 429:
                await asyncio.sleep(min(data.get("parameters", {}).get("retry_after", 5), 30))
            raise TelegramError(f"Telegram rejected the request (code {code})")
        return data["result"]

    async def send(self, chat_id: int, message: str):
        return await self.call("sendMessage", chat_id=chat_id, text=message, parse_mode="HTML",
                               link_preview_options={"is_disabled": True})


def item_message(watch_name: str, data: dict) -> str:
    esc = lambda value: html.escape(str(value or ""))
    price = data.get("price_text") or f'{data.get("price", "—")} {data.get("currency", "")}'
    attrs = list(data.get("attributes", {}).items())[:6]
    lines = ["🔔 <b>New eBay match</b>", "", f'<b>{esc(data.get("title", "")[:300])}</b>',
             f'💰 {esc(price)}', f'🚚 {esc(data.get("shipping_text") or "Shipping not specified")}',
             f'📍 {esc(data.get("location") or "Location not specified")}',
             f'Condition: {esc(data.get("condition"))}', f'Search: {esc(watch_name)}']
    lines += [f"{esc(k)[:100]}: {esc(str(v)[:200])}" for k, v in attrs]
    lines += ["", f'<a href="{esc(data["url"])}">View on eBay ↗</a>']
    return "\n".join(lines)

