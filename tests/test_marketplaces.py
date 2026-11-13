import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.listing_identity import listing_identity
from app.models import Notification, Watch, WatchItem
from app.parser.ebay import Listing
from app.parser.errors import EbayAccessError, ParseError
from app.parser.markets import MarketAccessError, MarketClient, build_market_url, parse_buyee, parse_kleinanzeigen
from app.runtime import heartbeat
from app.telegram import item_message
from app.worker import scan_watch

FIXTURES = Path(__file__).parent / "fixtures"
PARSERS = {"buyee": parse_buyee, "kleinanzeigen": parse_kleinanzeigen}


def fixture(name):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize("platform, expected", [
    ("buyee", {"query": "ソニー / A&B", "sort": "end", "order": "d", "lang": "en",
               "aucminprice": "0", "aucmaxprice": "500"}),
    ("kleinanzeigen", {"keywords": "ソニー / A&B", "sortingField": "SORTING_DATE", "adType": "OFFER",
                      "minPrice": "0", "maxPrice": "500"}),
])
def test_native_urls_encode_keywords_and_filters(platform, expected):
    url = build_market_url(platform, "ソニー / A&B", {"min_price": 0, "max_price": 500})
    assert parse_qs(urlsplit(url).query) == {key: [value] for key, value in expected.items()}


def test_buyee_reads_auction_and_flea_market_cards_in_yen():
    page = parse_buyee(fixture("buyee-search.html"))
    auction, flea = page.listings
    assert page.has_next and auction.ebay_id.startswith("buyee:") and auction.price == 2200
    assert auction.buying_format == "auction" and flea.buying_format == "buy_now"
    assert auction.currency == flea.currency == "JPY" and flea.price == 8499
    assert auction.url.startswith("https://buyee.jp/item/jdirectitems/auction/") and "?" not in auction.url
    assert flea.image_url.startswith("https://cdnyauction-pctr.buyee.jp/")


def test_kleinanzeigen_reads_modern_dom_and_german_price():
    html = fixture("kleinanzeigen-search.html").replace("50 €", "1.150,50 € VB")
    page = parse_kleinanzeigen(html)
    ad = page.listings[0]
    assert page.has_next and len(page.listings) == 2
    assert (ad.platform, ad.price, ad.currency, ad.location) == ("kleinanzeigen", 1150.50, "EUR", "97076 Würzburg")
    assert ad.ebay_id == "kleinanzeigen:3570591159" and ad.image_url.startswith("https://img.kleinanzeigen.de/")


@pytest.mark.parametrize("platform", PARSERS)
def test_only_recognized_empty_searches_are_accepted(platform):
    parser = PARSERS[platform]
    assert parser(fixture(f"{platform}-empty.html")).listings == []
    with pytest.raises(ParseError):
        parser("<html><title>Verification</title><p>No results found</p></html>")


class Reply:
    def __init__(self, code, body="", **headers):
        self.status_code, self.text, self.content, self.headers = code, body, body.encode(), headers


@pytest.mark.parametrize("response, expected", [
    (Reply(404, fixture("buyee-empty.html")), None),
    (Reply(404, "<title>Page not found</title>"), ParseError),
    (Reply(202, "<title>Challenge</title>"), MarketAccessError),
    (Reply(302, Location="https://not-buyee.example/item/search"), ParseError),
    (Reply(302, Location="/item/jdirectitems/auction/q123456"), ParseError),
])
async def test_buyee_transport_distinguishes_empty_and_rejects_unsafe_redirects(monkeypatch, response, expected):
    client = MarketClient(Settings(request_delay_seconds=0, request_delay_jitter_seconds=0), "buyee")
    requests = []

    async def get(url):
        requests.append(url)
        return response

    monkeypatch.setattr(client.client, "get", get)
    try:
        if expected:
            with pytest.raises(expected):
                await client.search("walkman", {})
        else:
            assert (await client.search("walkman", {}))[:2] == ([], False)
        assert len(requests) == 1
    finally:
        await client.close()


