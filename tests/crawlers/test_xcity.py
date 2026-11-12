"""xcity 爬虫单元测试: 原文双轨的声明口径."""

from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.sites.xcity import XCityCrawler

_DETAIL_HTML = (
    "<html><body>"
    '<span id="program_detail_title">タイトル</span>'
    '<span id="hinban">ABP-123</span>'
    '<p class="lead">{plot}</p>'
    "</body></html>"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            _DETAIL_HTML.format(plot="あらすじ"),
            ("タイトル", "あらすじ", {"original_title": "jp", "original_plot": "jp"}),
            id="title_and_plot",
        ),
        pytest.param(
            _DETAIL_HTML.format(plot=""),
            ("タイトル", None, {"original_title": "jp"}),
            id="plot_missing",
        ),
    ],
)
async def test_xcity_scrape_emits_original_fields(
    html: str, expected: tuple[str | None, str | None, dict[str, str] | None]
) -> None:
    """原文与标题 / 简介同源 (日文), 简介缺省时只声明标题."""
    web = AsyncMock()
    web.get_text.return_value = html
    crawler = XCityCrawler(client=HttpClient(web=web))
    meta = await crawler._scrape("https://xcity.jp/avod/detail/?id=123456")
    assert meta is not None
    assert (meta.original_title, meta.original_plot, meta.field_languages) == expected
