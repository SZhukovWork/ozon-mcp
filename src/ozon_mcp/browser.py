"""A long-lived Chromium that passes Ozon's anti-bot check (Variti).

Why a browser at all: Ozon's storefront API (``composer-api.bx``) only
answers requests that carry a fresh anti-bot session *and* come from the
browser that earned it. Replaying the same cookies from ``requests`` or even
from a TLS-impersonating client gets HTTP 403/307. So the server keeps one
Chromium page open on www.ozon.ru and runs ``fetch()`` from inside it — the
same requests the site itself makes.

What decides whether the challenge passes (checked 2026-09):
- Playwright's default headless mode (the "headless shell") and a visible
  window pass; the "new" headless mode (``channel="chromium"``) is detected
  and lands on "Похоже, нет соединения";
- a User-Agent without the "HeadlessChrome" marker;
- images and styles must load — the challenge fetches its own assets.

Threading: sync Playwright objects belong to the thread that created them,
while the MCP SDK runs tools in worker threads. ``BrowserThread`` owns the
browser in one dedicated thread and executes submitted jobs one at a time,
which also serialises requests (Ozon throttles bursts anyway).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import quote, unquote, urlparse

log = logging.getLogger(__name__)

SITE = "https://www.ozon.ru"
HOME = SITE + "/"
API = SITE + "/api/composer-api.bx/page/json/v2?url="
# Tiny composer page (~5 KB) used to probe the session and read the region.
PROBE_PATH = "/modal/addressbook?set_sm=1"

_BLOCKED_TITLES = ("нет соединения", "доступ ограничен", "access denied", "antibot", "403")
_DEAD = re.compile(r"Target page, context or browser has been closed|Browser has been closed|"
                   r"Connection closed|browser has disconnected|Target closed", re.I)

T = TypeVar("T")


class BrowserError(RuntimeError):
    """The browser could not do its job (not installed, crashed, timed out)."""


class ChallengeFailed(BrowserError):
    """Ozon's anti-bot check was not passed (blocked IP, VPN, changed challenge)."""


class BrowserDead(BrowserError):
    """The Chromium process went away; the next job relaunches it."""


@dataclass
class Response:
    status: int
    text: str
    url: str
    redirected: bool
    content_type: str

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


_FETCH_JS = """
async ([url, method, body, timeoutMs]) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const opts = {method, headers: {accept: 'application/json'}, credentials: 'include', signal: ctrl.signal};
    if (body !== null) { opts.headers['content-type'] = 'application/json'; opts.body = body; }
    const r = await fetch(url, opts);
    const text = await r.text();
    return {status: r.status, text, url: r.url, redirected: r.redirected,
            ctype: r.headers.get('content-type') || ''};
  } catch (e) {
    return {status: 0, text: String(e), url, redirected: false, ctype: ''};
  } finally {
    clearTimeout(timer);
  }
}
"""


