"""刮削落库集成: 原文双轨四字段经 ScrapeHandler 落库, 并可从 DB 与 API 读回; 翻译层只改写译轨."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx2 import ASGITransport, AsyncClient

from amane.aggregate import AggregatedMetadata, CrawlerLike
from amane.api.routes import API_PREFIX
from amane.config import HotSettings
from amane.crawlers.models import FetchOptions, MediaMetadata, SearchQuery
from amane.enums import Language, MetadataField, SiteName
from amane.handlers import ScrapeHandler, ScrapePayload
from amane.llm.protocol import Translator
from amane.parsing import ContentType
from tests.api.conftest import make_app

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from amane.db.repository import Repository


class OriginalCrawler:
    """样例来源: 返回含原文双轨与逐字段语言声明的 MediaMetadata."""

    name = SiteName.JAVDB

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        return MediaMetadata.model_validate(
            {
                "number": "MIDV-123",
                "title": "日本語タイトル",
                "plot": "日本語あらすじ",
                "original_title": "日本語タイトル",
                "original_plot": "日本語あらすじ",
                "field_languages": {"original_title": "jp", "original_plot": "jp"},
                "source_url": "https://www.javdb.com/v/abc123",
                "poster_urls": ["http://img.example.com/p.jpg"],
            }
        )


class FakeFactory:
    def __init__(self, crawlers: dict[str, CrawlerLike]):
        self._crawlers = crawlers

    async def get_crawlers(self, names: Iterable[str]) -> dict[str, CrawlerLike]:
        wanted = set(names)
        return {name: crawler for name, crawler in self._crawlers.items() if name in wanted}


class StubTranslator:
    """按 Translator 协议回写 ``[{target}]{text}`` 形态."""

    async def translate(
        self, text: str, target: Language, field: MetadataField, *, use_cache: bool = True
    ) -> str | None:
        return f"[{target}]{text}"


def _handler(
    repo: Repository, config: HotSettings | None = None, translator: Translator | None = None
) -> ScrapeHandler:
    return ScrapeHandler(
        repo=repo,
        factory=FakeFactory({"javdb": OriginalCrawler()}),
        resource_store=AsyncMock(),
        pipeline_config=config if config is not None else HotSettings(),
        translator=translator,
    )


@pytest_asyncio.fixture
async def api_env(tmp_path: Path):
    """文件 DB 服务端环境, 返回 (client, repo); 刮削与 API 读回共用同一库."""
    app = make_app(HotSettings(), tmp_path / "data", tmp_path / "logs", tmp_path / "files")
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=f"http://test{API_PREFIX}/") as client:
        yield client, app.state.runtime.repo
    await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio(loop_scope="function")
async def test_scrape_persists_original_fields_to_db_and_api(api_env):
    """刮削后原文四字段落库, GET /metadata/{id} 四字段可读回."""
    client, repo = api_env
    handler = _handler(repo)

    result = await handler.handle(ScrapePayload(number="MIDV-123", content_type=ContentType.CENSORED))
    assert result.success is True
    assert result.result is not None
    metadata_id = result.result.metadata_id

    row = await repo.get_metadata_by_number("MIDV-123")
    assert row is not None
    assert row.original_title == "日本語タイトル"
    assert row.original_plot == "日本語あらすじ"
    assert row.website == "https://www.javdb.com/v/abc123"
    assert row.field_languages == {"original_title": "jp", "original_plot": "jp"}

    resp = await client.get(f"/metadata/{metadata_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["original_title"] == "日本語タイトル"
    assert body["metadata"]["original_plot"] == "日本語あらすじ"
    assert body["metadata"]["website"] == "https://www.javdb.com/v/abc123"
    assert body["metadata"]["field_languages"] == {"original_title": "jp", "original_plot": "jp"}


@pytest.mark.asyncio(loop_scope="function")
async def test_translate_keeps_original_fields(repo: Repository, resource_store):
    """_translate_metadata 只改写 title / plot; 原文双轨保持源站取值."""
    handler = _handler(repo, translator=StubTranslator())
    metadata = AggregatedMetadata(
        number="MIDV-123",
        title="日本語タイトル",
        plot="日本語あらすじ",
        original_title="日本語タイトル",
        original_plot="日本語あらすじ",
        field_languages={"original_title": "jp", "original_plot": "jp"},
    )

    await handler._translate_metadata(
        metadata,
        {MetadataField.TITLE: Language.ZH_CN, MetadataField.PLOT: Language.ZH_CN},
        use_cache=False,
    )

    assert metadata.title == "[zh_cn]日本語タイトル"
    assert metadata.plot == "[zh_cn]日本語あらすじ"
    assert metadata.original_title == "日本語タイトル"
    assert metadata.original_plot == "日本語あらすじ"
    assert metadata.field_languages == {"original_title": "jp", "original_plot": "jp"}
