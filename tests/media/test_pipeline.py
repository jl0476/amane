"""测试刮削期资源管线 materialize_images (选源/裁剪/就地超分) 与双入口策略分派."""

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
import sqlalchemy.exc
from PIL import Image

from amane.config import HotSettings, MetadataConfig, ScrapingConfig, SrConfig
from amane.enums import DownloadableResource
from amane.handlers import OrganizeHandler, OrganizePayload
from amane.handlers.file import _place_library_images
from amane.media import CropRotation, ResourceStore, crop_poster, manual_crop_image, materialize_images
from amane.media import pipeline as pipeline_mod
from amane.media.resource_store import TMP_MARK
from amane.organize import ResolvedPaths
from amane.parsing import ContentType
from tests.handlers.test_file_ops import MappedClient, _pixel_close, _seed_library
from tests.helpers import LOAD_TOLERANT_TIMEOUT, await_for

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.net.http import WebClient


class FakeClient:
    """伪 WebClient: download 按 url 写出预设尺寸的图片 (或视频占位); fail 集合内 URL 下载失败."""

    def __init__(
        self,
        sizes: dict[str, tuple[int, int]],
        fail: set[str] | None = None,
        orientations: dict[str, int] | None = None,
    ):
        self._sizes = sizes
        self.fail = fail or set()
        self.orientations = orientations or {}
        self.downloaded: list[str] = []

    async def download(self, url: str, dest: Path, **kwargs) -> bool:
        self.downloaded.append(url)
        if url in self.fail:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = self._sizes.get(url)
        if size is None:
            dest.write_bytes(b"video-bytes")  # trailer 等非图
            return True
        img = Image.new("RGB", size, "blue")
        orientation = self.orientations.get(url)
        if orientation is None:
            img.save(dest)
        else:
            exif = img.getexif()
            exif[274] = orientation
            img.save(dest, exif=exif)
        return True

    async def get_filesize(self, url: str, **kwargs) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs) -> str:
        return url


@pytest.mark.asyncio
async def test_poster_cropped_when_candidate_small(resource_store: ResourceStore, tmp_path: Path):
    """poster 候选偏矮 → 从 thumb 按配置 poster_ratio 靠右裁, 不用候选长宽比."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (300, 420),  # b/h=0.78 < 0.9 → 裁; 候选比 ≈0.714 ≠ 配置 0.7
        }
    )
    cfg = HotSettings()  # sr.enabled 默认 False; poster_ratio 默认 0.7
    out = await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.thumb_urls == ["https://s/t.jpg"]  # 外部列表原样
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)  # 内部派生
    # 派生 args 编码 executed:aspect:ratio, 比例取配置 0.7, 非候选 300/420
    url_hash = out.poster_urls[0].rsplit("/", 1)[-1]
    got = await resource_store.get_by_url_hash(url_hash)
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.meta is not None
    assert crop_res.meta.get("args") == "right:legacy:0.7000"
    poster = Image.open(crop_path)
    assert poster.size == (376, 538)  # int(538 * 0.7), 非 int(538 * 300/420)=384


@pytest.mark.asyncio
async def test_poster_external_when_candidate_tall(resource_store: ResourceStore, tmp_path: Path):
    """poster 候选够高 → 不裁剪, poster 记外部 URL."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (379, 538),  # b/h=1.0 ≥ 0.9 → 不裁
        }
    )
    cfg = HotSettings()
    out = await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == ["https://s/p.jpg"]  # 外部列表原样


@pytest.mark.asyncio
async def test_no_poster_source_crops_from_thumb(resource_store: ResourceStore, tmp_path: Path):
    """无 poster 候选 → 从 thumb 裁剪 (内部 URL)."""
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    cfg = HotSettings()
    out = await materialize_images(
        [],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)


@pytest.mark.parametrize(
    ("content_type", "expect_crop", "desc"),
    [
        (ContentType.CENSORED, True, "有码"),
        (ContentType.UNCENSORED, True, "无码"),
        (ContentType.WESTERN, False, "欧美"),
        (ContentType.FC2, False, "FC2"),
    ],
)
@pytest.mark.asyncio
async def test_poster_crop_gated_by_content_type(
    resource_store: ResourceStore,
    tmp_path: Path,
    content_type: ContentType,
    expect_crop: bool,
    desc: str,
):
    """只对策略矩阵放行的类型裁切; 不允许时也不为裁切临时下载 thumb."""
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.poster]))
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    out = await materialize_images(
        [],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=content_type,
        metadata_cfg=cfg.metadata,
    )
    if expect_crop:
        assert len(out.poster_urls) == 1, desc
        assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX), desc
        assert "https://s/t.jpg" in client.downloaded, desc
    else:
        assert out.poster_urls == [], desc
        assert "https://s/t.jpg" not in client.downloaded, desc


