"""Pure translations of Ozon's composer-API pages into tool output.

Ozon's storefront is a "composer" app: every page (search, product card,
reviews, modals) is a JSON document whose ``widgetStates`` maps widget ids
("webPrice-3121879-default-1") to JSON *strings* with that widget's state.
Nothing here does I/O, so every function is exercised by tests on recorded
responses (tests/fixtures).

Money arrives as display strings ("6 415 ₽", thin/no-break spaces) and
leaves as numbers of rubles. Dates arrive as Russian words ("Завтра",
"30 сентября") and leave as ISO dates computed for the delivery region's
local "today", with the original text kept next to them.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

SITE = "https://www.ozon.ru"

# ---- generic composer helpers ---------------------------------------------


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def widgets(page: dict | None, name: str) -> list[dict]:
    """All states of widgets called exactly ``name`` (the part before the first "-")."""
    out = []
    for key, raw in ((page or {}).get("widgetStates") or {}).items():
        if key.split("-")[0] == name:
            state = _loads(raw)
            if isinstance(state, dict):
                out.append(state)
    return out


def widget(page: dict | None, name: str) -> dict | None:
    found = widgets(page, name)
    return found[0] if found else None


def layout_entries(page: dict | None) -> list[dict]:
    """Flattened layout: one dict per widget with stateId, component, name, asyncData."""
    out: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "stateId" in node and "component" in node:
                info = _loads(node.get("widgetTrackingInfo")) or {}
                out.append({
                    "stateId": node["stateId"],
                    "component": node["component"],
                    "name": info.get("name") if isinstance(info, dict) else None,
                    "asyncData": node.get("asyncData"),
                })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk((page or {}).get("layout"))
    return out


def shared(page: dict | None) -> dict:
    value = _loads((page or {}).get("shared"))
    return value if isinstance(value, dict) else {}


_SPACES = re.compile(r"[\s\u2009\u202f\xa0]")
_ODD_SPACES = re.compile(r"[\xa0\u2009\u202f]")


def norm(text: Any) -> Any:
    """Display text with no-break/thin spaces turned into plain ones."""
    return _ODD_SPACES.sub(" ", text) if isinstance(text, str) else text


def rub(text: Any) -> int | float | None:
    """"6 415 ₽" -> 6415, "115,50 ₽" -> 115.5; anything without digits -> None."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return text
    if not isinstance(text, str):
        return None
    m = re.search(r"(\d+)(?:[.,](\d{1,2}))?", _SPACES.sub("", text))
    if not m:
        return None
    if m.group(2):
        return round(float(f"{m.group(1)}.{m.group(2)}"), 2)
    return int(m.group(1))


def count(text: Any) -> int | None:
    """"3 536 отзывов" -> 3536."""
    if isinstance(text, int):
        return text
    if not isinstance(text, str):
        return None
    m = re.search(r"\d+", _SPACES.sub("", text))
    return int(m.group(0)) if m else None


def percent(text: Any) -> int | None:
    """"−52%" -> 52."""
    if not isinstance(text, str):
        return None
    m = re.search(r"(\d+)\s*%", text)
    return int(m.group(1)) if m else None


def clean_url(link: str | None) -> str | None:
    """Absolute product URL without tracking parameters."""
    if not link:
        return None
    path = str(link).split("?")[0]
    return path if path.startswith("http") else SITE + path


def product_url(sku: int | str) -> str:
    return f"{SITE}/product/{sku}/"


def sku_from_text(value: str) -> int | None:
    """SKU from a bare number, a product URL/path or a slug ending in -<sku>."""
    v = value.strip()
    if v.isdigit():
        return int(v)
    path = urlparse(v).path if v.startswith("http") else v
    m = re.search(r"/product/(?:[^/]*-)?(\d{5,})/?", path) or re.search(r"(?:^|-)(\d{5,})/?$", path)
    return int(m.group(1)) if m else None


# ---- dates ------------------------------------------------------------------

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")")


