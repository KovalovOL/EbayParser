"""First-page adapters for the public Buyee and Kleinanzeigen catalogs."""
import asyncio
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from app.parser.ebay import Listing, SearchPage, pick, text
from app.parser.errors import ParseError
from app.platforms import MARKET_CURRENCIES, PLATFORM_NAMES

ORIGINS = {"buyee": "https://buyee.jp", "kleinanzeigen": "https://www.kleinanzeigen.de"}
BUYEE_ITEM = re.compile(r"/(?:item/jdirectitems/auction|paypayfleamarket/item)/([a-z][0-9]{5,20})")
KLEIN_ITEM = re.compile(r"/s-anzeige/[^/]+/([0-9]{5,20})-[0-9]+-[0-9]+")
PRICE_JPY = re.compile(r"(?:¥\s*([\d,]+)|([\d,]+)\s*(?:JPY|YEN|円))", re.I)
PRICE_EUR = re.compile(r"^\s*(\d+(?:\.\d{3})*(?:,\d{1,2})?)\s*€")
MAX_HTML = 12_000_000


class MarketAccessError(ParseError):
    def __init__(self, message, retry_at):
        super().__init__(message)
        self.retry_at = retry_at


def build_market_url(platform: str, query: str, filters: dict) -> str:
    if platform == "buyee":
        # Verified against live HTML; the equivalent keyword path returned a challenge.
        params = {"query": query, "sort": "end", "order": "d", "lang": "en"}
        for key, native in (("min_price", "aucminprice"), ("max_price", "aucmaxprice")):
            if filters.get(key) is not None:
                params[native] = filters[key]
        return f"{ORIGINS[platform]}/item/search?" + urlencode(params)
    params = {"keywords": query, "sortingField": "SORTING_DATE", "adType": "OFFER"}
    for key, native in (("min_price", "minPrice"), ("max_price", "maxPrice")):
        if filters.get(key) is not None:
            params[native] = filters[key]
    return ORIGINS[platform] + "/s-suchanfrage.html?" + urlencode(params)


def _same_origin(value: str, platform: str):
    try:
        parsed = urlsplit(urljoin(ORIGINS[platform], value))
        if (parsed.scheme == "https" and parsed.hostname == urlsplit(ORIGINS[platform]).hostname
                and parsed.port in (None, 443) and not parsed.username and not parsed.password):
            return parsed
    except ValueError:
        pass
    return None


def item_link(platform: str, href: str):
    parsed = _same_origin(href, platform)
    pattern = BUYEE_ITEM if platform == "buyee" else KLEIN_ITEM
    if parsed and (match := pattern.fullmatch(parsed.path)):
        # Strip tracking parameters. Namespace the old DB's ebay_id key so IDs from
        # different platforms can coexist without rewriting historical eBay rows.
        return f"{platform}:{match[1]}", ORIGINS[platform] + parsed.path
    return None


def _image(card, platform):
    image = card.select_one("img[data-src], img[src]")
    if not image:
        return ""
    try:
        parsed = urlsplit(urljoin(ORIGINS[platform], image.get("data-src") or image.get("src", "")))
        host = parsed.hostname or ""
        domain = "buyee.jp" if platform == "buyee" else "kleinanzeigen.de"
        if (parsed.scheme == "https" and host.endswith("." + domain)
                and not parsed.username and not parsed.password and parsed.port in (None, 443)):
            return parsed.geturl()
    except ValueError:
        pass
    return ""


def parse_buyee(html: str) -> SearchPage:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".auctionSearchResult li.itemCard")
    listings = {}
    for card in cards:
        anchor = card.select_one(".itemCard__itemName a[href]")
        link = item_link("buyee", anchor["href"]) if anchor else None
        title = text(anchor)
        if not (link and title):
            continue
        price_text = pick(card, ".g-priceDetails .g-price")
        found = PRICE_JPY.search(price_text)
        price = int((found[1] or found[2]).replace(",", "")) if found else None
        # Buyee co-lists flea market items with a fixed price on this catalog.
        kind = "buy_now" if "/paypayfleamarket/" in link[1] or not card.select_one(
            ".itemCard__infoList") else "auction"
        listings[link[0]] = Listing(*link, title, price=price, currency="JPY", price_text=price_text,
                                    location="Japan", buying_format=kind, image_url=_image(card, "buyee"),
                                    platform="buyee")
    if not listings:
        # Do not interpret an unrecognized page, block or empty template as a scan.
        empty = pick(soup, ".bidNotfound_middle_tit") == "No Results Found"
        if cards or not empty or not soup.select_one("form [name=query]"):
            raise ParseError("Buyee search results could not be recognized.")
    return SearchPage(list(listings.values()), bool(soup.select_one("nav.search_page_navi a")),
                      "", "buyee-auction")


