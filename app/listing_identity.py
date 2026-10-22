"""Stable identities for search-card matches; independent of eBay IDs and publication dates."""
import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation


def normalized_text(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def listing_identity(data: dict) -> dict:
    title = normalized_text(data.get("title"))
    location = re.sub(r"^(?:located in|from)\s+", "", normalized_text(data.get("location")))
    currency = str(data.get("currency") or "").strip().upper()
    try:
        price = Decimal(str(data.get("price")))
        if not price.is_finite():
            raise InvalidOperation
        price = format(price.normalize(), "f") if price else "0"
    except (InvalidOperation, ValueError, TypeError):
        price = "text:" + normalized_text(data.get("price_text"))
    parts = [title, currency, price, location]
    fingerprint = hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"fingerprint": fingerprint, "title": title, "price": price,
            "currency": currency, "location": location}