@pytest.mark.asyncio
async def test_trailer_downloaded_external_url(resource_store: ResourceStore, tmp_path: Path):
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    settings = HotSettings()
    settings.scraping.download_resources.append(DownloadableResource.trailer)
    out = await materialize_images(
        [],
        ["https://s/t.jpg"],
        ["https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        settings,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=settings.metadata,
    )
    assert out.trailer_urls == ["https://s/trailer.mp4"]
    assert "https://s/trailer.mp4" in client.downloaded  # 已下载到本地


@pytest.mark.asyncio
async def test_empty_sources(resource_store: ResourceStore, tmp_path: Path):
    cfg = HotSettings()
    out = await materialize_images(
        [],
        [],
        [],
        resource_store,
        cast("WebClient", FakeClient({})),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == []
    assert out.thumb_urls == []
    assert out.trailer_urls == []


@pytest.mark.asyncio
async def test_eager_sr_overwrites(resource_store: ResourceStore, tmp_path: Path, monkeypatch):
    """sr.enabled 时, 低质图被就地超分 (meta 打 sr 标记)."""
    from amane.sr.run import SrResult

    async def fake_run_sr(inp, out, cfg, data_dir, **kwargs):
        Image.new("RGB", (1600, 1076), "green").save(out)
        return SrResult(success=True, output=out)

    monkeypatch.setattr(pipeline_mod, "run_SR", fake_run_sr)

    client = FakeClient({"https://s/t.jpg": (800, 538)})  # max=800 < 1200 → 超分
    cfg = HotSettings()
    cfg.sr.enabled = True
    out = await materialize_images(
        [],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )

    thumb_res = await resource_store.get_by_url("https://s/t.jpg")
    assert thumb_res is not None
    assert thumb_res.meta is not None and "sr" in thumb_res.meta
    assert out.thumb_urls == ["https://s/t.jpg"]  # URL 不变 (就地覆盖)


class _GatedClient:
    """伪 WebClient: 下载开始时登记并停住, 由调用方放行 — 钉住「判缓存 → 落盘」的窗口.

    只有首次下载过闸: 后续下载直接落盘, 于是「谁在什么时候覆盖了同一路径」可被断言."""

    def __init__(self, sizes: dict[str, tuple[int, int]], gate: asyncio.Future[None]):
        self._sizes = sizes
        self._gate = gate
        self.downloaded: list[str] = []

    async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
        first = not self.downloaded
        self.downloaded.append(url)
        if first:
            await self._gate  # 停在这里时缓存里还没有这条记录
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", self._sizes[url], "blue").save(dest)
        return True

    async def get_filesize(self, url: str, **kwargs: object) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs: object) -> str:
        return url


@pytest.mark.asyncio
async def test_concurrent_materialize_keeps_sr_result(
    resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """同一 URL 被两个影片任务并发物化: 后到的调用不得覆盖先落盘的超分结果.

    第一个任务的下载停在闸门上 (缓存里还没有这条记录), 第二个任务同时越过缓存判据. 放行后第一个
    任务走完下载 / 落库 / 超分, 第二个任务若仍去下载, 就把超分结果覆盖回原图, 其落库被 url UNIQUE
    拒绝 — 磁盘退回原图而记录仍记 sr 标记.
    """
    from amane.sr.run import SrResult

    calls = 0

    async def fake_run_sr(inp, out, cfg, data_dir, **kwargs):
        nonlocal calls
        calls += 1
        Image.new("RGB", (1600, 1076), "green").save(out)
        return SrResult(success=True, output=out)

    monkeypatch.setattr(pipeline_mod, "run_SR", fake_run_sr)

    gate = asyncio.get_running_loop().create_future()
    client = _GatedClient({"https://s/t.jpg": (800, 538)}, gate)
    cfg = HotSettings(
        sr=SrConfig(enabled=True, max_bytes_threshold=64 * 1024),
        scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}),
    )

    async def run() -> None:
        # 重复下载的落库被 url UNIQUE 拒绝: 文件已覆盖, 记录没写成 — 正是本用例要暴露的路径.
        with contextlib.suppress(sqlalchemy.exc.IntegrityError):
            await materialize_images(
                [],
                ["https://s/t.jpg"],
                [],
                resource_store,
                cast("WebClient", client),
                cfg,
                tmp_path,
                content_type=ContentType.CENSORED,
                metadata_cfg=cfg.metadata,
            )

    async def first_download_started() -> bool:
        return bool(client.downloaded)

    async def sr_done() -> bool:
        return calls == 1

    first = asyncio.create_task(run())
    second = asyncio.create_task(run())
    try:
        await await_for(first_download_started, timeout=LOAD_TOLERANT_TIMEOUT)
        gate.set_result(None)
        # 等下载方完成超分再收尾: 覆盖点若存在, 就落在「磁盘已是超分结果」之后.
        await await_for(sr_done, timeout=LOAD_TOLERANT_TIMEOUT)
        await asyncio.gather(first, second)
    finally:
        if not gate.done():
            gate.set_result(None)
        await asyncio.gather(first, second, return_exceptions=True)

    record = await resource_store.get_by_url("https://s/t.jpg")
    assert record is not None
    disk = resource_store.full_path(record)
    assert record.meta is not None and "sr" in record.meta
    assert resource_store._hash_file(disk) == record.content_hash
    assert Image.open(disk).size == (1600, 1076)  # 磁盘是超分结果, 不是后写的原图
    assert client.downloaded == ["https://s/t.jpg"]  # 记录写完即命中, 不重复下载
    assert calls == 1


