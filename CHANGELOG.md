# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org): patch releases fix breakage caused by
changes on the marketplace's side, minor releases add features.

## [Unreleased]

### Added
- Search with real pages, six sort orders and a price window; items outside
  Ozon's approximate price filter are flagged; `next_page` until the end.
- Three labelled prices (with Ozon Card, without it, before discount), price
  per unit, sale timer, units left, paid express delivery kept separate.
- Product card: per-SKU rating computed separately from the product-line
  rating shown on the card (with its scope), seller with OGRN/INN and legal
  address, delivery options and dates for the region, variants with prices,
  other sellers, characteristics, package contents, description, price trend
  when Ozon shows one.
- Reviews with pages, worst/best/helpful sorting, SKU or line scope, pros,
  cons, photos, seller replies; comparison of up to 20 SKUs.
- One long-lived headless Chromium on its own thread runs the storefront API
  calls; it restarts itself and re-passes the anti-bot check when needed.
