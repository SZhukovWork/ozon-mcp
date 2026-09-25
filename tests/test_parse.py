import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ozon_mcp import parse

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 25)  # the day the fixtures were recorded


def load(name):
    return json.loads((FIXTURES / name).read_text())


# ---- primitives ------------------------------------------------------------------


@pytest.mark.parametrize("text, value", [
    ("6 415 ₽", 6415), ("13 619 ₽", 13619), ("115,50 ₽", 115.5), ("от 4\xa0874 ₽", 4874),
    ("", None), (None, None), ("нет", None),
])
def test_rub(text, value):
    assert parse.rub(text) == value


def test_count_and_percent():
    assert parse.count("3 536\xa0отзывов") == 3536
    assert parse.count("202 вопроса") == 202
    assert parse.percent("−52%") == 52


@pytest.mark.parametrize("value, sku", [
    ("1837133915", 1837133915),
    ("https://www.ozon.ru/product/fiskars-x18-topor-universalnyy-1-13-kg-1069103-1837133915/?at=xyz", 1837133915),
    ("https://www.ozon.ru/product/686632091/", 686632091),
    ("/product/686632091/reviews/", 686632091),
    ("fiskars-x17-m-topor-kolun-1015641-686632091", 686632091),
    ("топор", None),
])
def test_sku_from_text(value, sku):
    assert parse.sku_from_text(value) == sku


@pytest.mark.parametrize("text, expected", [
    ("Завтра", {"text": "Завтра", "date": "2026-09-26"}),
    ("Послезавтра", {"text": "Послезавтра", "date": "2026-09-27"}),
    ("Сегодня", {"text": "Сегодня", "date": "2026-09-25"}),
    ("30 сентября", {"text": "30 сентября", "date": "2026-09-30"}),
    ("Доставим завтра, 26\xa0сентября", {"text": "Доставим завтра, 26 сентября", "date": "2026-09-26"}),
    ("28 сентября – 2 октября", {"text": "28 сентября – 2 октября", "date": "2026-09-28", "date_to": "2026-10-02"}),
    ("Перейти", None), ("В корзину", None), (None, None),
])
def test_delivery_when(text, expected):
    assert parse.delivery_when(text, TODAY) == expected


def test_delivery_when_rolls_over_the_year():
    assert parse.delivery_when("5 января", date(2026, 12, 29))["date"] == "2027-01-05"


def test_region_today_uses_the_regions_offset():
    late_evening_utc = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    assert parse.region_today({"timezone": "UTC+5"}, late_evening_utc) == date(2026, 9, 26)
    assert parse.region_today({"timezone": "UTC+3"}, late_evening_utc) == date(2026, 9, 25)


def test_location_from_page():
    loc = parse.location(load("search_topor_fiskars.json"))
    assert loc == {"city": "Екатеринбург", "address": "Екатеринбург, Свердловская область",
                   "area_id": 26575, "timezone": "UTC+5"}


def test_location_template_address_is_unwrapped():
    page = {"location": {"current": {"city": "Москва", "fullName": "{{{Россия, Москва, Винницкая улица, 8 корпус 4|ls_dvs_team@x}}}"}}}
    assert parse.location(page)["address"] == "Россия, Москва, Винницкая улица, 8 корпус 4"


# ---- search --------------------------------------------------------------------------


def test_search_page_reports_ozons_category_and_brand_guess():
    sp = parse.search_page(load("search_topor_fiskars.json"), TODAY)
    assert sp["category"] == "Топоры" and sp["category_predicted"]
    assert sp["active_filters"] == [{"filter": "Бренд", "values": ["Fiskars"]}]
    assert sp["price_range_of_results"] == {"min_rub": 2785, "max_rub": 29919}
    assert sp["has_next_link"] and not sp["results_error"]
    assert "/category/topory-9907/fiskars-25422355/" in sp["effective_url"]


