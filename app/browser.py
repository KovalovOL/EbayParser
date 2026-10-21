"""A dedicated persistent Chromium profile owned by the monitor, never the user's desktop profile."""
import asyncio
import fcntl
import json
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import suppress
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from httpx import Headers
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from app.config import Settings
from app.parser.errors import ParseError
from app.virtual_display import VirtualDisplay

log = logging.getLogger(__name__)
ASSET_DOMAINS = ("ebay.com", "ebaystatic.com", "ebayimg.com", "ebaydesc.com", "ebayrtm.com")
READY_SELECTOR = (
    "li.s-card, li.s-item, .srp-controls__count-heading, .srp-save-null-search, "
    "h1.x-item-title__mainTitle, h1#itemTitle, #captcha_form, #challenge-form"
)


def allowed_browser_request(url: str, *, navigation: bool = False) -> bool:
    parsed = urlparse(url)
    try:
        valid = parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        return False
    if not valid:
        return False
    host = (parsed.hostname or "").lower()
    if navigation:
        return host == "www.ebay.com"
    return any(host == domain or host.endswith("." + domain) for domain in ASSET_DOMAINS)


def browser_proxy(value: str) -> dict | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ParseError("Некоректна адреса EBAY_PROXY")
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        raise ParseError("Некоректний порт EBAY_PROXY") from None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    result = {"server": f"{parsed.scheme}://{host}{port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


@dataclass
class BrowserResponse:
    status_code: int
    text: str
    url: str
    headers: Mapping[str, str]

    def __post_init__(self):
        # Playwright returns lowercase names; match HTTP clients' case-insensitive header behavior.
        self.headers = Headers(self.headers)

    @property
    def content(self):
        return self.text.encode("utf-8")


class BrowserTransport:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile_dir = settings.ebay_browser_profile_dir or settings.state_dir / "browser-profile"
        self.context = None
        self.page = None
        self.playwright = None
        self.profile_lock = None
        self.display = None
        self.start_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()

    async def start(self):
        async with self.start_lock:
            if self.context is not None:
                return
            self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.profile_dir.chmod(0o700)
            handle = (self.profile_dir / ".signal-profile.lock").open("a")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise ParseError("Профіль браузера зайнятий іншим процесом. Зупиніть worker або використайте інший --profile-dir.") from None
            self.profile_lock = handle
            try:
                self.playwright = await async_playwright().start()
                # Do not pass bot/secret environment variables to the browser subprocess.
                browser_env = {key: value for key, value in os.environ.items() if key in {
                    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "DISPLAY",
                    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "LD_LIBRARY_PATH",
                }}
                if not self.settings.ebay_browser_headless and sys.platform == "linux" and not browser_env.get("DISPLAY"):
                    self.display = VirtualDisplay()
                    browser_env["DISPLAY"] = await self.display.start()
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir), channel="chromium",
                    headless=self.settings.ebay_browser_headless, chromium_sandbox=True,
                    locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000},
                    accept_downloads=False, service_workers="block", proxy=browser_proxy(self.settings.ebay_proxy),
                    timeout=30_000, env=browser_env,
                    # Python handles termination and flushes browser storage before exiting.
                    handle_sigterm=False, handle_sigint=False, handle_sighup=False,
                )
                await self.restore_session_cookies()
                await self.context.route("**/*", self.route_request)
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                self.page.set_default_navigation_timeout(self.settings.ebay_browser_timeout_seconds * 1000)
                log.info("Persistent Chromium profile ready")
            except BaseException as exc:
                await self.close()
                if isinstance(exc, PlaywrightError):
                    message = str(exc)
                    if self.settings.ebay_proxy:
                        message = message.replace(self.settings.ebay_proxy, "[proxy]")
                        password = urlparse(self.settings.ebay_proxy).password
                        if password:
                            message = message.replace(password, "[redacted]")
                    log.warning("Chromium startup failed: %s", message[:1200])
                    raise ParseError("Chromium не запустився. Перевірте встановлення браузера та seccomp-профіль Docker у README.") from None
                raise

    async def route_request(self, route):
        request = route.request
        main_navigation = request.is_navigation_request() and request.frame.parent_frame is None
        if not allowed_browser_request(request.url, navigation=main_navigation):
            await route.abort()
        elif request.resource_type in {"media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    async def restore_session_cookies(self):
        # Chromium may discard session-only cookies on clean exit; retain them explicitly too.
        path = self.profile_dir / "session-state.json"
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            cookies = [cookie for cookie in state.get("cookies", []) if isinstance(cookie, dict)
                       and any(cookie.get("domain", "").lstrip(".") == domain
                               or cookie.get("domain", "").endswith("." + domain) for domain in ASSET_DOMAINS)
                       and (cookie.get("expires", -1) <= 0 or cookie["expires"] > time.time())]
            if cookies:
                await self.context.add_cookies(cookies)
        except (OSError, ValueError, TypeError, AttributeError, PlaywrightError):
            log.warning("Saved browser session could not be restored; keeping the native profile")

    async def save_session(self):
        temporary = None
        try:
            state = await self.context.storage_state()
            with tempfile.NamedTemporaryFile(mode="w", dir=self.profile_dir, delete=False) as handle:
                temporary = handle.name
                json.dump(state, handle)
            os.replace(temporary, self.profile_dir / "session-state.json")
            temporary = None
        except (OSError, PlaywrightError):
            log.warning("Browser session snapshot could not be saved")
        finally:
            if temporary:
                with suppress(OSError):
                    os.unlink(temporary)

    async def snapshot_error(self, status: int | None, title: str):
        if not self.page:
            return
        try:
            await self.page.screenshot(path=str(self.profile_dir / "last-error.png"), timeout=5000)
            meta = {"timestamp": time.time(), "status": status, "title": title[:150],
                    "path": urlparse(self.page.url).path}
            (self.profile_dir / "last-error.json").write_text(json.dumps(meta, ensure_ascii=False))
        except (PlaywrightError, OSError):
            pass

    @staticmethod
    def challenge_url(url: str) -> bool:
        return any(x in urlparse(url).path.lower() for x in ("splashui", "captcha", "challenge"))

    async def get(self, url: str, *, headers: dict | None = None) -> BrowserResponse:
        if not allowed_browser_request(url, navigation=True):
            raise ParseError("Недозволена адреса браузерного запиту")
        async with self.request_lock:
            await self.start()
            try:
                if self.page.is_closed():
                    self.page = await self.context.new_page()
                response = await self.page.goto(url, wait_until="domcontentloaded",
                    referer=(headers or {}).get("Referer"),
                    timeout=self.settings.ebay_browser_timeout_seconds * 1000)
                if not allowed_browser_request(self.page.url, navigation=True):
                    raise ParseError("eBay перенаправив браузер на недозволений домен")
                status = response.status if response else 502
                title = await self.page.title()
                if self.challenge_url(self.page.url):
                    # Normal JavaScript checks can redirect themselves back to the listing.
                    # Give that navigation a bounded chance; never click or solve a CAPTCHA.
                    try:
                        await self.page.wait_for_url(
                            lambda target: allowed_browser_request(str(target), navigation=True)
                            and not self.challenge_url(str(target)),
                            wait_until="domcontentloaded", timeout=12_000,
                        )
                    except PlaywrightTimeout:
                        pass
                    title = await self.page.title()
                blocked = status in {403, 429, 503} or re.search(
                    r"pardon our interruption|access denied|robot check|error page", title, re.I
                ) or self.challenge_url(self.page.url)
                if not blocked and status == 200:
                    # Wait for page content, not endlessly active eBay analytics connections.
                    try:
                        await self.page.wait_for_selector(READY_SELECTOR, state="attached", timeout=10_000)
                    except PlaywrightTimeout:
                        pass  # The parser distinguishes unknown markup from a valid empty page.
                if blocked:
                    await self.snapshot_error(status, title)
                return BrowserResponse(status, await self.page.content(), self.page.url,
                                       await response.all_headers() if response else {})
            except PlaywrightTimeout:
                await self.snapshot_error(None, "Navigation timeout")
                raise ParseError("Перевищено час завантаження eBay у браузері. Спробуємо пізніше.") from None
            except PlaywrightError:
                await self.close()
                raise ParseError("Помилка браузера eBay. На наступній спробі браузер буде запущено повторно.") from None

    async def close(self):
        try:
            if self.context is not None:
                try:
                    await asyncio.wait_for(self.save_session(), timeout=5)
                except (PlaywrightError, TimeoutError):
                    log.warning("Browser session snapshot timed out")
                try:
                    await asyncio.wait_for(self.context.close(), timeout=15)
                except (PlaywrightError, TimeoutError):
                    log.warning("Chromium context did not close cleanly")
            if self.playwright is not None:
                try:
                    await asyncio.wait_for(self.playwright.stop(), timeout=10)
                except (PlaywrightError, TimeoutError):
                    log.warning("Playwright driver did not close cleanly")
        finally:
            if self.display is not None:
                await self.display.close()
                self.display = None
            self.context = self.page = self.playwright = None
            if self.profile_lock:
                self.profile_lock.close()
                self.profile_lock = None
