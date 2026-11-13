"""One results page per selected platform; no item requests, database writes or Telegram messages."""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from app.config import get_settings
from app.parser.ebay import EbayClient, ParseError
from app.parser.markets import MarketClient
from app.platforms import PLATFORM_NAMES


async def diagnose(args) -> int:
    settings = get_settings().model_copy()
    if args.client:
        settings.ebay_http_client = args.client
    query, filters = args.query, {"currency": "USD"}
    platforms, market_filters = [args.platform or "ebay"], {}
    if args.watch_id:
        from app.db import SessionLocal
        from app.models import Watch
        with SessionLocal() as db:
            watch = db.get(Watch, args.watch_id)
            if not watch:
                print(json.dumps({"result": "error", "message": "Search not found"}, ensure_ascii=False))
                return 1
            query, filters = watch.query, dict(watch.filters)
            if args.platform and args.platform not in watch.platforms:
                print(json.dumps({"result": "error", "message": "This platform is not enabled for the search."}))
                return 1
            platforms = [args.platform] if args.platform else watch.platforms
            market_filters = watch.market_filters
    temporary_profile = None
    if "ebay" in platforms and settings.ebay_http_client == "playwright":
        if args.profile_dir:
            settings.ebay_browser_profile_dir = args.profile_dir
        else:
            # Never race the running monitor for its persistent browser profile.
            temporary_profile = tempfile.TemporaryDirectory(prefix="signal-browser-check-")
            settings.ebay_browser_profile_dir = Path(temporary_profile.name)
    failed = False
    try:
        for platform in platforms:
            ebay = platform == "ebay"
            source = EbayClient(settings) if ebay else MarketClient(settings, platform)
            client = settings.ebay_http_client if ebay else "curl_cffi"
            try:
                items, truncated, url = await source.search(
                    query, filters if ebay else market_filters.get(platform, {}))
                print(json.dumps({"phase": "search", "platform": platform, "result": "ok",
                                  "client": client, "proxy_configured": ebay and bool(settings.ebay_proxy),
                                  "listings": len(items), "more_pages": truncated, "url": url,
                                  "browser": getattr(source, "last_browser", None)}, ensure_ascii=False), flush=True)
            except ParseError as exc:
                failed = True
                print(json.dumps({"platform": platform, "result": "error", "client": client,
                                  "browser": getattr(source, "last_browser", None), "message": str(exc)},
                                 ensure_ascii=False), flush=True)
            finally:
                await source.close()
    finally:
        if temporary_profile:
            temporary_profile.cleanup()
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="sony walkman")
    parser.add_argument("--platform", choices=tuple(PLATFORM_NAMES), help="Default: all sources of the watch, or eBay")
    parser.add_argument("--client", choices=("playwright", "curl_cffi", "httpx"), help="Override the eBay transport")
    parser.add_argument("--watch-id", type=int, help="Read an existing watch's criteria without changing its data")
    parser.add_argument("--profile-dir", type=Path, help="Dedicated diagnostic profile; default is temporary")
    raise SystemExit(asyncio.run(diagnose(parser.parse_args())))


if __name__ == "__main__":
    main()
