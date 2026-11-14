"""刮削落库集成: 原文双轨四字段经 ScrapeHandler 落库, 并可从 DB 与 API 读回; 翻译层只改写译轨."""

import io
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import numpy as np
import pytest
import pytest_asyncio
from httpx2 import ASGITransport, AsyncClient
from PIL import Image

from amane.aggregate import AggregatedMetadata, CrawlerLike
from amane.api.routes import API_PREFIX
from amane.config import HotSettings, MetadataConfig
from amane.crawlers.models import FetchOptions, MediaMetadata, SearchQuery
from amane.enums import CropStrategy, Language, MetadataField, SiteName
from amane.handlers import ScrapeHandler, ScrapePayload
from amane.llm.protocol import Translator
from amane.media import ResourceStore, crop_poster, optional_cv2
from amane.media import crop as crop_mod
from amane.media.resource_store import internal_url_hash
from amane.parsing import ContentType
from tests.api.conftest import make_app

if TYPE_CHECKING:
    from collections.abc import Iterable

    from amane.db.repository import Repository
    from amane.net.http import WebClient


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


# --- face 策略刮削入口接线 (检测先于键构造, R-materialize-face-wiring) ---

_FACE_FIXTURE = Path(__file__).parent.parent / "media" / "fixtures" / "yunet_face.jpg"
_FACE_THUMB_URL = "https://img.example.com/t.jpg"
_FACE_POSTER_URL = "https://img.example.com/p.jpg"


def _jpeg_bytes(size: tuple[int, int], color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class _ImageBytesClient:
    """按 URL 写出预设图片字节并记录请求的下载桩."""

    def __init__(self, images: dict[str, bytes]) -> None:
        self._images = images

    async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._images[url])
        return True

    async def get_filesize(self, url: str, **kwargs: object) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs: object) -> str:
        return url


class _PosterCrawler:
    """样例来源: 矮 poster 候选 + thumb, face 策略下命中裁剪路径."""

    name = SiteName.JAVDB

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        return MediaMetadata(
            number="MIDV-123",
            title="タイトル",
            poster_urls=[_FACE_POSTER_URL],
            thumb_urls=[_FACE_THUMB_URL],
        )


def _expected_face_window(thumb: Path, ratio: float) -> tuple[int, int, int, int]:
    """测试内独立检测 (不经被测实现): YuNet 检出最大人脸, 按契约口径推导期望裁剪窗."""
    cv2 = optional_cv2.module()
    assert cv2 is not None
    with Image.open(thumb) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        bgr = np.asarray(rgb)[:, :, ::-1]
    detector = cv2.FaceDetectorYN.create(str(crop_mod._model_path()), "", (width, height), 0.6)
    detector.setInputSize((width, height))
    _, faces = detector.detect(bgr)
    assert faces is not None
    box = max(
        ((int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces),
        key=lambda b: (b[2] * b[3], -b[0]),
    )
    target_w = int(height * ratio)
    left = max(0, min(round(box[0] + box[2] / 2 - target_w / 2), width - target_w))
    return (left, 0, left + target_w, height)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("thumb_case", "expected_args"),
    [("face", "face:legacy:0.7000"), ("plain", "right:legacy:0.7000")],
    ids=["face_detected", "no_face_degrades_to_right"],
)
async def test_face_strategy_scrape_materializes_face_product(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    thumb_case: str,
    expected_args: str,
) -> None:
    """face 策略刮削 (无媒体文件的番号): 持久化 URL 与磁盘产物都来自 Face 流程 — 检出落 face 键与人脸窗产物; 无人脸降级 right 键与右裁产物."""
    thumb_bytes = _FACE_FIXTURE.read_bytes() if thumb_case == "face" else _jpeg_bytes((512, 512), "blue")
    client = cast(
        "WebClient",
        _ImageBytesClient({_FACE_THUMB_URL: thumb_bytes, _FACE_POSTER_URL: _jpeg_bytes((300, 420), "blue")}),
    )
    cfg = HotSettings(metadata=MetadataConfig(crop_type={ContentType.CENSORED: CropStrategy.FACE}))
    handler = ScrapeHandler(
        repo=repo,
        factory=FakeFactory({"javdb": _PosterCrawler()}),
        resource_store=resource_store,
        pipeline_config=cfg,
        web_client=client,
    )

    result = await handler.handle(ScrapePayload(number="MIDV-123", content_type=ContentType.CENSORED))
    assert result.success is True

    meta = await repo.get_metadata_by_number("MIDV-123")
    assert meta is not None
    assert meta.thumb_urls == [_FACE_THUMB_URL]
    poster_url = meta.poster_urls[0]
    assert poster_url.startswith("/api/resources/")  # 持久化 URL 来自裁剪派生
    url_hash = internal_url_hash(poster_url)
    assert url_hash is not None
    found = await resource_store.get_by_url_hash(url_hash)
    assert found is not None
    record, product = found
    assert record.meta is not None
    assert record.meta.get("args") == expected_args  # 键编码实际执行策略
    assert record.meta.get("src") == _FACE_THUMB_URL

    thumb_file = tmp_path / "thumb.jpg"
    thumb_file.write_bytes(thumb_bytes)
    if thumb_case == "face":
        window = _expected_face_window(thumb_file, 0.7)
        buf = io.BytesIO()
        Image.open(thumb_file).convert("RGB").crop(window).save(buf, format="JPEG", quality=95)
    else:
        assert crop_poster(thumb_file, tmp_path / "reference.jpg", poster_ratio=0.7, jpeg_quality=95) is True
        buf = io.BytesIO((tmp_path / "reference.jpg").read_bytes())
    assert product.read_bytes() == buf.getvalue()  # 磁盘产物像素与实际执行策略一致
