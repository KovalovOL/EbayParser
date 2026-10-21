import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.parser.ebay import EbayAccessError, EbayClient, ParseError


def response(status=200, html="<title>eBay</title>", headers=None):
    return httpx.Response(status, text=html, headers=headers, request=httpx.Request("GET", "https://www.ebay.com/"))


@pytest.fixture
async def ebay():
    client = EbayClient(Settings(_env_file=None, request_delay_seconds=0, ebay_http_client="httpx"))
    original = client.client
    client.client = AsyncMock()
    yield client
    await original.aclose()


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