def region_today(location: dict | None, now: datetime | None = None) -> date:
    """Today's date in the delivery region (Ozon reports its UTC offset, e.g. "UTC+5")."""
    now = now or datetime.now(timezone.utc)
    tz = (location or {}).get("timezone") or ""
    m = re.match(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?$", tz)
    offset = timedelta(0)
    if m:
        offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        if m.group(1) == "-":
            offset = -offset
    else:
        offset = timedelta(hours=3)  # Ozon's home time zone (Moscow)
    return (now.astimezone(timezone.utc) + offset).date()


def delivery_when(text: str | None, today: date) -> dict | None:
    """Ozon's delivery wording -> {"text", "date"[, "date_to"]}; None if it names no day.

    Handles "Сегодня", "Завтра", "Послезавтра", "30 сентября",
    "Доставим завтра, 26 сентября" and ranges "28 сентября – 2 октября".
    Button captions such as "Перейти" or "В корзину" carry no date -> None.
    """
    if not text:
        return None
    t = norm(text).lower()
    days: list[date] = []
    for day, month in _DATE_RE.findall(t):
        try:
            d = date(today.year, MONTHS[month], int(day))
        except ValueError:
            continue
        if d < today - timedelta(days=31):  # "5 января" seen in December
            d = date(today.year + 1, d.month, d.day)
        days.append(d)
    if not days:
        if "послезавтра" in t:
            days.append(today + timedelta(days=2))
        elif "завтра" in t:
            days.append(today + timedelta(days=1))
        elif "сегодня" in t:
            days.append(today)
    if not days:
        return None
    out = {"text": " ".join(norm(text).split()), "date": days[0].isoformat()}
    if len(days) > 1 and days[-1] != days[0]:
        out["date_to"] = days[-1].isoformat()
    return out


MOSCOW = timezone(timedelta(hours=3))


def iso_date(ts: Any) -> str | None:
    """Unix time -> date in Moscow time, as ozon.ru prints review dates."""
    try:
        return datetime.fromtimestamp(int(ts), MOSCOW).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---- region -------------------------------------------------------------------


def location(page: dict | None) -> dict | None:
    """The delivery region Ozon computed this page for."""
    cur = ((page or {}).get("location") or {}).get("current") or {}
    if not cur:
        return None
    full = cur.get("fullName") or ""
    m = re.search(r"\{\{\{(.*?)(?:\|.*)?\}\}\}", full)
    if m:
        full = m.group(1)
    return {
        "city": cur.get("city") or cur.get("name"),
        "address": full or None,
        "area_id": cur.get("areaId"),
        "timezone": cur.get("timeZoneUtcname"),
    }


# ---- search -------------------------------------------------------------------

PRICE_STYLE_CARD = {"CARD_PRICE", "SALE_PRICE"}
_NOT_BRAND = {"бренд проверен", "стало дешевле", "оригинал", "хит продаж", "новинка", "распродажа"}
_SPONSORED = ("реклама", "спонсор")


def result_grids(page: dict | None) -> list[dict]:
    """Search-result tile grids only.

    Past the last results page Ozon keeps the infinite scroll going with
    a "shelf.infiniteScroll" grid of unrelated products; empty searches show
    recommendations. Only ``catalog.searchResultsV2`` grids are results.
    """
    states = (page or {}).get("widgetStates") or {}
    out = []
    for entry in layout_entries(page):
        if entry["component"] == "tileGridDesktop" and entry["name"] == "catalog.searchResultsV2":
            state = _loads(states.get(entry["stateId"]))
            if isinstance(state, dict):
                out.append(state)
    return out


_UNIT_PRICE = re.compile(r"^\s*([\d\s.,]+)\s*₽\s*/\s*(.+?)\s*$")


def name_of(it: dict) -> str:
    for s in it.get("mainState") or []:
        if s.get("id") == "name":
            return (s.get("textDS") or {}).get("text") or ""
    return ""


def _brand_like(text: str, name: str, verified: bool) -> bool:
    """A leading icon-less tile label is the brand only if it looks like one.

    Tiles put the brand there (often followed by "Бренд проверен"), but the
    same slot also carries other plain labels; require the brand to appear in
    the product name or to be vouched for by the verified-brand badge.
    """
    low = text.lower()
    if low in _NOT_BRAND or "₽" in text or "%" in text or text[:1].isdigit():
        return False
    return verified or low in name.lower()


def _texts_with_icons(items: Iterable[dict]) -> list[tuple[str | None, str]]:
    """labelListV2 items -> [(icon that precedes the text or None, text)]."""
    out = []
    icon = None
    for x in items or []:
        if x.get("type") == "icon":
            icon = ((x.get("icon") or {}).get("icon") or {}).get("icon")
        elif x.get("type") == "text":
            text = ((x.get("text") or {}).get("text") or "").strip()
            if text:
                out.append((icon, norm(text)))
            icon = None
    return out


def _button_caption(multi_button: dict | None, key: str = "ozonButton") -> str | None:
    """Caption of a tile cart button ("30 сентября", "Завтра", "+1 359 ₽ сегодня", "Перейти")."""
    button = (multi_button or {}).get(key) or {}
    for value in button.values():
        if isinstance(value, dict):
            title = (value.get("actionButton") or {}).get("title")
            if isinstance(title, str):
                return norm(title)
    return None


def _express(caption: str | None, today: date) -> dict | None:
    """"+1 359 ₽ сегодня" -> {"text", "date", "surcharge_rub"}."""
    when = delivery_when(caption, today)
    if not when:
        return None
    m = re.match(r"\s*\+\s*([\d\s]+)\s*₽", caption or "")
    if m:
        when["surcharge_rub"] = rub(m.group(1))
    return when


def tile(it: dict, today: date) -> dict:
    """One search result as the tile shows it."""
    out: dict[str, Any] = {"sku": int(it["sku"]) if str(it.get("sku", "")).isdigit() else it.get("sku")}
    labels: list[str] = []
    all_texts: list[str] = []
    for s in it.get("mainState") or []:
        kind = s.get("type")
        if kind == "priceV2":
            block = s.get("priceV2") or {}
            style = (block.get("priceStyle") or {}).get("styleType")
            for p in block.get("price") or []:
                value = rub(p.get("text"))
                if p.get("textStyle") == "PRICE":
                    if style in PRICE_STYLE_CARD:
                        out["price_with_ozon_card_rub"] = value
                    else:
                        out["price_rub"] = value
                elif p.get("textStyle") == "ORIGINAL_PRICE":
                    out["price_before_discount_rub"] = value
            if block.get("discount"):
                out["discount_percent"] = percent(block.get("discount"))
        elif kind == "textDS":
            text = (s.get("textDS") or {}).get("text") or ""
            if s.get("id") == "name":
                out["name"] = norm(text)
            else:
                all_texts.append(text)
                m = re.search(r"(\d[\d\s\u2009\u202f\xa0]*)\s*ед\.?\s*остал", text)
                if m:
                    out["units_left"] = count(m.group(1))
        elif kind == "labelListV2":
            ll = s.get("labelListV2") or {}
            aid = (ll.get("testInfo") or {}).get("automatizationId")
            pairs = _texts_with_icons(ll.get("items"))
            if aid == "tile-list-rating" or any(i and "star" in i for i, _ in pairs):
                for icon, text in pairs:
                    if icon and "star" in icon:
                        try:
                            out["rating"] = float(text.replace(",", "."))
                        except ValueError:
                            pass
                    elif icon and "dialog" in icon:
                        out["reviews"] = count(text)
                    elif icon and "ozon" in icon:
                        out["sold_by_ozon"] = True
            else:
                verified = any(t.lower() == "бренд проверен" for _, t in pairs)
                for icon, text in pairs:
                    low = text.lower()
                    unit = _UNIT_PRICE.match(text)
                    if unit:
                        # "24 ₽ / шт" — Ozon's price per unit for multi-packs
                        out["price_per_unit"] = {"price_rub": rub(unit.group(1)), "unit": unit.group(2)}
                    elif low == "бренд проверен":
                        out["brand_verified"] = True
                    elif icon is None and "brand" not in out and _brand_like(text, name_of(it), verified):
                        out["brand"] = text
                    else:
                        labels.append(text)
    badge = norm(((it.get("tileImage") or {}).get("leftBottomBadgeV2") or {}).get("text"))
    if badge:
        labels.append(badge)
        m = re.search(r"остал\w*\s+(\d+)\s*шт", badge.lower())
        if m and "units_left" not in out:
            out["units_left"] = int(m.group(1))
    if labels:
        out["labels"] = labels
    # Only labels count: a product *named* "Реклама и PR" is not an advert.
    if any(t.lower().startswith(_SPONSORED) for t in all_texts + labels):
        out["sponsored"] = True
    when = delivery_when(_button_caption(it.get("multiButton")), today)
    if when:
        out["delivery"] = when
    express = _express(_button_caption(it.get("multiButton"), "expressButton"), today)
    if express:
        out["express_delivery"] = express
    if it.get("isAdult"):
        out["adult_only"] = True
    out["url"] = clean_url((it.get("action") or {}).get("link")) or product_url(out["sku"])
    return out


def mark_shared_ratings(items: list[dict]) -> None:
    """Flag ratings that several results share.

    Ozon shows the rating of the whole variant line (all colours, sizes,
    weights — sometimes different models) on every member's tile. Identical
    rating *and* review count on different SKUs is that line rating.
    """
    groups: dict[tuple, list[int]] = {}
    for it in items:
        if it.get("reviews") and it.get("rating") is not None:
            groups.setdefault((it["rating"], it["reviews"]), []).append(it["sku"])
    for it in items:
        skus = groups.get((it.get("rating"), it.get("reviews"))) or []
        if len(skus) > 1 and it.get("reviews", 0) >= 5:
            it["rating_shared_with"] = [s for s in skus if s != it["sku"]]


def _active_filters(page: dict | None) -> list[dict]:
    out = []
    for w in widgets(page, "searchResultsFiltersActive"):
        for f in w.get("activeFilters") or []:
            values = [v.get("title") for v in f.get("activeValues") or [] if v.get("title")]
            out.append({"filter": f.get("name") or f.get("key"), "values": values})
    return out


def _price_range(page: dict | None) -> dict | None:
    for w in widgets(page, "filtersDesktop"):
        for section in w.get("sections") or []:
            for f in section.get("filters") or []:
                if f.get("key") != "currency_price":
                    continue
                body = f.get(f.get("type")) or {}
                rng = body.get("rangeFilter") or body
                if rng.get("minValue") is not None:
                    return {"min_rub": rub(rng.get("minValue")), "max_rub": rub(rng.get("maxValue"))}
    return None


def next_link(page: dict | None) -> str | None:
    for w in widgets(page, "infiniteVirtualPaginator"):
        if w.get("nextPage"):
            return w["nextPage"]
    return (page or {}).get("nextPage")


def search_page(page: dict, today: date) -> dict:
    """Items and page facts of one search results page."""
    grids = result_grids(page)
    items = [tile(it, today) for g in grids for it in g.get("items") or [] if it.get("sku")]
    info = page.get("pageInfo") or {}
    catalog = shared(page).get("catalog") or {}
    nxt = next_link(page)
    return {
        "items": items,
        "effective_url": SITE + re.sub(r"(?<=[?&])__rr=\d+&?", "", info["url"]).rstrip("?&") if info.get("url") else None,
        "page_type": info.get("pageType"),
        "category": (catalog.get("category") or {}).get("name"),
        "category_predicted": "category_was_predicted=true" in (info.get("url") or ""),
        "active_filters": _active_filters(page),
        "price_range_of_results": _price_range(page),
        "results_error": bool(widgets(page, "searchResultsError")),
        # Ozon links a next page even past the end; "non_found" pages are
        # recommendations, never results.
        "has_next_link": bool(nxt) and "non_found=1" not in nxt,
    }


# ---- product card ------------------------------------------------------------


def json_ld(page: dict | None) -> dict:
    for s in ((page or {}).get("seo") or {}).get("script") or []:
        if s.get("type") == "application/ld+json":
            data = _loads(s.get("innerHTML"))
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
    return {}


def _rich_text(nodes: Any) -> str:
    """Plain text of Ozon rich text ([{"type": "text", "content": ...}, {"type": "newLine"}])."""
    parts = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") in ("newLine", "br"):
            parts.append("\n")
        elif isinstance(n.get("content"), str):
            parts.append(n["content"])
        elif isinstance(n.get("text"), str):
            parts.append(n["text"])
    return norm(re.sub(r"\n{3,}", "\n\n", "".join(parts))).strip()


def seller(page: dict | None) -> dict | None:
    """Seller of the offer shown on the card, with Ozon's "about the shop" data."""
    cur = widget(page, "webCurrentSeller")
    sticky = (widget(page, "webStickyProducts") or {}).get("seller") or {}
    if not cur and not sticky:
        return None
    cur = cur or {}
    cell = cur.get("sellerCell") or {}
    title = ((cell.get("centerBlock") or {}).get("title") or {}).get("text")
    name = sticky.get("name") or title
    raw = json.dumps(cur, ensure_ascii=False)
    m = re.search(r'"sellerId":\s*"?(\d+)', raw) or re.search(r"[?&]user_id=(\d+)", raw)
    rating_text = ((cur.get("rating") or {}).get("title") or {}).get("text")
    try:
        rating = float(rating_text.replace(",", ".")) if rating_text else None
    except ValueError:
        rating = None
    out: dict[str, Any] = {
        "name": name,
        "seller_id": (int(m.group(1)) or None) if m else None,
        "rating": rating,
        "url": clean_url(sticky.get("link") or ((cell.get("common") or {}).get("action") or {}).get("link")),
    }
    if name and name.endswith("...") and not sticky.get("name"):
        out["name_truncated"] = True
    facts = {}
    for tf in cur.get("trustFactors") or []:
        label = ((tf.get("title") or {}).get("text") or "").strip()
        badge = (tf.get("badge") or {}).get("text")
        tooltip = tf.get("tooltip") or {}
        if label == "О магазине":
            out.update(_legal(tooltip.get("subtitle")))
        elif label:
            facts[label] = norm(badge if badge is not None else ((tooltip.get("title") or {}).get("text")))
    if facts:
        out["facts"] = facts  # e.g. {"Заказы": "1.3 M"} — Ozon's own wording
    if out.get("url") and "/seller/ozon/" in out["url"]:
        out["is_ozon"] = True
    return out


def _legal(subtitle: Any) -> dict:
    """The shop's legal block: company name, OGRN/INN, address, as Ozon prints it.

    Sellers write "ОГРН - 1157746501770" / "Адрес - ..."; Ozon's own block
    has bare lines (name, address, number), so bare 13/15-digit numbers are
    read as ОГРН/ОГРНИП and lines starting with a postcode as the address.
    """
    lines = [ln.strip() for ln in _rich_text(subtitle).split("\n") if ln.strip()]
    out: dict[str, Any] = {}
    other = []
    for i, line in enumerate(lines):
        m = re.match(r"(ОГРНИП|ОГРН|ИНН|УНП|БИН)\s*[-—:]\s*(\S+)", line)
        bare = re.fullmatch(r"\d{10,15}", line)
        if m:
            out.setdefault("registration", {})[m.group(1)] = m.group(2)
        elif bare:
            kind = {13: "ОГРН", 15: "ОГРНИП", 10: "ИНН", 12: "ИНН"}.get(len(line))
            if kind:
                out.setdefault("registration", {})[kind] = line
            else:
                other.append(line)
        elif re.match(r"Адрес\s*[-—:]", line):
            out["legal_address"] = re.sub(r"^Адрес\s*[-—:]\s*", "", line)
        elif i == 0:
            out["legal_name"] = line
        elif re.match(r"\d{6},", line) and "legal_address" not in out:
            out["legal_address"] = line
        else:
            other.append(line)
    if other:
        out["legal_notes"] = other
    return out


def price_block(page: dict | None) -> dict:
    """All prices on the card, each named for what it is."""
    w = widget(page, "webPrice") or {}
    card = rub(w.get("cardPrice"))
    regular = rub(w.get("price"))
    before = rub(w.get("originalPrice")) if w.get("showOriginalPrice", True) else None
    out: dict[str, Any] = {
        "price_with_ozon_card_rub": card,
        "price_without_ozon_card_rub": regular,
        "price_before_discount_rub": before,
    }
    if card is None and regular is not None:
        out["note"] = "Ozon shows a single price for this offer (no Ozon Card discount)."
    return out


def breadcrumbs(page: dict | None) -> list[str]:
    w = widget(page, "breadCrumbs") or {}
    return [b.get("text") for b in w.get("breadcrumbs") or [] if b.get("text")]


def variants(aspects_state: dict | None) -> list[dict]:
    """webAspects / webAspectsModal -> [{aspect, total, options: [...]}]."""
    out = []
    for asp in (aspects_state or {}).get("aspects") or []:
        options = []
        for v in asp.get("variants") or []:
            data = v.get("data") or {}
            value = data.get("searchableText") or _rich_text(data.get("textRs")) or None
            opt = {
                "sku": int(v["sku"]) if str(v.get("sku", "")).isdigit() else v.get("sku"),
                "value": value,
                "price_with_ozon_card_rub": v.get("price") if isinstance(v.get("price"), (int, float)) else rub(data.get("price")),
                "price_before_discount_rub": rub(data.get("originalPrice")),
                "available": v.get("availability") == "inStock",
                "title": data.get("title"),
            }
            if v.get("active"):
                opt["current"] = True
            options.append({k: val for k, val in opt.items() if val is not None})
        total = count((asp.get("aspectModalInfo") or {}).get("realNumberOfVariants"))
        out.append({
            "aspect": asp.get("aspectName"),
            "total": total or len(options),
            "options": options,
        })
    return out


def label_variant_prices(variant_blocks: list[dict], prices: dict) -> list[dict]:
    """Name variant prices after checking what the current variant's price is.

    Variant chips carry one price: the Ozon Card price on most cards, the
    single price on cards without a card discount (e-books, some sellers).
    The current variant's chip is compared with the card's own prices.
    """
    card, regular = prices.get("price_with_ozon_card_rub"), prices.get("price_without_ozon_card_rub")
    current = next((o.get("price_with_ozon_card_rub") for a in variant_blocks for o in a["options"] if o.get("current")), None)
    if current is not None and card is not None and current == card:
        return variant_blocks
    key = "price_rub" if current is not None and card is None and current == regular else "price_shown_rub"
    for a in variant_blocks:
        for o in a["options"]:
            if "price_with_ozon_card_rub" in o:
                o[key] = o.pop("price_with_ozon_card_rub")
        if key == "price_shown_rub":
            a["price_note"] = "Could not tell whether these are Ozon Card prices; compare with get_product of the variant."
    return variant_blocks


def _characteristics_list(w: dict) -> list[dict]:
    out = []
    for group in w.get("characteristics") or []:
        title = group.get("title")
        for part in ("short", "long"):
            for ch in group.get(part) or []:
                values = [v.get("text") for v in ch.get("values") or [] if v.get("text")]
                if ch.get("key") == "Sku" or not values:
                    continue
                item = {"name": ch.get("name"), "value": ", ".join(values)}
                if title:
                    item["group"] = title
                out.append(item)
    return out


def characteristics(page2: dict | None, page: dict | None = None) -> list[dict]:
    """Full attribute list from the card's second page (short list from page 1 as fallback)."""
    for w in widgets(page2, "webCharacteristics"):
        found = _characteristics_list(w)
        if found:
            return found
    out = []
    for w in widgets(page, "webShortCharacteristics"):
        for c in w.get("characteristics") or []:
            name = _rich_text((c.get("title") or {}).get("textRs"))
            value = ", ".join(
                _rich_text(v.get("textRs")) if isinstance(v, dict) and v.get("textRs") else (v.get("text") if isinstance(v, dict) else "")
                for v in c.get("values") or []
            )
            if name and value:
                out.append({"name": name, "value": value})
    return out


def _walk_rich(node: Any, parts: list[str]) -> None:
    if isinstance(node, list):
        for n in node:
            _walk_rich(n, parts)
        return
    if not isinstance(node, dict):
        return
    text = node.get("text")
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, list):
            parts.append("\n".join(c for c in content if isinstance(c, str)))
        items = text.get("items")
        if isinstance(items, list):
            chunk = []
            for it in items:
                if isinstance(it, dict):
                    if it.get("type") == "text" and isinstance(it.get("content"), str):
                        chunk.append(it["content"])
                    elif it.get("type") == "br":
                        chunk.append("\n")
            parts.append("".join(chunk))
    for key, value in node.items():
        if key != "text" and isinstance(value, (dict, list)):
            _walk_rich(value, parts)


