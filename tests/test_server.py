import anyio

from ozon_mcp import server

TOOLS = {"search_products", "get_product", "get_reviews", "compare_products"}


def _tools():
    return {t.name: t for t in anyio.run(server.mcp.list_tools)}


def test_tools_are_registered_read_only():
    tools = _tools()
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description


def test_search_options_are_enumerated():
    props = _tools()["search_products"].input_schema["properties"]
    assert set(props["sort"]["enum"]) == {"popular", "price_asc", "price_desc", "rating", "newest", "discount"}
    assert {"page", "price_min", "price_max", "limit", "auto_category"} <= set(props)


def test_review_options_are_enumerated():
    props = _tools()["get_reviews"].input_schema["properties"]
    assert set(props["sort"]["enum"]) == {"helpful", "worst", "best"}
    assert set(props["scope"]["enum"]) == {"sku", "line"}


def test_product_accepts_sku_or_url():
    assert server._sku("1837133915") == 1837133915
    assert server._sku("https://www.ozon.ru/product/fiskars-x18-1069103-1837133915/?at=1") == 1837133915


def test_anticipated_errors_reach_the_agent():
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="Not an Ozon SKU"):
        server.get_product(product="not a sku")
