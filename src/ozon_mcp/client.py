"""Talking to Ozon's storefront API through the browser session.

Every request goes through ``browser.BrowserThread`` — one Chromium page on
www.ozon.ru that passed the anti-bot check — and is validated here:

- 200 with a JSON composer document -> data;
- 403 / 307 / an HTML answer -> the session was revoked: pass the check
  again once, then report the IP as blocked;
- 429 / 5xx -> one pause-and-retry, then a clear "rate limited" error
  (retrying in a loop makes Ozon's throttling worse);
- 404 -> "no such product" for product pages.

The client also remembers the delivery region Ozon reported in the last
answer — prices and dates are regional, so every tool reports it.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import browser, parse

log = logging.getLogger(__name__)

SITE = browser.SITE
API = browser.API
WIDGET_URL = SITE + "/api/composer-api.bx/widget/json/v2?widgetStateId="
ACTION_URL = SITE + "/api/composer-api.bx/_action/"

SORTS = {
    "popular": None,          # Ozon's default ("Популярные", sorting=score)
    "price_asc": "price",
    "price_desc": "price_desc",
    "rating": "rating",
    "newest": "new",
    "discount": "discount",
}


class OzonError(RuntimeError):
    """Anything that stops a tool from returning trustworthy data."""


class Blocked(OzonError):
    pass


class RateLimited(OzonError):
    pass


class NotFound(OzonError):
    pass


def cache_dir() -> Path:
    root = os.environ.get("OZON_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ozon-mcp"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


class OzonClient:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._location_setting = (os.environ.get("OZON_LOCATION") or "").strip() or None
        headless = os.environ.get("OZON_HEADLESS", "1") != "0"
        proxy = os.environ.get("OZON_PROXY") or None
        min_interval = float(os.environ.get("OZON_MIN_INTERVAL", "1.0"))
        idle = float(os.environ.get("OZON_IDLE_TIMEOUT", "600"))
        state_dir = cache_dir()
        self._thread = browser.BrowserThread(
            lambda: browser.Browser(state_dir, headless=headless, proxy=proxy,
                                    location=self._location_setting, min_interval=min_interval),
            idle_timeout=idle,
        )
        self._location: dict | None = None
        atexit.register(self._thread.stop)

    # ---- region ------------------------------------------------------------------

    def region(self) -> dict:
        """Where Ozon computed prices and delivery dates for, and why."""
        loc = dict(self._location or {})
        loc.pop("area_id", None)
        status = self._thread.location_status or {}
        if self._location_setting:
            loc["source"] = f"OZON_LOCATION={self._location_setting}"
            if not status.get("applied", False):
                loc["warning"] = (
                    "OZON_LOCATION could not be applied"
                    + (f" ({status['error']})" if status.get("error") else "")
                    + "; Ozon used its default region for this IP."
                )
        else:
            loc["source"] = "Ozon's default for this IP address (set OZON_LOCATION to change)"
        return loc

    def today(self) -> date:
        return parse.region_today(self._location)

    # ---- transport -----------------------------------------------------------------

    def _request(self, url: str, method: str = "GET", body: Any = None,
                 allow_404: bool = False) -> tuple[int, dict]:
        with self._lock:
            rechallenged = False
            retried = False
            while True:
                try:
                    resp = self._thread.call(lambda b: b.fetch(url, method, body))
                except browser.ChallengeFailed as e:
                    raise Blocked(str(e)) from e
                except browser.BrowserError as e:
                    if retried:
                        raise OzonError(str(e)) from e
                    retried = True
                    log.warning("Browser request failed (%s); retrying once", e)
                    continue
                data = resp.json()
                if resp.status == 200 and isinstance(data, dict):
                    self._remember_location(data)
                    return 200, data
                if resp.status == 404 and allow_404 and isinstance(data, dict):
                    self._remember_location(data)
                    return 404, data
                if resp.status == 429 or 500 <= resp.status < 600 or resp.status == 0:
                    if retried:
                        if resp.status == 429:
                            raise RateLimited(
                                "Ozon is rate limiting this IP (HTTP 429). Wait a few minutes; "
                                "retrying in a loop makes it worse."
                            )
                        raise OzonError(f"Ozon is not answering properly (HTTP {resp.status}: {resp.text[:120]})")
                    retried = True
                    time.sleep(15 if resp.status == 429 else 3)
                    continue
                if resp.status in (301, 302, 307, 401, 403) or data is None:
                    if rechallenged:
                        raise Blocked(
                            f"Ozon rejects even a fresh anti-bot session (HTTP {resp.status}). "
                            "This usually means the IP is blocked (VPN, foreign or datacenter "
                            "address); use a Russian residential IP or OZON_PROXY."
                        )
                    rechallenged = True
                    try:
                        self._thread.call(lambda b: b.rechallenge())
                    except browser.ChallengeFailed as e:
                        raise Blocked(str(e)) from e
                    except browser.BrowserError as e:
                        raise OzonError(f"Could not renew the Ozon session: {e}") from e
                    continue
                raise OzonError(f"Ozon answered HTTP {resp.status} for {url[:160]}")

    def _remember_location(self, data: dict) -> None:
        loc = parse.location(data)
        if loc and loc.get("city"):
            self._location = loc

    def page(self, path: str, allow_404: bool = False) -> tuple[int, dict]:
        """A composer page by its site path ("/search/?text=...", "/product/123/")."""
        return self._request(API + quote(path, safe=""), allow_404=allow_404)

    def widget_state(self, state_id: str, async_data: str) -> dict | None:
        """A widget the site loads after the page (e.g. webDelivery)."""
        _, data = self._request(WIDGET_URL + quote(state_id, safe=""), "POST", {"asyncData": async_data})
        state = data.get("state")
        state = parse._loads(state) if isinstance(state, str) else state
        return state if isinstance(state, dict) else None

    def action(self, name: str, body: dict) -> dict:
        _, data = self._request(ACTION_URL + name, "POST", body)
        return data

    # ---- storefront -------------------------------------------------------------------

    def search(self, query: str, page: int, sort: str, price_min: int | None,
               price_max: int | None, auto_category: bool) -> dict:
        params: dict[str, Any] = {"text": query, "from_global": "true"}
        if not auto_category:
            params["deny_category_prediction"] = "true"
        if SORTS.get(sort):
            params["sorting"] = SORTS[sort]
        if price_min is not None or price_max is not None:
            low = max(0, price_min or 0)
            high = price_max if price_max is not None else 99_999_999
            params["currency_price"] = f"{low}.000;{high}.000"
        if page > 1:
            params["page"] = page
        _, data = self.page("/search/?" + urlencode(params))
        if "widgetStates" not in data:
            raise OzonError("Ozon returned a search answer without results widgets; not passing it on")
        return data

    def product(self, sku: int) -> dict:
        status, data = self.page(f"/product/{sku}/", allow_404=True)
        if status == 404:
            raise NotFound(f"Ozon has no product page for SKU {sku} (removed, merged into another card, or a wrong number)")
        return data

    def product_details(self, sku: int) -> dict:
        """The card's second half: full characteristics and description."""
        _, data = self.page(f"/product/{sku}/?layout_container=pdpPage2column&layout_page_index=2")
        return data

    def delivery(self, product_page: dict) -> dict | None:
        entry = parse.async_widget(product_page, "webDelivery")
        if not entry:
            return None
        return self.widget_state(entry["stateId"], entry["asyncData"])

    def cart_button(self, sku: int) -> dict:
        return self.action(f"v2/pdpGetButtonTexts?product_id={sku}", {"params": {
            "hideQuantButton": "false", "inCartPosition": "subtitle", "pageType": "pdp",
            "toCartPosition": "subtitle",
        }})

    def all_variants(self, sku: int) -> dict:
        _, data = self.page(f"/modal/aspectsNew?from_sku={sku}&product_id={sku}")
        return data

    def other_offers(self, sku: int) -> dict:
        _, data = self.page(f"/modal/otherOffersFromSellers?product_id={sku}&sort=price")
        return data

    def price_drop(self, sku: int) -> dict:
        _, data = self.page(f"/modal/web_pdp_lower_price?product_id={sku}")
        return data

    def reviews(self, sku: int, page: int, sort: str, this_sku_only: bool) -> dict:
        params = {"reviewsVariantMode": 1 if this_sku_only else 2, "sort": parse.REVIEW_SORTS[sort]}
        if page > 1:
            params["page"] = page
        _, data = self.page(f"/product/{sku}/reviews/?" + urlencode(params))
        if parse.widget(data, "webListReviews") is None:
            if parse.widget(data, "userAdultModal"):
                raise OzonError(_ADULT)
            raise OzonError(f"Ozon returned no review list for SKU {sku}")
        return data

    def review_comments(self, review_id: str, sku: int) -> dict:
        return self.action("rpGetCommentsByReviewUuid", {
            "reviewUuid": review_id, "sku": str(sku), "rootSku": str(sku), "limit": 10, "offset": 0,
        })


_ADULT = (
    "Ozon shows this product only after an 18+ confirmation with a date of birth. "
    "The server does not submit personal data or legal confirmations on your behalf; "
    "open the page in a browser to see it."
)
ADULT_MESSAGE = _ADULT
