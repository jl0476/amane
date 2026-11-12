"""mgstage 爬虫单元测试: 竖版海报地址的推导与守卫, 原文双轨的声明口径."""

from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.sites.mgstage import MGStageCrawler, _poster_url

_DETAIL_HTML = (
    '<html><body><div id="center_column"><div><h1>{title}</h1></div></div>'
    '<dl id="introduction"><dd><p>{plot}</p></dd></dl></body></html>'
)


@pytest.mark.parametrize(
    ("cover_url", "expected"),
    [
        (
            "https://image.mgstage.com/images/prestige/abf/355/pb_e_abf-355.jpg",
            "https://image.mgstage.com/images/prestige/abf/355/pf_e_abf-355.jpg",
        ),
        (
            "https://image.mgstage.com/images/shirouto/siro/3759/pb_e_siro-3759.jpg",
            "https://image.mgstage.com/images/shirouto/siro/3759/pf_e_siro-3759.jpg",
        ),
        # 详情页的图片兜底位给的不是横版放大图, 这些形态不推导.
        ("https://image.mgstage.com/images/prestige/abf/355/pf_o1_abf-355.jpg", None),
        ("https://image.mgstage.com/images/shirouto/siro/3759/pb_p_siro-3759.jpg", None),
        ("pb_e_abf-355.jpg", None),
        ("", None),
    ],
)
def test_poster_url(cover_url: str, expected: str | None) -> None:
    assert _poster_url(cover_url) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            _DETAIL_HTML.format(title="タイトル", plot="あらすじ"),
            ("タイトル", "あらすじ", {"original_title": "jp", "original_plot": "jp"}),
            id="title_and_plot",
        ),
        pytest.param(
            _DETAIL_HTML.format(title="タイトル", plot=""),
            ("タイトル", None, {"original_title": "jp"}),
            id="plot_missing",
        ),
    ],
)
async def test_mgstage_scrape_emits_original_fields(
    html: str, expected: tuple[str | None, str | None, dict[str, str] | None]
) -> None:
    """原文与标题 / 简介同源 (日文), 简介缺省时只声明标题."""
    web = AsyncMock()
    web.get_text.return_value = html
    crawler = MGStageCrawler(client=HttpClient(web=web))
    meta = await crawler._scrape("https://www.mgstage.com/product/product_detail/ABP-123/")
    assert meta is not None
    assert (meta.original_title, meta.original_plot, meta.field_languages) == expected