def test_search_tile_prices_are_ozon_card_prices():
    items = {i["sku"]: i for i in parse.search_page(load("search_topor_fiskars.json"), TODAY)["items"]}
    x18 = items[1837133915]
    # Checked against the product page: 6415 is "С банками" (Ozon Card), 7127 without it.
    assert x18["price_with_ozon_card_rub"] == 6415
    assert x18["price_before_discount_rub"] == 13619
    assert x18["discount_percent"] == 52
    assert "price_rub" not in x18
    assert x18["units_left"] == 241
    assert x18["delivery"] == {"text": "30 сентября", "date": "2026-09-30"}
    assert x18["rating"] == 4.9 and x18["reviews"] == 3536
    assert x18["url"].endswith("-1837133915/")
    assert items[858885612]["delivery"]["date"] == "2026-09-26"  # "Завтра"
    assert items[858885612]["express_delivery"] == {"text": "+1 359 ₽ сегодня", "date": "2026-09-25", "surcharge_rub": 1359}
    assert "express_delivery" not in x18


def test_verified_brand_badge_is_not_a_brand():
    """The old server reported "Бренд проверен" as the brand."""
    for item in parse.search_page(load("search_topor_fiskars.json"), TODAY)["items"]:
        assert item.get("brand") != "Бренд проверен"
    x18 = next(i for i in parse.search_page(load("search_topor_fiskars.json"), TODAY)["items"] if i["sku"] == 1837133915)
    assert x18["brand_verified"] is True and "brand" not in x18


def test_brand_label_and_sold_by_ozon():
    items = {i["sku"]: i for i in parse.search_page(load("search_naushniki.json"), TODAY)["items"]}
    realme = items[1646128465]
    assert realme["brand"] == "realme" and realme["brand_verified"]
    assert realme["sold_by_ozon"] is True
    assert realme["reviews"] == 13235  # "13 235" without the word "отзывов"


def test_single_price_items_are_not_labelled_card_price():
    items = parse.search_page(load("search_topor_price_asc.json"), TODAY)["items"]
    assert len(items) == 36
    ebook = next(i for i in items if i["sku"] == 907152656)
    assert ebook["price_rub"] == 34 and "price_with_ozon_card_rub" not in ebook


def test_shared_line_ratings_are_flagged():
    items = parse.search_page(load("search_topor_fiskars.json"), TODAY)["items"]
    parse.mark_shared_ratings(items)
    x18 = next(i for i in items if i["sku"] == 1837133915)
    assert {643368876, 643369840, 686738527, 1837133742} <= set(x18["rating_shared_with"])
    lonely = next(i for i in items if i["sku"] == 1300460403)
    assert "rating_shared_with" not in lonely


def test_page_past_the_end_has_no_results_and_no_next():
    sp = parse.search_page(load("search_past_end.json"), TODAY)
    assert sp["items"] == [] and sp["results_error"] and not sp["has_next_link"]


def test_infinite_scroll_shelf_is_not_results():
    """Following Ozon's next-page link past the end yields unrelated products."""
    page = load("search_shelf_after_end.json")
    assert parse.widgets(page, "tileGridDesktop")[0]["items"]
    assert parse.search_page(page, TODAY)["items"] == []


def test_empty_search():
    sp = parse.search_page(load("search_empty.json"), TODAY)
    assert sp["items"] == [] and not sp["has_next_link"]


def test_price_window_page_contains_off_window_items():
    """Ozon's price filter is approximate: the server must flag, not trust it."""
    sp = parse.search_page(load("search_price_window.json"), TODAY)
    assert sp["active_filters"][0]["filter"] == "Цена"
    prices = [i.get("price_with_ozon_card_rub", i.get("price_rub")) for i in sp["items"]]
    assert min(prices) < 1300 and max(prices) > 1450


# ---- product card ------------------------------------------------------------------


def test_product_prices_are_all_named():
    p = parse.product(load("product_1837133915.json"), TODAY)
    assert p["sku"] == 1837133915
    assert p["prices"] == {"price_with_ozon_card_rub": 6415, "price_without_ozon_card_rub": 7127,
                           "price_before_discount_rub": 13619}
    assert p["brand"] == "Fiskars"
    assert p["available"] is True
    assert p["promo"] == {"title": "Распродажа", "ends": "7 дней до конца", "units_left": 241}
    assert p["price_dropped_badge"] is True
    assert p["category_path"][-2:] == ["Топоры", "Fiskars"]
    assert p["questions"] == 202