def description(page2: dict | None, page: dict | None = None) -> dict:
    """Seller's description text plus titled blocks such as "Комплектация"."""
    text_parts: list[str] = []
    blocks: dict[str, str] = {}
    for w in widgets(page2, "webDescription"):
        for c in w.get("characteristics") or []:
            if c.get("title") and c.get("content"):
                blocks[c["title"]] = c["content"].strip()
        rich = _loads(w.get("richAnnotationJson")) if w.get("richAnnotationJson") is not None else None
        if rich:
            _walk_rich(rich.get("content") if isinstance(rich, dict) else rich, text_parts)
        elif isinstance(w.get("richAnnotation"), str):
            text_parts.append(re.sub(r"<[^>]+>", "\n", w["richAnnotation"]))
    text = "\n".join(p.strip() for p in text_parts if p and p.strip())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        text = (json_ld(page).get("description") or "").strip()
    return {"text": text or None, "blocks": blocks}


def other_sellers_summary(page: dict | None) -> dict | None:
    """"У других продавцов от 4 874 ₽" teaser on the card."""
    w = widget(page, "webBestSeller")
    if not w:
        return None
    text = _rich_text(w.get("textRs"))
    return {
        "offers": count(w.get("count")),
        "from_price_with_ozon_card_rub": rub(text.split("от")[-1]) if "от" in text else None,
        "text": " ".join(text.split()),
    }


