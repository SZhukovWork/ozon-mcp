"""Transport policy of the client, with the browser replaced by a script."""
import json

import pytest

from ozon_mcp import browser, client


class FakeThread:
    def __init__(self, responses):
        self.responses = list(responses)
        self.rechallenges = 0
        self.calls = 0
        self.location_status = None

    def call(self, fn, timeout=180.0):
        fake = self

        class B:
            def fetch(self, url, method="GET", body=None):
                fake.calls += 1
                item = fake.responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            def rechallenge(self):
                fake.rechallenges += 1

        return fn(B())

    def stop(self):
        pass


def resp(status, body):
    text = body if isinstance(body, str) else json.dumps(body)
    return browser.Response(status, text, "https://www.ozon.ru/api", False, "application/json")


PAGE = {"widgetStates": {}, "location": {"current": {"city": "Екатеринбург", "timeZoneUtcname": "UTC+5"}}}


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.setenv("OZON_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("OZON_LOCATION", raising=False)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)

    def _make(responses):
        c = client.OzonClient()
        c._thread = FakeThread(responses)
        return c
    return _make


def test_ok_answer_updates_region(make):
    c = make([resp(200, PAGE)])
    assert c.page("/search/?text=x") == (200, PAGE)
    assert c.region()["city"] == "Екатеринбург"
    assert "IP" in c.region()["source"]


def test_revoked_session_is_rechallenged_once(make):
    c = make([resp(403, {"incidentId": "x"}), resp(200, PAGE)])
    assert c.page("/x")[0] == 200
    assert c._thread.rechallenges == 1


def test_html_answer_counts_as_revoked_session(make):
    c = make([resp(200, "<html>challenge</html>"), resp(200, PAGE)])
    assert c.page("/x")[0] == 200
    assert c._thread.rechallenges == 1


def test_persistent_403_is_reported_as_blocked(make):
    c = make([resp(403, "{}x"), resp(403, "{}x")])
    with pytest.raises(client.Blocked, match="IP"):
        c.page("/x")
    assert c._thread.rechallenges == 1


def test_429_is_retried_once_then_reported(make):
    c = make([resp(429, "{}"), resp(429, "{}")])
    with pytest.raises(client.RateLimited):
        c.page("/x")
    assert c._thread.calls == 2


def test_404_product_is_not_found(make):
    c = make([resp(404, PAGE)])
    with pytest.raises(client.NotFound):
        c.product(123)


def test_browser_error_is_retried_once(make):
    c = make([browser.BrowserError("Execution context was destroyed"), resp(200, PAGE)])
    assert c.page("/x")[0] == 200


def test_challenge_failure_is_blocked(make):
    c = make([browser.ChallengeFailed("title 'Похоже, нет соединения'")])
    with pytest.raises(client.Blocked):
        c.page("/x")


def test_location_warning_when_not_applied(make, monkeypatch):
    monkeypatch.setenv("OZON_LOCATION", "moskva")
    c = make([resp(200, PAGE)])
    c._thread.location_status = {"requested": "moskva", "applied": False, "error": "no button"}
    c.page("/x")
    region = c.region()
    assert region["source"] == "OZON_LOCATION=moskva"
    assert "could not be applied" in region["warning"]


def test_search_url(make):
    c = make([resp(200, PAGE)])
    seen = {}
    real = c._request

    def spy(url, *a, **k):
        seen["url"] = url
        return real(url, *a, **k)

    c._request = spy
    c.search("топор", 2, "price_asc", 1000, None, auto_category=False)
    from urllib.parse import unquote
    url = unquote(unquote(seen["url"]))
    assert "/search/?text=топор" in url
    assert "sorting=price" in url and "page=2" in url and "deny_category_prediction=true" in url
    assert "currency_price=1000.000;99999999.000" in url


def test_proxy_credentials_are_split_for_chromium():
    assert browser.proxy_settings(None) is None
    assert browser.proxy_settings("http://host:3128") == {"server": "http://host:3128"}
    assert browser.proxy_settings("http://us%40er:p%3Ass@10.0.0.1:8080") == {
        "server": "http://10.0.0.1:8080", "username": "us@er", "password": "p:ss"}
    assert browser.proxy_settings("socks5://h:1080")["server"] == "socks5://h:1080"
