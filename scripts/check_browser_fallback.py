"""Real browser smoke test with intercepted pages. No eBay network or Telegram requests.

Run in the worker image with PYTHONPATH=/app and this file mounted at /tmp/check_browser_fallback.py.
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from app.browser import BrowserTransport
from app.config import Settings
from app.parser.ebay import EbayAccessError, EbayClient

URL = "https://www.ebay.com/sch/i.html?_nkw=fixture&_udlo=4&_udhi=20"
EMPTY = '<html><title>eBay results</title><h1 class="srp-controls__count-heading">0 results</h1></html>'
BLOCKED = '<html><title>Error Page | eBay</title><body>Fixture HTTP 403</body></html>'


async def run_case(root, name, statuses, expected, blocked=False):
    settings = Settings(_env_file=None, state_dir=root/name, ebay_browser_profile_dir=root/name/"profile",
                        ebay_http_client="playwright", ebay_firefox_fallback=True,
                        ebay_browser_headless=False, request_delay_seconds=0, request_delay_jitter_seconds=0)
    navigations, selected = [], []

    def select_browser(engine):
        selected.append(engine)
        if engine == "firefox":
            assert client.client.context is None, "Chromium must close before Firefox starts"

    client = EbayClient(settings, on_browser=select_browser)
    def intercept(engine):
        async def route_request(route):
            request = route.request
            if request.is_navigation_request() and request.frame.parent_frame is None:
                assert request.url == URL
                navigations.append(engine)
                status = statuses[engine]
                await route.fulfill(status=status, content_type="text/html",
                                    headers={"Set-Cookie":f"engine_fixture={engine}; Path=/; Secure; SameSite=Lax"},
                                    body=EMPTY if status == 200 else BLOCKED)
            else:
                await route.abort()
        return route_request

    for engine, transport in (("chromium", client.client), ("firefox", client.fallback_client)):
        transport.route_request = intercept(engine)
    try:
        if blocked:
            for _ in range(2):
                try:
                    await client.fetch(URL)
                except EbayAccessError as exc:
                    assert exc.retry_at >= time.time()+895
                else:
                    raise AssertionError("Both browsers blocked must start a shared pause")
        else:
            # Use exactly the same URL in both engines, including native price filters.
            html = await client.fetch(URL)
            assert "0 results" in html and client.blocked_until == 0
        assert navigations == expected, navigations
        assert selected == expected, selected
    finally:
        await client.close()
    if name == "recovery":
        # Session snapshots and native profiles are isolated and survive a clean restart.
        for engine in ("chromium", "firefox"):
            transport = BrowserTransport(settings, engine=engine)
            try:
                await transport.start()
                cookies = await transport.context.cookies("https://www.ebay.com/")
                assert next(c["value"] for c in cookies if c["name"] == "engine_fixture") == engine
            finally:
                await transport.close()
    print(json.dumps({"case":name,"browsers":navigations,"shared_pause":blocked,"result":"passed"}), flush=True)


async def main():
    with tempfile.TemporaryDirectory(prefix="mira-browser-smoke-") as directory:
        root = Path(directory)
        await run_case(root,"normal",{"chromium":200,"firefox":200},["chromium"])
        await run_case(root,"recovery",{"chromium":403,"firefox":200},["chromium","firefox"])
        await run_case(root,"blocked",{"chromium":403,"firefox":403},["chromium","firefox"],blocked=True)


if __name__ == "__main__":
    asyncio.run(main())