def other_sellers(page: dict | None, today: date) -> list[dict]:
    """The "Другие предложения от продавцов" modal: same product, other offers."""
    out = []
    for w in widgets(page, "webSellerList"):
        for s in w.get("sellers") or []:
            creds = [c for c in s.get("credentials") or [] if c and not c.startswith("Режим работы")]
            price = s.get("price") or {}
            delivery = None
            for adv in s.get("advantages") or []:
                if adv.get("key") == "delivery":
                    delivery = delivery_when(_rich_text((adv.get("contentRs") or {}).get("headRs")), today)
            item = {
                "sku": int(s["sku"]) if str(s.get("sku", "")).isdigit() else s.get("sku"),
                "seller": s.get("name"),
                "seller_id": int(s["id"]) if str(s.get("id", "")).isdigit() else s.get("id"),
                "legal_name": creds[0] if creds else None,
                "legal_address": ", ".join(creds[1:]) or None,
                "price_with_ozon_card_rub": rub((price.get("cardPrice") or {}).get("price")),
                "price_rub": rub((price.get("price") or {}).get("price")) if isinstance(price.get("price"), dict) else None,
                "delivery": delivery,
                "url": clean_url(s.get("productLink")) or product_url(s.get("sku")),
            }
            out.append({k: v for k, v in item.items() if v is not None})
    return out


