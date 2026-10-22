import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.parser.ebay import EbayAccessError, EbayClient, ParseError
from app.parser.errors import BrowserError


def response(status=200, html="<title>eBay</title>", headers=None):
    return httpx.Response(status, text=html, headers=headers,
                          request=httpx.Request("GET", "https://www.ebay.com/sch/i.html"))


@pytest.fixture
async def fallback(monkeypatch):
    order = []
    transports = {}
    for name in ("chromium", "firefox"):
        async def get(*args, _name=name, **kwargs):
            order.append(_name)
            return response()
        transports[name] = SimpleNamespace(get=AsyncMock(side_effect=get), close=AsyncMock())
    monkeypatch.setattr("app.browser.BrowserTransport", lambda settings, engine="chromium": transports[engine])
    settings = Settings(_env_file=None, ebay_http_client="playwright", request_delay_seconds=0,
                        request_delay_jitter_seconds=0, ebay_firefox_fallback=True)
    selected = []
    client = EbayClient(settings, on_browser=selected.append)
    yield SimpleNamespace(client=client, chromium=transports["chromium"], firefox=transports["firefox"],
                          order=order, selected=selected)
    await client.close()


async def test_success_uses_only_chromium(fallback):
    f = fallback
    await f.client.fetch("https://www.ebay.com/sch/i.html?_nkw=sony")
    await f.client.fetch("https://www.ebay.com/sch/i.html?_nkw=lego")
    assert f.order == ["chromium", "chromium"]
    f.firefox.get.assert_not_awaited()
    f.chromium.close.assert_not_awaited()


async def test_403_tries_firefox_once_with_same_url_and_filters(fallback):
    f = fallback
    f.chromium.get.side_effect = None
    f.chromium.get.return_value = response(403)
    f.firefox.get.side_effect = None
    f.firefox.get.return_value = response(html="<title>eBay</title>Firefox results")
    url = "https://www.ebay.com/sch/i.html?_nkw=sony&_udlo=4&_udhi=20"
    assert "Firefox results" in await f.client.fetch(url)
    assert f.selected == ["chromium", "firefox"]
    assert f.chromium.get.call_args.args == f.firefox.get.call_args.args == (url,)
    f.chromium.close.assert_awaited_once()
    assert f.client.blocked_until == 0 and f.client.last_browser == "firefox"
    # Every subsequent check starts with Chromium again, with its existing profile.
    f.chromium.get.return_value = response()
    await f.client.fetch(url)
    assert f.selected == ["chromium", "firefox", "chromium"]
    f.firefox.close.assert_awaited_once()


async def test_both_blocked_set_one_cooldown_and_prevent_more_requests(fallback):
    f = fallback
    for transport in (f.chromium, f.firefox):
        transport.get.side_effect = None
        transport.get.return_value = response(403)
    with pytest.raises(EbayAccessError) as failure:
        await f.client.fetch("https://www.ebay.com/sch/i.html")
    assert "Chromium" in str(failure.value) and "Firefox" in str(failure.value)
    assert failure.value.retry_at >= time.time() + 899
    with pytest.raises(EbayAccessError):
        await f.client.fetch("https://www.ebay.com/sch/i.html?_nkw=other")
    assert f.chromium.get.await_count == f.firefox.get.await_count == 1


async def test_retry_after_from_first_browser_is_preserved_if_firefox_also_fails(fallback):
    f = fallback
    f.chromium.get.side_effect = None
    f.chromium.get.return_value = response(503, headers={"Retry-After":"3600"})
    f.firefox.get.side_effect = BrowserError("Firefox could not start.")
    with pytest.raises(EbayAccessError) as failure:
        await f.client.fetch("https://www.ebay.com/sch/i.html")
    assert failure.value.retry_at >= time.time() + 3599
    assert "Firefox could not start" in str(failure.value)


async def test_rate_limit_pauses_without_second_browser(fallback):
    f = fallback
    f.chromium.get.side_effect = None
    f.chromium.get.return_value = response(429, headers={"Retry-After":"1800"})
    with pytest.raises(EbayAccessError) as failure:
        await f.client.fetch("https://www.ebay.com/sch/i.html")
    assert failure.value.retry_at >= time.time() + 1799
    f.firefox.get.assert_not_awaited()


async def test_firefox_http_error_does_not_discard_chromium_restriction(fallback):
    f = fallback
    f.chromium.get.side_effect = None
    f.chromium.get.return_value = response(403, headers={"Retry-After":"1800"})
    f.firefox.get.side_effect = None
    f.firefox.get.return_value = response(500)
    with pytest.raises(EbayAccessError) as failure:
        await f.client.fetch("https://www.ebay.com/sch/i.html")
    assert failure.value.retry_at >= time.time()+1799
    assert "HTTP 500" in str(failure.value)


@pytest.mark.parametrize("failure", [BrowserError("Chromium could not start."),
                                     BrowserError("Chromium navigation timed out.")])
async def test_browser_failure_uses_firefox(fallback, failure):
    f = fallback
    f.chromium.get.side_effect = failure
    await f.client.fetch("https://www.ebay.com/sch/i.html")
    f.firefox.get.assert_awaited_once()


async def test_200_challenge_uses_firefox(fallback):
    f = fallback
    f.chromium.get.side_effect = None
    f.chromium.get.return_value = response(html="<title>Pardon Our Interruption</title>")
    await f.client.fetch("https://www.ebay.com/sch/i.html")
    f.firefox.get.assert_awaited_once()


async def test_disallowed_redirect_is_not_retried_in_firefox(fallback):
    f = fallback
    f.chromium.get.side_effect = None
    f.chromium.get.return_value = response(302, headers={"Location":"https://127.0.0.1/admin"})
    with pytest.raises(ParseError, match="not allowed"):
        await f.client.fetch("https://www.ebay.com/sch/i.html")
    f.firefox.get.assert_not_awaited()


async def test_fallback_uses_shared_pacing(fallback, monkeypatch):
    f = fallback
    clock = SimpleNamespace(now=100.0, waits=[])

    async def sleep(delay):
        clock.waits.append(delay)
        clock.now += delay

    monkeypatch.setattr("app.parser.ebay.time", SimpleNamespace(time=time.time, monotonic=lambda: clock.now))
    monkeypatch.setattr("app.parser.ebay.asyncio", SimpleNamespace(sleep=sleep))
    f.client.settings.request_delay_seconds = 10
    f.client.settings.request_delay_jitter_seconds = 0
    starts = []

    async def blocked(*args, **kwargs):
        starts.append(clock.now)
        clock.now += 2
        return response(403)

    async def success(*args, **kwargs):
        starts.append(clock.now)
        return response()

    f.chromium.get.side_effect, f.firefox.get.side_effect = blocked, success
    await f.client.fetch("https://www.ebay.com/sch/i.html")
    assert starts == [100, 112] and clock.waits == [10]
