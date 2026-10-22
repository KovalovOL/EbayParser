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


def country_filter(value: str):
    value = " ".join(value.strip().casefold().split())
    return next((country for country in COUNTRIES if value in
                 {country[1].casefold(), *country[2]}), None)