def test_product_line_rating_and_variants():
    p = parse.product(load("product_1837133915.json"), TODAY)
    assert p["line_rating"] == {"rating": 4.9, "reviews": 3536}
    weight = p["variants"][0]
    assert weight["aspect"] == "Вес, кг" and weight["total"] == 24 and len(weight["options"]) == 8
    current = next(o for o in weight["options"] if o.get("current"))
    assert current["sku"] == 1837133915 and current["price_with_ozon_card_rub"] == 6415
    # The "variants" of this card are different models (X13, X5, Solid A6...).
    assert any("X13" in o["title"] for o in weight["options"])


def test_product_colour_and_size_aspects():
    p = parse.product(load("product_2235242997.json"), TODAY)
    assert [a["aspect"] for a in p["variants"]] == ["Цвет", "Размер"]
    sizes = p["variants"][1]["options"]
    assert any(o["value"] == "41 RU / 25,5 см" and o["price_with_ozon_card_rub"] == 5005 for o in sizes)


def test_seller_with_legal_entity():
    s = parse.product(load("product_1837133915.json"), TODAY)["seller"]
    assert s["name"] == "Садовая техника и инструменты"  # the card cell truncates it
    assert s["seller_id"] == 28895 and s["rating"] == 4.8
    assert s["legal_name"] == 'ООО "САДОВАЯ ТЕХНИКА И ИНСТРУМЕНТЫ"'
    assert s["registration"] == {"ОГРН": "1157746501770"}
    assert s["legal_address"].startswith("105082, Россия, г. Москва")
    assert s["facts"] == {"Заказы": "1.3 M"}


def test_ozon_as_seller():
    s = parse.product(load("product_1646128465.json"), TODAY)["seller"]
    assert s["is_ozon"] and s["seller_id"] is None
    assert s["legal_name"] == "ООО «Интернет Решения»"
    assert s["registration"] == {"ОГРН": "1027739244741"}
    assert s["legal_address"].startswith("123112, город Москва")


def test_other_sellers_teaser_and_list():
    p = parse.product(load("product_858885612.json"), TODAY)
    assert p["other_sellers"] == {"offers": 9, "from_price_with_ozon_card_rub": 4874,
                                  "text": "У других продавцов от 4 874 ₽"}
    offers = parse.other_sellers(load("other_offers_858885612.json"), TODAY)
    assert len(offers) == 9
    cheapest = offers[0]
    assert cheapest["price_with_ozon_card_rub"] == 4874
    assert cheapest["delivery"]["date"] == "2026-10-23"
    assert cheapest["legal_name"] == "WU HAN SHI JIA FENG MAO YI CO., LTD"
    assert any(o["seller"] == "Садовая техника и инструменты" and o["delivery"]["date"] == "2026-09-26" for o in offers)


def test_adult_gate_and_missing_product():
    assert parse.product(load("product_adult_1621563846.json"), TODAY)["adult_gate"] is True
    gone = parse.product(load("product_404.json"), TODAY)
    assert gone["out_of_stock_page"] is True and gone["name"] is None


def test_characteristics_and_description():
    page2 = load("product_page2_1837133915.json")
    chars = parse.characteristics(page2)
    assert {"name": "Партномер", "value": "1069103"} in chars
    assert {"name": "Страна-изготовитель", "value": "Финляндия"} in chars
    assert all(c["name"] != "Артикул" for c in chars)
    desc = parse.description(page2, load("product_1837133915.json"))
    assert desc["blocks"] == {"Комплектация": "Топор с чехлом."}
    assert desc["text"].startswith("ТОПОР FISKARS X18 S")


def test_long_characteristics_are_included():
    chars = parse.characteristics(load("product_page2_858885612.json"))
    features = next(c for c in chars if c["name"] == "Особенности")
    assert "Резиновое покрытие рукоятки" in features["value"]


def test_delivery_widget():
    state = load("delivery_1837133915.json")["state"]
    d = parse.delivery(parse._loads(state), TODAY)
    assert d["to"] == "Екатеринбург, Свердловская область"
    assert d["ships_from"] == "Со склада продавца, Москва"
    assert {"method": "Пункты выдачи и постаматы", "date": "2026-09-30", "text": "30 сентября",
            "cost": "Входит в заказ"} in d["options"]
    assert d["returns"] == "Можно вернуть в течение 15 дней"


