"""Browser checks against isolated API fixtures. Never sends Telegram messages or changes real data.

Run against a running web service with Chromium installed:
  python scripts/check_ui.py --url http://localhost:8000 --output /tmp/mira-ui
"""
import argparse
import asyncio
import copy
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect


async def check(url: str, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    now = time.time()
    filters = dict(min_price=None, max_price=150, currency="USD", condition="any", buying_format="any",
                   location="", seller="", free_shipping=False, include_shipping=False,
                   exclude_keywords=[], category_id="", attributes={})
    watch = dict(id=1, name="Sony Walkman", query="Sony Walkman WM-EX", filters=filters, interval_seconds=300,
                 active=True, initialized=True, created_at=now-3600, tracking_since=now-3600,
                 last_checked_at=now-120, next_check_at=now+180, error=None, search_url="", truncated=True,
                 counts=dict(total=2, new=1, pending=0))
    items = [dict(id=i, watch_id=1, watch_name=watch["name"], ebay_id=f"12345678900{i}",
                  title=title, price=price, price_text=f"US ${price:.2f}", currency="USD",
                  url=f"https://www.ebay.com/itm/12345678900{i}",
                  image_url="https://i.ebayimg.com/ui-fixture.svg", condition="Used", location="Japan",
                  shipping_text="US $12.00 shipping", baseline=i == 1, is_new=i == 2,
                  first_seen_at=now-120*i, last_seen_at=now-60, published_at=now-300 if i == 2 else now-86400,
                  detailed_at=now-60, detail_error=None, attributes={"Brand":"Sony","Model":"WM-EX"},
                  notification_sent=i == 2, notification_pending=False, notification_error=None, raw={"fixture":True})
             for i,title,price in [(1,"Sony Walkman WM-EX cassette player · silver",89),(2,"Sony Walkman WM-EX portable cassette player",120)]]
    fixtures = dict(watches=[watch], delivered=False, authenticated=False, offline=False, wrong_code=False,
                    created=None, updates=[], deleted=[], challenge_status="waiting",
                    monitoring=dict(state="monitoring", retry_at=None, next_check_at=now+180,
                                    active_searches=1, failed_searches=0, error=None, browser="chromium"), bot_online=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=True,
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or p.chromium.executable_path,
            env={key:os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ})
        context = await browser.new_context(viewport={"width":1440,"height":1000}, device_scale_factor=1)
        errors = []
        page = await context.new_page()
        page.on("pageerror",lambda error: errors.append(str(error)))
        async def api(route):
            path = urlsplit(route.request.url).path
            method = route.request.method
            body = route.request.post_data_json if method in {"POST","PUT","PATCH"} and route.request.post_data else {}
            if fixtures["offline"] and path in {"/api/watches","/api/items","/api/status"}:
                return await route.abort("failed")
            status = 200
            if path == "/api/auth/refresh":
                status = 200 if fixtures["authenticated"] else 401
                data = {"access_token":"ui-test-token","expires_in":600} if status == 200 else {"detail":"No active session"}
            elif path == "/api/config": data = {"bot_username":"mira_test_bot","telegram_ready":True,"error":None}
            elif path == "/api/auth/start":
                data = {"challenge_id":"test-challenge-capability-0001","expires_in":600,
                        "bot_url":"https://t.me/mira_test_bot?start=login_test-challenge-capability-0001"}
            elif path.startswith("/api/auth/challenges/"):
                data = {"status":fixtures["challenge_status"],"expires_in":590,"attempts_remaining":4 if fixtures["wrong_code"] else 5}
            elif path == "/api/auth/verify":
                if body["code"] != "123456":
                    fixtures["wrong_code"] = True; status = 400
                    data = {"detail":"Incorrect verification code. Check the six digits in Telegram."}
                else:
                    fixtures["authenticated"] = True
                    data = {"access_token":"ui-test-token","expires_in":600}
            elif path == "/api/auth/logout": fixtures["authenticated"] = False; data = {"ok":True}
            elif path == "/api/me": data = {"id":1,"username":"mira_tester"}
            elif path == "/api/status":
                data = {"services":{key:{"online":key != "bot" or fixtures["bot_online"],"error":None}
                                    for key in ["worker","bot"]}, "monitoring":fixtures["monitoring"]}
            elif path == "/api/watches" and method == "GET": data = copy.deepcopy(fixtures["watches"])
            elif path == "/api/watches" and method == "POST":
                fixtures["created"] = body
                created = copy.deepcopy(watch) | body | {"id":2,"initialized":False,"counts":{}}
                fixtures["watches"].append(created); status = 201; data = created
            elif path == "/api/items":
                params = parse_qs(urlsplit(route.request.url).query)
                result = [] if params.get("watch_id") == ["2"] else copy.deepcopy(items)
                if params.get("only_new") == ["true"]: result = [x for x in result if x["is_new"]]
                data = {"items":result,"total":len(result)}
            elif path.startswith("/api/watches/"):
                wid = int(path.split("/")[3]); target = next(w for w in fixtures["watches"] if w["id"] == wid)
                if method == "PUT": fixtures["updates"].append(body); target.update(body); data = target
                elif method == "PATCH": target.update(body); data = target
                elif method == "DELETE":
                    fixtures["deleted"].append(wid); fixtures["watches"] = [w for w in fixtures["watches"] if w["id"] != wid]
                    status = 204; data = None
                elif path.endswith("/check"): target["next_check_at"] = time.time(); data = {"ok":True}
                else: raise AssertionError(f"Unexpected fixture request: {method} {path}")
            else: raise AssertionError(f"Unmocked API request: {method} {path}")
            await route.fulfill(status=status,content_type="application/json",body=json.dumps(data) if data is not None else "")
        await context.route("**/api/**",api)
        await context.route("https://i.ebayimg.com/**",lambda route: route.fulfill(content_type="image/svg+xml",body='<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180"><rect width="180" height="180" fill="#f0f2eb"/><rect x="35" y="27" width="110" height="130" rx="12" fill="#c3c9bf" stroke="#798273" stroke-width="2"/><rect x="48" y="55" width="84" height="55" rx="5" fill="#4e5848"/><circle cx="70" cy="82" r="15" fill="#d3d8cb"/><circle cx="110" cy="82" r="15" fill="#d3d8cb"/><text x="90" y="139" text-anchor="middle" fill="#36452c" font-family="sans-serif" font-size="10">UI TEST FIXTURE</text></svg>'))
        await page.goto(url)
        await expect(page.locator('link[rel="stylesheet"]')).to_have_attribute("href", re.compile(r"/static/style\.css\?v=[a-f0-9]+"))
        await expect(page.locator('script[src]')).to_have_attribute("src", re.compile(r"/static/app\.js\?v=[a-f0-9]+"))
        await expect(page.locator("#login-dialog")).to_be_visible()
        await expect(page.locator(".login-guide")).to_be_visible()
        assert not re.search(r"[А-Яа-яІіЇїЄєҐґ]",await page.locator("body").inner_text())
        await page.screenshot(path=str(output/"login-desktop.png"),full_page=True)
        await page.locator("#username").fill("@mira_tester")
        await page.locator("#login-start-form button").click()
        await expect(page.locator("#bot-link")).to_have_attribute("href",re.compile(r"https://t.me/.*\?start=login_"))
        await expect(page.locator("#login-expiry")).to_contain_text("expires in")
        fixtures["challenge_status"] = "sent"
        await expect(page.locator("#login-delivery")).to_contain_text("Code sent",timeout=10000)
        await page.locator("#login-code").fill("000000")
        await page.locator("#login-submit").click()
        await expect(page.locator("#login-error")).to_contain_text("Incorrect verification code")
        await page.locator("#login-code").fill("123456")
        await page.locator("#login-submit").click()
        await expect(page.locator("#login-dialog")).not_to_be_visible()
        await expect(page.locator(".item-row")).to_have_count(2)
        await page.screenshot(path=str(output/"dashboard-desktop.png"),full_page=True)
        await page.locator('[data-item="1"]').click()
        await expect(page.locator("#item-dialog")).to_be_visible()
        await expect(page.locator(".detail-table")).to_contain_text("Brand")
        await expect(page.locator("[data-details]")).to_have_count(0)
        await expect(page.locator(".detail-table")).to_contain_text("Title + price + location")
        await page.locator("#item-detail summary").click()
        items[0]["detailed_at"] = now
        items[0]["attributes"]["Color"] = "Silver"
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await expect(page.locator(".detail-table")).to_contain_text("Silver")
        assert await page.locator("#item-detail details").get_attribute("open") is not None
        await expect(page.locator('[data-focus="item-raw"]')).to_be_focused()
        await page.locator('[data-close="item-dialog"]').click()
        await page.locator('[data-select-watch="1"]').first.click()
        await expect(page.locator("#selected-watch")).to_be_visible()
        await expect(page.locator("#selected-watch")).not_to_contain_text("Only the first eBay results page")
        await expect(page.locator("#connection-text")).to_have_text("Monitoring")
        for code, label in [("restricted", "eBay paused until"), ("checking", "Checking eBay"),
                            ("paused", "All searches paused"), ("offline", "Monitoring worker offline"),
                            ("retrying", "Searches waiting to retry"), ("error", "Monitoring error"),
                            ("no_searches", "No saved searches")]:
            fixtures["monitoring"].update(state=code, retry_at=now+900 if code == "restricted" else None)
            await page.evaluate("load()")
            await expect(page.locator("#connection-text")).to_contain_text(label)
            if code == "restricted":
                await expect(page.locator('[data-action="check"]')).to_be_disabled()
                await page.screenshot(path=str(output/"dashboard-paused-desktop.png"),full_page=True)
        fixtures["monitoring"].update(state="checking",browser="firefox")
        await page.evaluate("load()")
        await expect(page.locator("#connection-text")).to_have_text("Checking eBay · Firefox")
        # The old per-search one-hour delay must not override the shared access pause.
        original = copy.deepcopy(fixtures["watches"])
        fixtures["watches"] = original + [copy.deepcopy(watch) | {
            "id":999,"name":"Other search","error":"HTTP 403","next_check_at":now+3600}]
        fixtures["monitoring"].update(state="restricted",retry_at=now+900)
        await page.evaluate("load()")
        shared_date = await page.evaluate("value => date(value)",now+900)
        await expect(page.locator("#selected-watch .watch-meta").first).to_contain_text(f"Retries resume: {shared_date}")
        await page.locator('[data-select-watch="999"]').first.click()
        await expect(page.locator("#page-title")).to_have_text("Other search")
        await expect(page.locator("#selected-watch .watch-meta").first).to_contain_text(f"Retries resume: {shared_date}")
        await expect(page.locator("#selected-watch .watch-meta").first).not_to_contain_text("Scheduled check")
        fixtures["watches"] = original
        fixtures["monitoring"].update(state="monitoring",retry_at=None,browser="chromium")
        await page.locator('[data-select-watch="1"]').first.click()
        await expect(page.locator("#page-title")).to_have_text("Sony Walkman")
        fixtures["monitoring"]["state"] = "monitoring"
        fixtures["bot_online"] = False
        await page.evaluate("load()")
        await expect(page.locator("#connection-detail")).to_contain_text("Telegram bot offline")
        fixtures["bot_online"] = True
        await page.evaluate("load()")
        await page.locator('[data-action="edit"]').click()
        await expect(page.locator("#watch-change-note")).not_to_be_visible()
        await page.locator("#watch-name").fill("Portable cassette players")
        await expect(page.locator("#watch-change-note")).not_to_be_visible()
        await page.locator("#watch-query").fill("Aiwa cassette player")
        await expect(page.locator("#watch-change-note")).to_be_visible()
        await expect(page.locator("#watch-save")).to_have_text("Replace results & save")
        await page.locator('[data-close="watch-dialog"]').first.click()
        await page.locator("#new-watch").click()
        await expect(page.locator('[name="location_region"][value="default"]')).to_be_checked()
        await expect(page.locator("#custom-location")).not_to_be_visible()
        # A disabled location is absent from FormData. Every region must still serialize safely.
        for region in ("default", "us", "north_america", "europe", "asia", "worldwide"):
            await page.locator(f'[name="location_region"][value="{region}"]').check()
            payload = await page.evaluate("watchPayload()")
            assert payload["filters"]["location_region"] == region
            assert payload["filters"]["location"] == ""
            assert await page.evaluate('new FormData(document.querySelector("#watch-form")).get("location")') is None
        await page.locator('[name="location_region"][value="custom"]').check()
        await expect(page.locator("#location")).to_be_visible()
        await page.locator("#location").fill(" Japan ")
        assert (await page.evaluate("watchPayload()"))["filters"]["location"] == "Japan"
        await page.locator("#watch-name").focus()
        await page.locator("#location").evaluate("el => el.disabled = true")
        assert (await page.evaluate("watchPayload()"))["filters"]["location"] == ""
        await page.evaluate("updateLocation()")
        # Native radio keyboard behavior remains available with the custom card design.
        await page.locator('[name="location_region"][value="us"]').focus()
        await page.keyboard.press("ArrowRight")
        await expect(page.locator('[name="location_region"][value="north_america"]')).to_be_checked()
        await page.locator('[name="location_region"][value="europe"]').check()
        await expect(page.locator("#location")).to_be_disabled()
        await expect(page.locator("#location-summary")).to_have_text("Europe")
        for name in ("accepts_offers", "returns_accepted", "free_returns", "sale_items"):
            await page.locator(f'[name="{name}"]').check()
        await page.locator("#watch-location").scroll_into_view_if_needed()
        assert await page.locator("#watch-location").evaluate("el => el.getBoundingClientRect().height < 480")
        await page.locator("#watch-location").screenshot(path=str(output/"location-picker-desktop.png"))
        await page.screenshot(path=str(output/"search-filters-desktop.png"), full_page=True)
        await page.locator("#watch-query").fill("Fujifilm X100")
        await page.locator("#min-price").fill("100")
        await page.locator("#max-price").fill("50")
        await page.locator("#watch-save").click()
        await expect(page.locator("#watch-error")).to_contain_text("Maximum price")
        await page.locator("#max-price").fill("1000")
        await page.locator("#watch-save").click()
        await expect(page.locator("#watch-dialog")).not_to_be_visible()
        assert fixtures["created"]["name"] == "Fujifilm X100"
        saved_filters = fixtures["created"]["filters"]
        assert saved_filters["location_region"] == "europe" and saved_filters["location"] == ""
        assert all(saved_filters[key] for key in ("accepts_offers", "returns_accepted", "free_returns", "sale_items"))
        await expect(page.locator("#page-title")).to_have_text("Fujifilm X100")
        await expect(page.locator("#selected-watch .filter-tags")).to_contain_text("Location: Europe")
        await expect(page.locator("#selected-watch .filter-tags")).to_contain_text("Free returns")
        await page.locator('[data-action="edit"]').click()
        await expect(page.locator('[name="location_region"][value="europe"]')).to_be_checked()
        for name in ("accepts_offers", "returns_accepted", "free_returns", "sale_items"):
            await expect(page.locator(f'[name="{name}"]')).to_be_checked()
        await expect(page.locator("#watch-change-note")).not_to_be_visible()
        await page.locator('[name="location_region"][value="asia"]').check()
        await expect(page.locator("#watch-change-note")).to_be_visible()
        await page.locator("#watch-save").click()
        await expect(page.locator("#watch-dialog")).not_to_be_visible()
        assert fixtures["updates"][-1]["filters"]["location_region"] == "asia"
        await expect(page.locator("#selected-watch .filter-tags")).to_contain_text("Location: Asia")
        await page.locator('[data-action="delete"]').click()
        await expect(page.locator("#delete-description")).to_contain_text("cannot be undone")
        await page.locator('[data-close="delete-dialog"]').click()
        assert not fixtures["deleted"]
        await page.locator('[data-action="delete"]').click()
        await page.locator('#delete-form button[type="submit"]').click()
        await expect(page.locator("#delete-dialog")).not_to_be_visible()
        assert fixtures["deleted"] == [2]
        await expect(page.locator("#page-title")).to_have_text("All searches")
        fixtures["offline"] = True
        await page.locator("#tab-new").click()
        await expect(page.locator("#load-error")).to_be_visible()
        fixtures["offline"] = False
        await page.locator("#retry-load").click()
        await expect(page.locator("#load-error")).not_to_be_visible()
        await expect(page.locator(".item-row")).to_have_count(1)
        await expect(page.locator("#items-count")).to_have_text("2")
        await page.locator("#tab-all").click()
        await page.set_viewport_size({"width":390,"height":844})
        await expect(page.locator("#mobile-watch-select")).to_be_visible()
        await page.locator("#mobile-watch-select").select_option("1")
        await expect(page.locator("#selected-watch")).to_be_visible()
        fixtures["monitoring"].update(state="restricted",retry_at=now+900)
        await page.evaluate("load()")
        await expect(page.locator("#connection-text")).to_contain_text("eBay paused until")
        await expect(page.locator(".connection")).to_be_in_viewport()
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await expect(page.locator(".connection")).to_be_in_viewport()
        await expect(page.locator("#overview-stats")).not_to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.evaluate("window.scrollTo(0, 0)")
        await expect(page.locator("#toast")).not_to_be_visible(timeout=10000)
        await expect(page.locator(".skip-link")).not_to_be_in_viewport()
        await page.screenshot(path=str(output/"dashboard-mobile.png"),full_page=True)
        await page.locator("#new-watch").click()
        await expect(page.locator('[name="location_region"][value="default"]')).to_be_checked()
        await expect(page.locator('[name="free_returns"]')).not_to_be_checked()
        await page.locator('[name="location_region"][value="europe"]').check()
        await expect(page.locator("#location-summary")).to_have_text("Europe")
        await page.locator("#watch-location").scroll_into_view_if_needed()
        assert await page.locator("#watch-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
        await page.locator("#watch-location").screenshot(path=str(output/"location-picker-mobile.png"))
        await page.screenshot(path=str(output/"search-mobile.png"),full_page=True)
        await page.set_viewport_size({"width":320,"height":740})
        assert await page.locator("#watch-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
        await page.set_viewport_size({"width":390,"height":844})
        await page.locator('[data-close="watch-dialog"]').first.click()
        await page.locator("#logout").click()
        await expect(page.locator("#login-start-form")).to_be_visible()
        await expect(page.locator("#login-code-step")).not_to_be_visible()
        await page.screenshot(path=str(output/"login-mobile.png"),full_page=True)
        assert await page.locator("#login-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
        assert not errors, errors
        await browser.close()
    print("UI checks passed: login, listings, region selection, custom country, offers/returns/sale filters, "
          "filter persistence and editing, delete/cancel, offline retry, monitoring states, mobile layout, sign-out.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",default="http://localhost:8000")
    parser.add_argument("--output",type=Path,default=Path("/tmp/mira-ui"))
    args = parser.parse_args()
    asyncio.run(check(args.url,args.output))
