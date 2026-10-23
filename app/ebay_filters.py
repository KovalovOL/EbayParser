"""Country values from eBay's public Advanced Search form, checked 2026-10-22."""
import json
from pathlib import Path

_ALIASES = {
    "Australia": ("au",), "Canada": ("ca",), "China": ("cn",), "France": ("fr",),
    "Germany": ("de", "deutschland"), "Japan": ("jp",), "Lithuania": ("lt", "lietuva"),
    "Poland": ("pl", "polska"), "Ukraine": ("ua",),
    "United Kingdom": ("uk", "gb", "great britain"),
    "United States": ("us", "usa", "united states of america"),
}
COUNTRIES = tuple((code, name, _ALIASES.get(name, ())) for code, name in
                  json.loads(Path(__file__).with_name("ebay_countries.json").read_text())["countries"])

# Copied from ebay.com's live search-result links on 2026-10-23.
# The Advanced Search form still emits older US/North America values;
# use the result-page links, not those legacy form values.
# Source snapshot: tests/fixtures/ebay-location-links.json.
LOCATION_REGIONS = (
    ("default", "Default", "98"),
    ("us", "US Only", "3"),
    ("north_america", "North America", "4"),
    ("europe", "Europe", "5"),
    ("asia", "Asia", "6"),
    ("worldwide", "Worldwide", "2"),
    ("custom", "Specific country or location", ""),
)
REGION_PARAMS = {key: param for key, _, param in LOCATION_REGIONS if param}

NATIVE_BOOLEAN_FILTERS = {
    "accepts_offers": "LH_BO",
    "returns_accepted": "LH_RPA",
    "free_returns": "LH_FR",
    "sale_items": "LH_SaleItems",
}


def country_filter(value: str):
    value = " ".join(value.strip().casefold().split())
    return next((country for country in COUNTRIES if value in
                 {country[1].casefold(), *country[2]}), None)
