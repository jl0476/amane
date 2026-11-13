"""javlibrary 爬虫单元测试: 站点无简介栏, 原文只声明标题 (宁缺勿猜)."""

from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.sites.javlibrary import JavLibraryCrawler

_DETAIL_HTML = (
    "<html><body>"
    '<div id="video_id"><table><tr><td class="text">ABP-123</td></tr></table></div>'
    '<h3 class="post-title"><a href="/?v=javme123">タイトル</a></h3>'
    "</body></html>"
)
_DETAIL_HTML_WITHOUT_TITLE = (
    '<html><body><div id="video_id"><table><tr><td class="text">ABP-123</td></tr></table></div></body></html>'
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(_DETAIL_HTML, ("タイトル", None, {"original_title": "jp"}), id="title_declared"),
        pytest.param(_DETAIL_HTML_WITHOUT_TITLE, (None, None, None), id="title_missing_undeclared"),
    ],
)
async def test_javlibrary_scrape_emits_original_fields(
    html: str, expected: tuple[str | None, str | None, dict[str, str] | None]
) -> None:
    """标题即日文原文; 站点不提供简介, 原文简介与语言声明留空."""
    web = AsyncMock()
    web.get_text.return_value = html
    crawler = JavLibraryCrawler(client=HttpClient(web=web))
    meta = await crawler._scrape("https://www.javlibrary.com/cn/?v=javme123")
    assert meta is not None
    assert (meta.original_title, meta.original_plot, meta.field_languages) == expected
