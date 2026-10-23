import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from app.parser.ebay import Listing, ParseError, amount, build_search_url, canonical_url, currency, matches_filters, parse_detail, parse_search, timestamp
from app.schemas import Filters

FIXTURES = Path(__file__).parent / "fixtures"


def test_current_layout_and_placeholder():
    page = parse_search((FIXTURES / "search-card.html").read_text())
    assert page.layout == "s-card"
    assert page.has_next
    assert len(page.listings) == 2
    first, second = page.listings
    assert first.title == "Sony Walkman WM-EX"
    assert first.ebay_id == "123456789001"
    assert first.url == "https://www.ebay.com/itm/123456789001"
    assert first.price == 89.99
    assert first.shipping == 0
    assert "Japan" in first.location
    assert first.image_url.startswith("https://i.ebayimg.com/")
    assert first.buying_format == "buy_now"
    assert second.currency == "CAD"
    assert second.price == 1234.5
    assert second.buying_format == "auction"


def test_sanitized_live_card_excerpt():
    page = parse_search((FIXTURES / "search-live-excerpt.html").read_text())
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.title == "Sony Walkman Radio & Cassette Player WM- FX221 Tape Audio Music"
    assert "Opens in a new window" not in item.title
    assert item.price == 46.83
    assert item.buying_format == "buy_now"
    assert "United Kingdom" in item.location
    assert item.image_url.startswith("https://i.ebayimg.com/")


def test_legacy_layout():
    page = parse_search((FIXTURES / "search-legacy.html").read_text())
    assert page.layout == "s-item"
    assert not page.has_next
    item = page.listings[0]
    assert (item.currency, item.price, item.shipping) == ("EUR", 150.5, 10)


def test_detail_preserves_arbitrary_attributes():
    listing = Listing("123456789001", "https://www.ebay.com/itm/123456789001", "old")
    item = parse_detail((FIXTURES / "detail.html").read_text(), listing)
    assert item.attributes == {"Brand":"Sony", "Model":"WM-EX", "Color":"Silver", "Custom Attribute":"Any future value"}
    assert item.published_at == timestamp("2026-10-21T10:00:00Z")
    assert item.seller == "retro_shop"
    assert item.raw["json_ld"]


@pytest.mark.parametrize("html", ["<title>Pardon Our Interruption</title>", "<title>Error Page | eBay</title>", "<html>Unexpected new layout</html>"])
def test_blocked_and_unknown_are_not_empty(html):
    with pytest.raises(ParseError):
        parse_search(html)


def test_real_zero_is_valid():
    assert parse_search('<h1 class="srp-controls__count-heading">0 results for unicorn</h1>').listings == []


@pytest.mark.parametrize("value, expected", [("$1,234.56",1234.56),("EUR 1.234,56",1234.56),("€ 150,50",150.5),("1 200.00",1200),("Call for price",None)])
def test_money(value, expected):
    assert amount(value) == expected


@pytest.mark.parametrize("value, expected", [("DKK 100","DKK"),("AU $100","AUD"),("US $100","USD"),("C $100","CAD"),("£100","GBP"),("100","")])
def test_currency(value, expected):
    assert currency(value) == expected


def test_no_invented_listing_date():
    item = Listing("123456789001", "https://www.ebay.com/itm/123456789001", "Sony")
    parse_detail('<h1 id="itemTitle">Sony</h1><script type="application/ld+json">{"@type":"Product","dateModified":"2026-10-21T10:00:00Z","offers":{"validFrom":"2026-10-21T10:00:00Z"}}</script>', item)
    assert item.published_at is None
    assert timestamp("2026-10-21") is None
    assert timestamp("2026-10-21T10:00:00") is None


def test_embedded_start_time_must_belong_to_item():
    item = Listing("123456789001", "https://www.ebay.com/itm/123456789001", "Sony")
    parse_detail('<h1 id="itemTitle">Sony</h1><script type="application/json">{"itemId":"999999999999","startTime":"2026-10-21T10:00:00Z"}</script>', item)
    assert item.published_at is None


def test_filters_include_shipping_and_dynamic_attributes():
    item = Listing("1", "", "Sony Walkman", price=90, currency="USD", shipping=15, condition="Pre-Owned",
                   location="Tokyo, Japan", attributes={"Brand":"Sony"})
    filters = Filters(max_price=100, include_shipping=True, location="Japan", category_id="15054", attributes={"Brand":"sony"}).model_dump()
    assert matches_filters(item, filters) is False
    item.shipping = 5
    assert matches_filters(item, filters) is True
    item.attributes = {}
    assert matches_filters(item, filters) is None
    item.currency = "EUR"
    assert matches_filters(item, filters) is False


def test_query_is_encoded_and_ssrf_rejected():
    url = build_search_url("sony & lego", {"min_price":5,"max_price":100,"condition":"used","currency":"EUR"}, page=2)
    query = parse_qs(urlparse(url).query)
    assert query["_nkw"] == ["sony & lego"]
    assert query["_sop"] == ["10"]
    assert query["_pgn"] == ["2"]
    assert "_ipg" not in query
    assert query["_curr"] == ["EUR"]
    assert query["_udlo"] == ["5"] and query["_udhi"] == ["100"]
    assert query["LH_ItemCondition"] == ["3000"]
    assert canonical_url("https://evil.test/itm/123456789001") is None
    assert canonical_url("javascript:alert(1)") is None


