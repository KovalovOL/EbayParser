"""eBay DOM/HTML parser with HTTP and persistent-browser transports; no private API or CAPTCHA solving."""
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException as CurlRequestError

from app.config import Settings
from app.parser.errors import EbayAccessError, ParseError


def text(node: Tag | None) -> str:
    return " ".join(node.stripped_strings) if node else ""


def pick(node, *selectors) -> str:
    for selector in selectors:
        value = text(node.select_one(selector))
        if value:
            return value
    return ""


def amount(value: str) -> float | None:
    match = re.search(r"\d[\d\s,.]*", value.replace("\xa0", " "))
    if not match:
        return None
    number = match.group().strip().replace(" ", "").rstrip(".,")
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(",", ".") if len(number.rsplit(",", 1)[1]) == 2 else number.replace(",", "")
    try:
        return float(Decimal(number))
    except InvalidOperation:
        return None


def currency(value: str) -> str:
    # Never guess that an unrecognized currency is USD.
    for pattern, code in ((r"US\s*\$|USD", "USD"), (r"C\s*\$|CAD", "CAD"),
                          (r"AU\s*\$|A\s*\$|AUD", "AUD"), (r"€|EUR", "EUR"),
                          (r"£|GBP", "GBP"), (r"DKK", "DKK"), (r"JPY|¥", "JPY")):
        if re.search(pattern, value, re.I):
            return code
    return "USD" if "$" in value else ""