def price_trend(page: dict | None) -> dict | None:
    """The "Стало дешевле" modal: current regular price vs last month's average."""
    w = widget(page, "webPriceDecreasedFullView")
    if not w:
        return None
    points = []
    for line in w.get("lines") or []:
        parts = [p for p in _rich_text(line.get("textRs")).split("\n") if p.strip()]
        if len(parts) >= 2:
            points.append({"label": parts[1].strip(), "price_rub": rub(parts[0])})
    explanation = None
    for tb in widgets(page, "textBlock"):
        for b in tb.get("body") or []:
            text = ((b.get("textAtom") or {}).get("text") or "").strip()
            if text and text != "Стало дешевле":
                explanation = text
    return {"points": points, "explanation": explanation} if points else None


def delivery(state: dict | None, today: date) -> dict | None:
    """webDelivery (loaded asynchronously on the card): options and dates for the region."""
    if not state:
        return None
    out: dict[str, Any] = {"options": []}
    for sec in state.get("sections") or []:
        if sec.get("type") == "separator":
            continue
        lines = [ln.strip() for ln in _rich_text(sec.get("descriptionRs")).split("\n") if ln.strip()]
        if not lines:
            continue
        if sec.get("type") == "addressSelect":
            out["to"] = lines[0]
            # Second line is "Со склада продавца, Москва" — or a prompt
            # ("Укажите полный адрес") that says nothing about the warehouse.
            if len(lines) > 1 and re.search(r"склад|^из\s", lines[1], re.I):
                out["ships_from"] = lines[1]
            continue
        opt: dict[str, Any] = {"method": lines[0]}
        when = delivery_when(" ".join(lines[1:]), today) if len(lines) > 1 else None
        if when:
            opt.update({"date": when["date"], "text": when["text"]})
            if "date_to" in when:
                opt["date_to"] = when["date_to"]
        elif len(lines) > 1:
            opt["text"] = " ".join(lines[1:])
        cost = (sec.get("priceBadge") or {}).get("text")
        if cost:
            opt["cost"] = cost  # Ozon's wording: "Входит в заказ", "Бесплатно", "99 ₽"
        out["options"].append(opt)
    ret = (state.get("returnInfo") or {}).get("text")
    if ret:
        out["returns"] = norm(ret)
    return out if out["options"] or out.get("to") else None