def test_used_is_not_new():
    item = Listing("1","","camera",currency="USD",condition="Used, like new")
    assert matches_filters(item, Filters(condition="new").model_dump()) is False


def test_unknown_shipping_is_not_free():
    item = Listing("1","","camera",currency="USD")
    assert matches_filters(item, Filters(free_shipping=True).model_dump()) is None


def test_native_ebay_filters_include_seller_country_exclusions_and_item_specifics():
    filters = Filters(min_price=4, max_price=20, category_id="11450", condition="used",
                      buying_format="buy_now", seller="retro_shop", location="Japan", free_shipping=True,
                      exclude_keywords=["broken", "empty box"], attributes={"Brand":"Sony", "Storage Capacity":"1 TB"})
    query = parse_qs(urlparse(build_search_url("Clothes", filters.model_dump())).query)
    assert query["_udlo"] == ["4.0"] and query["_udhi"] == ["20.0"]
    assert query["_sacat"] == ["11450"] and query["_dcat"] == ["11450"]
    assert query["LH_ItemCondition"] == ["3000"] and query["LH_BIN"] == ["1"]
    assert query["LH_FS"] == ["1"] and query["_sasl"] == ["retro_shop"]
    assert query["_saslop"] == ["1"] and query["_salic"] == ["104"] and query["LH_LocatedIn"] == ["1"]
    assert query["_ex_kw"] == ['"broken" "empty box"']
    assert query["Brand"] == ["Sony"]
    assert unquote(query["Storage%20Capacity"][0]) == "1 TB"


@pytest.mark.parametrize("native_filter", json.loads((FIXTURES / "ebay-location-links.json").read_text())["regions"],
                         ids=lambda entry: entry["key"])
def test_item_regions_match_live_ebay_filter_links(native_filter):
    filters = Filters(location_region=native_filter["key"]).model_dump()
    query = parse_qs(urlparse(build_search_url("Sony", filters)).query)
    native_query = parse_qs(urlparse(native_filter["url"]).query)
    assert query["LH_PrefLoc"] == native_query["LH_PrefLoc"]
    assert query["rt"] == ["nc"]
    assert "_salic" not in query and "LH_LocatedIn" not in query
    # A search card does not need to expose regional metadata to use eBay's filter.
    assert matches_filters(Listing("1", "", "Sony", currency="USD"), filters, native=True) is True


def test_default_resets_the_location_preference_and_countries_use_located_in():
    query = parse_qs(urlparse(build_search_url("Sony", Filters().model_dump())).query)
    assert query["LH_PrefLoc"] == ["98"] and "_salic" not in query
    filters = Filters(location=" Japan ").model_dump()
    assert filters["location_region"] == "custom"
    query = parse_qs(urlparse(build_search_url("Sony", filters)).query)
    assert query["_salic"] == ["104"] and query["LH_LocatedIn"] == ["1"]
    assert "LH_PrefLoc" not in query


def test_legacy_country_is_not_overridden_by_default_region():
    query = parse_qs(urlparse(build_search_url("Sony", {"location":"Japan"})).query)
    assert query["_salic"] == ["104"] and query["LH_LocatedIn"] == ["1"]
    assert "LH_PrefLoc" not in query


@pytest.mark.parametrize("currency_code", ["USD", "EUR", "GBP", "CAD", "AUD"])
def test_currency_does_not_change_native_location_filter(currency_code):
    filters = Filters(location_region="europe", currency=currency_code).model_dump()
    query = parse_qs(urlparse(build_search_url("Sony", filters)).query)
    assert query["LH_PrefLoc"] == ["5"] and query["_curr"] == [currency_code]


def test_offer_return_and_sale_filters_are_enforced_by_native_search():
    filters = Filters(accepts_offers=True, returns_accepted=True, free_returns=True, sale_items=True).model_dump()
    query = parse_qs(urlparse(build_search_url("Sony", filters)).query)
    assert all(query[key] == ["1"] for key in ("LH_BO", "LH_RPA", "LH_FR", "LH_SaleItems"))
    item = Listing("1", "", "Sony", currency="USD")
    assert matches_filters(item, filters, native=True) is True
    assert matches_filters(item, filters) is None
    unfiltered = parse_qs(urlparse(build_search_url("Sony", Filters().model_dump())).query)
    assert not {"LH_BO", "LH_RPA", "LH_FR", "LH_SaleItems"} & unfiltered.keys()


def test_native_filters_do_not_require_per_item_metadata():
    item = Listing("1", "", "Sony", price=10, currency="USD")
    filters = Filters(condition="used", buying_format="buy_now", seller="retro_shop", location="Japan",
                      free_shipping=True, category_id="15054", attributes={"Brand":"Sony"}).model_dump()
    assert matches_filters(item, filters, native=True) is True
    item.location = "Canada"
    assert matches_filters(item, filters, native=True) is False


def test_total_price_minimum_does_not_exclude_cheap_items_with_shipping():
    filters = Filters(min_price=10, max_price=20, include_shipping=True).model_dump()
    query = parse_qs(urlparse(build_search_url("Sony", filters)).query)
    assert "_udlo" not in query and query["_udhi"] == ["20.0"]
    assert matches_filters(Listing("1", "", "Sony", price=5, shipping=6, currency="USD"), filters, native=True)


@pytest.mark.parametrize("name", ["_udhi", " _nkw", "LH_BIN", "rt"])
def test_attributes_cannot_override_search_parameters(name):
    with pytest.raises(ValueError):
        Filters(category_id="11450", attributes={name:"value"})
