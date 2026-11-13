"""prestige 爬虫单元测试: 原文双轨的声明口径."""

from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.sites.prestige import PrestigeCrawler

_PRODUCT_UUID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
_SKU = [{"deliveryItemId": "ABW350"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"uuid": _PRODUCT_UUID, "title": "タイトル", "body": "あらすじ", "sku": _SKU},
            ("タイトル", "あらすじ", {"original_title": "jp", "original_plot": "jp"}),
            id="title_and_plot",
        ),
        pytest.param(
            {"uuid": _PRODUCT_UUID, "title": "タイトル", "sku": _SKU},
            ("タイトル", None, {"original_title": "jp"}),
            id="plot_missing",
        ),
        pytest.param(
            {"uuid": _PRODUCT_UUID, "body": "あらすじ", "sku": _SKU},
            (None, "あらすじ", {"original_plot": "jp"}),
            id="title_missing",
        ),
    ],
)
async def test_prestige_scrape_emits_original_fields(
    payload: dict[str, object], expected: tuple[str | None, str | None, dict[str, str] | None]
) -> None:
    """原文与标题 / 简介同源 (日文), 单边缺省时只声明给出的一侧."""
    web = AsyncMock()
    web.get_json.return_value = payload
    crawler = PrestigeCrawler(client=HttpClient(web=web))
    meta = await crawler._scrape(f"https://www.prestige-av.com/api/product/{_PRODUCT_UUID}")
    assert meta is not None
    assert (meta.original_title, meta.original_plot, meta.field_languages) == expected