def cart_surcharge(page: dict | None) -> int | float | None:
    """Extra charge of the card's express cart button ("+1 937 ₽"), if Ozon offers one.

    With two cart buttons the first is the paid express delivery (its own
    delivery schema and a "+N ₽" badge), the second the regular one.
    """
    w = widget(page, "webAddToCart") or {}
    first, second = w.get("firstButton"), w.get("secondButton")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    badge = norm(((first.get("toCart") or {}).get("badge") or {}).get("text") or "")
    return rub(badge) if badge.strip().startswith("+") else None


def button_delivery(action: dict | None, today: date, surcharge: int | float | None = None) -> dict:
    """pdpGetButtonTexts: dates under the cart button(s).

    Returns {"standard": {...}} and, when Ozon offers paid express delivery,
    {"express": {..., "surcharge_rub": N}} — so "Сегодня" for an extra
    charge is never passed off as the regular delivery date.
    """
    data = (action or {}).get("data") or {}
    out: dict[str, Any] = {}
    buttons = [data.get("firstButton"), data.get("secondButton")]
    two = all(isinstance(b, dict) for b in buttons)
    for i, b in enumerate(buttons):
        if not isinstance(b, dict):
            continue
        text = (b.get("toCartText") or {}).get("text") or (b.get("inCartText") or {}).get("text")
        when = delivery_when(text, today)
        if not when:
            continue
        if two and i == 0 and surcharge:
            out["express"] = {**when, "surcharge_rub": surcharge}
        elif "standard" not in out:
            out["standard"] = when
    return out