def proxy_settings(url: str | None) -> dict | None:
    """OZON_PROXY URL -> Playwright proxy dict (Chromium wants credentials separately)."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "http://" + url)
    server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
        out["password"] = unquote(parsed.password or "")
    return out


def _storefront(title: str) -> bool:
    low = (title or "").lower()
    return "ozon" in low and not any(bad in low for bad in _BLOCKED_TITLES)


class Browser:
    """Playwright + one Chromium + one context + one page on www.ozon.ru.

    Lives entirely inside the ``BrowserThread`` that created it.
    """

    def __init__(self, state_dir: Path, headless: bool = True, proxy: str | None = None,
                 location: str | None = None, min_interval: float = 1.0,
                 challenge_timeout: float = 45.0) -> None:
        self.state_dir = state_dir
        self.headless = headless
        self.proxy = proxy
        self.location = location
        self.min_interval = min_interval
        self.challenge_timeout = challenge_timeout
        self.location_status: dict[str, Any] = {"requested": location, "applied": location is None}
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._last_request = 0.0
        self._state_file = state_dir / "browser-state.json"
        self._meta_file = state_dir / "session-meta.json"

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        started = time.monotonic()
        self._pw = sync_playwright().start()
        try:
            self._launch(PlaywrightError)
        except _BrowserMissing:
            _install_chromium()
            self._launch(PlaywrightError)
        self._new_context(reuse_state=True)
        try:
            self._challenge()
        except ChallengeFailed:
            if not self._reused_state:
                raise
            log.info("The stored Ozon session was rejected; starting a fresh one")
            self._new_context(reuse_state=False)
            self._challenge()
        if self.location:
            self._ensure_location()
        self._save_state()
        log.info("Ozon session ready in %.1fs", time.monotonic() - started)

    def _launch(self, error_cls: type) -> None:
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                proxy=proxy_settings(self.proxy),
                args=["--disable-blink-features=AutomationControlled"],
            )
        except error_cls as e:
            if "Executable doesn't exist" in str(e):
                raise _BrowserMissing() from e
            raise BrowserError(f"Could not start Chromium: {e}") from e

    def _new_context(self, reuse_state: bool) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        probe = self._browser.new_page()
        user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        probe.close()
        meta = self._read_meta()
        state = None
        # A stored session carries the delivery point chosen for it; reuse it
        # only when it was made for the location asked for now.
        if reuse_state and self._state_file.exists() and meta.get("location") == self.location:
            state = str(self._state_file)
        self._context = self._browser.new_context(
            user_agent=user_agent, locale="ru-RU", viewport={"width": 1440, "height": 900},
            storage_state=state,
        )
        self._page = self._context.new_page()
        self._reused_state = state is not None

    def _challenge(self) -> None:
        page = self._page
        page.goto(HOME, wait_until="domcontentloaded", timeout=int(self.challenge_timeout * 1000))
        deadline = time.monotonic() + self.challenge_timeout
        title = ""
        while time.monotonic() < deadline:
            title = page.title()
            if _storefront(title) and urlparse(page.url).hostname == "www.ozon.ru":
                break
            page.wait_for_timeout(500)
        else:
            raise ChallengeFailed(
                f"Ozon's anti-bot check did not pass within {self.challenge_timeout:.0f}s "
                f"(page title: {title!r}). Ozon blocks foreign/VPN/datacenter IPs — "
                "use a Russian residential IP or set OZON_PROXY."
            )
        # The title can flip before the session is usable: probe the API.
        for attempt in range(3):
            resp = self.fetch(API + quote(PROBE_PATH, safe=""))
            if resp.status == 200 and isinstance(resp.json(), dict):
                return
            page.wait_for_timeout(2000 * (attempt + 1))
        raise ChallengeFailed(
            f"Ozon's storefront opened but its API still refuses the session (HTTP {resp.status}). "
            "The IP is probably blocked (VPN, foreign or datacenter address)."
        )

    def rechallenge(self) -> None:
        """Throw the session away and pass the check again (after 403/307)."""
        log.info("Ozon rejected the session; passing the anti-bot check again")
        self._new_context(reuse_state=False)
        self._challenge()
        if self.location:
            self.location_status = {"requested": self.location, "applied": False}
            self._ensure_location(force=True)
        self._save_state()

    def alive(self) -> bool:
        try:
            return bool(self._browser and self._browser.is_connected() and self._page and not self._page.is_closed())
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = self._page = None

    # ---- requests ------------------------------------------------------------

    def fetch(self, url: str, method: str = "GET", body: Any = None, timeout: float = 30.0) -> Response:
        """Run fetch() inside the Ozon page, spaced by ``min_interval``."""
        page = self._page
        if page is None:
            raise BrowserDead("no page")
        if urlparse(page.url).hostname != "www.ozon.ru":
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            page.wait_for_timeout(int(wait * 1000))
        payload = None if body is None else json.dumps(body, ensure_ascii=False)
        try:
            r = page.evaluate(_FETCH_JS, [url, method, payload, int(timeout * 1000)])
        except Exception as e:  # noqa: BLE001
            if _DEAD.search(str(e)):
                raise BrowserDead(str(e)) from e
            raise BrowserError(f"Browser request failed: {e}") from e
        finally:
            self._last_request = time.monotonic()
        return Response(r["status"], r["text"], r["url"], r["redirected"], r["ctype"])

    # ---- delivery location ------------------------------------------------------

    def _ensure_location(self, force: bool = False) -> None:
        meta = self._read_meta()
        if not force and self._reused_state and meta.get("location") == self.location and meta.get("location_result"):
            self.location_status = {"requested": self.location, "applied": True, **meta["location_result"]}
            return
        try:
            result = self._apply_location(self.location)
            self.location_status = {"requested": self.location, "applied": True, **result}
            self._write_meta({"location": self.location, "location_result": result})
        except Exception as e:  # noqa: BLE001 - reported to the agent, not fatal
            log.warning("Could not set Ozon delivery location %r: %s", self.location, e)
            self.location_status = {"requested": self.location, "applied": False, "error": str(e)}
            self._write_meta({"location": None})

    def _apply_location(self, value: str) -> dict:
        """Pick a pickup point the way an anonymous visitor does on the site.

        ``value`` is either a pickup-point page (https://www.ozon.ru/geo/<city>/<id>/)
        or a city slug from Ozon's /geo/ pages ("moskva", "ekaterinburg");
        for a slug the first pickup point Ozon lists for the city is used.
        """
        m = re.search(r"/geo/([a-z0-9-]+)/(\d+)/?", value)
        if m:
            point = f"/geo/{m.group(1)}/{m.group(2)}/"
        elif re.fullmatch(r"[a-z0-9-]+", value.strip()):
            city = value.strip()
            resp = self.fetch(API + quote(f"/geo/{city}/", safe=""))
            found = re.search(rf"/geo/{re.escape(city)}/(\d+)/", resp.text or "")
            if resp.status != 200 or not found:
                raise BrowserError(f"no pickup points found at {SITE}/geo/{city}/ (HTTP {resp.status}) — check the city slug")
            point = f"/geo/{city}/{found.group(1)}/"
        else:
            raise BrowserError(
                "OZON_LOCATION must be a city slug such as 'moskva' or a pickup-point URL "
                "like https://www.ozon.ru/geo/moskva/495226/"
            )
        before = self._current_location()
        page = self._page
        page.goto(SITE + point, wait_until="load", timeout=60000)
        button = page.locator("button", has_text="Сохранить адрес").first
        button.wait_for(state="visible", timeout=20000)
        page.wait_for_timeout(1500)  # the button does nothing until the page is hydrated
        start_url = page.url
        for _ in range(3):
            button.click()
            for _ in range(25):
                if page.url != start_url:
                    break
                page.wait_for_timeout(300)
            if page.url != start_url:
                break
            page.wait_for_timeout(2000)
        if urlparse(page.url).hostname != "www.ozon.ru" or "/geo/" in page.url:
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        after = self._current_location()
        if not after.get("city") or (after.get("uid"), after.get("fullName")) == (before.get("uid"), before.get("fullName")):
            raise BrowserError(f"Ozon did not accept pickup point {SITE + point} (region stayed {before.get('city')!r})")
        address = re.sub(r"^\{\{\{(.*?)(?:\|.*)?\}\}\}$", r"\1", after.get("fullName") or "")
        return {"pickup_point": SITE + point, "city": after.get("city"), "address": address or None}

    def _current_location(self) -> dict:
        resp = self.fetch(API + quote(PROBE_PATH, safe=""))
        return ((resp.json() or {}).get("location") or {}).get("current") or {}

    # ---- persisted state ----------------------------------------------------------

    def _save_state(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(self._state_file))
            os.chmod(self._state_file, 0o600)
            meta = self._read_meta()
            if not self.location:
                meta = {"location": None}
            self._write_meta(meta)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not persist the Ozon session: %s", e)

    def _read_meta(self) -> dict:
        try:
            return json.loads(self._meta_file.read_text())
        except (OSError, ValueError):
            return {}

    def _write_meta(self, meta: dict) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._meta_file.write_text(json.dumps(meta, ensure_ascii=False))
            os.chmod(self._meta_file, 0o600)
        except OSError as e:
            log.warning("Could not write %s: %s", self._meta_file, e)


class _BrowserMissing(Exception):
    pass


def _install_chromium() -> None:
    """First run under uvx has no browser yet: fetch Playwright's Chromium.

    Output goes to stderr — stdout belongs to the MCP stdio transport.
    """
    log.warning("Playwright Chromium is not installed; installing it (one-time, ~250 MB)")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdout=sys.stderr, stderr=sys.stderr, check=False,
    )
    if result.returncode != 0:
        raise BrowserError(
            "Chromium is required for Ozon's anti-bot check and could not be installed "
            "automatically. Run: python -m playwright install chromium"
        )


_STOP = object()


class BrowserThread:
    """Owns a ``Browser`` in one thread; other threads submit jobs to it.

    The browser starts lazily with the first job, is relaunched if it dies,
    and is closed after ``idle_timeout`` seconds without work.
    """

    def __init__(self, factory: Callable[[], Browser], idle_timeout: float = 600.0) -> None:
        self._factory = factory
        self._idle_timeout = idle_timeout
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self.browser: Browser | None = None
        self.location_status: dict[str, Any] | None = None

    def call(self, fn: Callable[[Browser], T], timeout: float = 180.0) -> T:
        self._ensure_thread()
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fn, future))
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            raise BrowserError(f"Browser did not answer within {timeout:.0f}s") from e

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread and self._thread.is_alive():
            self._jobs.put(_STOP)
            self._thread.join(timeout)

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="ozon-browser", daemon=True)
                self._thread.start()

    def _close_browser(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def _run(self) -> None:
        last_used = time.monotonic()
        while True:
            try:
                job = self._jobs.get(timeout=5)
            except queue.Empty:
                if self.browser is not None and time.monotonic() - last_used > self._idle_timeout:
                    log.info("Closing the idle browser")
                    self._close_browser()
                continue
            if job is _STOP:
                self._close_browser()
                return
            fn, future = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if self.browser is None or not self.browser.alive():
                    self._close_browser()
                    fresh = self._factory()
                    try:
                        fresh.start()
                    except BaseException as e:
                        self.location_status = fresh.location_status
                        fresh.close()
                        if isinstance(e, BrowserError) or not isinstance(e, Exception):
                            raise
                        raise BrowserError(f"Could not open www.ozon.ru: {e}") from e
                    self.browser = fresh
                future.set_result(fn(self.browser))
            except BaseException as e:  # noqa: BLE001 - handed to the caller
                if isinstance(e, (BrowserDead, ChallengeFailed)) or _DEAD.search(str(e)):
                    self._close_browser()
                    if not isinstance(e, (BrowserDead, ChallengeFailed)):
                        e = BrowserDead(str(e))
                elif isinstance(e, Exception) and not isinstance(e, BrowserError):
                    e = BrowserError(str(e))
                future.set_exception(e)
            finally:
                if self.browser is not None:
                    self.location_status = self.browser.location_status
                last_used = time.monotonic()
