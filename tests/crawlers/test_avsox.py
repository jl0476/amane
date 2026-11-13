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


# ------------------------------------------------------------------
# 原文双轨: 原文取日文轨, 未给出的轨道不声明语言
# ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("movie", "expected"),
    [
        pytest.param(
            dict(_MOVIE, title_ja="日本語タイトル"),
            ("日本語タイトル", "日本語", {"original_title": "jp", "original_plot": "jp"}),
            id="both_tracks",
        ),
        pytest.param(
            dict(_MOVIE, title_ja=""),
            (None, "日本語", {"original_plot": "jp"}),
            id="title_ja_missing",
        ),
        pytest.param(
            dict(_MOVIE, title_ja="日本語タイトル", description_ja=None),
            ("日本語タイトル", None, {"original_title": "jp"}),
            id="description_ja_missing",
        ),
        pytest.param(
            dict(_MOVIE, title_ja="", description_ja=None),
            (None, None, None),
            id="ja_tracks_all_missing",
        ),
    ],
)
async def test_avsox_scrape_emits_original_fields(
    monkeypatch: pytest.MonkeyPatch,
    movie: dict[str, Any],
    expected: tuple[str | None, str | None, dict[str, str] | None],
):
    """原文只取日文轨; 该轨为空时不声明对应字段的语言."""
    crawler = _crawler_with_movie(monkeypatch, movie)
    meta = await crawler._scrape("https://avsox.click/movies/1", FetchOptions(language=Language.JP))
    assert meta is not None
    assert (meta.original_title, meta.original_plot, meta.field_languages) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("movie", "expected_title"),
    [
        pytest.param({"title": "", "title_ja": "日本語タイトル"}, "日本語タイトル", id="blank_base_takes_ja"),
        pytest.param({"title": "", "title_ja": "", "title_en": "English"}, "English", id="ja_missing_takes_en"),
        pytest.param(
            {"title": "", "title_ja": "", "title_en": "", "title_cn": "中文"}, "中文", id="en_missing_takes_cn"
        ),
        pytest.param(
            {"title": "", "title_ja": "", "title_en": "", "title_cn": "", "title_tw": "繁體"},
            "繁體",
            id="cn_missing_takes_tw",
        ),
        # 各轨同时有值: 次序任一交换 (含中文提到日文前) 都会让期望落空, 与逐级回退参数合起来钉死整条顺序.
        pytest.param(
            {"title": "", "title_ja": "日本語", "title_en": "English", "title_cn": "中文", "title_tw": "繁體"},
            "日本語",
            id="all_tracks_takes_ja",
        ),
        pytest.param({"title": "站内标题", "title_ja": "日本語"}, "站内标题", id="base_title_beats_tracks"),
        pytest.param({"title": "", "title_ja": ""}, None, id="all_blank_is_none"),
    ],
)
async def test_title_falls_back_by_japanese_first_order(
    monkeypatch: pytest.MonkeyPatch, movie: dict[str, Any], expected_title: str | None
):
    """标题链按 title, title_ja, title_en, title_cn, title_tw 的顺序回退; base title 为空时日文轨是首个候选."""
    crawler = _crawler_with_movie(monkeypatch, dict(_MOVIE, **movie))
    meta = await crawler._scrape("https://avsox.click/movies/1")
    assert meta is not None
    assert meta.title == expected_title