def promo(page: dict | None) -> dict | None:
    """Sale banner on the card ("Распродажа", "7 дней до конца", "241 единица осталась")."""
    w = widget(page, "bigPromoPDP")
    if not w:
        return None
    title = norm((w.get("title") or {}).get("text"))
    if not title:
        return None
    out: dict[str, Any] = {"title": title}
    ends = norm(((w.get("timerBadge") or {}).get("timerText") or {}).get("text"))
    if ends:
        out["ends"] = ends
    left = count((w.get("stockNumber") or {}).get("text"))
    if left is not None:
        out["units_left"] = left
    return out


def product(page: dict, today: date) -> dict:
    """Everything the first card page tells about the offer."""
    ld = json_ld(page)
    heading = widget(page, "webProductHeading") or {}
    price_w = widget(page, "webPrice") or {}
    score = widget(page, "webReviewProductScore") or {}
    detail = widget(page, "webDetailSKU") or {}
    info = page.get("pageInfo") or {}
    sku = count(detail.get("copyText")) or count(ld.get("sku")) or count(str((info.get("analyticsInfo") or {}).get("sku") or ""))
    canonical = next((ln.get("href") for ln in (page.get("seo") or {}).get("link") or [] if ln.get("rel") == "canonical"), None)
    available = price_w.get("isAvailable")
    if available is None and ld.get("offers"):
        available = str(ld["offers"].get("availability", "")).endswith("InStock")
    crumbs = breadcrumbs(page)
    aspects = widget(page, "webAspects")
    questions = widget(page, "webQuestionCount") or {}
    out = {
        "sku": sku,
        "url": clean_url(canonical) or (product_url(sku) if sku else None),
        "name": norm(heading.get("title") or ld.get("name")),
        "brand": ld.get("brand") or None,
        "category_path": crumbs or None,
        "available": available,
        "prices": price_block(page),
        "promo": promo(page),
        "price_dropped_badge": bool(widget(page, "webPriceDecreasedCompact")),
        "line_rating": {
            "rating": score.get("totalScore") or None,
            "reviews": score.get("reviewsCount"),
        },
        "variants": label_variant_prices(variants(aspects), price_block(page)) if aspects else [],
        "seller": seller(page),
        "other_sellers": other_sellers_summary(page),
        "questions": count(questions.get("text")),
        "image": ld.get("image"),
        "adult_gate": bool(widget(page, "userAdultModal")),
        "out_of_stock_page": bool(widget(page, "webOutOfStock")),
    }
    if not out["line_rating"]["reviews"]:
        out["line_rating"] = {"rating": None, "reviews": 0}
    return out


def async_widget(page: dict | None, component: str) -> dict | None:
    """Layout entry of a widget the site loads after the page (e.g. webDelivery)."""
    for entry in layout_entries(page):
        if entry["component"] == component and entry.get("asyncData"):
            return entry
    return None


# ---- reviews -----------------------------------------------------------------


REVIEW_SORTS = {"helpful": "usefulness_desc", "best": "score_desc", "worst": "score_asc"}


