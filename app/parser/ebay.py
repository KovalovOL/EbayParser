"""eBay DOM/HTML parser with HTTP and persistent-browser transports; no private API or CAPTCHA solving."""
import asyncio
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException as CurlRequestError

from app.config import Settings
from app.ebay_filters import NATIVE_BOOLEAN_FILTERS, REGION_PARAMS, country_filter
from app.parser.errors import BrowserError, EbayAccessError, ParseError

log = logging.getLogger(__name__)


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
        raise ParseError("eBay blocked the request or displayed a verification page. A retry is scheduled automatically.")
    if soup.select_one('form[action*="splashui"], #captcha_form, #challenge-form'):
        raise ParseError("eBay requires browser verification. Search results are temporarily unavailable.")


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
        raise ParseError("The eBay results format was not recognized. The parser needs an update; the initial snapshot has not been saved.")
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
        raise ParseError("The eBay item format was not recognized. Item details are temporarily unavailable.")
    return listing


def build_search_url(query: str, filters: dict, page: int = 1) -> str:
    # Keep eBay's default page size. In a live comparison, adding _ipg=240
    # turned a successful search into HTTP 403 in the same Chromium session.
    params = {"_nkw": query, "_sop": "10", "_pgn": str(page), "_dmd": "1", "rt": "nc",
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
    for filter_name, parameter in NATIVE_BOOLEAN_FILTERS.items():
        if filters.get(filter_name):
            params[parameter] = "1"
    region = filters.get("location_region", "default")
    # Existing watches can have a country without the newer location_region field.
    if filters.get("location") and region == "default":
        region = "custom"
    if region in REGION_PARAMS:
        params["LH_PrefLoc"] = REGION_PARAMS[region]
    if filters.get("category_id"):
        params["_sacat"] = filters["category_id"]
    if filters.get("seller"):
        params.update(_sasl=filters["seller"].strip(), _saslop="1")
    if filters.get("exclude_keywords"):
        params["_ex_kw"] = " ".join(
            '"' + value.replace('"', ' ').strip() + '"' for value in filters["exclude_keywords"])
    country = country_filter(filters.get("location", ""))
    if country:
        params.update(_salic=country[0], LH_LocatedIn="1")
    if filters.get("attributes"):
        if not filters.get("category_id"):
            raise ParseError("Choose an eBay category ID before adding item-specific filters.")
        params["_dcat"] = filters["category_id"]
        for key, value in filters["attributes"].items():
            key, value = key.strip(), value.strip()
            if key.startswith("_") or key.casefold().startswith("lh_") or key.casefold() == "rt":
                raise ParseError("Item-specific filter names cannot override eBay search parameters.")
            # eBay facet names/values use an additional encoding layer (e.g. Storage%2520Capacity).
            params[quote(key.strip(), safe="")] = quote(value.strip(), safe="")
    return "https://www.ebay.com/sch/i.html?" + urlencode(params)


def matches_filters(item: Listing, filters: dict, *, native: bool = False) -> bool | None:
    """None means a required value is absent; such a listing must never trigger a notification."""
    missing = False
    # These criteria are enforced by eBay's search, not inferred from incomplete card text.
    regional = filters.get("location_region", "default") in REGION_PARAMS.keys() - {"default", "worldwide"}
    if not native and (regional or any(filters.get(key) for key in NATIVE_BOOLEAN_FILTERS)):
        missing = True
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
        if item.shipping is None and not native:
            missing = True
        elif item.shipping is not None:
            return False
    for key in ("location", "seller"):
        if filters.get(key):
            if not getattr(item, key):
                if not native or (key == "location" and not country_filter(filters[key])):
                    missing = True
            elif key == "location" and (country := country_filter(filters[key])):
                if not any(re.search(r"\b" + re.escape(value) + r"\b", item.location, re.I)
                           for value in (country[1], *country[2])):
                    return False
            elif filters[key].casefold() not in getattr(item, key).casefold():
                return False
    conditions = {"new": ("new", "newcondition"), "used": ("used", "pre-owned", "usedcondition"),
                  "refurbished": ("refurbished",), "parts": ("parts", "not working")}
    desired = filters.get("condition", "any")
    if desired in conditions:
        if not item.condition:
            missing |= not native
        else:
            condition = item.condition.casefold()
            if not any(x in condition for x in conditions[desired]):
                return False
            if desired == "new" and any(x in condition for x in ("used", "parts", "refurbished", "like new")):
                return False
    desired_format = filters.get("buying_format", "any")
    if desired_format != "any":
        if not item.buying_format:
            missing |= not native
        elif desired_format != item.buying_format:
            return False
    attrs = {k.casefold().strip(): str(v).casefold() for k, v in item.attributes.items()}
    for key, expected in filters.get("attributes", {}).items():
        value = attrs.get(key.casefold().strip())
        if value is None:
            missing |= not native
        elif expected.casefold() not in value:
            return False
    return None if missing else True


class EbayClient:
    def __init__(self, settings: Settings, *, on_browser=None):
        self.settings = settings
        self.next_request_at = 0.0
        self.blocked_until = 0.0
        self.search_referrer = "https://www.ebay.com/"
        self.lock = asyncio.Lock()
        self.on_browser = on_browser
        self.last_browser = None
        self.active_transport = None
        self.fallback_client = None
        if settings.ebay_http_client == "playwright":
            from app.browser import BrowserTransport
            self.client = BrowserTransport(settings)
            if settings.ebay_firefox_fallback:
                self.fallback_client = BrowserTransport(settings, engine="firefox")
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
        try:
            if self.settings.ebay_http_client in {"curl_cffi", "playwright"}:
                await self.client.close()
            else:
                await self.client.aclose()
        finally:
            if self.fallback_client is not None:
                await self.fallback_client.close()

    @staticmethod
    def validate_url(url: str):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.ebay.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ParseError("eBay item address is not allowed")

    def access_error(self, message: str, retry_after: str = "", *, status_code=None):
        cooldown = self.settings.ebay_block_cooldown_seconds
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 0
        return EbayAccessError(message, time.time() + max(cooldown, min(86400, delay)), status_code=status_code)

    async def fetch(self, url: str, *, referer: str | None = None) -> str:
        self.validate_url(url)
        if referer:
            self.validate_url(referer)
        async with self.lock:
            if self.blocked_until > time.time():
                raise EbayAccessError("eBay access is restricted. All searches are waiting for the shared retry delay.", self.blocked_until)
            browser = self.settings.ebay_http_client == "playwright"
            candidates = [("chromium" if browser else self.settings.ebay_http_client, self.client)]
            if self.fallback_client is not None:
                candidates.append(("firefox", self.fallback_client))
            failures = []
            for name, client in candidates:
                # Only one browser stays open on the VPS. Native profiles survive closing.
                if browser:
                    if self.active_transport is not None and self.active_transport is not client:
                        await self.active_transport.close()
                    self.active_transport = client
                    self.last_browser = name
                    if self.on_browser:
                        self.on_browser(name)
                    log.info("Checking eBay with %s%s", name, " after Chromium failed" if failures else "")
                try:
                    return await self.fetch_with(client, url, referer=referer)
                except (EbayAccessError, BrowserError) as exc:
                    failures.append((name, exc))
                    # An explicit rate limit applies to the service, not a browser profile.
                    if isinstance(exc, EbayAccessError) and exc.status_code == 429:
                        break
                except ParseError as exc:
                    if not failures:
                        raise  # Invalid addresses/redirects must not trigger another browser.
                    failures.append((name, exc))
                    break  # Preserve Chromium's access restriction if Firefox also failed.
            access_failures = [exc for _, exc in failures if isinstance(exc, EbayAccessError)]
            message = " ".join(f"{name.title()}: {exc}" for name, exc in failures) if browser else str(failures[0][1])
            if access_failures:
                self.blocked_until = max(time.time() + self.settings.ebay_block_cooldown_seconds,
                                         *(exc.retry_at for exc in access_failures))
                minutes = round((self.blocked_until - time.time()) / 60)
                raise EbayAccessError(f"{message} All eBay requests are paused for at least {minutes} min.",
                                      self.blocked_until)
            raise BrowserError(message)

    async def fetch_with(self, client, url: str, *, referer: str | None = None) -> str:
        for _ in range(4):
            wait = self.next_request_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await client.get(url, **({"headers": {"Referer": referer}} if referer else {}))
            except (httpx.HTTPError, CurlRequestError):
                raise ParseError("Could not connect to eBay. Check the network and HTTP client settings.") from None
            finally:
                # Redirects and fallback attempts share the same request spacing.
                self.next_request_at = time.monotonic() + random.uniform(
                    self.settings.request_delay_seconds,
                    self.settings.request_delay_seconds + self.settings.request_delay_jitter_seconds,
                )
            if response.status_code in (403, 429, 503):
                raise self.access_error(f"eBay restricted access (HTTP {response.status_code}).",
                                        response.headers.get("Retry-After", ""), status_code=response.status_code)
            final_path = urlparse(str(getattr(response, "url", url))).path.lower()
            if any(part in final_path for part in ("splashui", "captcha", "challenge")):
                raise self.access_error("eBay requires human verification.", response.headers.get("Retry-After", ""))
            if 300 <= response.status_code < 400:
                target = urljoin(url, response.headers.get("Location", ""))
                self.validate_url(target)
                path = urlparse(target).path
                if any(part in path.lower() for part in ("splashui", "captcha", "challenge")):
                    raise self.access_error("eBay requires browser verification; the HTTP client cannot access this page.")
                if not path.startswith(("/itm/", "/sch/")):
                    raise ParseError("eBay redirected the request outside item and search pages.")
                url = target
                continue
            if response.status_code != 200:
                raise ParseError(f"eBay returned HTTP {response.status_code}.")
            if len(response.content) > 12_000_000:
                raise ParseError("The eBay page exceeds the size limit.")
            title = re.search(r"<title[^>]*>(.*?)</title>", response.text[:20000], re.I | re.S)
            if title and any(label in title[1].lower() for label in (
                    "pardon our interruption", "access denied", "robot check", "error page")):
                raise self.access_error("eBay returned a verification page instead of search results.")
            return response.text
        raise ParseError("Too many eBay redirects")

    async def search(self, query: str, filters: dict) -> tuple[list[Listing], bool, str]:
        self.search_referrer = build_search_url(query, filters)
        page = parse_search(await self.fetch(self.search_referrer))
        return page.listings, page.has_next, self.search_referrer
