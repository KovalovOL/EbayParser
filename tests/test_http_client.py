import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.parser.ebay import EbayAccessError, EbayClient, ParseError


def response(status=200, html="<title>eBay</title>", headers=None):
    return httpx.Response(status, text=html, headers=headers, request=httpx.Request("GET", "https://www.ebay.com/"))


@pytest.fixture
async def ebay():
    client = EbayClient(Settings(_env_file=None, request_delay_seconds=0,
                                request_delay_jitter_seconds=0, ebay_http_client="httpx"))
    original = client.client
    client.client = AsyncMock()
    yield client
    await original.aclose()


@pytest.fixture
def pacing_clock(ebay, monkeypatch):
    clock = SimpleNamespace(now=100.0, sleeps=[])

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr("app.parser.ebay.time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    monkeypatch.setattr("app.parser.ebay.asyncio", SimpleNamespace(sleep=sleep))
    ebay.settings.request_delay_seconds = 10
    ebay.settings.request_delay_jitter_seconds = 10
    return clock


async def test_random_pacing_is_shared_by_searches_and_details_without_delaying_idle_requests(
    ebay, pacing_clock, monkeypatch
):
    clock, starts = pacing_clock, []
    sample = Mock(side_effect=[10, 20, 15, 10])
    monkeypatch.setattr("app.parser.ebay.random.uniform", sample)

    async def get(*args, **kwargs):
        starts.append(clock.now)
        clock.now += 3  # Loading time is separate from the delay between pages.
        return response()

    ebay.client.get.side_effect = get
    await ebay.fetch("https://www.ebay.com/sch/i.html?_nkw=sony")
    clock.now += 4  # Work between fetches counts towards the pending delay.
    await ebay.fetch("https://www.ebay.com/itm/123456789001", referer="https://www.ebay.com/sch/i.html")
    await ebay.fetch("https://www.ebay.com/sch/i.html?_nkw=lego")
    clock.now += 30
    await ebay.fetch("https://www.ebay.com/sch/i.html?_nkw=clothes")

    assert starts == [100, 113, 136, 169]
    assert clock.sleeps == [6, 20]
    assert sample.call_count == 4
    assert all(call.args == (10, 20) for call in sample.call_args_list)


@pytest.mark.parametrize("first_result", ["connection_failure", "redirect"])
async def test_pacing_also_applies_after_connection_failure_or_http_redirect(
    ebay, pacing_clock, monkeypatch, first_result
):
    clock, starts = pacing_clock, []
    monkeypatch.setattr("app.parser.ebay.random.uniform", lambda low, high: 13)

    async def get(*args, **kwargs):
        starts.append(clock.now)
        clock.now += 2
        if len(starts) == 1:
            if first_result == "connection_failure":
                raise httpx.ConnectError("Connection failed")
            return response(301, headers={"Location": "/itm/123456789001"})
        return response()

    ebay.client.get.side_effect = get
    if first_result == "connection_failure":
        with pytest.raises(ParseError, match="Could not connect"):
            await ebay.fetch("https://www.ebay.com/sch/i.html")
    await ebay.fetch("https://www.ebay.com/sch/i.html")

    assert starts == [100, 115]
    assert clock.sleeps == [13]


async def test_normal_ebay_redirect_is_followed(ebay):
    ebay.client.get.side_effect = [response(301, headers={"Location":"/itm/123456789001"}), response(html="item html")]
    assert await ebay.fetch("https://www.ebay.com/itm/123456789002") == "item html"
    assert ebay.client.get.call_count == 2


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_block_cools_down_all_searches(ebay, status):
    ebay.client.get.return_value = response(status)
    with pytest.raises(EbayAccessError) as failure:
        await ebay.fetch("https://www.ebay.com/sch/i.html?_nkw=sony")
    assert failure.value.retry_at >= time.time() + 899
    with pytest.raises(EbayAccessError):
        await ebay.fetch("https://www.ebay.com/sch/i.html?_nkw=lego")
    assert ebay.client.get.call_count == 1


async def test_retry_after_is_respected(ebay):
    ebay.client.get.return_value = response(429, headers={"Retry-After":"1800"})
    with pytest.raises(EbayAccessError) as failure:
        await ebay.fetch("https://www.ebay.com/")
    assert failure.value.retry_at >= time.time() + 1799


async def test_captcha_redirect_is_not_fetched(ebay):
    ebay.client.get.return_value = response(307, headers={"Location":"https://www.ebay.com/splashui/challenge?token=secret"})
    with pytest.raises(EbayAccessError) as failure:
        await ebay.fetch("https://www.ebay.com/itm/123456789001")
    assert "secret" not in str(failure.value)
    assert ebay.client.get.call_count == 1


async def test_external_redirect_is_not_fetched(ebay):
    ebay.client.get.return_value = response(302, headers={"Location":"https://evil.test/"})
    with pytest.raises(ParseError):
        await ebay.fetch("https://www.ebay.com/itm/123456789001")
    assert ebay.client.get.call_count == 1


async def test_200_challenge_is_not_success(ebay):
    ebay.client.get.return_value = response(html="<title>Pardon Our Interruption</title>")
    with pytest.raises(EbayAccessError):
        await ebay.fetch("https://www.ebay.com/sch/i.html")


async def test_referer_is_sent_for_details(ebay):
    ebay.client.get.return_value = response(html="item html")
    assert await ebay.fetch("https://www.ebay.com/itm/123456789001", referer="https://www.ebay.com/") == "item html"
    assert ebay.client.get.call_args.kwargs["headers"] == {"Referer":"https://www.ebay.com/"}


async def test_default_curl_session_is_available():
    client = EbayClient(Settings(_env_file=None, ebay_http_client="curl_cffi"))
    assert client.settings.ebay_http_client == "curl_cffi"
    assert client.client.impersonate == "chrome"
    await client.close()


async def test_one_results_html_yields_all_56_cards_without_pagination_or_item_requests(ebay):
    cards = ''.join(f'<li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/{123456789001+n}">'
                    f'<h3 class="s-item__title">Clothes {n}</h3></a><span class="s-item__price">US $10.00</span></li>'
                    for n in range(56))
    ebay.client.get.return_value = response(html=f'<ul>{cards}</ul><a class="pagination__next" href="?p=2">Next</a>')
    items, more, url = await ebay.search("Clothes", {"min_price":4, "max_price":20, "currency":"USD", "category_id":"11450"})
    assert len(items) == 56 and more
    assert ebay.client.get.await_count == 1
    query = parse_qs(urlparse(url).query)
    assert query["_udlo"] == ["4"] and query["_udhi"] == ["20"]
    assert query["_sacat"] == ["11450"] and query["_sop"] == ["10"]
    assert query["_pgn"] == ["1"] and "_ipg" not in query