def test_api_saves_platform_selection_and_independent_budgets(client, auth):
    body = {"name": "Walkman", "query": "walkman", "platforms": ["kleinanzeigen", "buyee", "ebay"],
            "filters": {"max_price": 120},
            "market_filters": {"buyee": {"max_price": 15000}, "kleinanzeigen": {"max_price": 100}}}
    response = client.post("/api/watches", headers=auth, json=body)
    assert response.status_code == 201
    assert response.json()["platforms"] == ["ebay", "buyee", "kleinanzeigen"]
    saved = client.get("/api/watches", headers=auth).json()[0]
    assert saved["market_filters"]["buyee"]["max_price"] == 15000 and saved["filters"]["max_price"] == 120
    urls = saved["search_urls"]
    assert parse_qs(urlsplit(urls["buyee"]).query)["aucmaxprice"] == ["15000"]
    assert parse_qs(urlsplit(urls["kleinanzeigen"]).query)["maxPrice"] == ["100"]
    assert parse_qs(urlsplit(urls["ebay"]).query)["_udhi"] == ["120.0"]
    for invalid in ([], ["invalid"], ["buyee", "buyee"]):
        assert client.post("/api/watches", headers=auth, json=body | {"platforms": invalid}).status_code == 422
    updated = client.put(f"/api/watches/{saved['id']}", headers=auth, json=body | {"platforms": ["kleinanzeigen"]})
    assert updated.json()["platforms"] == ["kleinanzeigen"]
    assert "buyee" not in updated.json()["market_filters"]


async def test_combined_watch_keeps_independent_baselines_retries_and_alerts(client, auth, user):
    now = time.time()
    with SessionLocal() as db:
        watch = Watch(user_id=user.id, name="Mixed search", query="sony", platforms=["ebay", "buyee", "kleinanzeigen"],
                      market_filters={"buyee": {"max_price": 5000}})
        db.add(watch)
        db.commit()
        watch_id = watch.id
    buyee, klein = (PARSERS[key](fixture(f"{key}-search.html")).listings for key in PARSERS)
    class Source:
        def __init__(self, items, url):
            self.items, self.url, self.error, self.calls = items, url, None, 0

        async def search(self, query, filters):
            self.calls += 1
            if self.error:
                raise self.error
            return self.items, False, self.url

    ebay_item = Listing("123456789001", "https://www.ebay.com/itm/123456789001", "Sony", price=1, currency="USD")
    ebay = Source([ebay_item], "https://www.ebay.com/sch/i.html?_nkw=sony")
    ebay.error = EbayAccessError("eBay blocked", now + 900)
    sources = {key: Source(items, build_market_url(key, "sony", {}))
               for key, items in zip(PARSERS, (buyee, klein), strict=True)}
    await scan_watch(watch_id, ebay, sources, due_only=True)
    heartbeat("worker")
    assert client.get("/api/status", headers=auth).json()["monitoring"]["state"] != "restricted"
    with SessionLocal() as db:
        watch = db.get(Watch, watch_id)
        assert not watch.platform_state["ebay"]["initialized"]
        assert watch.platform_state["buyee"]["initialized"] and watch.platform_state["kleinanzeigen"]["initialized"]
        assert watch.next_check_at < now + 900
        assert db.scalar(select(Notification)) is None
        watch.platform_state = {**watch.platform_state,
                                "buyee": watch.platform_state["buyee"] | {"next_check_at": now - 1}}
        db.commit()
    buyee[0].price -= 100
    await scan_watch(watch_id, ebay, sources, due_only=True)
    assert ebay.calls == sources["kleinanzeigen"].calls == 1 and sources["buyee"].calls == 2
    with SessionLocal() as db:
        alert = db.scalars(select(Notification)).one()
        assert alert.data["platform"] == "buyee"
        assert "New Buyee match" in item_message("Mixed search", alert.data)
        assert db.scalar(select(WatchItem).where(WatchItem.ebay_id == buyee[1].ebay_id)).matches is False
    result = client.get("/api/items", headers=auth).json()
    assert {item["platform"] for item in result["items"]} == {"buyee", "kleinanzeigen"}
    ebay.error = None
    await scan_watch(watch_id, ebay, sources)
    with SessionLocal() as db:
        assert db.get(Watch, watch_id).initialized
        assert len(list(db.scalars(select(Notification)))) == 1


def test_identical_cards_on_different_platforms_have_distinct_identities():
    data = {"title": "Sony", "price": 100, "currency": "EUR", "location": "Berlin"}
    assert listing_identity(data) == listing_identity(data | {"platform": "ebay"})
    assert listing_identity(data) != listing_identity(data | {"platform": "kleinanzeigen"})