def test_delivery_widget_is_found_in_layout():
    entry = parse.async_widget(load("product_1837133915.json"), "webDelivery")
    assert entry["stateId"].startswith("webDelivery-") and entry["asyncData"]


def test_cart_button_delivery():
    page = load("product_686632091.json")
    assert parse.cart_surcharge(page) is None
    assert parse.button_delivery(load("cart_button_686632091.json"), TODAY, None) == {
        "standard": {"text": "Доставим 28 сентября", "date": "2026-09-28"}}


def test_paid_express_is_not_the_delivery_date():
    """Two cart buttons: "Сегодня" costs +1 937 ₽, the regular one is "Завтра"."""
    page = load("product_858886091.json")
    surcharge = parse.cart_surcharge(page)
    assert surcharge == 1937
    dates = parse.button_delivery(load("cart_buttons_858886091.json"), TODAY, surcharge)
    assert dates["standard"] == {"text": "Завтра", "date": "2026-09-26"}
    assert dates["express"] == {"text": "Сегодня", "date": "2026-09-25", "surcharge_rub": 1937}


def test_delivery_widget_without_warehouse_line():
    state = load("delivery_858886091.json")["state"]
    d = parse.delivery(parse._loads(state), TODAY)
    assert "ships_from" not in d  # the second line is the prompt "Укажите полный адрес"
    partner = next(o for o in d["options"] if o["method"] == "Курьерской службой партнёра")
    assert partner["date"] == "2026-09-25" and "cost" not in partner


def test_all_variants_modal():
    modal = parse.widget(load("variants_modal_686632091.json"), "webAspectsModal")
    v = parse.variants(modal)
    assert v[0]["total"] == 24 and len(v[0]["options"]) == 24


def test_price_trend_popup():
    trend = parse.price_trend(load("price_drop_1837133915.json"))
    assert trend["points"] == [{"label": "Цена сейчас", "price_rub": 7127},
                               {"label": "Средняя за август", "price_rub": 7940}]
    assert "без банков-партнёров" in trend["explanation"]


# ---- reviews --------------------------------------------------------------------------


def test_reviews_of_one_sku():
    rp = parse.reviews_page(load("reviews_1837133915_sku_worst.json"))
    assert rp["variant_mode"] == 1 and rp["sort"] == "score_asc"
    assert rp["total"] == 76 and rp["line_total"] == 3536
    assert rp["line_rating"]["stars"] == {"5": 3386, "4": 102, "3": 21, "2": 7, "1": 20}
    assert all(r["sku"] == 1837133915 for r in rp["reviews"])
    assert rp["reviews"][0]["variant"] == "Вес, кг: 1.13"


def test_sku_rating_is_exact_when_a_five_star_review_is_on_the_worst_first_page():
    rp = parse.reviews_page(load("reviews_1837133915_sku_worst.json"))
    own = parse.sku_rating(rp["reviews"], rp["total"])
    assert own == {"rating": 4.99, "reviews": 76, "stars": {"5": 75, "4": 1, "3": 0, "2": 0, "1": 0}, "exact": True}


def test_line_reviews_mix_models_and_rating_is_not_exact():
    rp = parse.reviews_page(load("reviews_1837133915_line_worst.json"))
    assert rp["variant_mode"] == 2
    assert len({r["sku"] for r in rp["reviews"]}) > 1  # reviews of other models of the line
    own = parse.sku_rating(rp["reviews"], rp["total"])
    assert own["exact"] is False and own["rating"] is None


def test_review_fields():
    rp = parse.reviews_page(load("reviews_1837133915_line_worst.json"))
    r = next(r for r in rp["reviews"] if r["rating"] == 1 and r["text"])
    assert set(r) >= {"review_id", "date", "rating", "text", "sku", "variant",
                      "photos", "videos", "helpful", "comments"}
    assert "author" not in r
    # Ozon's purchase flag is false on every review while it states that only
    # buyers can review: a false flag must not be reported as "not purchased".
    assert "purchase_badge" not in r and "purchased_on_ozon" not in r
    assert rp["policy"].startswith("Отзывы могут оставлять только те, кто купил товар")
    assert any(x.get("outdated") for x in rp["reviews"])


