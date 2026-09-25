# ozon-mcp

**English** · [Русский](README.ru.md)

> A server for buyers: it reads the public storefront as an anonymous
> visitor, no Ozon account or token needed. For the **seller side** (orders,
> stock, the official Seller API) you need a different tool.

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents live,
honestly-labelled data from [Ozon](https://www.ozon.ru): search with real
pagination, sorting and a price window; product cards with every price
labelled, delivery dates for your region, seller legal details, variants and
other sellers' offers; reviews with the SKU's own rating separated from the
rating Ozon shares across a whole product line.

## Why another Ozon server

Ozon scrapers tend to return numbers that look right and mean something
else. This one is built around the traps found while checking its output
against the site:

| Pitfall | What this server does |
|---|---|
| Ozon's anti-bot (Variti) blocks plain HTTP clients, even with the browser's cookies (403/307) | Keeps one Chromium page on ozon.ru and calls the storefront API from inside it; renews the session when Ozon revokes it |
| One "price" without saying which | Every price is named: `price_with_ozon_card_rub` (paying with an Ozon Bank card), `price_without_ozon_card_rub`, `price_before_discount_rub` (crossed out). Search tiles show only the Ozon Card price, and the field says so |
| The rating on a card is the rating of a whole **variant line** — for a Fiskars axe, 24 different models share "4.9 • 3 536 reviews" | Shows the card rating with its scope next to the SKU's own rating, computed from its own reviews (e.g. 4.94 from 389 reviews); flags shared ratings in search results |
| "Brand" read from the tile's first label — which is often "Бренд проверен" or "24 ₽ / шт" | Brand only when the tile really shows one (otherwise from the card's structured data); unit prices become `price_per_unit` |
| No pagination; for the default sort Ozon pages hold only 8 items, so "limit 36" silently returns 8 | Walks real result pages, reports `next_page`; never cuts a page |
| Following Ozon's "next page" past the end yields an endless shelf of unrelated products | Only Ozon's search-results widget counts as results |
| Ozon's price filter and price sort are approximate (a 1300–1450 ₽ window returns 1058–1545 ₽) | Items outside the requested window are kept and flagged `outside_price_window` |
| "Delivery today" that actually costs +1 937 ₽ extra | Regular and paid express delivery are separate fields, with the surcharge |
| Seller name truncated by the card layout ("Садовая техника и и...") | Full name, rating, order count, legal entity, ОГРН/ИНН and address |

Every answer carries `fetched_at` and the region the prices and dates were
computed for.

## Tools

