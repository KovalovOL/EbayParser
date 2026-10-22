"""One eBay search results page; no item requests, database writes or Telegram messages."""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from app.config import get_settings
from app.parser.ebay import EbayClient, ParseError


async def diagnose(args) -> int:
    settings = get_settings().model_copy()
    if args.client:
        settings.ebay_http_client = args.client
    query, filters = args.query, {"currency": "USD"}
    if args.watch_id:
        from app.db import SessionLocal
        from app.models import Watch
        with SessionLocal() as db:
            watch = db.get(Watch, args.watch_id)
            if not watch:
                print(json.dumps({"result": "error", "message": "Search not found"}, ensure_ascii=False))
                return 1
            query, filters = watch.query, dict(watch.filters)
    temporary_profile = None
    if settings.ebay_http_client == "playwright":
        if args.profile_dir:
            settings.ebay_browser_profile_dir = args.profile_dir
        else:
            # Never race the running monitor for its persistent browser profile.
            temporary_profile = tempfile.TemporaryDirectory(prefix="signal-browser-check-")
            settings.ebay_browser_profile_dir = Path(temporary_profile.name)
    ebay = EbayClient(settings)
    try:
        items, truncated, _ = await ebay.search(query, filters)
        print(json.dumps({"phase": "search", "result": "ok", "client": settings.ebay_http_client,
                          "proxy_configured": bool(settings.ebay_proxy), "listings": len(items),
                          "more_pages": truncated, "browser": ebay.last_browser}), flush=True)
        return 0
    except ParseError as exc:
        print(json.dumps({"result": "error", "client": settings.ebay_http_client,
                          "browser": ebay.last_browser, "message": str(exc)},
                         ensure_ascii=False), flush=True)
        return 1
    finally:
        await ebay.close()
        if temporary_profile:
            temporary_profile.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="sony walkman")
    parser.add_argument("--client", choices=("playwright", "curl_cffi", "httpx"))
    parser.add_argument("--watch-id", type=int, help="Read an existing watch's criteria without changing its data")
    parser.add_argument("--profile-dir", type=Path, help="Dedicated diagnostic profile; default is temporary")
    raise SystemExit(asyncio.run(diagnose(parser.parse_args())))


if __name__ == "__main__":
    main()
