"""End-to-end check over the real MCP stdio transport against live Ozon.

Deselected by default; run with:  pytest -m live
Needs a Russian IP (Ozon blocks many foreign/VPN addresses) and Playwright's
Chromium (installed automatically on first use).
"""
import json
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.live

X17 = 686632091   # Fiskars X17: one of 24 products sharing one rating
X21 = 858886091   # Fiskars X21 from another seller, with cheaper offers elsewhere
X7 = 1664051565


async def _call_all():
    params = StdioServerParameters(command=sys.executable, args=["-m", "ozon_mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            results = {}
            for key, name, args in [
                ("search", "search_products", {"query": "топор fiskars", "limit": 10}),
                ("search_price", "search_products", {"query": "топор колун", "sort": "price_asc",
                                                     "price_min": 1500, "price_max": 4000, "limit": 36}),
                ("search_page2", "search_products", {"query": "топор колун", "sort": "price_asc",
                                                     "price_min": 1500, "price_max": 4000, "page": 2, "limit": 1}),
                ("product", "get_product", {"product": str(X17)}),
                ("reviews", "get_reviews", {"product": str(X17), "sort": "worst", "limit": 30,
                                            "include_seller_replies": True}),
                ("compare", "compare_products", {"products": [str(X17), str(X21), f"https://www.ozon.ru/product/{X7}/"]}),
            ]:
                res = await session.call_tool(name, args)
                assert not res.is_error, (name, res.content)
                results[key] = res.structured_content or json.loads(res.content[0].text)
            return tools, results


def test_all_tools_over_stdio():
    tools, r = anyio.run(_call_all)
    assert set(tools) == {"search_products", "get_product", "get_reviews", "compare_products"}

    search = r["search"]
    assert search["items"] and search["region"]["city"]
    assert search["resolved_to"]["url"].startswith("https://www.ozon.ru/")
    assert all(i.get("brand") != "Бренд проверен" for i in search["items"])
    assert any("rating_shared_with" in i for i in search["items"])
    assert all("price_with_ozon_card_rub" in i or "price_rub" in i for i in search["items"])

    first, second = r["search_price"], r["search_page2"]
    assert first["returned"] >= 30 and first["next_page"] == 2
    overlap = {i["sku"] for i in first["items"]} & {i["sku"] for i in second["items"]}
    assert len(overlap) <= 3, "page 2 should mostly be new items"
    in_window = [i for i in first["items"] if not i.get("outside_price_window")]
    assert len(in_window) >= len(first["items"]) * 0.8

    product = r["product"]
    prices = product["prices"]
    assert prices["price_with_ozon_card_rub"] <= prices["price_without_ozon_card_rub"]
    assert product["brand"] == "Fiskars"
    assert product["seller"]["legal_name"] and product["seller"]["rating"]
    assert product["delivery"]["options"] and product["delivery"]["options"][0]["date"]
    rating = product["rating"]
    assert rating["this_sku"]["reviews"] < rating["shown_on_card"]["reviews"]
    assert rating["this_sku"]["exact"] is True
    assert len(product["variants"][0]["options"]) == product["variants"][0]["total"] > 8
    assert product["characteristics"] and product["region"]["city"]

    reviews = r["reviews"]
    assert reviews["returned"] >= 30 and reviews["next_page"] == 2
    assert all(rv["sku"] == X17 for rv in reviews["reviews"])
    assert [rv["rating"] for rv in reviews["reviews"]] == sorted(rv["rating"] for rv in reviews["reviews"])
    assert reviews["this_sku_rating"]["reviews"] == rating["this_sku"]["reviews"]
    assert any(rv.get("seller_reply") for rv in reviews["reviews"])

    rows = {row["sku"]: row for row in r["compare"]["items"]}
    assert set(rows) == {X17, X21, X7}
    assert rows[X17]["price_with_ozon_card_rub"] == prices["price_with_ozon_card_rub"]
    assert all(row.get("delivery", {}).get("date") for row in rows.values() if row.get("available"))
