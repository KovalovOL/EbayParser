import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from app.browser import BrowserTransport, allowed_browser_request, browser_proxy
from app.config import Settings
from app.parser.ebay import EbayAccessError, EbayClient, ParseError


@pytest.fixture
def browser_fake(monkeypatch, tmp_path):
    page = SimpleNamespace(
        url="https://www.ebay.com/sch/i.html", is_closed=MagicMock(return_value=False),
        set_default_navigation_timeout=MagicMock(), goto=AsyncMock(), title=AsyncMock(return_value="Sony | eBay"),
        wait_for_selector=AsyncMock(), wait_for_url=AsyncMock(),
        content=AsyncMock(return_value='<h1 class="srp-controls__count-heading">0 results</h1>'),
        screenshot=AsyncMock(),
    )
    response = SimpleNamespace(status=200, all_headers=AsyncMock(return_value={}))
    page.goto.return_value = response
    context = SimpleNamespace(pages=[page], route=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock(),
                              storage_state=AsyncMock(return_value={"cookies":[],"origins":[]}), add_cookies=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)), stop=AsyncMock())
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
    monkeypatch.setattr("app.browser.async_playwright", lambda: manager)
    settings = Settings(_env_file=None, ebay_http_client="playwright", ebay_browser_profile_dir=tmp_path,
                        request_delay_seconds=0, ebay_browser_headless=True)
    return SimpleNamespace(page=page, response=response, context=context, playwright=playwright, settings=settings)


@pytest.mark.parametrize("url,navigation,allowed", [
    ("https://www.ebay.com/sch/i.html", True, True),
    ("https://ir.ebaystatic.com/script.js", False, True),
    ("https://i.ebayimg.com/image.jpg", False, True),
    ("https://ir.ebaystatic.com/script.js", True, False),
    ("https://www.ebay.com.evil.test/", False, False),
    ("http://www.ebay.com/", True, False),
    ("https://127.0.0.1/admin", False, False),
    ("https://169.254.169.254/latest/meta-data", False, False),
    ("https://user:secret@www.ebay.com/", True, False),
    ("https://www.ebay.com:8443/", True, False),
    ("https://www.ebay.com:bad/", True, False),
])
def test_browser_network_scope(url, navigation, allowed):
    assert allowed_browser_request(url, navigation=navigation) is allowed


def test_proxy_credentials_are_separate_from_server_url():
    assert browser_proxy("http://name:p%40ss@proxy.example:8080") == {
        "server":"http://proxy.example:8080", "username":"name", "password":"p@ss"}
    assert browser_proxy("") is None
    with pytest.raises(ParseError):
        browser_proxy("file:///etc/passwd")


async def test_browser_reuses_context_and_closes_storage(browser_fake, monkeypatch):
    f = browser_fake
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-reach-chromium")
    transport = BrowserTransport(f.settings)
    try:
        await transport.get("https://www.ebay.com/sch/i.html")
        await transport.get("https://www.ebay.com/itm/123456789001", headers={"Referer":"https://www.ebay.com/"})
        launch = f.playwright.chromium.launch_persistent_context
        assert launch.call_count == 1
        assert launch.call_args.kwargs["chromium_sandbox"] is True
        assert "TELEGRAM_BOT_TOKEN" not in launch.call_args.kwargs["env"]
        assert launch.call_args.kwargs["user_data_dir"] == str(f.settings.ebay_browser_profile_dir)
        assert f.page.goto.call_args.kwargs["referer"] == "https://www.ebay.com/"
    finally:
        await transport.close()
    f.context.close.assert_awaited_once()
    f.playwright.stop.assert_awaited_once()
    assert transport.profile_lock is None


async def test_profile_lock_prevents_concurrent_browser_instances(browser_fake):
    first, second = BrowserTransport(browser_fake.settings), BrowserTransport(browser_fake.settings)
    try:
        await first.start()
        with pytest.raises(ParseError, match="зайнятий"):
            await second.start()
        assert browser_fake.playwright.chromium.launch_persistent_context.call_count == 1
        await first.close()
        await second.start()
        assert browser_fake.playwright.chromium.launch_persistent_context.call_count == 2
    finally:
        await first.close()
        await second.close()