@pytest.mark.parametrize("scores, total, expected", [
    ([], 0, {"rating": None, "reviews": 0, "exact": True}),
    ([3, 5], 2, {"rating": 4.0, "reviews": 2, "stars": {"5": 1, "4": 0, "3": 1, "2": 0, "1": 0}, "exact": True}),
    ([1, 2, 5], 10, {"rating": 4.3, "reviews": 10, "stars": {"5": 8, "4": 0, "3": 0, "2": 1, "1": 1}, "exact": True}),
])
def test_sku_rating_cases(scores, total, expected):
    assert parse.sku_rating([{"rating": s} for s in scores], total) == expected


def test_sku_rating_lower_bound_only():
    own = parse.sku_rating([{"rating": 1}] * 30, 100)
    assert own["exact"] is False and own["lowest_page_stars"]["1"] == 30


def test_seller_reply():
    reply = parse.seller_reply(load("review_comments.json"))
    assert reply["author"] == "Садовая техника и инструменты"
    assert reply["date"] == "2026-03-31" and reply["text"].startswith("Жаль, что товар")


def test_buyer_comment_is_not_a_seller_reply():
    action = {"comments": [{"author": {"firstName": "Иван", "clientOfficial": {"brandName": ""}},
                            "isUserComment": False, "comment": "у меня тоже"}]}
    assert parse.seller_reply(action) is None


def test_edited_review_shows_the_edit_date_like_the_site():
    raw = {"uuid": "u", "publishedAt": 1778231366, "updatedAt": 1765317433, "isEdited": True,
           "itemId": "642282616", "content": {"score": 5, "comment": "ok"}}
    r = parse.review(raw, {})
    assert r["date"] == "2025-12-10" and r["edited"] and r["published"] == "2026-05-08"


def test_unit_price_label_is_not_a_brand():
    """The old server reported "24 ₽ / шт" as the brand."""
    items = {i["sku"]: i for i in parse.search_page(load("search_unit_price.json"), TODAY)["items"]}
    wedge = items[4669685563]
    assert wedge["price_per_unit"] == {"price_rub": 24, "unit": "шт"}
    assert "brand" not in wedge
    assert wedge["units_left"] == 9  # "Осталось 9 шт" badge
    assert items[4296240198]["price_per_unit"]["price_rub"] == 22


def test_plain_label_needs_name_or_verified_badge_to_be_a_brand():
    tile = {
        "sku": 1,
        "mainState": [
            {"type": "labelListV2", "labelListV2": {"items": [{"type": "text", "text": {"text": "Хит сезона"}}],
                                                    "testInfo": {"automatizationId": "tile-list-labels"}}},
            {"type": "textDS", "id": "name", "textDS": {"text": "Топор кованый"}},
        ],
    }
    out = parse.tile(tile, TODAY)
    assert "brand" not in out and out["labels"] == ["Хит сезона"]


def test_variant_prices_are_named_after_the_current_variant():
    blocks = [{"aspect": "Тип книги", "total": 1, "options": [
        {"sku": 907152656, "value": "Электронная книга", "price_with_ozon_card_rub": 34, "current": True}]}]
    single = parse.label_variant_prices(blocks, {"price_with_ozon_card_rub": None, "price_without_ozon_card_rub": 34})
    assert single[0]["options"][0]["price_rub"] == 34 and "price_with_ozon_card_rub" not in single[0]["options"][0]
    card = parse.product(load("product_1837133915.json"), TODAY)["variants"]
    assert all("price_with_ozon_card_rub" in o for o in card[0]["options"])


def test_sponsored_only_from_labels_not_from_the_name():
    def make(name, label):
        return {"sku": 1, "mainState": [
            {"type": "textDS", "id": "name", "textDS": {"text": name}},
            {"type": "labelListV2", "labelListV2": {"items": [{"type": "text", "text": {"text": label}}],
                                                    "testInfo": {"automatizationId": "tile-list-labels"}}},
        ]}
    assert "sponsored" not in parse.tile(make("Реклама и PR. Учебник", "Хит"), TODAY)
    assert parse.tile(make("Топор", "Реклама"), TODAY)["sponsored"] is True
