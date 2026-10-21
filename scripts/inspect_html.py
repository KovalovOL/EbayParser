"""Inspect a saved eBay HTML page without network access or credentials."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.parser.ebay import Listing, ParseError, canonical_url, parse_detail, parse_search  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("html", type=Path)
parser.add_argument("--item-url", help="Parse an item detail page instead of search results")
args = parser.parse_args()
try:
    source = args.html.read_text()
    if args.item_url:
        pair = canonical_url(args.item_url)
        if not pair:
            parser.error("Expected https://www.ebay.com/itm/<item-id>")
        result = parse_detail(source, Listing(pair[0], pair[1], "")).to_dict()
    else:
        page = parse_search(source)
        result = {"layout": page.layout, "has_next": page.has_next, "total": page.total_text,
                  "items": [item.to_dict() for item in page.listings]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
except ParseError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