def star_split(score_widget: dict | None) -> dict | None:
    """webReviewProductScore on the reviews page: {"5": n, ..., "1": n}."""
    rows = (score_widget or {}).get("score")
    if not rows:
        return None
    out = {}
    for row in rows:
        m = re.match(r"(\d)", row.get("title") or "")
        if m:
            out[m.group(1)] = row.get("value") or 0
    return {k: out.get(k, 0) for k in ("5", "4", "3", "2", "1")}


def review(r: dict, products: dict) -> dict:
    c = r.get("content") or {}
    item_id = str(r.get("itemId") or "")
    prod = products.get(item_id) or {}
    variant = ", ".join(f"{v.get('name')}: {v.get('value')}" for v in prod.get("variants") or [] if v.get("value"))
    badges = " ".join(filter(None, [
        ((r.get("badgeDelivery") or {}).get("text")), ((r.get("badge") or {}).get("text")),
    ])).lower()
    usefulness = r.get("usefulness") or {}
    published = iso_date(r.get("publishedAt") or r.get("createdAt"))
    # The site shows "изменен <date>" for edited reviews instead of the publication date.
    shown = iso_date(r.get("updatedAt")) if r.get("isEdited") and r.get("updatedAt") else published
    out = {
        "review_id": r.get("uuid"),
        "date": shown,
        "rating": c.get("score"),
        "text": norm((c.get("comment") or "").strip()) or None,
        "pros": norm((c.get("positive") or "").strip()) or None,
        "cons": norm((c.get("negative") or "").strip()) or None,
        "sku": int(item_id) if item_id.isdigit() else None,
        "product_name": prod.get("name"),
        "variant": variant or None,
        "photos": len(c.get("photos") or []),
        "videos": len(c.get("videos") or []),
        "helpful": usefulness.get("useful") or 0,
        "unhelpful": usefulness.get("unuseful") or 0,
        "comments": ((r.get("comments") or {}).get("totalCount")) or 0,
    }
    if r.get("isEdited"):
        out["edited"] = True
        if published and published != shown:
            out["published"] = published
    if r.get("isItemPurchased"):
        out["purchase_badge"] = True  # "Товар куплен на OZON"
    if "устарел" in badges:
        out["outdated"] = True  # Ozon: "Отзыв устарел" — not counted in the current rating
    return out


def reviews_page(page: dict) -> dict:
    lr = widget(page, "webListReviews") or {}
    products = lr.get("products") or {}
    paging = lr.get("paging") or {}
    score = widget(page, "webReviewProductScore") or {}
    full = lr.get("fullRequestUrl") or ""
    mode = (parse_qs(urlparse(full).query).get("reviewsVariantMode") or [None])[0]
    policy = ((widget(page, "createReviewButton") or {}).get("subtitle") or {}).get("text")
    return {
        "policy": norm(policy) if policy else None,
        "reviews": [review(r, products) for r in lr.get("reviews") or []],
        "total": paging.get("total"),
        "line_total": paging.get("commonTotal"),
        "page": paging.get("page"),
        "per_page": paging.get("perPage"),
        "has_next": bool(paging.get("nextButton")),
        "variant_mode": int(mode) if mode and mode.isdigit() else None,
        "sort": next((s.get("value") for s in lr.get("sortings") or [] if s.get("active")), None),
        "line_rating": {
            "rating": score.get("totalScore") or lr.get("productScore") or None,
            "reviews": score.get("reviewsCount"),
            "stars": star_split(score),
        },
        "item_id": count(lr.get("itemId")),
    }


def sku_rating(worst_first: list[dict], total: int | None) -> dict:
    """Rating of one SKU from the first page of its own reviews sorted worst first.

    Ozon only shows the line rating. Sorted ascending, the first page holds
    every review below five stars as soon as it also holds a five-star one,
    so the split is exact then; otherwise only a lower bound is known.
    """
    total = total or 0
    scores = [r["rating"] for r in worst_first if isinstance(r.get("rating"), int) and 1 <= r["rating"] <= 5]
    if total == 0:
        return {"rating": None, "reviews": 0, "exact": True}
    stars = {str(k): 0 for k in (5, 4, 3, 2, 1)}
    for s in scores:
        stars[str(s)] += 1
    if len(scores) >= total or 5 in scores:
        stars["5"] = total - sum(stars[k] for k in ("4", "3", "2", "1"))
        avg = sum(int(k) * v for k, v in stars.items()) / total
        return {"rating": round(avg, 2), "reviews": total, "stars": stars, "exact": True}
    return {
        "rating": None, "reviews": total, "exact": False,
        "note": f"At least {len(scores)} of {total} reviews are below five stars; "
                "the first page is not enough to compute the average.",
        "lowest_page_stars": stars,
    }


def seller_reply(action: dict | None) -> dict | None:
    """First official (shop/brand) comment under a review."""
    for c in (action or {}).get("comments") or []:
        author = c.get("author") or {}
        official = (author.get("clientOfficial") or {}).get("brandName")
        if official:
            return {
                "author": official,
                "date": iso_date(c.get("publishedAt") or c.get("createdAt")),
                "text": c.get("comment"),
            }
    return None