@pytest.mark.asyncio
async def test_download_resources_gates_acquire(resource_store: ResourceStore, tmp_path: Path):
    """未选中的类型不写入 Resource."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (400, 600),
            "https://s/trailer.mp4": (1, 1),
        }
    )
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}))
    await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        ["https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
        extrafanart_urls={"javdb": ["https://s/e1.jpg"]},
    )
    assert await resource_store.get_by_url("https://s/t.jpg") is not None
    assert await resource_store.get_by_url("https://s/p.jpg") is None
    assert await resource_store.get_by_url("https://s/trailer.mp4") is None
    assert "https://s/t.jpg" in client.downloaded
    assert "https://s/p.jpg" not in client.downloaded
    assert "https://s/trailer.mp4" not in client.downloaded


def test_success_first_partition():
    """稳定分区: 成功保序前置, 失败保序排到末尾; 空成功集幂等."""
    cases = [
        (["d1", "l1", "d2", "l2"], {"l1", "l2"}, ["l1", "l2", "d1", "d2"]),
        (["a", "b"], {"a", "b"}, ["a", "b"]),  # 全成功
        (["a", "b"], set(), ["a", "b"]),  # 全失败
        (["a", "b", "c"], {"c"}, ["c", "a", "b"]),  # 尾部成功提至首位
        (["a", "b", "c"], {"b"}, ["b", "a", "c"]),  # 中部成功前置
        ([], set(), []),
    ]
    for urls, ok, expected in cases:
        assert pipeline_mod._success_first(urls, ok) == expected


@pytest.mark.asyncio
async def test_poster_reordered_when_first_url_dead(resource_store: ResourceStore, tmp_path: Path):
    """poster 首位 URL 下载失败 → 成功者前置 (无 thumb 不触发裁剪)."""
    client = FakeClient(sizes={"https://s/live.jpg": (400, 600)}, fail={"https://s/dead.jpg"})
    cfg = HotSettings()
    out = await materialize_images(
        ["https://s/dead.jpg", "https://s/live.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert "https://s/dead.jpg" in client.downloaded  # 死 URL 仍尝试下载


@pytest.mark.asyncio
async def test_thumb_trailer_reordered_when_first_dead(resource_store: ResourceStore, tmp_path: Path):
    """thumb/trailer 首位 URL 下载失败 → 成功者前置."""
    client = FakeClient(sizes={"https://s/live.jpg": (800, 538)}, fail={"https://s/dead.jpg", "https://s/dead.mp4"})
    settings = HotSettings()
    settings.scraping.download_resources.append(DownloadableResource.trailer)
    out = await materialize_images(
        [],
        ["https://s/dead.jpg", "https://s/live.jpg"],
        ["https://s/dead.mp4", "https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        settings,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=settings.metadata,
    )
    assert out.thumb_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert out.trailer_urls == ["https://s/trailer.mp4", "https://s/dead.mp4"]


@pytest.mark.asyncio
async def test_manual_crop_image_external_source(resource_store: ResourceStore, tmp_path: Path):
    client = FakeClient({"https://s/a.jpg": (800, 538)})

    url = await manual_crop_image(
        "https://s/a.jpg",
        (0, 0, 400, 538),
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
        subject="头像",
    )

    assert url.startswith(pipeline_mod.RESOURCE_URL_PREFIX)
    got = await resource_store.get_by_url_hash(url.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    # meta 记派生来源; 另有源内容版本 (srcv), 供命中时核对源是否已变.
    assert {k: v for k, v in record.meta.items() if k != "srcv"} == {
        "op": "crop",
        "src": "https://s/a.jpg",
        "args": "box:0,0,400,538",
    }
    assert "srcv" in record.meta
    assert Image.open(path).size == (400, 538)


@pytest.mark.asyncio
async def test_manual_crop_image_rotation_records_args_and_dims(resource_store: ResourceStore, tmp_path: Path):
    """旋转后按互换的宽高校验并裁切; 派生键带角度, 与不转的结果分开."""
    client = FakeClient({"https://s/a.jpg": (800, 538)})
    common = (resource_store, cast("WebClient", client), HotSettings(), tmp_path)

    rotated = await manual_crop_image("https://s/a.jpg", (0, 0, 538, 538), *common, subject="封面图", rotation=90)
    got = await resource_store.get_by_url_hash(rotated.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert {k: v for k, v in record.meta.items() if k != "srcv"} == {
        "op": "crop",
        "src": "https://s/a.jpg",
        "args": "box:0,0,538,538:r90",
    }
    assert "srcv" in record.meta
    assert Image.open(path).size == (538, 538)

    plain = await manual_crop_image("https://s/a.jpg", (0, 0, 538, 538), *common, subject="封面图")
    assert plain != rotated
    again = await manual_crop_image("https://s/a.jpg", (0, 0, 538, 538), *common, subject="封面图", rotation=90)
    assert again == rotated  # 同角度同框命中缓存


@pytest.mark.asyncio
async def test_manual_crop_image_exif_source_uses_oriented_size(resource_store: ResourceStore, tmp_path: Path):
    """方向 6 的源图 (400x300) 按转正后 300x400 校验与裁切."""
    client = FakeClient({"https://s/o6.jpg": (400, 300)}, orientations={"https://s/o6.jpg": 6})
    args = (resource_store, cast("WebClient", client), HotSettings(), tmp_path)

    url = await manual_crop_image("https://s/o6.jpg", (0, 0, 300, 400), *args, subject="头像")
    got = await resource_store.get_by_url_hash(url.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert {k: v for k, v in record.meta.items() if k != "srcv"} == {
        "op": "crop",
        "src": "https://s/o6.jpg",
        "args": "box:0,0,300,400",
    }
    assert "srcv" in record.meta
    assert Image.open(path).size == (300, 400)

    with pytest.raises(ValueError, match="裁切区域无效"):
        await manual_crop_image("https://s/o6.jpg", (0, 0, 400, 300), *args, subject="头像")


@pytest.mark.asyncio
async def test_manual_crop_image_internal_source_records_locator(resource_store: ResourceStore, tmp_path: Path):
    async def producer(dest: Path) -> bool:
        Image.new("RGB", (400, 538), "blue").save(dest)
        return True

    seed = await resource_store.acquire_derived("https://s/orig.jpg", "crop", "box:0,0,400,538", producer)
    assert seed is not None
    internal = f"{pipeline_mod.RESOURCE_URL_PREFIX}/{ResourceStore.url_hash(seed.url)}"

    class _ExplodingClient:
        async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
            raise AssertionError("内部源不应触网")

    url = await manual_crop_image(
        internal,
        (0, 0, 200, 269),
        resource_store,
        cast("WebClient", _ExplodingClient()),
        HotSettings(),
        tmp_path,
        subject="头像",
    )

    got = await resource_store.get_by_url_hash(url.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert record.meta is not None
    assert record.meta["src"] == seed.url  # 底层 locator, 而非内部 URL
    assert Image.open(path).size == (200, 269)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_url", "box", "rotation", "message"),
    [
        ("https://s/missing.jpg", (0, 0, 10, 10), 0, "无法获取头像"),
        ("data:image/png;base64,AAAA", (0, 0, 10, 10), 0, "无法获取头像"),
        ("https://s/video.mp4", (0, 0, 10, 10), 0, "头像无法读取"),
        ("https://s/a.jpg", (10, 0, 5, 10), 0, "裁切区域无效"),
        ("https://s/a.jpg", (0, 0, 800, 538), 90, "裁切区域无效"),
    ],
)
async def test_manual_crop_image_invalid_inputs(
    resource_store: ResourceStore,
    tmp_path: Path,
    source_url: str,
    box: tuple[int, int, int, int],
    rotation: CropRotation,
    message: str,
):
    client = FakeClient({"https://s/a.jpg": (800, 538)}, fail={"https://s/missing.jpg"})
    with pytest.raises(ValueError, match=message):
        await manual_crop_image(
            source_url,
            box,
            resource_store,
            cast("WebClient", client),
            HotSettings(),
            tmp_path,
            subject="头像",
            rotation=rotation,
        )


@pytest.mark.asyncio
async def test_all_urls_fail_keeps_order(resource_store: ResourceStore, tmp_path: Path):
    """全部 URL 下载失败 → 列表保持原序 (死 URL 留尾供来源恢复后重试)."""
    client = FakeClient(sizes={}, fail={"https://s/a.jpg", "https://s/b.jpg"})
    cfg = HotSettings()
    out = await materialize_images(
        ["https://s/a.jpg", "https://s/b.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == ["https://s/a.jpg", "https://s/b.jpg"]


@pytest.mark.asyncio
async def test_unselected_kind_not_reordered(resource_store: ResourceStore, tmp_path: Path):
    """未选中下载的类型不重排 (无逐 URL 成败信息)."""
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}))
    client = FakeClient(sizes={"https://s/t.jpg": (800, 538)})
    out = await materialize_images(
        ["https://s/p1.jpg", "https://s/p2.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == ["https://s/p1.jpg", "https://s/p2.jpg"]


@pytest.mark.asyncio
async def test_cache_hit_counts_as_success(resource_store: ResourceStore, tmp_path: Path):
    """Resource 缓存命中计成功: 已缓存 URL 前置, 且不重复下载."""
    seed = FakeClient(sizes={"https://s/live.jpg": (800, 538)})
    assert await resource_store.acquire("https://s/live.jpg", cast("WebClient", seed)) is not None

    client = FakeClient(sizes={}, fail={"https://s/dead.jpg"})
    cfg = HotSettings()
    out = await materialize_images(
        ["https://s/dead.jpg", "https://s/live.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert "https://s/live.jpg" not in client.downloaded  # 缓存命中, 无网络请求
    assert "https://s/dead.jpg" in client.downloaded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aspect", "expected_args", "expected_size"),
    [
        ("legacy", "right:legacy:0.7000", (376, 538)),
        ("emby", "right:emby:0.6667", (358, 538)),
        ("full", "right:full:0.7067", (380, 538)),
    ],
)
async def test_derived_args_encode_executed_strategy(
    resource_store: ResourceStore,
    tmp_path: Path,
    aspect: Literal["legacy", "emby", "full"],
    expected_args: str,
    expected_size: tuple[int, int],
):
    """派生键按实际执行策略编码: args = f"{executed}:{aspect}:{ratio:.4f}", 比例由 crop_aspect 解析."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (300, 420),  # b/h=0.78 < 0.9 → 裁
        }
    )
    cfg = HotSettings(metadata=MetadataConfig(crop_aspect=aspect))
    out = await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    got = await resource_store.get_by_url_hash(out.poster_urls[0].rsplit("/", 1)[-1])
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.meta is not None
    assert crop_res.meta.get("args") == expected_args
    assert Image.open(crop_path).size == expected_size


