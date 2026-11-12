"""avsox 多语言能力声明与简介语言轨选取."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from amane.crawlers.models import FetchOptions
from amane.crawlers.sites.avsox import AvsoxCrawler
from amane.enums import Language, SiteName

from .driven import http_client

# description 缺省, 各语言轨给不同值: 轨为空的语言不得借用其它语言.
_MOVIE: dict[str, Any] = {
    "movieId": "1",
    "movieFanHao": "ABP-123",
    "title": "标题",
    "description_cn": "中文",
    "description_ja": "日本語",
}


def _crawler_with_movie(monkeypatch: pytest.MonkeyPatch, movie: dict[str, Any]) -> AvsoxCrawler:
    crawler = AvsoxCrawler(client=http_client(AsyncMock()))

    async def fake_api(method: str, payload: list[Any], *, referer: str) -> dict[str, Any]:
        # _api 契约: 返回已解包的 data 部分.
        return movie

    monkeypatch.setattr(crawler, "_api", fake_api)
    return crawler


def test_profile_declares_multi_language():
    from amane.crawlers.site_roles import MULTI_LANGUAGE_SITES

    assert SiteName.AVSOX in MULTI_LANGUAGE_SITES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected_plot"),
    [
        pytest.param(Language.JP, "日本語", id="ja_track"),
        pytest.param(Language.ZH_CN, "中文", id="cn_track"),
        pytest.param(Language.EN, None, id="en_track_missing"),
    ],
)
async def test_plot_takes_exact_language_track(
    monkeypatch: pytest.MonkeyPatch, language: Language, expected_plot: str | None
):
    """简介按请求语言取对应轨; 该语言轨为空时不借用其它语言."""
    crawler = _crawler_with_movie(monkeypatch, dict(_MOVIE))
    meta = await crawler._scrape("https://avsox.click/movies/1", FetchOptions(language=language))
    assert meta is not None
    assert meta.plot == expected_plot


@pytest.mark.asyncio
async def test_plot_keeps_page_description(monkeypatch: pytest.MonkeyPatch):
    """页面正文 description 在前, 作为请求语言的取值之一."""
    movie = dict(_MOVIE, description="页面简介")
    crawler = _crawler_with_movie(monkeypatch, movie)
    meta = await crawler._scrape("https://avsox.click/ja/movies/1", FetchOptions(language=Language.JP))
    assert meta is not None
    assert meta.plot == "页面简介"