| Tool | What it returns |
|---|---|
| `search_products(query, page, sort, price_min, price_max, limit, auto_category)` | Whole Ozon result pages from `page` until `limit` items; sort `popular` / `price_asc` / `price_desc` / `rating` / `newest` / `discount`; `next_page`; `resolved_to` (the category/brand Ozon narrowed the query to). Per item: SKU, name, brand when shown, Ozon Card price, crossed-out price, discount label, price per unit, rating & reviews as on the tile + `rating_shared_with`, delivery date (and paid express with surcharge), units left, sold by Ozon, 18+ flag, labels, URL |
| `get_product(product, include_variants, include_other_sellers, include_description)` | All three prices; sale banner and its end; Ozon's only price-history hint; availability; delivery options with dates, where it ships from, returns; seller with rating, orders and legal entity; card rating (scope, star split) + this SKU's own rating; every variant with its SKU and price; other sellers' offers of the same product; characteristics; description; package contents |
| `get_reviews(product, page, limit, sort, scope, include_seller_replies)` | 30 per page; sort `helpful` (Ozon's default) / `worst` / `best`; `scope=sku` or the whole `line`; per review: date, stars, text, pros, cons, SKU and variant it is about, photo/video counts, helpful votes, `outdated`, comment count, seller reply (optional); line star split and the SKU's own rating |
| `compare_products(products, with_delivery)` | Up to 20 SKUs: prices, availability, card rating with the size of its variant line, seller, cheapest other-seller offer, delivery date |

`product` accepts a SKU (`1837133915`) or a product URL.

## Requirements

| | |
|---|---|
| Python | ≥ 3.10, with [uv](https://docs.astral.sh/uv/) (or pip) |
| Browser | Chromium via Playwright, downloaded automatically on first run (≈300 MB download, ≈650 MB on disk in `~/.cache/ms-playwright`, shared by all Playwright tools). Ozon's anti-bot lets no plain HTTP client through, so every request is made from inside a Chromium page (headless shell): the browser stays open while the server is in use and closes after 10 minutes idle. The first request of a session takes ≈15–35 s (browser start plus the anti-bot check; 34 s measured) |
| Docker | Not needed: Chromium runs as a child process of the server on the host |
| System libraries | Already present on desktop Linux, macOS and Windows. On a minimal Debian/Ubuntu server install them once (root): `uvx --from playwright playwright install --with-deps chromium` |
| Network | A Russian IP: VPN, foreign and many datacenter addresses are blocked. Switch the VPN off or set `OZON_PROXY`. Ozon can also stop letting an IP through for a while after many fresh browser sessions in a day (seen during development) — then wait rather than retry, and keep the cache directory: a saved session is what keeps the check passing |
| Memory | ≈0.8 GB while the browser is open (measured) |
| Display | Not needed (`OZON_HEADLESS=0` for debugging needs one) |
| Tested on | Linux (CachyOS; Playwright uses its Ubuntu build there). macOS and Windows are supported by Playwright but untested |

## Install

Claude Code:

```bash
claude mcp add ozon -- uvx --from git+https://github.com/SZhukovWork/ozon-mcp ozon-mcp
```

Any MCP client (`claude_desktop_config.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "ozon": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SZhukovWork/ozon-mcp", "ozon-mcp"]
    }
  }
}
```

From a checkout: `uv venv && uv pip install -e . && .venv/bin/ozon-mcp`.

The first request of a session opens the browser and passes the anti-bot
check: expect 2–15 seconds (more on the very first run, when Chromium is
downloaded). Later requests take about a second each. The browser closes
after 10 idle minutes and reopens on demand.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `OZON_LOCATION` | — | Delivery region. By default Ozon picks a city from your IP (reported in every answer). Set a city slug from Ozon's `/geo/` pages (`moskva`, `ekaterinburg`, `sankt-peterburg`; the server picks the first pickup point Ozon lists there) or the URL of your pickup point, e.g. `https://www.ozon.ru/geo/ekaterinburg/220380/` (open the point on the Ozon map and use "Поделиться"). The server selects it the way an anonymous visitor does; if that fails, answers say so in `region.warning` |
| `OZON_PROXY` | — | Proxy for the browser, e.g. `http://user:pass@host:3128` |
| `OZON_MIN_INTERVAL` | `1.0` | Seconds between requests to Ozon |
| `OZON_IDLE_TIMEOUT` | `600` | Close the browser after this many idle seconds |
| `OZON_HEADLESS` | `1` | `0` shows the browser window (debugging) |
| `OZON_CACHE_DIR` | `~/.cache/ozon-mcp` | Where the browser session is kept (files are `0600`) |

## What the numbers mean

- **`price_with_ozon_card_rub`** — the green price on the site, "С банками":
  paying with an Ozon Bank card (Ozon also lists a few partner banks).
  **`price_without_ozon_card_rub`** — "С другими банками", any other card.
  **`price_before_discount_rub`** — the crossed-out price; the seller sets it,
  so large "discounts" against it are marketing. Search tiles carry only the
  Ozon Card price (`price_rub` for the few items that have a single price).
- **Region**: prices and dates differ by region (checked: the same axe was
  8 453 ₽ in Yekaterinburg and 8 494 ₽ in Moscow). A signed-in buyer may see
  other prices.
- **Ratings**: `rating.shown_on_card` is what the site shows; its `scope`
  says whether it covers only this product or a variant line of N products.
  `rating.this_sku` is computed from the SKU's own reviews via Ozon's
  "this variant" filter; `exact: false` means only a lower bound was
  available. In search, `rating_shared_with` lists other results carrying
  the identical rating — that is a line rating.
- **Delivery**: dates are the ones Ozon shows for the region, converted to
  ISO dates in the region's time zone (the original wording is kept in
  `text`). `express_delivery` / `express_surcharge_rub` is Ozon's paid fast
  option.
- **Stock**: `units_left` only when Ozon itself shows a counter ("241 ед
  осталось"); Ozon's internal cart limits are not reported as stock.
- **Reviews**: `date` is the date the site shows (the edit date for edited
  reviews). Ozon states that only buyers can review (`review_policy`); its
  per-review "Товар куплен на OZON" flag is almost never set, so a missing
  `purchase_badge` does not mean "not bought". `outdated` reviews no longer
  count toward the rating. Buyer names are not returned.
- **Price history**: Ozon has none. When it shows "Стало дешевле",
  `get_product` returns its comparison of the current price (without
  Ozon Card) with last month's average — nothing more.

## Limitations

- Unofficial: uses the storefront's internal composer API, which Ozon can
  change at any time. Parsers are isolated in `parse.py` and covered by
  tests on recorded responses.
- Needs a Russian IP; Ozon's anti-bot rejects VPN/foreign/datacenter
  addresses. Use `OZON_PROXY` if needed.
- Ozon reports no total number of search results; the last page is only
  known when the next one comes back empty.
- The search tile does not name the seller (only whether Ozon itself sells);
  use `get_product` / `compare_products`.
- 18+ products (some knives, machetes) require a date-of-birth confirmation
  on the site; the server does not submit personal data on your behalf:
  search marks them `adult_only`, the card tools return an explicit error.
- Sponsored placement: during testing Ozon did not label any search tile
  as advertising; `sponsored` is set only if a tile says so.
- Read-only and anonymous (see Roadmap).

## Roadmap

- **Later: optional account mode** (off by default). Log in once in a visible
  browser window — phone number and SMS code never pass through the MCP client —
  to see your personal prices and exact delivery dates for your address.
- **Later: cart** — `add_to_cart` / `get_cart` on top of the account mode.
- Checkout, payment and changing the account's delivery address are
  deliberately out of scope.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest            # offline tests on recorded responses
.venv/bin/pytest -m live    # end-to-end over MCP stdio against live Ozon (Russian IP)
```

Layout: `browser.py` (Chromium in its own thread, anti-bot, location),
`client.py` (request policy: re-challenge, rate limits, region), `parse.py`
(pure functions over Ozon's JSON), `server.py` (tools).

## Disclaimer & credits

Not affiliated with Ozon. Intended for personal price research; respect
Ozon's terms of use and keep request rates low. The idea of calling Ozon's
composer API from inside a browser page follows the MIT-licensed
`eduard256/ozon-mcp-server` (Node.js); this is an independent implementation.

License: MIT.