def parse_kleinanzeigen(html: str) -> SearchPage:
    soup = BeautifulSoup(html, "html.parser")
    summary = pick(soup, "#srp-breadcrumb-summary")
    cards = soup.select("#srchrslt-adtable article[data-adid]")
    listings = {}
    for card in cards:
        anchor = card.select_one('h3 a[href*="/s-anzeige/"], h2 a[href*="/s-anzeige/"]')
        if not anchor:
            heading = card.select_one("h3, h2")
            anchor = heading.find_parent("a", href=True) if heading else None
        link = item_link("kleinanzeigen", card.get("data-href") or (anchor["href"] if anchor else ""))
        title = pick(card, "h3", "h2")
        if not link or link[0] != "kleinanzeigen:" + card["data-adid"] or not title:
            continue
        price_text = pick(card, "p.text-title3", ".aditem-main--middle--price-shipping--price")
        found = PRICE_EUR.search(price_text)
        price = float(found[1].replace(".", "").replace(",", ".")) if found else (
            0 if price_text.casefold() == "zu verschenken" else None)
        location_icon = card.select_one('svg[data-title="locationOutline"]')
        location = text(location_icon.parent) if location_icon else pick(card, ".aditem-main--top--left")
        shipping = next((text(tag) for tag in card.select("span") if text(tag) in
                         ("Versand möglich", "Nur Abholung", "Keine Lieferung")), "")
        listings[link[0]] = Listing(*link, title, price=price, currency="EUR", price_text=price_text,
                                    location=location, shipping_text=shipping, image_url=_image(card, "kleinanzeigen"),
                                    platform="kleinanzeigen")
    if not listings and (cards or not summary.startswith("Es wurden keine Ergebnisse")):
        raise ParseError("Kleinanzeigen search results could not be recognized.")
    return SearchPage(list(listings.values()), bool(soup.select_one('a[href*="seite:2/"], a[rel=next]')),
                      summary, "kleinanzeigen")


def matches_market(item: Listing, filters: dict) -> bool | None:
    if item.currency != MARKET_CURRENCIES[item.platform]:
        return False if item.currency else None
    low, high = filters.get("min_price"), filters.get("max_price")
    if item.price is None:
        return None if low is not None or high is not None else True
    return (low is None or item.price >= low) and (high is None or item.price <= high)


def _search_target(url, platform):
    parsed = _same_origin(url, platform)
    if not parsed:
        return None
    path = parsed.path
    if platform == "buyee":
        allowed = path == "/item/search" or path.startswith("/item/search/")
    else:
        allowed = path == "/s-suchanfrage.html" or (path.startswith("/s-") and path.rsplit("/", 1)[-1] == "k0")
    return urlunsplit(parsed._replace(fragment="")) if allowed else None


class MarketClient:
    def __init__(self, settings, platform):
        self.platform = platform
        self.name = PLATFORM_NAMES[platform]
        self.delay = settings.request_delay_seconds
        self.jitter = settings.request_delay_jitter_seconds
        self.cooldown = settings.ebay_block_cooldown_seconds
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.client = AsyncSession(impersonate="chrome", timeout=20, allow_redirects=False, max_clients=1)

    async def close(self):
        await self.client.close()

    def _restricted(self, status, retry_after):
        try:
            delay = int(retry_after)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = 0
        self.blocked_until = time.time() + max(self.cooldown, min(86400, delay))
        return MarketAccessError(f"{self.name} restricted access (HTTP {status}).", self.blocked_until)

    async def search(self, query, filters):
        if self.blocked_until > time.time():
            raise MarketAccessError(f"{self.name} is waiting to retry after an access restriction.", self.blocked_until)
        url = build_market_url(self.platform, query, filters)
        for _ in range(4):
            if not _search_target(url, self.platform):
                raise ParseError(f"{self.name} returned a redirect outside its search catalog.")
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            try:
                response = await self.client.get(url)
            except RequestException:
                raise ParseError(f"Could not connect to {self.name}.") from None
            finally:
                self.next_request = time.monotonic() + self.delay + random.uniform(0, self.jitter)
            if response.status_code in {202, 403, 429, 503}:
                raise self._restricted(response.status_code, response.headers.get("Retry-After", ""))
            if 300 <= response.status_code < 400 and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                continue
            expected_empty = self.platform == "buyee" and response.status_code == 404
            if response.status_code != 200 and not expected_empty:
                raise ParseError(f"{self.name} returned HTTP {response.status_code}.")
            if len(response.content) > MAX_HTML:
                raise ParseError(f"{self.name} search page exceeds the size limit.")
            parser = parse_buyee if self.platform == "buyee" else parse_kleinanzeigen
            page = parser(response.text)
            if expected_empty and page.listings:
                raise ParseError("Buyee returned listings with HTTP 404.")
            return page.listings, page.has_next, url
        raise ParseError(f"{self.name} returned too many redirects.")