def timestamp(value) -> float | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            return number / 1000 if number > 100_000_000_000 else number
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # A date without an offset/time is not evidence of a precise listing time.
        return date.timestamp() if date.tzinfo and "T" in str(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def canonical_url(value: str) -> tuple[str, str] | None:
    parsed = urlparse(urljoin("https://www.ebay.com", value))
    if parsed.hostname not in {"ebay.com", "www.ebay.com"} or parsed.scheme != "https":
        return None
    match = re.search(r"/itm/(?:[^/]+/)?(\d{9,15})(?:[/?]|$)", parsed.path)
    return (match[1], f"https://www.ebay.com/itm/{match[1]}") if match else None


@dataclass
class Listing:
    ebay_id: str
    url: str
    title: str
    price: float | None = None
    currency: str = ""
    price_text: str = ""
    shipping: float | None = None
    shipping_text: str = ""
    condition: str = ""
    location: str = ""
    seller: str = ""
    buying_format: str = ""
    image_url: str = ""
    published_at: float | None = None
    availability: str = ""
    attributes: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class SearchPage:
    listings: list[Listing]
    has_next: bool
    total_text: str
    layout: str


def check_page(soup: BeautifulSoup):
    title = text(soup.title).lower()
    if any(term in title for term in ("pardon our interruption", "access denied", "robot check", "error page")):
        raise ParseError("eBay заблокував запит або показав сторінку перевірки. Спробуємо пізніше.")
    if soup.select_one('form[action*="splashui"], #captcha_form, #challenge-form'):
        raise ParseError("eBay вимагає перевірку браузера. HTML видачі недоступний.")


def parse_search(html: str) -> SearchPage:
    soup = BeautifulSoup(html, "html.parser")
    check_page(soup)
    root = soup.select_one("#srp-river-results") or soup.select_one(".srp-river-results") or soup
    cards = root.select("li.s-card")
    layout = "s-card" if cards else "s-item"
    cards = cards or root.select("li.s-item")
    listings = []
    known = set()
    for card in cards:
        # eBay inserts broader/recommended results after an explicit separator.
        section = card.find_previous(class_=re.compile(r"srp-river-answer|srp-river-message"))
        if section and re.search(r"fewer words|similar|related|no exact|results matching fewer", text(section), re.I):
            continue
        link = card.select_one('a.s-card__link[href*="/itm/"], a.s-item__link[href*="/itm/"]')
        pair = canonical_url(link.get("href", "")) if link else None
        title = pick(card, ".s-card__title .su-styled-text.primary", ".s-card__title", ".s-item__title")
        title = re.sub(r"^New Listing\s*", "", title, flags=re.I).strip()
        title = re.sub(r"\s*Opens in a new window or tab\s*$", "", title, flags=re.I).strip()
        if not pair or not title or "shop on ebay" in title.lower() or pair[0] in known:
            continue
        known.add(pair[0])
        price_node = card.select_one(".s-card__price, .s-item__price")
        if price_node:
            price_node = BeautifulSoup(str(price_node), "html.parser")
            for strike in price_node.select("s, del, .strikethrough, .STRIKETHROUGH"):
                strike.decompose()
        price_text = text(price_node)
        rows = [text(x) for x in card.select(".s-card__attribute-row, .s-card__footer--row") if text(x)]
        shipping = pick(card, ".s-item__shipping", ".s-item__logisticsCost")
        shipping = shipping or next((x for x in rows if re.search(r"shipping|delivery|postage", x, re.I)), "")
        ship_amount = 0.0 if re.search(r"free.*(?:shipping|delivery)|(?:shipping|delivery).*free", shipping, re.I) else amount(shipping)
        # Import charges or delivery dates are not shipping prices.
        if shipping and not re.search(r"\$|USD|EUR|€|GBP|£|CAD|AUD", shipping) and ship_amount != 0:
            ship_amount = None
        if ship_amount and currency(shipping) and currency(shipping) != currency(price_text):
            ship_amount = None
        image = card.select_one(".s-card__image img, img.s-card__image, .s-item__image-wrapper img, img.s-item__image-img")
        subtitle = pick(card, ".s-card__subtitle", ".SECONDARY_INFO", ".s-item__subtitle")
        location = pick(card, ".s-item__location", ".s-card__location")
        location = location or next((x for x in rows if re.match(r"(?:Located in|From)\b", x, re.I)), "")
        seller = pick(card, ".s-item__seller-info-text", ".s-card__seller")
        seller = seller or next((x for x in rows if re.search(r"positive|seller", x, re.I)), "")
        whole = text(card)
        fmt = "auction" if re.search(r"\b\d+\s+bids?\b", whole, re.I) else (
            "buy_now" if re.search(r"buy it now|or best offer", whole, re.I) else "")
        listings.append(Listing(
            ebay_id=pair[0], url=pair[1], title=title,
            price=amount(price_text), currency=currency(price_text), price_text=price_text,
            shipping=ship_amount, shipping_text=shipping, condition=subtitle,
            location=location, seller=seller, buying_format=fmt,
            image_url=(image.get("data-src") or image.get("data-defer-load") or image.get("src") or "") if image else "",
            raw={"layout": layout, "rows": rows, "subtitle": subtitle,
                 "card_attributes": dict(card.attrs), "text": whole},
        ))
    total_text = pick(soup, "h1.srp-controls__count-heading", ".srp-controls__count-heading")
    empty = soup.select_one(".srp-save-null-search, .srp-river-answer--NO_RESULTS, .srp-river-answer--NO_RESULTS_FOUND")
    empty = empty or bool(re.search(r"\b0 results\b|No exact matches found|No matching results", total_text + " " + text(root), re.I))
    if not listings and not empty:
        raise ParseError("Не розпізнано HTML видачі eBay. Потрібна перевірка селекторів; початковий знімок не збережено.")
    next_link = soup.select_one('a.pagination__next[href], a[aria-label="Go to next search page"][href]')
    return SearchPage(listings, bool(next_link and next_link.get("aria-disabled") != "true"), total_text, layout)


def json_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_nodes(child)


def parse_detail(html: str, listing: Listing) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    check_page(soup)
    attributes = dict(listing.attributes)
    for row in soup.select(".ux-labels-values"):
        label = pick(row, ".ux-labels-values__labels").rstrip(":")
        value = pick(row, ".ux-labels-values__values")
        if label and value:
            attributes[label] = value
    for label in soup.select("dl.ux-labels-values dt, .itemAttr dt"):
        value = label.find_next_sibling("dd")
        if text(label) and value:
            attributes[text(label).rstrip(":")] = text(value)
    for label in soup.select('td.attrLabels'):
        value = label.find_next_sibling("td")
        if value:
            attributes[text(label).rstrip(":")] = text(value)
    structured = []
    product_parsed = False
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        structured.append(obj)
        for node in json_nodes(obj):
            kind = node.get("@type", "")
            if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
                product_url = canonical_url(str(node.get("url", "")))
                if product_url and product_url[0] != listing.ebay_id:
                    continue
                if product_parsed:
                    continue
                product_parsed = True
                for prop in node.get("additionalProperty", []):
                    if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
                        attributes[str(prop["name"])] = prop["value"]
                listing.condition = str(node.get("itemCondition") or listing.condition).split("/")[-1]
                published = timestamp(node.get("datePublished"))
                if published:
                    listing.published_at = published
                offers = node.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    listing.availability = str(offers.get("availability", ""))
    # Explicit item publication metadata; never dateModified, first-seen, bid end time or price validFrom.
    for meta in soup.select('meta[itemprop="datePublished"], meta[property="product:published_time"], time[itemprop="datePublished"]'):
        published = timestamp(meta.get("content") or meta.get("datetime"))
        if published:
            listing.published_at = published
    # eBay's embedded item JSON sometimes exposes startTime. Only accept it next to this item's ID.
    for script in soup.select('script[type="application/json"]'):
        try:
            obj = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for node in json_nodes(obj):
            if str(node.get("itemId", "")) == listing.ebay_id:
                published = timestamp(node.get("startTime") or node.get("listingDate"))
                if published:
                    listing.published_at = published
    listing.attributes = attributes
    listing.title = pick(soup, "h1.x-item-title__mainTitle", "h1#itemTitle") or listing.title
    listing.location = pick(soup, ".ux-labels-values--itemLocation .ux-labels-values__values") or listing.location
    listing.seller = pick(soup, ".x-sellercard-atf__info__about-seller a", ".x-sellercard-atf__info__about-seller") or listing.seller
    listing.condition = pick(soup, ".x-item-condition-text", ".ux-icon-text--itemCondition") or listing.condition
    if not listing.buying_format:
        buybox = pick(soup, ".x-buybox", "#bidBtn_btn")
        if re.search(r"Place bid|Enter.*bid", buybox, re.I):
            listing.buying_format = "auction"
        elif re.search(r"Buy It Now", buybox, re.I):
            listing.buying_format = "buy_now"
    listing.raw = {**listing.raw, "json_ld": structured,
                   "description": pick(soup, "#viTabs_0_is", ".x-item-description"),
                   "description_url": (soup.select_one("iframe#desc_ifr").get("src", "")
                                       if soup.select_one("iframe#desc_ifr") else ""),
                   "detail_meta": {m.get("property") or m.get("itemprop"): m.get("content")
                                   for m in soup.select('meta[content]') if m.get("property") or m.get("itemprop")}}
    if not attributes and not structured and not soup.select_one("h1.x-item-title__mainTitle, #itemTitle"):
        raise ParseError("Не розпізнано сторінку товару eBay; характеристики поки недоступні.")
    return listing


def build_search_url(query: str, filters: dict, page: int = 1) -> str:
    params = {"_nkw": query, "_sop": "10", "_ipg": "240", "_pgn": str(page), "_dmd": "1",
              "_curr": filters.get("currency", "USD")}
    if filters.get("min_price") is not None and not filters.get("include_shipping"):
        params["_udlo"] = str(filters["min_price"])
    if filters.get("max_price") is not None:
        params["_udhi"] = str(filters["max_price"])
    condition = {"new": "1000", "used": "3000", "refurbished": "2000|2010|2020|2030|2500", "parts": "7000"}
    if filters.get("condition") in condition:
        params["LH_ItemCondition"] = condition[filters["condition"]]
    if filters.get("buying_format") in ("auction", "buy_now"):
        params["LH_Auction" if filters["buying_format"] == "auction" else "LH_BIN"] = "1"
    if filters.get("free_shipping"):
        params["LH_FS"] = "1"
    if filters.get("category_id"):
        params["_sacat"] = filters["category_id"]
    return "https://www.ebay.com/sch/i.html?" + urlencode(params)


def matches_filters(item: Listing, filters: dict) -> bool | None:
    """None means a required value is absent; such a listing must never trigger a notification."""
    missing = False
    if any(value in item.availability.casefold() for value in ("outofstock", "soldout", "discontinued")):
        return False
    if any(word.casefold() in (item.title + " " + item.condition).casefold()
           for word in filters.get("exclude_keywords", [])):
        return False
    if item.currency != filters.get("currency", "USD"):
        return False if item.currency else None
    value = item.price
    if filters.get("include_shipping"):
        value = value + item.shipping if value is not None and item.shipping is not None else None
    for key, compare in (("min_price", lambda a, b: a >= b), ("max_price", lambda a, b: a <= b)):
        if filters.get(key) is not None:
            if value is None:
                missing = True
            elif not compare(value, filters[key]):
                return False
    if filters.get("free_shipping") and item.shipping != 0:
        if item.shipping is None:
            missing = True
        else:
            return False
    for key in ("location", "seller"):
        if filters.get(key):
            if not getattr(item, key):
                missing = True
            elif filters[key].casefold() not in getattr(item, key).casefold():
                return False
    conditions = {"new": ("new", "newcondition"), "used": ("used", "pre-owned", "usedcondition"),
                  "refurbished": ("refurbished",), "parts": ("parts", "not working")}
    desired = filters.get("condition", "any")
    if desired in conditions:
        if not item.condition:
            missing = True
        else:
            condition = item.condition.casefold()
            if not any(x in condition for x in conditions[desired]):
                return False
            if desired == "new" and any(x in condition for x in ("used", "parts", "refurbished", "like new")):
                return False
    desired_format = filters.get("buying_format", "any")
    if desired_format != "any":
        if not item.buying_format:
            missing = True
        elif desired_format != item.buying_format:
            return False
    attrs = {k.casefold().strip(): str(v).casefold() for k, v in item.attributes.items()}
    for key, expected in filters.get("attributes", {}).items():
        value = attrs.get(key.casefold().strip())
        if value is None:
            missing = True
        elif expected.casefold() not in value:
            return False
    return None if missing else True


class EbayClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_request = 0.0
        self.blocked_until = 0.0
        self.search_referrer = "https://www.ebay.com/"
        self.lock = asyncio.Lock()
        if settings.ebay_http_client == "playwright":
            from app.browser import BrowserTransport
            self.client = BrowserTransport(settings)
        elif settings.ebay_http_client == "curl_cffi":
            # curl_cffi supplies a consistent browser TLS/HTTP profile and matching headers.
            # Keep the same session and its server-issued cookies across search/detail requests.
            self.client = AsyncSession(impersonate=settings.ebay_browser, max_clients=1,
                timeout=35, allow_redirects=False, proxy=settings.ebay_proxy or None,
                headers={"Accept-Language": "en-US,en;q=0.9"})
        else:
            self.client = httpx.AsyncClient(
                timeout=35, follow_redirects=False, proxy=settings.ebay_proxy or None,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                         "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html"},
            )

    async def close(self):
        if self.settings.ebay_http_client in {"curl_cffi", "playwright"}:
            await self.client.close()
        else:
            await self.client.aclose()

    @staticmethod
    def validate_url(url: str):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.ebay.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ParseError("Недозволена адреса товару eBay")

    def block(self, message: str, retry_after: str = ""):
        cooldown = self.settings.ebay_block_cooldown_seconds
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 0
        self.blocked_until = time.time() + max(cooldown, min(86400, delay))
        raise EbayAccessError(message + " Усі запити до eBay призупинено щонайменше на "
                              + str(round((self.blocked_until - time.time()) / 60)) + " хв.", self.blocked_until)

    async def fetch(self, url: str, *, referer: str | None = None) -> str:
        self.validate_url(url)
        if referer:
            self.validate_url(referer)
        for _ in range(4):
            async with self.lock:
                if self.blocked_until > time.time():
                    raise EbayAccessError("Доступ eBay обмежено. Діє спільна пауза для всіх пошуків.", self.blocked_until)
                await asyncio.sleep(max(0, self.settings.request_delay_seconds - (time.monotonic() - self.last_request)))
                try:
                    response = await self.client.get(url, **({"headers": {"Referer": referer}} if referer else {}))
                except (httpx.HTTPError, CurlRequestError):
                    raise ParseError("Не вдалося з’єднатися з eBay. Перевірте мережу та налаштування HTTP-клієнта.") from None
                finally:
                    self.last_request = time.monotonic()
                final_path = urlparse(str(getattr(response, "url", url))).path.lower()
                if any(part in final_path for part in ("splashui", "captcha", "challenge")):
                    self.block("eBay вимагає підтвердження людиною. Автоматичну перевірку призупинено.")
                if response.status_code in (403, 429, 503):
                    self.block(f"eBay обмежив доступ (HTTP {response.status_code}).", response.headers.get("Retry-After", ""))
                if 300 <= response.status_code < 400:
                    target = urljoin(url, response.headers.get("Location", ""))
                    self.validate_url(target)
                    path = urlparse(target).path
                    if any(part in path.lower() for part in ("splashui", "captcha", "challenge")):
                        self.block("eBay вимагає перевірку браузера; сторінка недоступна HTTP-клієнту.")
                    if not path.startswith(("/itm/", "/sch/")):
                        raise ParseError("eBay перенаправив запит за межі сторінок товарів і пошуку")
                    url = target
                    continue
                if response.status_code != 200:
                    raise ParseError(f"eBay повернув HTTP {response.status_code}.")
                if len(response.content) > 12_000_000:
                    raise ParseError("Сторінка eBay перевищує допустимий розмір")
                title = re.search(r"<title[^>]*>(.*?)</title>", response.text[:20000], re.I | re.S)
                if title and any(label in title[1].lower() for label in (
                        "pardon our interruption", "access denied", "robot check", "error page")):
                    self.block("eBay повернув сторінку перевірки замість результатів пошуку.")
                return response.text
        raise ParseError("Забагато перенаправлень eBay")

    async def search(self, query: str, filters: dict) -> tuple[list[Listing], bool, str]:
        self.search_referrer = build_search_url(query, filters)
        items = {}
        page = None
        for number in range(1, self.settings.max_search_pages + 1):
            page = parse_search(await self.fetch(build_search_url(query, filters, number)))
            for item in page.listings:
                items[item.ebay_id] = item
            if not page.has_next:
                break
        return list(items.values()), bool(page and page.has_next), build_search_url(query, filters)

    async def detail(self, listing: Listing) -> Listing:
        return parse_detail(await self.fetch(listing.url, referer=self.search_referrer), listing)