@pytest.mark.asyncio
async def test_materialize_global_scope_right(resource_store: ResourceStore, tmp_path: Path):
    """物化按全局 MetadataConfig 裁剪: 默认 legacy 0.7 的产物与 legacy 右侧裁剪算法逐字节一致."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (300, 420),
        }
    )
    cfg = HotSettings()
    out = await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    got = await resource_store.get_by_url_hash(out.poster_urls[0].rsplit("/", 1)[-1])
    assert got is not None
    _, crop_path = got

    # 独立重放 legacy 右侧裁剪 (poster_ratio=0.7, q=95), 与派生产物逐字节比对.
    thumb_record = await resource_store.get_by_url("https://s/t.jpg")
    assert thumb_record is not None
    reference = tmp_path / "reference.jpg"
    assert crop_poster(resource_store.full_path(thumb_record), reference, poster_ratio=0.7, jpeg_quality=95) is True

    assert crop_path.read_bytes() == reference.read_bytes()
    assert Image.open(crop_path).size == (376, 538)


def test_allows_poster_crop_removed() -> None:
    """media 包不再导出 allows_poster_crop; 干净进程导入 media 与 handlers 无残留引用."""
    import amane.media as media_pkg
    from amane.media import images as images_mod

    assert not hasattr(images_mod, "allows_poster_crop")
    assert not hasattr(media_pkg, "allows_poster_crop")
    assert "allows_poster_crop" not in media_pkg.__all__

    for statement in (
        "import amane.media, amane.handlers.file, amane.handlers.scrape",
        "import amane.handlers.scrape; import amane.handlers.file; import amane.media",
    ):
        result = subprocess.run([sys.executable, "-c", statement], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"导入失败: {statement}\n{result.stderr}"


def _organize_paths(base: Path) -> ResolvedPaths:
    return ResolvedPaths(
        video=base / "MIDV-123.mp4",
        thumb=base / "thumb.jpg",
        poster=base / "poster.jpg",
        fanart=base / "fanart.jpg",
        extrafanart_dir=base / "extrafanart",
        nfo=base / "MIDV-123.nfo",
        trailer=base / "trailer.mp4",
    )


@pytest.mark.asyncio
async def test_custom_skip_ratio_respected_both_entries(resource_store: ResourceStore, tmp_path: Path):
    """skip_ratio 为既有可配置阈值: 同一组尺寸 0.9 → 候选原样, 0.95 → 裁剪 (不得硬编码 0.9).

    刮削入口 materialize_images 对两个取值分别验证; 整理入口现状路径 (_place_library_images)
    无候选高度比较, skip_ratio 不参与, 本任务阶段仅验证迁移后类型门槛保持:
    有码从 thumb 裁剪, fc2 不裁 (整理期重选在任务 8 接入).
    """
    sizes = {"https://s/t.jpg": (800, 538), "https://s/p.jpg": (379, 500)}  # b/h ≈ 0.929

    keep_cfg = HotSettings(scraping=ScrapingConfig(poster_crop_skip_ratio=0.9))
    out_keep = await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", FakeClient(sizes)),
        keep_cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=keep_cfg.metadata,
    )
    assert out_keep.poster_urls == ["https://s/p.jpg"]  # 0.929 ≥ 0.9 → 候选原样

    crop_cfg = HotSettings(scraping=ScrapingConfig(poster_crop_skip_ratio=0.95))
    out_crop = await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", FakeClient(sizes)),
        crop_cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=crop_cfg.metadata,
    )
    assert out_crop.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)  # 0.929 < 0.95 → 裁剪

    # 整理入口 (现状路径): 类型门槛经策略矩阵保持 — 有码裁剪, fc2 不裁.
    thumb_local = tmp_path / "src.jpg"
    Image.new("RGB", (800, 538), "blue").save(thumb_local)
    organize_cfg = HotSettings()
    cropped = _organize_paths(tmp_path / "lib" / "MIDV-123")
    await _place_library_images(
        cropped,
        thumb_local=thumb_local,
        poster_local=None,
        trailer_local=None,
        extrafanart_targets=[],
        kinds={DownloadableResource.thumb, DownloadableResource.poster},
        config=organize_cfg,
        file_info=None,
        content_type=ContentType.CENSORED,
        watermark_dir=None,
    )
    assert Image.open(cropped.poster).size == (376, 538)

    skipped = _organize_paths(tmp_path / "lib2" / "FC2-1")
    await _place_library_images(
        skipped,
        thumb_local=thumb_local,
        poster_local=None,
        trailer_local=None,
        extrafanart_targets=[],
        kinds={DownloadableResource.thumb, DownloadableResource.poster},
        config=organize_cfg,
        file_info=None,
        content_type=ContentType.FC2,
        watermark_dir=None,
    )
    assert not skipped.poster.exists()


# --- quality/priority 双入口分派 (先择优后裁剪, R2 PLAN-09) ---

# thumb 低清在前高清在后, 两者高度不同 → 裁剪产物高度可区分裁剪源 (600=首个, 538=胜者).
_THUMB_URLS = ["https://s/t-low.jpg", "https://s/t-high.jpg"]
_THUMB_SIZES = {"https://s/t-low.jpg": (600, 600), "https://s/t-high.jpg": (800, 538)}
# poster 候选一矮一高: 对 600 高 thumb b/h≈0.733 → 裁; 对 538 高 thumb b/h≈0.929 → 候选原样.
_POSTER_URLS = ["https://s/p-low.jpg", "https://s/p-high.jpg"]
_POSTER_SIZES = {"https://s/p-low.jpg": (500, 440), "https://s/p-high.jpg": (500, 500)}

_ORGANIZE_IMAGES = {
    "https://s/t-low.jpg": ((600, 600), "blue"),
    "https://s/t-high.jpg": ((800, 538), "green"),
    "https://s/p-low.jpg": ((500, 440), "purple"),
    "https://s/p-high.jpg": ((500, 500), "orange"),
    "https://s/s-low.jpg": ((460, 460), "purple"),
    "https://s/s-high.jpg": ((500, 440), "purple"),
}


def _tmp_leftovers(store: ResourceStore) -> list[Path]:
    """base_dir 下残留的独占临时; 择优轮结束后必须为空, priority 路径则从不产生."""
    return sorted(store._base_dir.rglob(f"*{TMP_MARK}*"))


@pytest.mark.asyncio(loop_scope="function")
async def test_priority_strategy_matches_current_behavior(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """priority 沿用 acquire_first 零改动: 刮削与整理两入口产物与现状一致 — 取首个候选与首个 thumb, 无独占临时."""
    client = FakeClient({**_THUMB_SIZES, **_POSTER_SIZES})
    cfg = HotSettings(metadata=MetadataConfig(thumb_download_strategy="priority"))
    out = await materialize_images(
        _POSTER_URLS,
        _THUMB_URLS,
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    # 刮削入口: 首个候选偏矮 → 从首个 thumb 右侧裁剪; 逐 URL acquire, 全候选落缓存.
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)
    got = await resource_store.get_by_url_hash(out.poster_urls[0].rsplit("/", 1)[-1])
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.meta is not None
    assert crop_res.meta.get("args") == "right:legacy:0.7000"
    assert crop_res.meta.get("src") == "https://s/t-low.jpg"  # 裁剪源 = 首个 thumb
    assert Image.open(crop_path).size == (420, 600)  # 高度 600 → 来自首个 thumb, 非胜者 (538)
    for url in (*_THUMB_URLS, *_POSTER_URLS):
        assert await resource_store.get_by_url(url) is not None
    assert _tmp_leftovers(resource_store) == []

    # 整理入口: 库未覆盖 → priority, poster = 首个 thumb 的裁剪产物, thumb 沿现状取首个候选.
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=_THUMB_URLS,
        raw={"dmm": {"poster_urls": _POSTER_URLS}},
    )
    assert meta.id is not None
    lib, root = await _seed_library(repo, tmp_path, name="p", metadata_id=meta.id, number="MIDV-123")
    assert lib.id is not None
    handler = OrganizeHandler(
        repo, HotSettings(), resource_store, web_client=cast("WebClient", MappedClient(_ORGANIZE_IMAGES))
    )
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0
    poster = root / "MIDV-123" / "poster.jpg"
    assert Image.open(poster).size == (420, 600)
    assert _pixel_close(poster, (0, 0, 255))
    with Image.open(root / "MIDV-123" / "thumb.jpg") as img:
        assert img.size == (600, 600)  # thumb 取首个候选 (现状行为)


@pytest.mark.asyncio(loop_scope="function")
async def test_quality_strategy_picks_best_both_entries(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """quality 全候选独占临时下载后择优: 两入口低清在前高清在后 → 高分辨率者胜出; 只提交胜者, 裁剪源取胜者 (先择优后裁剪)."""
    # 刮削入口: 胜者 p-high 够高 → 免裁, poster 首项为胜者, 磁盘与缓存只有胜者.
    client = MappedClient(_ORGANIZE_IMAGES)
    cfg = HotSettings(metadata=MetadataConfig(thumb_download_strategy="quality"))
    out = await materialize_images(
        _POSTER_URLS,
        _THUMB_URLS,
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert out.poster_urls == ["https://s/p-high.jpg", "https://s/p-low.jpg"]  # 胜者置首, 其余保序
    assert out.thumb_urls == ["https://s/t-high.jpg", "https://s/t-low.jpg"]  # 胜者置首, 其余保序
    thumb_winner = await resource_store.get_by_url("https://s/t-high.jpg")
    assert thumb_winner is not None
    assert Image.open(resource_store.full_path(thumb_winner)).size == (800, 538)  # 磁盘字节 = 高清胜者
    assert await resource_store.get_by_url("https://s/t-low.jpg") is None  # 落选者不提交
    assert await resource_store.get_by_url("https://s/p-high.jpg") is not None
    assert await resource_store.get_by_url("https://s/p-low.jpg") is None
    assert _tmp_leftovers(resource_store) == []

    # 刮削入口 (两个候选都偏矮): 裁剪执行, 裁剪源 = thumb 择优胜者 → 先择优后裁剪.
    out2 = await materialize_images(
        ["https://s/s-low.jpg", "https://s/s-high.jpg"],
        _THUMB_URLS,
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    assert len(out2.poster_urls) == 1
    got = await resource_store.get_by_url_hash(out2.poster_urls[0].rsplit("/", 1)[-1])
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.meta is not None
    assert crop_res.meta.get("args") == "right:legacy:0.7000"
    assert crop_res.meta.get("src") == "https://s/t-high.jpg"  # 裁剪源 = quality 胜者, 非首个
    assert Image.open(crop_path).size == (376, 538)  # 高度 538 → 来自胜者 thumb
    assert await resource_store.get_by_url("https://s/s-high.jpg") is not None  # poster 胜者提交
    assert await resource_store.get_by_url("https://s/s-low.jpg") is None

    # 整理入口: 库级 quality → 重选胜者 p-high 原样成为产物 (免裁), 落选者不落缓存.
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=_THUMB_URLS,
        raw={"dmm": {"poster_urls": _POSTER_URLS}},
    )
    assert meta.id is not None
    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="q",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"thumb_download_strategy": "quality"},
    )
    assert lib.id is not None
    handler = OrganizeHandler(
        repo, HotSettings(), resource_store, web_client=cast("WebClient", MappedClient(_ORGANIZE_IMAGES))
    )
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0
    poster = root / "MIDV-123" / "poster.jpg"
    assert Image.open(poster).size == (500, 500)  # 胜者候选原样, 非首个候选也非裁剪产物
    assert _pixel_close(poster, (255, 165, 0))
    assert await resource_store.get_by_url("https://s/p-low.jpg") is None  # 重选落选者不落缓存
    assert _tmp_leftovers(resource_store) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_quality_poster_urls_lead_with_winner(resource_store: ResourceStore, tmp_path: Path) -> None:
    """quality 胜者置于落库 URL 首位: 低清在前高清在后时首项 = 胜者; 其余成功候选保持原相对顺序."""
    client = MappedClient(_ORGANIZE_IMAGES)
    cfg = HotSettings(metadata=MetadataConfig(thumb_download_strategy="quality"))
    out = await materialize_images(
        _POSTER_URLS,
        [],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    # 胜者 p-high (500x500, 面积更大) 置于首位, 落选者 p-low 在后.
    assert out.poster_urls[0] == "https://s/p-high.jpg"
    assert out.poster_urls == ["https://s/p-high.jpg", "https://s/p-low.jpg"]

    # priority 路径不变: 首个成功项仍居首.
    prio = HotSettings(metadata=MetadataConfig(thumb_download_strategy="priority"))
    out_prio = await materialize_images(
        _POSTER_URLS,
        [],
        [],
        resource_store,
        cast("WebClient", client),
        prio,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=prio.metadata,
    )
    assert out_prio.poster_urls[0] == "https://s/p-low.jpg"


@pytest.mark.asyncio(loop_scope="function")
async def test_quality_thumb_urls_lead_with_winner(resource_store: ResourceStore, tmp_path: Path) -> None:
    """quality 胜者置于落库 thumb URL 首位: 低清在前高清在后时首项 = 胜者; priority 路径首项不变."""
    client = MappedClient(_ORGANIZE_IMAGES)
    cfg = HotSettings(metadata=MetadataConfig(thumb_download_strategy="quality"))
    out = await materialize_images(
        [],
        _THUMB_URLS,
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=cfg.metadata,
    )
    # 胜者 t-high (800x538, 面积更大) 置于首位, 落选者 t-low 在后.
    assert out.thumb_urls[0] == "https://s/t-high.jpg"
    assert out.thumb_urls == ["https://s/t-high.jpg", "https://s/t-low.jpg"]

    # priority 路径不变: 首个成功项仍居首.
    prio = HotSettings(metadata=MetadataConfig(thumb_download_strategy="priority"))
    out_prio = await materialize_images(
        [],
        _THUMB_URLS,
        [],
        resource_store,
        cast("WebClient", client),
        prio,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=prio.metadata,
    )
    assert out_prio.thumb_urls[0] == "https://s/t-low.jpg"
    assert out_prio.thumb_urls == _THUMB_URLS