async def test_browser_challenge_triggers_cooldown_and_saves_private_diagnostic(browser_fake):
    f = browser_fake
    f.page.url = "https://www.ebay.com/splashui/challenge?token=do-not-log"
    f.page.title.return_value = "Pardon Our Interruption"
    client = EbayClient(f.settings)
    try:
        with pytest.raises(EbayAccessError):
            await client.fetch("https://www.ebay.com/sch/i.html")
        meta = (f.settings.ebay_browser_profile_dir / "last-error.json").read_text()
        assert "do-not-log" not in meta
        assert json.loads(meta)["path"] == "/splashui/challenge"
        with pytest.raises(EbayAccessError):
            await client.fetch("https://www.ebay.com/sch/i.html?_nkw=lego")
        assert f.page.goto.call_count == 1
    finally:
        await client.close()


async def test_browser_retry_after_header_is_case_insensitive(browser_fake):
    f = browser_fake
    f.response.status = 429
    f.response.all_headers.return_value = {"retry-after":"1800"}
    client = EbayClient(f.settings)
    try:
        with pytest.raises(EbayAccessError) as failure:
            await client.fetch("https://www.ebay.com/sch/i.html")
        assert failure.value.retry_at >= time.time() + 1799
    finally:
        await client.close()


async def test_browser_crash_releases_profile_for_next_attempt(browser_fake):
    f = browser_fake
    f.page.goto.side_effect = PlaywrightError("browser disconnected")
    transport = BrowserTransport(f.settings)
    with pytest.raises(ParseError, match="Помилка браузера"):
        await transport.get("https://www.ebay.com/sch/i.html")
    assert transport.profile_lock is None
    f.page.goto.side_effect = None
    try:
        await transport.get("https://www.ebay.com/sch/i.html")
        assert f.playwright.chromium.launch_persistent_context.call_count == 2
    finally:
        await transport.close()


async def test_automatic_js_redirect_can_finish_without_human_input(browser_fake):
    f = browser_fake
    f.page.url = "https://www.ebay.com/splashui/challenge"
    f.page.title.side_effect = ["Pardon Our Interruption", "Sony | eBay"]
    async def finish(*args, **kwargs):
        f.page.url = "https://www.ebay.com/sch/i.html"
    f.page.wait_for_url.side_effect = finish
    client = EbayClient(f.settings)
    try:
        html = await client.fetch("https://www.ebay.com/sch/i.html")
        assert "0 results" in html
        assert client.blocked_until == 0
    finally:
        await client.close()


async def test_cancelled_start_releases_profile(browser_fake):
    f = browser_fake
    f.playwright.chromium.launch_persistent_context.side_effect = asyncio.CancelledError()
    transport = BrowserTransport(f.settings)
    with pytest.raises(asyncio.CancelledError):
        await transport.start()
    assert transport.profile_lock is None
    f.playwright.stop.assert_awaited_once()


async def test_browser_aborts_private_network_and_non_ebay_assets(browser_fake):
    transport = BrowserTransport(browser_fake.settings)
    for url in ("https://127.0.0.1/", "https://ads.example/track"):
        request = SimpleNamespace(url=url, resource_type="script", is_navigation_request=lambda:False)
        route = SimpleNamespace(request=request, abort=AsyncMock(), continue_=AsyncMock())
        await transport.route_request(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()


async def test_headed_linux_browser_starts_and_stops_private_virtual_display(browser_fake, monkeypatch):
    f = browser_fake
    f.settings.ebay_browser_headless = False
    display = SimpleNamespace(start=AsyncMock(return_value=":77"), close=AsyncMock())
    monkeypatch.setattr("app.browser.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("app.browser.VirtualDisplay", lambda: display)
    transport = BrowserTransport(f.settings)
    try:
        await transport.start()
        options = f.playwright.chromium.launch_persistent_context.call_args.kwargs
        assert options["headless"] is False
        assert options["env"]["DISPLAY"] == ":77"
        assert "DISPLAY" not in os.environ
    finally:
        await transport.close()
    display.start.assert_awaited_once()
    display.close.assert_awaited_once()


async def test_session_only_cookies_are_saved_privately_and_restored(browser_fake):
    f = browser_fake
    cookie = {"name":"session_test", "value":"private-test-value", "domain":".ebay.com", "path":"/", "expires":-1,
              "httpOnly":True, "secure":True, "sameSite":"Lax"}
    f.context.storage_state.return_value = {"cookies":[cookie],"origins":[]}
    first = BrowserTransport(f.settings)
    await first.start()
    await first.close()
    state_path = f.settings.ebay_browser_profile_dir / "session-state.json"
    assert state_path.stat().st_mode & 0o777 == 0o600
    second = BrowserTransport(f.settings)
    try:
        await second.start()
        f.context.add_cookies.assert_awaited_once_with([cookie])
    finally:
        await second.close()
