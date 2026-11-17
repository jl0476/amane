"""测试 file ops 消费已物化 URL (内部派生 / 外部) + trailer 落盘."""

import io
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import numpy as np
import pytest
from PIL import Image, ImageDraw
from structlog.testing import capture_logs

from amane.api.models.media import MediaFileResponse, to_media_file_response
from amane.api.models.tasks import OrganizeSubmission
from amane.config import HotSettings, MetadataConfig, ScrapingConfig, WatermarkConfig
from amane.crawlers.models import FetchOptions, MediaMetadata, SearchQuery
from amane.db.models import Library, MediaFile, MediaFileStatus, Metadata
from amane.db.repo_types import WriteMode
from amane.enums import CropStrategy, DownloadableResource
from amane.handlers import OrganizeHandler, OrganizePayload, ScrapeHandler, ScrapePayload
from amane.handlers.file import (
    FileOperationsResult,
    _acquire_candidates_local,
    _copy_into_library,
    execute_file_operations,
    resolve_effective_config,
)
from amane.media import ResourceStore, crop_poster, materialize_images, optional_cv2
from amane.media import crop as crop_mod
from amane.media.resource_store import TMP_MARK, internal_url_hash
from amane.organize import MoveMode, PlaceOutcome, ResolvedPaths, resolve_paths
from amane.parsing import ContentType, parse_file_info

if TYPE_CHECKING:
    from collections.abc import Iterable

    from amane.aggregate import CrawlerLike
    from amane.db.repository import Repository
    from amane.net.http import WebClient


class FakeClient:
    """download 写出小图/视频占位."""

    async def download(self, url: str, dest: Path, **kwargs) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".mp4"):
            dest.write_bytes(b"video")
        else:
            Image.new("RGB", (800, 538), "blue").save(dest)
        return True

    async def get_filesize(self, url: str, **kwargs):
        return None

    async def resolve_final_url(self, url: str, **kwargs) -> str:
        return url


def _pixel(path: Path) -> tuple[int, int, int]:
    with Image.open(path) as img:
        return cast("tuple[int, int, int]", img.convert("RGB").getpixel((0, 0)))


def _paths(base: Path) -> ResolvedPaths:
    return ResolvedPaths(
        video=base / "MIDV-123" / "MIDV-123.mp4",
        thumb=base / "MIDV-123" / "thumb.jpg",
        poster=base / "MIDV-123" / "poster.jpg",
        fanart=base / "MIDV-123" / "fanart.jpg",
        extrafanart_dir=base / "MIDV-123" / "extrafanart",
        nfo=base / "MIDV-123" / "MIDV-123.nfo",
        trailer=base / "MIDV-123" / "trailer.mp4",
    )


@pytest.mark.asyncio
async def test_internal_poster_and_trailer(resource_store: ResourceStore, tmp_path: Path):
    """poster = 内部派生 URL → 从 store 取文件; trailer 下载并复制."""
    client = cast("WebClient", FakeClient())

    # 造一个裁剪派生资源, 作为 metadata 的内部 poster URL
    async def producer(dest: Path) -> bool:
        Image.new("RGB", (379, 538), "red").save(dest)
        return True

    crop = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
    assert crop is not None
    internal_url = f"/api/resources/{ResourceStore.url_hash(crop.url)}"

    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(
        number="MIDV-123",
        thumb_urls=["https://s/t.jpg"],
        poster_urls=[internal_url],
        trailer_urls=["https://s/trailer.mp4"],
    )
    out_base = tmp_path / "out"
    paths = _paths(out_base)

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
    )
    assert result.outcome is PlaceOutcome.PLACED
    assert paths.thumb.exists()  # 外部下载
    assert paths.poster.exists()  # 内部派生解析
    assert paths.trailer.exists()  # trailer 落盘
    assert paths.trailer.read_bytes() == b"video"


@pytest.mark.asyncio
async def test_target_occupied_returns_before_side_effects(resource_store: ResourceStore, tmp_path: Path):
    """目标被占用时立即返回: 不下载图片、不写 NFO、不动磁盘."""
    client = cast("WebClient", FakeClient())

    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    paths = _paths(tmp_path / "out")
    paths.video.parent.mkdir(parents=True)
    paths.video.write_text("existing content")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"])

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.MOVE,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
    )

    assert result.outcome is PlaceOutcome.CONFLICT
    assert result.conflict_target == paths.video
    assert paths.video.read_text() == "existing content"
    assert src.read_bytes() == b"movie"
    assert not paths.thumb.exists()
    assert not paths.nfo.exists()


@pytest.mark.asyncio
async def test_unknown_target_state_returns_before_side_effects(
    resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """读不出目标状态时立即返回并记为失败: 不下载图片、不写 NFO、不动磁盘."""
    client = cast("WebClient", FakeClient())

    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    paths = _paths(tmp_path / "out")
    paths.video.parent.mkdir(parents=True)
    paths.video.write_text("existing content")

    def denied(self: Path, other: object) -> bool:
        raise PermissionError(13, "Permission denied", str(other))

    monkeypatch.setattr(Path, "samefile", denied)

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"])

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.MOVE,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    assert "Permission denied" in result.error
    assert result.conflict_target is None
    assert paths.video.read_text() == "existing content"
    assert src.read_bytes() == b"movie"
    assert not paths.thumb.exists()
    assert not paths.nfo.exists()


@pytest.mark.asyncio
async def test_copy_resources_skips_trailer(resource_store: ResourceStore, tmp_path: Path):
    """copy_resources 不含 trailer 时不落盘预告片, 仍复制封面."""
    client = cast("WebClient", FakeClient())
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"], trailer_urls=["https://s/trailer.mp4"])
    paths = _paths(tmp_path / "out")
    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
        copy_resources=[DownloadableResource.thumb, DownloadableResource.poster],
    )
    assert result.outcome is PlaceOutcome.PLACED
    assert paths.thumb.exists()
    assert not paths.trailer.exists()


@pytest.mark.asyncio
async def test_write_nfo_false_skips_nfo(resource_store: ResourceStore, tmp_path: Path):
    client = cast("WebClient", FakeClient())
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", title="T", thumb_urls=["https://s/t.jpg"])
    paths = _paths(tmp_path / "out")
    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
        write_nfo=False,
    )
    assert result.outcome is PlaceOutcome.PLACED
    assert not paths.nfo.exists()


@pytest.mark.parametrize(
    ("content_type", "expected", "desc"),
    [
        (ContentType.CENSORED, True, "有码回退裁剪"),
        (ContentType.WESTERN, False, "欧美不裁剪"),
    ],
)
@pytest.mark.asyncio
async def test_missing_internal_resource_crop_fallback_by_content_type(
    resource_store: ResourceStore, tmp_path: Path, content_type: ContentType, expected: bool, desc: str
):
    """poster 内部 URL 指向不存在资源 → 仅允许的类型回退从 thumb 裁剪."""
    client = cast("WebClient", FakeClient())
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(
        number="MIDV-123",
        content_type=content_type,
        thumb_urls=["https://s/t.jpg"],
        poster_urls=["/api/resources/deadbeefdeadbeef"],  # 不存在
    )
    paths = _paths(tmp_path / "out")
    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
    )
    assert result.outcome is PlaceOutcome.PLACED
    assert paths.poster.exists() == expected, desc


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_watermark_respects_enabled(resource_store: ResourceStore, tmp_path: Path, enabled: bool):
    client = cast("WebClient", FakeClient())
    src = tmp_path / "src" / "MIDV-123-C.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    (tmp_path / "watermarks").mkdir()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(tmp_path / "watermarks" / "subtitle.png")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"])
    paths = _paths(tmp_path / "out")
    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src, has_external_subtitle=mf.has_external_subtitle),
        web_client=client,
        config=HotSettings(watermark=WatermarkConfig(enabled=enabled, scale=0.2)),
        data_dir=tmp_path,
    )
    assert result.outcome is PlaceOutcome.PLACED
    with Image.open(paths.thumb) as img:
        pixel = img.convert("RGB").getpixel((20, 20))
    assert isinstance(pixel, tuple)
    if enabled:
        assert pixel[0] > 180 and pixel[2] < 80
    else:
        assert pixel[2] > 180 and pixel[0] < 80


@pytest.mark.asyncio
async def test_watermark_from_external_subtitle(resource_store: ResourceStore, tmp_path: Path):
    """文件名没有 -C 时, 库里的同目录字幕命中同样打出封面中字角标."""
    client = cast("WebClient", FakeClient())
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    (tmp_path / "watermarks").mkdir()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(tmp_path / "watermarks" / "subtitle.png")

    mf = MediaFile(path=str(src), library_id=1, has_external_subtitle=True)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"])
    paths = _paths(tmp_path / "out")
    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src, has_external_subtitle=mf.has_external_subtitle),
        web_client=client,
        config=HotSettings(watermark=WatermarkConfig(enabled=True, scale=0.2)),
        data_dir=tmp_path,
    )
    assert result.outcome is PlaceOutcome.PLACED
    with Image.open(paths.thumb) as img:
        pixel = img.convert("RGB").getpixel((20, 20))
    assert isinstance(pixel, tuple)
    assert pixel[0] > 180 and pixel[2] < 80


@pytest.mark.asyncio
async def test_in_place_rewrites_existing_artifacts(resource_store: ResourceStore, tmp_path: Path) -> None:
    """源已在目标路径上时仍重写库路径上的旧产物: NFO 按本次元数据, 图片与预告片按本次资源.

    fanart 与 thumb 同源, 因此与 thumb 一同重写.
    """
    client = cast("WebClient", FakeClient())

    paths = _paths(tmp_path / "out")
    paths.video.parent.mkdir(parents=True)
    paths.video.write_bytes(b"movie")
    paths.nfo.write_text("旧 NFO", encoding="utf-8")
    stale = (paths.thumb, paths.poster, paths.fanart)
    before = {}
    for image in stale:
        Image.new("RGB", (800, 538), "red").save(image)
        before[image] = _pixel(image)
    paths.trailer.write_bytes(b"old trailer")

    mf = MediaFile(path=str(paths.video), library_id=1)
    meta = Metadata(
        number="MIDV-123",
        title="新标题",
        thumb_urls=["https://s/t.jpg"],
        poster_urls=["https://s/p.jpg"],
        trailer_urls=["https://s/trailer.mp4"],
    )

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.MOVE,
        resource_store=resource_store,
        file_info=parse_file_info(paths.video),
        web_client=client,
        config=HotSettings(),
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert result.dest == paths.video
    assert "新标题" in paths.nfo.read_text(encoding="utf-8")
    for image in stale:
        assert _pixel(image) != before[image]
    assert paths.trailer.read_bytes() == b"video"


@pytest.mark.parametrize("pre_existing", [True, False])
@pytest.mark.asyncio
async def test_fanart_follows_thumb_selection(
    resource_store: ResourceStore, tmp_path: Path, pre_existing: bool
) -> None:
    """thumb 不在 copy_resources 时 fanart 既不重写也不补建."""
    client = cast("WebClient", FakeClient())

    paths = _paths(tmp_path / "out")
    paths.video.parent.mkdir(parents=True)
    paths.video.write_bytes(b"movie")
    if pre_existing:
        Image.new("RGB", (800, 538), "red").save(paths.fanart)
    before = _pixel(paths.fanart) if pre_existing else None

    mf = MediaFile(path=str(paths.video), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"], poster_urls=["https://s/p.jpg"])

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.MOVE,
        resource_store=resource_store,
        file_info=parse_file_info(paths.video),
        web_client=client,
        config=HotSettings(),
        copy_resources=[DownloadableResource.poster],
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert not paths.thumb.exists()
    assert paths.poster.exists()
    assert paths.fanart.exists() is pre_existing
    if before is not None:
        assert _pixel(paths.fanart) == before


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(1, "Operation not permitted"),
        OSError(95, "Operation not supported"),
    ],
)
def test_copy_into_library_tolerates_metadata_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    """目标挂载拒绝 utime、chmod 时, 内容照常落盘."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"image")
    dst = tmp_path / "dst.jpg"

    def reject(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr("amane.handlers.file.shutil.copystat", reject)

    _copy_into_library(src, dst)

    assert dst.read_bytes() == b"image"


def test_copy_into_library_logs_both_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内容写不进去时记录两次尝试的原文, 并原样抛出回退那次的错误."""

    def reject(error: OSError) -> Callable[..., None]:
        def _reject(*args: object, **kwargs: object) -> None:
            raise error

        return _reject

    first = PermissionError(13, "Permission denied")
    second = OSError(22, "Invalid argument")
    monkeypatch.setattr("amane.handlers.file.shutil.copy2", reject(first))
    monkeypatch.setattr("amane.handlers.file.shutil.copyfile", reject(second))
    recorded: list[tuple[str, dict[str, object]]] = []

    def record(event: str, **fields: object) -> None:
        recorded.append((event, fields))

    monkeypatch.setattr("amane.handlers.file.logger.error", record)

    with pytest.raises(OSError) as excinfo:
        _copy_into_library(tmp_path / "src.jpg", tmp_path / "dst.jpg")

    assert excinfo.value is second
    assert recorded == [
        (
            "library copy failed",
            {
                "src": str(tmp_path / "src.jpg"),
                "dst": str(tmp_path / "dst.jpg"),
                "copy2_error": str(first),
                "copyfile_error": str(second),
            },
        )
    ]


def test_copy_into_library_preserves_metadata_when_possible(tmp_path: Path) -> None:
    """挂载允许写元数据时, mtime 仍随源文件."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"image")
    stamp = 1_000_000_000
    os.utime(src, (stamp, stamp))
    dst = tmp_path / "dst.jpg"

    _copy_into_library(src, dst)

    assert int(dst.stat().st_mtime) == stamp


def test_copy_into_library_keeps_copy2_on_success_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """成功路径必须与 copy2 一致: Windows 上即 CopyFile2, 该调用不能被绕开."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"image")
    dst = tmp_path / "dst.jpg"
    calls: list[tuple[Path, Path]] = []
    real_copy2 = shutil.copy2

    def spy(s: Path, d: Path) -> Path | str:
        calls.append((s, d))
        return real_copy2(s, d)

    monkeypatch.setattr("amane.handlers.file.shutil.copy2", spy)

    _copy_into_library(src, dst)

    assert calls == [(src, dst)]
    assert dst.read_bytes() == b"image"


@pytest.mark.asyncio
async def test_images_placed_when_metadata_copy_denied(
    resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标挂载拒绝元数据时, 整理仍应完成并留下图片."""
    client = cast("WebClient", FakeClient())

    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")
    paths = _paths(tmp_path / "out")

    def reject(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("amane.handlers.file.shutil.copystat", reject)

    result = await execute_file_operations(
        media_file=MediaFile(path=str(src), library_id=1),
        metadata=Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"], poster_urls=["https://s/p.jpg"]),
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert paths.thumb.exists()
    assert paths.poster.exists()
    assert paths.fanart.read_bytes() == paths.thumb.read_bytes()


@pytest.mark.asyncio
async def test_extrafanart_names_follow_library_template(resource_store: ResourceStore, tmp_path: Path):
    """剧照文件名按库模板逐张渲染; 宽度由本轮数量决定."""
    client = cast("WebClient", FakeClient())
    root = tmp_path / "lib"
    root.mkdir()
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(
        number="MIDV-123",
        extrafanart_urls={"dmm": [f"https://s/e{i}.jpg" for i in range(1, 11)]},
    )
    lib = Library(
        name="t",
        path=str(root),
        video_template="{number}/{number}.{ext}",
        extrafanart_name_template="{number}-{index_padded}.jpg",
    )
    paths = resolve_paths(lib, meta, ext="mp4", source_path=src)

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
        library=lib,
    )

    assert result.outcome is PlaceOutcome.PLACED
    names = sorted(p.name for p in paths.extrafanart_dir.iterdir())
    assert len(names) == 10
    assert names[0] == "MIDV-123-01.jpg"
    assert names[-1] == "MIDV-123-10.jpg"


@pytest.mark.asyncio
async def test_existing_stills_in_dir_are_left_alone(resource_store: ResourceStore, tmp_path: Path):
    """换模板或张数跨位数后旧名字留在目录里: 本 PR 只写本次的写集, 不清理目录."""
    client = cast("WebClient", FakeClient())
    root = tmp_path / "lib"
    root.mkdir()
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", extrafanart_urls={"dmm": ["https://s/e1.jpg", "https://s/e2.jpg"]})
    lib = Library(name="t", path=str(root), video_template="{number}/{number}.{ext}")
    paths = resolve_paths(lib, meta, ext="mp4", source_path=src)
    paths.extrafanart_dir.mkdir(parents=True)
    stale = paths.extrafanart_dir / "99.jpg"
    stale.write_bytes(b"old")

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
        library=lib,
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert stale.exists()
    assert sorted(p.name for p in paths.extrafanart_dir.iterdir()) == ["1.jpg", "2.jpg", "99.jpg"]


@pytest.mark.asyncio
async def test_extrafanart_names_fall_back_without_library(resource_store: ResourceStore, tmp_path: Path):
    """未带库列时用默认模板, 与传入路径时的默认输出一致."""
    client = cast("WebClient", FakeClient())
    root = tmp_path / "lib"
    root.mkdir()
    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", extrafanart_urls={"dmm": ["https://s/e1.jpg", "https://s/e2.jpg"]})
    lib = Library(name="t", path=str(root), video_template="{number}/{number}.{ext}")
    paths = resolve_paths(lib, meta, ext="mp4", source_path=src)

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert sorted(p.name for p in paths.extrafanart_dir.iterdir()) == ["1.jpg", "2.jpg"]


# --- 任务 13: 任务层 explicit_fields 与生效配置传递 ---


def _organize_handler(repo: Repository, resource_store: ResourceStore) -> OrganizeHandler:
    return OrganizeHandler(repo, HotSettings(), resource_store)


@pytest.mark.asyncio(loop_scope="function")
async def test_explicit_fields_survive_json_roundtrip(repo: Repository, tmp_path: Path) -> None:
    """入队前记录的 explicit_fields 经 model_dump / model_validate 往返后保持一致."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None

    submission = OrganizeSubmission(type="organize", library_id=lib.id, write_nfo=False)
    await submission.resolve(repo)
    assert submission.explicit_fields == ["write_nfo"]

    raw = submission.model_dump(mode="json")
    payload = OrganizePayload.model_validate(raw)
    assert payload.explicit_fields == ["write_nfo"]


@pytest.mark.asyncio(loop_scope="function")
async def test_apply_library_values_not_explicit(repo: Repository, tmp_path: Path) -> None:
    """_apply_library 回填的库值不进 explicit_fields."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        write_nfo=False,
        copy_resources=[DownloadableResource.thumb],
    )
    assert lib.id is not None

    payload = OrganizePayload(library_id=lib.id)
    await payload.resolve(repo)

    assert payload.explicit_fields == []
    assert payload.write_nfo is False
    assert payload.copy_resources == [DownloadableResource.thumb]


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, []),
        ({"write_nfo": None, "copy_resources": []}, ["copy_resources"]),
        ({"write_nfo": False}, ["write_nfo"]),
        ({"write_nfo": False, "copy_resources": []}, ["copy_resources", "write_nfo"]),
    ],
)
async def test_null_payload_field_not_explicit(
    repo: Repository, tmp_path: Path, body: dict[str, Any], expected: list[str]
) -> None:
    """null 是「未指定」不进显式集; 空列表是显式值."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None

    submission = OrganizeSubmission(type="organize", library_id=lib.id, **body)
    await submission.resolve(repo)

    assert sorted(submission.explicit_fields) == sorted(expected)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, []),
        ({"write_nfo": False}, ["write_nfo"]),
        ({"write_nfo": None, "copy_resources": ["thumb"]}, ["copy_resources"]),
    ],
)
async def test_legacy_payload_without_key_uses_non_null_fields(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    body: dict[str, Any],
    expected: list[str],
) -> None:
    """升级前入队的任务没有 explicit_fields 键: 按「非 None 即显式」重建旧语义."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None

    raw: dict[str, Any] = {"library_id": lib.id, "path": "", **body}
    payload = _organize_handler(repo, resource_store).parse_payload(raw)

    assert sorted(payload.explicit_fields) == sorted(expected)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    "explicit",
    [
        ["video_template"],
        ["write_nfo"],
        ["copy_resources"],
        ["video_template", "write_nfo", "copy_resources"],
    ],
)
async def test_malformed_explicit_fields_are_dropped(repo: Repository, tmp_path: Path, explicit: list[str]) -> None:
    """explicit_fields 混入未知键或 None 值 (带外写入的畸形名单) 时被剔除, 生效值跟随全局默认."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None

    payload = OrganizePayload(library_id=lib.id)
    payload.explicit_fields = explicit
    settings = HotSettings()
    effective = await resolve_effective_config(repo, settings, lib, payload)

    assert effective.naming == settings.naming


@pytest.mark.asyncio(loop_scope="function")
async def test_manual_and_auto_same_effective_config(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """手动提交与自动整理后继都不带模板字段: explicit_fields 为空, 生效 naming 相同."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "naming", {"video_template": "{ov}/{number}.{ext}"})

    manual = OrganizeSubmission(type="organize", library_id=lib.id)
    await manual.resolve(repo)
    auto_raw = OrganizePayload(library_id=lib.id, media_file_ids=[1]).model_dump(mode="json")
    auto = _organize_handler(repo, resource_store).parse_payload(auto_raw)

    assert manual.explicit_fields == []
    assert auto.explicit_fields == []
    manual_effective = await resolve_effective_config(repo, HotSettings(), lib, manual)
    auto_effective = await resolve_effective_config(repo, HotSettings(), lib, auto)
    assert manual_effective.naming == auto_effective.naming
    assert manual_effective.naming.video_template == "{ov}/{number}.{ext}"


@pytest.mark.asyncio(loop_scope="function")
async def test_override_naming_drives_real_dest_path(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """naming 覆盖驱动真实落盘: 视频 (move) 与 NFO (write_nfo) 的磁盘路径都按覆盖模板生成."""
    lib_root = tmp_path / "lib"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    src = src_dir / "NSFS-039.mp4"
    src.write_bytes(b"video")

    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=True)
    assert lib.id is not None
    await repo.upsert_config_override(
        lib.id,
        "naming",
        {"video_template": "{number}-moved/{number}.{ext}", "nfo_template": "{video_dir}/{number}-info.nfo"},
    )
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio")
    assert meta.id is not None
    await repo.create_media_file(
        lib.id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )

    org = _organize_handler(repo, resource_store)
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None
    assert result.result.failed == 0

    dest = lib_root / "NSFS-039-moved" / "NSFS-039.mp4"
    assert dest.exists()
    assert not (lib_root / "Studio" / "NSFS-039" / "NSFS-039.mp4").exists()
    # NFO 落在覆盖后的 nfo_template 渲染路径; 默认模板同名文件不出现, 证明渲染走的是覆盖值.
    assert (lib_root / "NSFS-039-moved" / "NSFS-039-info.nfo").exists()
    assert not (lib_root / "NSFS-039-moved" / "NSFS-039.nfo").exists()
    updated = await repo.list_media_files(library_id=lib.id, limit=None)
    assert any(mf.path == str(dest) for mf in updated)


@pytest.mark.asyncio(loop_scope="function")
async def test_new_library_without_copy_resources_still_copies_trailer(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """未显式设置 copy_resources 的新库, 整理复制 trailer.

    create_library 未显式接收该字段时落库全量清单 (含 trailer); 字段未列入 template_overrides
    时生效值取全局默认, 亦为全量清单 — 两处默认一致, 缺省行为即复制 trailer.
    """
    lib_root = tmp_path / "lib"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    src = src_dir / "NSFS-039.mp4"
    src.write_bytes(b"video")

    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=False)
    assert lib.id is not None
    assert lib.template_overrides == []
    assert DownloadableResource.trailer in lib.copy_resources
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio", trailer_urls=["https://s/trailer.mp4"])
    assert meta.id is not None
    await repo.create_media_file(
        lib.id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )

    org = OrganizeHandler(
        repo, HotSettings(), resource_store, web_client=cast("WebClient", FakeClient()), safe_dirs=[tmp_path]
    )
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None
    assert result.result.failed == 0

    assert (lib_root / "Studio" / "NSFS-039" / "trailer.mp4").read_bytes() == b"video"


@pytest.mark.asyncio(loop_scope="function")
async def test_override_subtitle_template_drives_real_subtitle_path(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """naming 覆盖驱动字幕真实落盘: place_subtitles 经 resolve_subtitle_path(naming=…) 渲染."""
    lib_root = tmp_path / "lib"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    src = src_dir / "NSFS-039.mp4"
    src.write_bytes(b"video")
    (src_dir / "NSFS-039.srt").write_text("sub", encoding="utf-8")

    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=False, subtitle_extensions=["srt"])
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "naming", {"subtitle_template": "{video_dir}/subs-{raw_srt_name}.{ext}"})
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio")
    assert meta.id is not None
    await repo.create_media_file(
        lib.id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )

    org = _organize_handler(repo, resource_store)
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None
    assert result.result.failed == 0

    dest_dir = lib_root / "Studio" / "NSFS-039"
    assert (dest_dir / "NSFS-039.mp4").exists()
    assert (dest_dir / "subs-NSFS-039.srt").read_text(encoding="utf-8") == "sub"
    assert not (dest_dir / "NSFS-039.srt").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_jinja_syntax_drives_real_organize(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """naming.syntax=jinja 驱动真实整理: 视频/附属/字幕/STRM 正文/剧照文件名全部按 Jinja 渲染.

    任一入口漏接, 该入口的产物路径会出现字面量 `{{`.
    """
    lib_root = tmp_path / "lib"
    local = tmp_path / "emby"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    local.mkdir(parents=True)
    src = src_dir / "MIDV-123.mp4"
    src.write_bytes(b"video")
    (src_dir / "MIDV-123.srt").write_text("sub", encoding="utf-8")

    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=False, subtitle_extensions=["srt"])
    assert lib.id is not None
    await repo.upsert_config_override(
        lib.id,
        "naming",
        {
            "syntax": "jinja",
            "video_template": "{{ studio }}/{{ number }}/{{ number }}.{{ ext }}",
            "thumb_template": "{{ link_dir }}/thumb.jpg",
            "poster_template": "{{ link_dir }}/poster.jpg",
            "fanart_template": "{{ link_dir }}/fanart.jpg",
            "extrafanart_template": "{{ link_dir }}/extrafanart",
            "extrafanart_name_template": "{{ number }}-{{ index_padded }}.jpg",
            "nfo_template": "{{ link_dir }}/{{ video_name }}.nfo",
            "trailer_template": "{{ link_dir }}/trailer.mp4",
            "link_template": str(local / "{{ number }}" / "{{ number }}.{{ ext }}"),
            "strm_content_template": "/{{ video_relpath }}",
            "subtitle_template": "{{ video_dir }}/subs-{{ raw_srt_name }}.{{ ext }}",
        },
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        studio="Studio",
        thumb_urls=["https://s/t.jpg"],
        extrafanart_urls={"dmm": ["https://s/e1.jpg", "https://s/e2.jpg"]},
    )
    assert meta.id is not None
    media = await repo.create_media_file(
        lib.id, path=str(src), number="MIDV-123", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )
    assert media.id is not None

    org = OrganizeHandler(
        repo,
        HotSettings(),
        resource_store,
        web_client=cast("WebClient", FakeClient()),
        safe_dirs=[tmp_path],
    )
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None
    assert result.result.failed == 0

    video = lib_root / "Studio" / "MIDV-123" / "MIDV-123.mp4"
    link_dir = local / "MIDV-123"
    assert video.exists()
    assert (link_dir / "thumb.jpg").exists()
    assert (link_dir / "poster.jpg").exists()
    assert (link_dir / "MIDV-123.nfo").exists()
    assert (link_dir / "MIDV-123.strm").read_text(encoding="utf-8") == "/Studio/MIDV-123/MIDV-123.mp4\n"
    assert (lib_root / "Studio" / "MIDV-123" / "subs-MIDV-123.srt").read_text(encoding="utf-8") == "sub"
    extrafanart = sorted(p.name for p in (link_dir / "extrafanart").iterdir())
    assert extrafanart == ["MIDV-123-1.jpg", "MIDV-123-2.jpg"]
    # 六个入口全部接通: 磁盘产物路径与文件名不含字面量 {{
    for produced in lib_root.rglob("*"):
        assert "{{" not in produced.name
    for produced in local.rglob("*"):
        assert "{{" not in produced.name
    updated = await repo.get_media_file(media.id)
    assert updated is not None and updated.path == str(video)


# --- 任务 24: OCCUPIED 分派与覆写协议 ---


OVERWRITE_SOURCE = b"S" * 4096 + bytes(range(256)) * 48 + b"src-tail" + b"s" * 4096
OVERWRITE_OCCUPANT = b"O" * 4096 + bytes(range(255, -1, -1)) * 48 + b"occ-tail" + b"o" * 4096
"""等长不同内容, 且都 ≥ 128 KiB: oshash 对更小的文件返回 None, 协议会在补算后放弃覆写."""


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("overwrite_video", [False, True])
async def test_occupied_dispatch_by_overwrite_video(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, overwrite_video: bool
) -> None:
    """目标被占: overwrite_video=False 维持冲突跳过; True 走覆写协议, 目标变为源内容."""
    lib_root = tmp_path / "lib"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    src = src_dir / "NSFS-039.mp4"
    src.write_bytes(OVERWRITE_SOURCE * 8)
    dest_dir = lib_root / "Studio" / "NSFS-039"
    dest_dir.mkdir(parents=True)
    target = dest_dir / "NSFS-039.mp4"
    target.write_bytes(OVERWRITE_OCCUPANT * 8)

    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=False)
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "common", {"overwrite_video": overwrite_video})
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio")
    assert meta.id is not None
    media = await repo.create_media_file(
        lib.id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )
    assert media.id is not None
    # 目标上已索引的旧文件: 覆写接管后该行删除.
    occupant = await repo.create_media_file(
        lib.id, path=str(target), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )
    assert occupant.id is not None

    org = _organize_handler(repo, resource_store)
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None

    if overwrite_video:
        assert result.result.overwritten == 1
        assert result.result.conflicted == 0
        assert result.result.failed == 0
        assert target.read_bytes() == OVERWRITE_SOURCE * 8
        assert not src.exists()
        assert await repo.list_organize_journals() == []
        updated = await repo.get_media_file(media.id)
        assert updated is not None and updated.path == str(target)
        assert await repo.get_media_file(occupant.id) is None
    else:
        assert result.result.overwritten == 0
        assert result.result.conflicted == 1
        assert target.read_bytes() == OVERWRITE_OCCUPANT * 8
        assert src.read_bytes() == OVERWRITE_SOURCE * 8
        updated = await repo.get_media_file(media.id)
        assert updated is not None and updated.path == str(src)


@pytest.mark.asyncio(loop_scope="function")
async def test_overwrite_conflict_when_journal_in_flight(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """同一目标已有进行中的覆写 (journal 核验不过被保留) → 恢复无法收敛: 引用的源行受保护
    跳过本次整理, journal 与磁盘保持原状, 等人工处理 (联合审核 P1 修复后的契约)."""
    lib_root = tmp_path / "lib"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    src = src_dir / "NSFS-039.mp4"
    src.write_bytes(OVERWRITE_SOURCE * 8)
    dest_dir = lib_root / "Studio" / "NSFS-039"
    dest_dir.mkdir(parents=True)
    target = dest_dir / "NSFS-039.mp4"
    target.write_bytes(OVERWRITE_OCCUPANT * 8)
    backup = target.parent / f"{target.name}.amane-overwrite-1"
    backup.write_bytes(OVERWRITE_OCCUPANT * 8)

    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=False)
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "common", {"overwrite_video": True})
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio")
    assert meta.id is not None
    media = await repo.create_media_file(
        lib.id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )
    assert media.id is not None
    # 进行中的覆写: journal 指纹与磁盘事实不符, 就地恢复核验不过, 行保留 (转人工).
    await repo.create_organize_journal(
        task_id=None,
        mode=MoveMode.MOVE.value,
        source_path=str(src),
        source_file_id=media.id,
        source_oshash="deadbeefdeadbeef",
        source_size=src.stat().st_size,
        source_metadata_id=None,
        source_number=None,
        target_path=str(target),
        target_file_id=None,
        backup_path=str(backup),
    )

    org = _organize_handler(repo, resource_store)
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None
    # prune 前的恢复核验不过 → journal 保留; 引用的源行受保护跳过, 不再发起覆写.
    assert result.result.conflicted == 0
    assert result.result.skipped >= 1
    assert result.result.overwritten == 0
    assert target.read_bytes() == OVERWRITE_OCCUPANT * 8
    rows = await repo.list_organize_journals()
    assert len(rows) == 1 and rows[0].last_error is not None
    assert target.read_bytes() == OVERWRITE_OCCUPANT * 8
    assert src.read_bytes() == OVERWRITE_SOURCE * 8
    rows = await repo.list_organize_journals()
    assert len(rows) == 1 and rows[0].last_error is not None


@pytest.mark.parametrize("overwrite_image", [False, True])
@pytest.mark.asyncio
async def test_overwrite_image_toggle_skips_existing_images(
    resource_store: ResourceStore, tmp_path: Path, overwrite_image: bool
) -> None:
    """overwrite_image=False: 目标已有图片跳过并计 skipped; True (默认) 维持覆盖写."""
    client = cast("WebClient", FakeClient())

    src = tmp_path / "src" / "MIDV-123.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")
    paths = _paths(tmp_path / "out")
    paths.thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 538), "red").save(paths.thumb)
    Image.new("RGB", (800, 538), "red").save(paths.poster)
    before = (_pixel(paths.thumb), _pixel(paths.poster))

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"], poster_urls=["https://s/p.jpg"])

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=client,
        config=HotSettings(),
        overwrite_image=overwrite_image,
    )

    assert result.outcome is PlaceOutcome.PLACED
    if overwrite_image:
        assert result.skipped_images == 0
        # 下载图是纯蓝: 覆盖后红通道让位给蓝通道.
        assert _pixel(paths.thumb)[0] < 50 and _pixel(paths.thumb)[2] > 200
        assert _pixel(paths.poster)[0] < 50 and _pixel(paths.poster)[2] > 200
    else:
        # 封面 (thumb+fanart 同源一对) 与海报各计一次跳过, 旧内容原样保留.
        assert result.skipped_images == 2
        assert _pixel(paths.thumb) == before[0]
        assert _pixel(paths.poster) == before[1]


@pytest.mark.parametrize("overwrite_image", [False, True])
@pytest.mark.asyncio
async def test_watermark_leaves_skipped_images_untouched(
    resource_store: ResourceStore, tmp_path: Path, overwrite_image: bool
) -> None:
    """水印只作用于本轮写出的图: overwrite_image=False 时旧图不被重编码叠印, True 照常覆写叠印."""
    client = cast("WebClient", FakeClient())
    src = tmp_path / "src" / "MIDV-123-C.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"movie")

    (tmp_path / "watermarks").mkdir()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(tmp_path / "watermarks" / "subtitle.png")

    paths = _paths(tmp_path / "out")
    paths.thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 538), "green").save(paths.thumb)
    Image.new("RGB", (800, 538), "green").save(paths.poster)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, _pixel(p)) for p in (paths.thumb, paths.poster)}

    mf = MediaFile(path=str(src), library_id=1)
    meta = Metadata(number="MIDV-123", thumb_urls=["https://s/t.jpg"], poster_urls=["https://s/p.jpg"])

    result = await execute_file_operations(
        media_file=mf,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src, has_external_subtitle=mf.has_external_subtitle),
        web_client=client,
        config=HotSettings(watermark=WatermarkConfig(enabled=True, scale=0.2)),
        data_dir=tmp_path,
        overwrite_image=overwrite_image,
    )

    assert result.outcome is PlaceOutcome.PLACED
    if overwrite_image:
        # 覆盖写: 旧图换成本轮下载图并叠印, 与无旧图时的基线一致.
        for image in (paths.thumb, paths.poster):
            with Image.open(image) as img:
                pixel = img.convert("RGB").getpixel((20, 20))
            assert isinstance(pixel, tuple)
            assert pixel[0] > 180 and pixel[2] < 80
    else:
        # 跳过: 旧图字节与 mtime 原样保留, 不重编码也不再次叠印.
        for image in (paths.thumb, paths.poster):
            content, mtime, pixel = before[image]
            assert image.read_bytes() == content
            assert image.stat().st_mtime_ns == mtime
            assert _pixel(image) == pixel


# --- MDC P3 任务 5: 水印作用目标 (targets) 与库级生效配置传递 ---

type WatermarkTarget = Literal["thumb", "poster", "fanart"]


def _targets(*members: WatermarkTarget) -> frozenset[WatermarkTarget]:
    return frozenset(members)


class _GarbageImageClient(FakeClient):
    """指定 URL 写非图片字节: 诱导单张剧照叠印失败."""

    def __init__(self, garbage_urls: set[str]) -> None:
        self._garbage_urls = garbage_urls

    async def download(self, url: str, dest: Path, **kwargs) -> bool:
        if url in self._garbage_urls:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"not-an-image")
            return True
        return await super().download(url, dest, **kwargs)


def _assert_stamped(path: Path, stamped: bool) -> None:
    """判定位 (20, 20) 落在 scale=0.2 的左上章面内: 红章为真, 蓝底为假."""
    with Image.open(path) as img:
        pixel = img.convert("RGB").getpixel((20, 20))
    assert isinstance(pixel, tuple)
    assert (pixel[0] > 180 and pixel[2] < 80) is stamped


def _stamp_paths(tmp_path: Path, name: str = "lib") -> ResolvedPaths:
    """带 context 的库路径 (extrafanart 文件名渲染需要); 产物落 {tmp_path}/{name}/MIDV-123/."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    lib = Library(name="t", path=str(root), video_template="{number}/{number}.{ext}")
    return resolve_paths(lib, Metadata(number="MIDV-123"), ext="mp4", source_path=tmp_path / "src" / "MIDV-123-C.mp4")


async def _stamp_organize(
    tmp_path: Path,
    resource_store: ResourceStore,
    watermark_cfg: WatermarkConfig | None,
    *,
    extrafanart_urls: Sequence[str] = (),
    paths: ResolvedPaths | None = None,
    client: FakeClient | None = None,
    config: HotSettings | None = None,
    overwrite_image: bool = True,
) -> tuple[FileOperationsResult, ResolvedPaths]:
    """水印分派测试的公共整理路径: -C 源名 (中字相位) + data_dir 章图; watermark_cfg 直传入.

    有码条目: poster 无候选时回退从 thumb 裁剪, 保证三个目标都有本轮产物.
    """
    src = tmp_path / "src" / "MIDV-123-C.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"movie")
    (tmp_path / "watermarks").mkdir(exist_ok=True)
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(tmp_path / "watermarks" / "subtitle.png")

    if paths is None:
        paths = _stamp_paths(tmp_path)
    meta = Metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        extrafanart_urls={"dmm": list(extrafanart_urls)},
    )
    result = await execute_file_operations(
        media_file=MediaFile(path=str(src), library_id=1),
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.COPY,
        resource_store=resource_store,
        file_info=parse_file_info(src),
        web_client=cast("WebClient", client if client is not None else FakeClient()),
        config=config if config is not None else HotSettings(),
        data_dir=tmp_path,
        watermark_cfg=watermark_cfg,
        overwrite_image=overwrite_image,
    )
    return result, paths


@pytest.mark.asyncio
async def test_targets_default_thumb_poster(resource_store: ResourceStore, tmp_path: Path) -> None:
    """默认 targets = {thumb, poster}: 封面与海报叠印, 剧照不叠 (兼容现状)."""
    result, paths = await _stamp_organize(
        tmp_path,
        resource_store,
        WatermarkConfig(enabled=True, scale=0.2),
        extrafanart_urls=["https://s/e1.jpg", "https://s/e2.jpg"],
    )
    assert result.outcome is PlaceOutcome.PLACED
    _assert_stamped(paths.thumb, True)
    _assert_stamped(paths.poster, True)
    for name in ("1.jpg", "2.jpg"):
        _assert_stamped(paths.extrafanart_dir / name, False)


@pytest.mark.asyncio
async def test_targets_exclude_thumb(resource_store: ResourceStore, tmp_path: Path) -> None:
    """targets 不含 thumb: thumb 原样, poster 照叠."""
    result, paths = await _stamp_organize(
        tmp_path, resource_store, WatermarkConfig(enabled=True, scale=0.2, targets=_targets("poster"))
    )
    assert result.outcome is PlaceOutcome.PLACED
    _assert_stamped(paths.thumb, False)
    _assert_stamped(paths.poster, True)


@pytest.mark.asyncio
async def test_targets_fanart_stamps_all(resource_store: ResourceStore, tmp_path: Path) -> None:
    """targets 含 fanart: 剧照每张叠印, thumb / poster 照常."""
    result, paths = await _stamp_organize(
        tmp_path,
        resource_store,
        WatermarkConfig(enabled=True, scale=0.2, targets=_targets("thumb", "poster", "fanart")),
        extrafanart_urls=["https://s/e1.jpg", "https://s/e2.jpg"],
    )
    assert result.outcome is PlaceOutcome.PLACED
    _assert_stamped(paths.thumb, True)
    _assert_stamped(paths.poster, True)
    for name in ("1.jpg", "2.jpg"):
        _assert_stamped(paths.extrafanart_dir / name, True)


@pytest.mark.asyncio
async def test_fanart_overwrite_false_skips(resource_store: ResourceStore, tmp_path: Path) -> None:
    """overwrite_image=False: 已有剧照跳过不叠 (字节原样), 本轮新增剧照照叠."""
    paths = _stamp_paths(tmp_path)
    paths.extrafanart_dir.mkdir(parents=True)
    Image.new("RGB", (800, 538), "green").save(paths.extrafanart_dir / "1.jpg")
    existing = paths.extrafanart_dir / "1.jpg"
    before = existing.read_bytes()

    result, paths = await _stamp_organize(
        tmp_path,
        resource_store,
        WatermarkConfig(enabled=True, scale=0.2, targets=_targets("fanart")),
        extrafanart_urls=["https://s/e1.jpg", "https://s/e2.jpg"],
        paths=paths,
        overwrite_image=False,
    )
    assert result.outcome is PlaceOutcome.PLACED
    assert result.skipped_images == 1  # 仅已有 1.jpg; thumb / poster 本轮写出不计跳过
    assert existing.read_bytes() == before
    _assert_stamped(existing, False)
    _assert_stamped(paths.extrafanart_dir / "2.jpg", True)


@pytest.mark.asyncio
async def test_fanart_stamp_failure_isolated(resource_store: ResourceStore, tmp_path: Path) -> None:
    """单张剧照叠印失败记 warning, 不中断其余张."""
    with capture_logs() as logs:
        result, paths = await _stamp_organize(
            tmp_path,
            resource_store,
            WatermarkConfig(enabled=True, scale=0.2, targets=_targets("fanart")),
            extrafanart_urls=["https://s/e1.jpg", "https://s/e2.jpg"],
            client=_GarbageImageClient({"https://s/e1.jpg"}),
        )
    assert result.outcome is PlaceOutcome.PLACED
    # 坏图已复制落盘但未被改写 (叠印失败保留原样), 其余张照叠.
    assert (paths.extrafanart_dir / "1.jpg").read_bytes() == b"not-an-image"
    _assert_stamped(paths.extrafanart_dir / "2.jpg", True)
    failed = [log for log in logs if log["event"] == "cover watermark failed"]
    assert any(str(log.get("path", "")).endswith("1.jpg") for log in failed)


@pytest.mark.asyncio
async def test_watermark_config_dispatch_changes_product(resource_store: ResourceStore, tmp_path: Path) -> None:
    """传入的 WatermarkConfig 是唯一取值来源: 直传值胜过全局层 (分派不经覆盖层)."""
    _, paths_on = await _stamp_organize(
        tmp_path, resource_store, WatermarkConfig(enabled=True, scale=0.2), paths=_stamp_paths(tmp_path, "lib-on")
    )
    _assert_stamped(paths_on.thumb, True)  # 全局水印关, 直传开 → 叠

    _, paths_off = await _stamp_organize(
        tmp_path,
        resource_store,
        WatermarkConfig(enabled=False),
        paths=_stamp_paths(tmp_path, "lib-off"),
        config=HotSettings(watermark=WatermarkConfig(enabled=True, scale=0.2)),
    )
    _assert_stamped(paths_off.thumb, False)  # 全局水印开, 直传关 → 不叠


# --- 任务 27: MediaFile.link_path 持久化 ---


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("with_link_template", [True, False])
async def test_link_path_persisted_after_organize(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, with_link_template: bool
) -> None:
    """配置链接模板整理后 MediaFile.link_path == 链接落盘路径; 未配置链接模板 → None."""
    lib_root = tmp_path / "lib"
    src_dir = lib_root / "incoming"
    src_dir.mkdir(parents=True)
    src = src_dir / "NSFS-039.mp4"
    src.write_bytes(b"video")
    local = tmp_path / "emby"
    local.mkdir(parents=True)

    overrides: dict[str, Any] = {"video_template": "{number}/{number}.{ext}"}
    # link_mode 默认 STRM: 链接落盘是 .strm 正文文件, 路径由链接模板渲染.
    link = local / "NSFS-039" / "NSFS-039.strm"
    if with_link_template:
        overrides["link_template"] = str(local / "{number}" / "{number}.{ext}")
    lib = await repo.create_library(name="t", path=str(lib_root), write_nfo=False)
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "naming", overrides)
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio")
    assert meta.id is not None
    media = await repo.create_media_file(
        lib.id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )
    assert media.id is not None

    org = OrganizeHandler(repo, HotSettings(), resource_store, safe_dirs=[tmp_path])
    result = await org.handle(OrganizePayload(library_id=lib.id, path=str(src_dir)))
    assert result.success is True
    assert result.result is not None
    assert result.result.failed == 0

    updated = await repo.get_media_file(media.id)
    assert updated is not None
    if with_link_template:
        assert updated.link_path == str(link)
        assert link.is_file()  # strm 正文文件落盘
    else:
        assert updated.link_path is None


def test_media_file_response_has_link_path() -> None:
    """MediaFileResponse 是手写字段清单, link_path 必须显式存在并按字段名映射."""
    media = MediaFile(id=7, path="/lib/NSFS-039.mp4", library_id=1, link_path="/emby/NSFS-039.mp4")
    response = to_media_file_response(media)
    assert response.link_path == "/emby/NSFS-039.mp4"
    assert MediaFileResponse.model_fields["link_path"].annotation == str | None


# --- 任务 8: 整理期海报重选 (异步协调边界 + none 参与 + 用户产物优先) ---


class MappedClient:
    """按 URL 映射图内容的下载桩: 重选用例借此区分候选 / thumb / 用户产物的尺寸与颜色.

    未映射的 URL 一律 800x538 纯蓝, 与 FakeClient 的占位图一致.
    """

    def __init__(self, images: dict[str, tuple[tuple[int, int], str]]) -> None:
        self._images = images

    async def download(self, url: str, dest: Path, **kwargs) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        size, color = self._images.get(url, ((800, 538), "blue"))
        Image.new("RGB", size, color).save(dest)
        return True

    async def get_filesize(self, url: str, **kwargs):
        return None

    async def resolve_final_url(self, url: str, **kwargs) -> str:
        return url


def _pixel_close(path: Path, expected: tuple[int, int, int], *, tol: int = 3) -> bool:
    """JPEG 压缩有 ±1~2 的颜色漂移, 像素断言按容差比较."""
    pixel = _pixel(path)
    return all(abs(a - b) <= tol for a, b in zip(pixel, expected, strict=True))


def _write_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"movie")


async def _store_derived(
    resource_store: ResourceStore, src_url: str, args: str, *, size: tuple[int, int], color: str
) -> str:
    """落一个裁剪派生资源, 返回其内部 URL (供 metadata.poster_urls 引用)."""

    async def producer(dest: Path) -> bool:
        Image.new("RGB", size, color).save(dest)
        return True

    derived = await resource_store.acquire_derived(src_url, "crop", args, producer)
    assert derived is not None
    return f"/api/resources/{ResourceStore.url_hash(derived.url)}"


async def _seed_library(
    repo: Repository,
    tmp_path: Path,
    *,
    name: str,
    metadata_id: int,
    number: str,
    metadata_override: dict[str, Any] | None = None,
) -> tuple[Library, Path]:
    """建库 (固定视频模板, 产物与视频同目录) + 库内源文件 + 挂 metadata 的 MediaFile."""
    root = tmp_path / f"lib_{name}"
    src = root / "incoming" / f"{number}.mp4"
    _write_video(src)
    lib = await repo.create_library(name=name, path=str(root))
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "naming", {"video_template": "{number}/{number}.{ext}"})
    if metadata_override is not None:
        await repo.upsert_config_override(lib.id, "metadata", metadata_override)
    await repo.create_media_file(
        lib.id, path=str(src), number=number, status=MediaFileStatus.SCRAPED, metadata_id=metadata_id
    )
    return lib, root


@pytest.mark.asyncio(loop_scope="function")
async def test_reselect_none_uses_uncropped_candidate(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """none 与 right/face 同样进入重选 (决策 12): 库 A none 产物 = 未裁剪原始候选; 库 B face+emby 产物 = 按各自配置派生."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/t.jpg": ((800, 538), "blue"),
                "https://s/orig.jpg": ((600, 300), "green"),
            }
        ),
    )
    old_crop = await _store_derived(
        resource_store, "https://s/t.jpg", "right:legacy:0.7000", size=(376, 538), color="red"
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        poster_urls=[old_crop],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    lib_a, root_a = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"crop_type": {"censored": "none"}},
    )
    lib_b, root_b = await _seed_library(
        repo,
        tmp_path,
        name="b",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"crop_type": {"censored": "face"}, "crop_aspect": "emby"},
    )
    assert lib_a.id is not None and lib_b.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result_a = await handler.handle(OrganizePayload(library_id=lib_a.id, path=str(root_a)))
    result_b = await handler.handle(OrganizePayload(library_id=lib_b.id, path=str(root_b)))
    assert result_a.success and result_b.success
    assert result_a.result is not None and result_a.result.failed == 0
    assert result_b.result is not None and result_b.result.failed == 0

    poster_a = root_a / "MIDV-123" / "poster.jpg"
    poster_b = root_b / "MIDV-123" / "poster.jpg"
    # A: none 也是重选结果, 产物 = 未裁剪原始候选 (600x300 绿), 不是旧自动裁剪产物 (376x538 红).
    with Image.open(poster_a) as img:
        assert img.size == (600, 300)
    assert _pixel_close(poster_a, (0, 128, 0))
    # B: face 在纯色 thumb 上无人脸 → 降级 right, 按 emby 比例从 thumb 裁剪 → 358x538 蓝.
    with Image.open(poster_b) as img:
        assert img.size == (358, 538)
    assert _pixel_close(poster_b, (0, 0, 255))


@pytest.mark.asyncio(loop_scope="function")
async def test_user_manual_product_survives_reorganize(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """手动裁剪 (box:) 产物优先 (决策 18 路径 a): 两库不同策略重新整理, 手动产物字节原样保留."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/t.jpg": ((800, 538), "blue"),
                "https://s/orig.jpg": ((600, 300), "green"),
            }
        ),
    )
    manual = await _store_derived(resource_store, "https://s/t.jpg", "box:100,0,476,538", size=(376, 538), color="red")
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        poster_urls=[manual],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    manual_hash = internal_url_hash(manual)
    found = await resource_store.get_by_url_hash(manual_hash) if manual_hash is not None else None
    assert found is not None
    manual_bytes = found[1].read_bytes()

    lib_a, root_a = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"crop_type": {"censored": "none"}},
    )
    lib_b, root_b = await _seed_library(
        repo,
        tmp_path,
        name="b",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"crop_type": {"censored": "right"}, "crop_aspect": "emby"},
    )
    assert lib_a.id is not None and lib_b.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result_a = await handler.handle(OrganizePayload(library_id=lib_a.id, path=str(root_a)))
    result_b = await handler.handle(OrganizePayload(library_id=lib_b.id, path=str(root_b)))
    assert result_a.success and result_b.success
    assert result_a.result is not None and result_a.result.failed == 0
    assert result_b.result is not None and result_b.result.failed == 0

    # 两库产物都是手动裁剪字节: 不是候选原图 (绿), 也不是按库策略的裁剪 (蓝).
    assert (root_a / "MIDV-123" / "poster.jpg").read_bytes() == manual_bytes
    assert (root_b / "MIDV-123" / "poster.jpg").read_bytes() == manual_bytes


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    "library_override",
    [None, {"crop_type": {"censored": "right"}, "crop_aspect": "emby"}],
    ids=["default", "library_strategy"],
)
async def test_patched_external_poster_survives(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, library_override: dict[str, Any] | None
) -> None:
    """PATCH 外部海报 (MANUAL 写入自动锁定, 决策 18 路径 b) 优先于 raw 来源重选: 整理产物 = 外部图 B."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/t.jpg": ((800, 538), "blue"),
                "https://s/a.jpg": ((600, 300), "green"),
                "https://b.com/b.jpg": ((300, 400), "purple"),
            }
        ),
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        raw={"dmm": {"poster_urls": ["https://s/a.jpg"]}},
    )
    assert meta.id is not None
    patched = await repo.update_metadata(meta.id, mode=WriteMode.MANUAL, poster_urls=["https://b.com/b.jpg"])
    assert patched is not None
    assert "poster_urls" in patched.locked_fields

    lib, root = await _seed_library(
        repo, tmp_path, name="a", metadata_id=meta.id, number="MIDV-123", metadata_override=library_override
    )
    assert lib.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    # 产物 = PATCH 的外部图 B (300x400 紫), 不是 raw 来源 A (绿), 也不是按库策略的裁剪.
    poster = root / "MIDV-123" / "poster.jpg"
    with Image.open(poster) as img:
        assert img.size == (300, 400)
    assert _pixel_close(poster, (128, 0, 128))


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("crop_enabled", [True, False], ids=["reselect_path", "copy_path"])
async def test_overwrite_image_false_still_skips(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, crop_enabled: bool
) -> None:
    """overwrite_image=False 的既有跳过语义在重选与复制两条路径都适用: 目标已有 poster 跳过并计 skipped."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/t.jpg": ((800, 538), "blue"),
                "https://s/orig.jpg": ((600, 300), "green"),
            }
        ),
    )
    old_crop = await _store_derived(
        resource_store, "https://s/t.jpg", "right:legacy:0.7000", size=(376, 538), color="red"
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        poster_urls=[old_crop],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    lib, root = await _seed_library(repo, tmp_path, name="a", metadata_id=meta.id, number="MIDV-123")
    assert lib.id is not None
    await repo.upsert_config_override(lib.id, "common", {"overwrite_image": False})
    poster = root / "MIDV-123" / "poster.jpg"
    poster.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (300, 400), (1, 2, 3)).save(poster)

    config = HotSettings(scraping=ScrapingConfig(crop_poster=crop_enabled))
    handler = OrganizeHandler(repo, config, resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0
    # 重选路径 (开关 True) 与复制路径 (开关 False) 下 poster 各计一次跳过.
    assert result.result.skipped == 1
    # 旧 poster 内容原样保留, 本轮产物不落盘.
    assert _pixel_close(poster, (1, 2, 3))


@pytest.mark.asyncio(loop_scope="function")
async def test_switch_off_falls_back_to_materialized_copy(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """总开关 False 不重选 (决策 12): 沿用物化图复制路径 (旧自动裁剪产物原样), 记回退日志."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/t.jpg": ((800, 538), "blue"),
                "https://s/orig.jpg": ((600, 300), "green"),
            }
        ),
    )
    old_crop = await _store_derived(
        resource_store, "https://s/t.jpg", "right:legacy:0.7000", size=(376, 538), color="red"
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        poster_urls=[old_crop],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    # 库级策略配 none: 若重选未被开关拦下, 产物会变成候选原图 (绿).
    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"crop_type": {"censored": "none"}},
    )
    assert lib.id is not None

    handler = OrganizeHandler(
        repo, HotSettings(scraping=ScrapingConfig(crop_poster=False)), resource_store, web_client=client
    )
    with capture_logs() as logs:
        result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    # 现状物化图复制: 旧自动裁剪产物 (376x538 红) 原样复制, 候选原图 (绿) 不参与.
    poster = root / "MIDV-123" / "poster.jpg"
    with Image.open(poster) as img:
        assert img.size == (376, 538)
    assert _pixel_close(poster, (255, 0, 0))
    assert any(e["event"] == "poster reselect skipped" and e.get("reason") == "crop_disabled" for e in logs)


@pytest.mark.asyncio(loop_scope="function")
async def test_empty_rebuild_falls_back(repo: Repository, resource_store: ResourceStore, tmp_path: Path) -> None:
    """候选重建为空 (raw 无候选) 回退物化图复制路径 (规格 §3.6): 旧产物原样复制并记回退日志."""
    client = cast("WebClient", MappedClient({"https://s/t.jpg": ((800, 538), "blue")}))
    old_crop = await _store_derived(
        resource_store, "https://s/t.jpg", "right:legacy:0.7000", size=(376, 538), color="red"
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        poster_urls=[old_crop],
        raw={"dmm": {"title": "raw snapshot without poster_urls"}},
    )
    assert meta.id is not None
    lib, root = await _seed_library(repo, tmp_path, name="a", metadata_id=meta.id, number="MIDV-123")
    assert lib.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    with capture_logs() as logs:
        result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    # 回退现状: 旧自动裁剪产物 (376x538 红) 原样复制.
    poster = root / "MIDV-123" / "poster.jpg"
    with Image.open(poster) as img:
        assert img.size == (376, 538)
    assert _pixel_close(poster, (255, 0, 0))
    assert any(e["event"] == "poster reselect fallback" and e.get("reason") == "no_candidates" for e in logs)


@pytest.mark.asyncio(loop_scope="function")
async def test_locked_empty_poster_urls_falls_back(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """锁定空列表是病态数据 (R-empty-locked): 视为无可用用户产物, 落入回退分支并记回退日志."""
    client = cast("WebClient", MappedClient({"https://s/t.jpg": ((800, 538), "blue")}))
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    patched = await repo.update_metadata(meta.id, mode=WriteMode.MANUAL, poster_urls=[])
    assert patched is not None
    assert "poster_urls" in patched.locked_fields
    lib, root = await _seed_library(repo, tmp_path, name="a", metadata_id=meta.id, number="MIDV-123")
    assert lib.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    with capture_logs() as logs:
        result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    # 无可用用户产物 → 回退现状: poster 从已落盘 thumb 裁剪兜底 (有码 → right, 376x538 蓝).
    poster = root / "MIDV-123" / "poster.jpg"
    with Image.open(poster) as img:
        assert img.size == (376, 538)
    assert _pixel_close(poster, (0, 0, 255))
    assert any(e["event"] == "poster reselect fallback" and e.get("reason") == "no_candidates" for e in logs)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("crop_enabled", [True, False], ids=["reselect_path", "copy_path"])
async def test_user_product_survives_crop_disabled(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, crop_enabled: bool
) -> None:
    """用户产物保护与裁剪开关无关: 开关关闭 + quality 策略下, 产物仍是锁定的首项.

    开关只管「是否像素裁剪」, 不管「用哪张图」. 开关关闭时若先返回, 保护整段被跳过,
    回退复制路径按 quality 择优会另选一张更大的 —— 锁定的首项 (小图) 被顶掉.
    """
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/t.jpg": ((800, 538), "blue"),
                "https://s/small.jpg": ((300, 400), "purple"),
                "https://s/big.jpg": ((900, 1200), "green"),
            }
        ),
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t.jpg"],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    # 锁定 poster_urls: 首项是用户指定的小图, 后随一张更大的图.
    patched = await repo.update_metadata(
        meta.id, mode=WriteMode.MANUAL, poster_urls=["https://s/small.jpg", "https://s/big.jpg"]
    )
    assert patched is not None
    assert "poster_urls" in patched.locked_fields

    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"thumb_download_strategy": "quality"},
    )
    assert lib.id is not None

    config = HotSettings(scraping=ScrapingConfig(crop_poster=crop_enabled))
    handler = OrganizeHandler(repo, config, resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    # 产物 = 锁定的首项 (300x400 紫), 不是质量更大但用户没选的备用图 (900x1200 绿).
    poster = root / "MIDV-123" / "poster.jpg"
    with Image.open(poster) as img:
        assert img.size == (300, 400)
    assert _pixel_close(poster, (128, 0, 128))


# --- 最终审查修复: 兜底裁剪 face 经检测固化执行策略, 与刮削入口同语义 (规格 §4) ---


_FACE_FIXTURE = Path(__file__).parent.parent / "media" / "fixtures" / "yunet_face.jpg"
_FACE_THUMB_URL = "https://s/face.jpg"
_FACE_POSTER_URL = "https://s/face-poster.jpg"
_DEGRADE_EVENT = "face crop degraded to right"


def _jpeg_bytes(size: tuple[int, int], color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class _ImageBytesClient:
    """按 URL 写出预设图片字节: 兜底裁剪用例借此投喂真实人脸 fixture."""

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


def _oracle_face_window(thumb: Path, ratio: float) -> tuple[int, int, int, int]:
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


def _no_face_thumb(path: Path) -> None:
    """无人脸样例: 平滑渐变不含可检人脸 (与 tests/media/test_crop.py 的无人脸样本同口径)."""
    gradient = Image.new("RGB", (800, 538))
    draw = ImageDraw.Draw(gradient)
    for x in range(800):
        draw.line((x, 0, x, 538), fill=(x % 256, (x * 2) % 256, 128))
    gradient.save(path, format="JPEG", quality=95)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("thumb_case", "cv2_available"),
    [("face", True), ("plain", True), ("face", False)],
    ids=["face_window", "no_face_right", "backend_unavailable_right"],
)
async def test_fallback_crop_respects_face_strategy(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thumb_case: str,
    cv2_available: bool,
) -> None:
    """重选回退 + face 配置的兜底裁剪与刮削入口同语义: 检出按人脸窗对齐, 产物与刮削物化字节一致;
    无人脸是预期回退右裁无告警, 后端不可用降级右裁并告警 — 不再静默右裁."""
    if not cv2_available:
        monkeypatch.setattr(optional_cv2, "cv2", None)
    if thumb_case == "plain":
        plain = tmp_path / "no_face_thumb.jpg"
        _no_face_thumb(plain)
        thumb_bytes = plain.read_bytes()
    else:
        thumb_bytes = _FACE_FIXTURE.read_bytes()
    client = cast(
        "WebClient",
        _ImageBytesClient({_FACE_THUMB_URL: thumb_bytes, _FACE_POSTER_URL: _jpeg_bytes((300, 400), "purple")}),
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=[_FACE_THUMB_URL],
        raw={"dmm": {"poster_urls": ["https://s/orig.jpg"]}},
    )
    assert meta.id is not None
    # 锁定空列表是病态数据 (R-empty-locked): 视为无可用用户产物, 重选回退, poster 落入从已落盘
    # thumb 裁剪的兜底路径.
    patched = await repo.update_metadata(meta.id, mode=WriteMode.MANUAL, poster_urls=[])
    assert patched is not None
    assert "poster_urls" in patched.locked_fields
    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"crop_type": {"censored": "face"}},
    )
    assert lib.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    with capture_logs() as logs:
        result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    poster = root / "MIDV-123" / "poster.jpg"
    assert poster.exists()
    reference = tmp_path / "reference_thumb.jpg"
    reference.write_bytes(thumb_bytes)
    degrade_warnings = [e for e in logs if e["event"] == _DEGRADE_EVENT]
    if thumb_case == "face" and cv2_available:
        window = _oracle_face_window(reference, 0.7)
        with Image.open(reference) as img:
            width, height = img.size
        # fixture 人脸中心偏左: 人脸窗与右裁窗不同, 字节断言才能区分两种执行.
        assert window != (width - int(height * 0.7), 0, width, height)
        expected = io.BytesIO()
        Image.open(reference).convert("RGB").crop(window).save(expected, format="JPEG", quality=95)
        assert poster.read_bytes() == expected.getvalue()
        # 与刮削入口同配置同语义: 刮削物化的 face 派生产物与整理兜底产物字节一致.
        scrape_cfg = HotSettings(metadata=MetadataConfig(crop_type={ContentType.CENSORED: CropStrategy.FACE}))
        materialized = await materialize_images(
            [_FACE_POSTER_URL],
            [_FACE_THUMB_URL],
            [],
            resource_store,
            client,
            scrape_cfg,
            tmp_path / "scrape",
            content_type=ContentType.CENSORED,
            metadata_cfg=scrape_cfg.metadata,
        )
        product_hash = internal_url_hash(materialized.poster_urls[0])
        assert product_hash is not None
        found = await resource_store.get_by_url_hash(product_hash)
        assert found is not None
        assert poster.read_bytes() == found[1].read_bytes()
        assert degrade_warnings == []
    else:
        # 无人脸 (预期回退) 与后端不可用 (降级) 都执行右裁; 后者按契约告警, 前者无告警.
        reference_right = tmp_path / "reference_right.jpg"
        assert crop_poster(reference, reference_right, poster_ratio=0.7, jpeg_quality=95) is True
        assert poster.read_bytes() == reference_right.read_bytes()
        assert len(degrade_warnings) == (0 if cv2_available else 1)


@pytest.mark.asyncio(loop_scope="function")
async def test_quality_dispatch_respects_library_scope(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """两入口按各自生效 thumb_download_strategy 分派: 刮削入口取全局 quality, 整理入口库 A 覆盖 priority、库 B 继承全局."""
    images = {
        "https://s/t-low.jpg": ((600, 600), "blue"),
        "https://s/t-high.jpg": ((800, 538), "green"),
        "https://s/p-low.jpg": ((500, 440), "purple"),
        "https://s/p-high.jpg": ((500, 500), "orange"),
    }
    client = cast("WebClient", MappedClient(images))
    thumb_urls = ["https://s/t-low.jpg", "https://s/t-high.jpg"]
    poster_urls = ["https://s/p-low.jpg", "https://s/p-high.jpg"]
    global_cfg = HotSettings(metadata=MetadataConfig(thumb_download_strategy="quality"))

    # 刮削入口 (全局配置): quality 生效 — poster 胜者免裁, 外部 URL 原样, 落选者不提交.
    out = await materialize_images(
        poster_urls,
        thumb_urls,
        [],
        resource_store,
        client,
        global_cfg,
        tmp_path,
        content_type=ContentType.CENSORED,
        metadata_cfg=global_cfg.metadata,
    )
    assert out.poster_urls == ["https://s/p-high.jpg", "https://s/p-low.jpg"]  # 胜者置首
    assert await resource_store.get_by_url("https://s/p-low.jpg") is None
    assert await resource_store.get_by_url("https://s/p-high.jpg") is not None

    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=thumb_urls,
        raw={"dmm": {"poster_urls": poster_urls}},
    )
    assert meta.id is not None
    lib_a, root_a = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"thumb_download_strategy": "priority"},
    )
    lib_b, root_b = await _seed_library(repo, tmp_path, name="b", metadata_id=meta.id, number="MIDV-123")
    assert lib_a.id is not None and lib_b.id is not None

    handler = OrganizeHandler(repo, global_cfg, resource_store, web_client=client)
    result_a = await handler.handle(OrganizePayload(library_id=lib_a.id, path=str(root_a)))
    result_b = await handler.handle(OrganizePayload(library_id=lib_b.id, path=str(root_b)))
    assert result_a.success and result_b.success
    assert result_a.result is not None and result_a.result.failed == 0
    assert result_b.result is not None and result_b.result.failed == 0

    # 库 A (priority 覆盖): 首个候选偏矮 → 从首个 thumb 裁剪 (420x600 蓝).
    poster_a = root_a / "MIDV-123" / "poster.jpg"
    assert Image.open(poster_a).size == (420, 600)
    assert _pixel_close(poster_a, (0, 0, 255))
    # 库 B (继承全局 quality): 胜者 p-high 免裁原样 (500x500 橙).
    poster_b = root_b / "MIDV-123" / "poster.jpg"
    assert Image.open(poster_b).size == (500, 500)
    assert _pixel_close(poster_b, (255, 165, 0))
    assert sorted(resource_store._base_dir.rglob(f"*{TMP_MARK}*")) == []


# --- 任务 12: 剧照幕后场景 — 三层字段与双消费 (规格 §3.5) ---


class BehindScenesCrawler:
    """样例来源: 普通剧照 + 幕后图地址; 地址随番号变化, 同库多次刮削互不串扰."""

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        number = query.number
        return MediaMetadata(
            number=number,
            title="T",
            extrafanart=[f"https://s/{number}/e1.jpg", f"https://s/{number}/e2.jpg"],
            behind_scenes=[f"https://s/{number}/b1.jpg", f"https://s/{number}/b2.jpg"],
        )


class BehindScenesFactory:
    def __init__(self, crawlers: dict[str, CrawlerLike]):
        self._crawlers = crawlers

    async def get_crawlers(self, names: Iterable[str]) -> dict[str, CrawlerLike]:
        wanted = set(names)
        return {name: crawler for name, crawler in self._crawlers.items() if name in wanted}


_BEHIND_NUMBER = "MIDV-123"


def _behind_scrape_handler(
    repo: Repository,
    resource_store: ResourceStore,
    client: WebClient,
    behind_enabled: bool,
) -> ScrapeHandler:
    return ScrapeHandler(
        repo=repo,
        factory=BehindScenesFactory({"javdb": BehindScenesCrawler()}),
        resource_store=resource_store,
        pipeline_config=HotSettings(metadata=MetadataConfig(extra_fanarts_behind_scenes=behind_enabled)),
        web_client=client,
    )


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("behind_enabled", [False, True], ids=["toggle_off", "toggle_on"])
async def test_behind_scenes_persisted_regardless_of_toggle(
    repo: Repository, resource_store: ResourceStore, behind_enabled: bool
) -> None:
    """幕后图地址是原始数据: 无论全局开关状态都随刮削落库 (按来源分组)."""
    client = cast("WebClient", FakeClient())
    handler = _behind_scrape_handler(repo, resource_store, client, behind_enabled)

    result = await handler.handle(ScrapePayload(number=_BEHIND_NUMBER, content_type=ContentType.CENSORED))
    assert result.success is True

    row = await repo.get_metadata_by_number(_BEHIND_NUMBER)
    assert row is not None
    assert row.behind_scenes == {"javdb": [f"https://s/{_BEHIND_NUMBER}/b1.jpg", f"https://s/{_BEHIND_NUMBER}/b2.jpg"]}


@pytest.mark.asyncio(loop_scope="function")
async def test_materialize_consumes_behind_scenes_global(repo: Repository, resource_store: ResourceStore) -> None:
    """无媒体文件的番号刮削: 全局开关开启时幕后图实际物化进 Resource; 关闭时仍持久化但不物化."""
    client = cast("WebClient", MappedClient({"https://s/MIDV-123/b1.jpg": ((640, 480), "green")}))

    on_handler = _behind_scrape_handler(repo, resource_store, client, behind_enabled=True)
    result = await on_handler.handle(ScrapePayload(number=_BEHIND_NUMBER, content_type=ContentType.CENSORED))
    assert result.success is True

    row = await repo.get_metadata_by_number(_BEHIND_NUMBER)
    assert row is not None
    assert row.behind_scenes == {"javdb": [f"https://s/{_BEHIND_NUMBER}/b1.jpg", f"https://s/{_BEHIND_NUMBER}/b2.jpg"]}
    for i in (1, 2):
        assert await resource_store.get_by_url(f"https://s/{_BEHIND_NUMBER}/b{i}.jpg") is not None

    off_number = "MIDV-456"
    off_handler = _behind_scrape_handler(repo, resource_store, client, behind_enabled=False)
    result = await off_handler.handle(ScrapePayload(number=off_number, content_type=ContentType.CENSORED))
    assert result.success is True

    row = await repo.get_metadata_by_number(off_number)
    assert row is not None
    assert row.behind_scenes == {"javdb": [f"https://s/{off_number}/b1.jpg", f"https://s/{off_number}/b2.jpg"]}
    for i in (1, 2):
        assert await resource_store.get_by_url(f"https://s/{off_number}/b{i}.jpg") is None


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_filters_behind_scenes_by_library(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """同番号两库相反开关: 幕后图各按库开关追加到剧照集合, 普通剧照不受影响, 与普通候选重复的地址不重落."""
    client = cast("WebClient", MappedClient({"https://s/b1.jpg": ((640, 480), "green")}))  # 普通剧照蓝, 幕后图绿
    meta = await repo.upsert_metadata(
        number=_BEHIND_NUMBER,
        content_type=ContentType.CENSORED,
        extrafanart_urls={"dmm": ["https://s/e1.jpg", "https://s/e2.jpg"]},
        behind_scenes={"javdb": ["https://s/e2.jpg", "https://s/b1.jpg"]},
    )
    assert meta.id is not None
    lib_a, root_a = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number=_BEHIND_NUMBER,
        metadata_override={"extra_fanarts_behind_scenes": True},
    )
    lib_b, root_b = await _seed_library(
        repo,
        tmp_path,
        name="b",
        metadata_id=meta.id,
        number=_BEHIND_NUMBER,
        metadata_override={"extra_fanarts_behind_scenes": False},
    )
    assert lib_a.id is not None and lib_b.id is not None
    # payload 整体替换: 与 _seed_library 的 video_template 合并成一次写入.
    naming_override = {
        "video_template": "{number}/{number}.{ext}",
        "extrafanart_name_template": "{number}-{index_padded}.jpg",
    }
    await repo.upsert_config_override(lib_a.id, "naming", naming_override)
    await repo.upsert_config_override(lib_b.id, "naming", naming_override)

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result_a = await handler.handle(OrganizePayload(library_id=lib_a.id, path=str(root_a)))
    result_b = await handler.handle(OrganizePayload(library_id=lib_b.id, path=str(root_b)))
    assert result_a.success and result_b.success

    names_a = sorted(p.name for p in (root_a / _BEHIND_NUMBER / "extrafanart").iterdir())
    names_b = sorted(p.name for p in (root_b / _BEHIND_NUMBER / "extrafanart").iterdir())
    # 开: 普通两张在前, 幕后接续编号, 重复的 e2 不重落; 同一命名模板 (补零宽度 = 本轮张数).
    assert names_a == [
        f"{_BEHIND_NUMBER}-1.jpg",
        f"{_BEHIND_NUMBER}-2.jpg",
        f"{_BEHIND_NUMBER}-3.jpg",
    ]
    assert _pixel_close(root_a / _BEHIND_NUMBER / "extrafanart" / f"{_BEHIND_NUMBER}-3.jpg", (0, 128, 0))
    # 关: 只有普通剧照, 与库 A 的前两张一致 (普通剧照不受开关影响).
    assert names_b == [f"{_BEHIND_NUMBER}-1.jpg", f"{_BEHIND_NUMBER}-2.jpg"]
    assert _pixel_close(root_b / _BEHIND_NUMBER / "extrafanart" / f"{_BEHIND_NUMBER}-1.jpg", (0, 0, 255))


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_behind_scenes_dedup_against_landed_only(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """幕后图去重只认实际落盘的普通剧照: 未被选中的站点组不挡幕后图.

    普通剧照按站点优先级「有结果即停」, 只取首个成功站点组. 组 A 提供 a.jpg、组 B 提供
    b.jpg 且幕后含 b.jpg 时, 实际落盘的只有 a.jpg —— b.jpg 不该被去重挡掉, 否则幕后图一张不出.
    """
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/a.jpg": ((640, 480), "blue"),
                "https://s/b.jpg": ((640, 480), "green"),
            }
        ),
    )
    meta = await repo.upsert_metadata(
        number=_BEHIND_NUMBER,
        content_type=ContentType.CENSORED,
        extrafanart_urls={"site_a": ["https://s/a.jpg"], "site_b": ["https://s/b.jpg"]},
        behind_scenes={"javdb": ["https://s/b.jpg"]},
    )
    assert meta.id is not None
    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=meta.id,
        number=_BEHIND_NUMBER,
        metadata_override={"extra_fanarts_behind_scenes": True},
    )
    assert lib.id is not None
    await repo.upsert_config_override(
        lib.id,
        "naming",
        {
            "video_template": "{number}/{number}.{ext}",
            "extrafanart_name_template": "{number}-{index_padded}.jpg",
        },
    )

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success

    # 普通剧照只落组 A 的 a.jpg; 幕后 b.jpg 未与它重复, 应作为第二张落下.
    names = sorted(p.name for p in (root / _BEHIND_NUMBER / "extrafanart").iterdir())
    assert names == [f"{_BEHIND_NUMBER}-1.jpg", f"{_BEHIND_NUMBER}-2.jpg"]
    assert _pixel_close(root / _BEHIND_NUMBER / "extrafanart" / f"{_BEHIND_NUMBER}-1.jpg", (0, 0, 255))
    assert _pixel_close(root / _BEHIND_NUMBER / "extrafanart" / f"{_BEHIND_NUMBER}-2.jpg", (0, 128, 0))


@pytest.mark.asyncio(loop_scope="function")
async def test_scrape_off_organize_on_no_rescrape(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """刮削时关闭 → 整理时开启: 幕后地址已在刮削期持久化, 无需重刮, 重新整理即生效."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                f"https://s/{_BEHIND_NUMBER}/b1.jpg": ((640, 480), "green"),
                f"https://s/{_BEHIND_NUMBER}/b2.jpg": ((640, 480), "orange"),
            }
        ),
    )
    handler = _behind_scrape_handler(repo, resource_store, client, behind_enabled=False)
    result = await handler.handle(ScrapePayload(number=_BEHIND_NUMBER, content_type=ContentType.CENSORED))
    assert result.success is True

    row = await repo.get_metadata_by_number(_BEHIND_NUMBER)
    assert row is not None
    assert row.id is not None
    assert row.behind_scenes

    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="a",
        metadata_id=row.id,
        number=_BEHIND_NUMBER,
        metadata_override={"extra_fanarts_behind_scenes": True},
    )
    assert lib.id is not None

    organize = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result = await organize.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success is True

    names = sorted(p.name for p in (root / _BEHIND_NUMBER / "extrafanart").iterdir())
    assert names == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]  # 普通 1..2 在前, 幕后 3..4 在后
    assert _pixel_close(root / _BEHIND_NUMBER / "extrafanart" / "3.jpg", (0, 128, 0))
    assert _pixel_close(root / _BEHIND_NUMBER / "extrafanart" / "4.jpg", (255, 165, 0))


# --- R1 IMPL-02: 整理期候选获取按库级生效 thumb_download_strategy 分派 ---

# thumb 低清在前高清在后; 剧照同理. 库级 quality → 高分辨率者胜出; priority → 首个候选.
_THUMB_CANDIDATES = ["https://s/t-low.jpg", "https://s/t-high.jpg"]
_THUMB_CANDIDATE_IMAGES = {
    "https://s/t-low.jpg": ((600, 600), "blue"),
    "https://s/t-high.jpg": ((800, 538), "green"),
}
_FANART_CANDIDATES = ["https://s/e-low.jpg", "https://s/e-high.jpg"]
_FANART_CANDIDATE_IMAGES = {
    "https://s/e-low.jpg": ((460, 460), "purple"),
    "https://s/e-high.jpg": ((500, 440), "orange"),
}


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_thumb_dispatch_by_library_strategy(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """整理期 thumb / fanart 按库级生效策略分派: quality 取高分辨率者, priority 取首个候选.

    预告片是视频, 恒取顺序首个可获取候选 (库级 quality 对它不生效).
    剧照是多个不同图片的集合, 择优语义不适用: 两个库都产出该站全部候选剧照.
    """
    client = cast(
        "WebClient",
        MappedClient({**_THUMB_CANDIDATE_IMAGES, **_FANART_CANDIDATE_IMAGES}),
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=_THUMB_CANDIDATES,
        extrafanart_urls={"dmm": _FANART_CANDIDATES},
    )
    assert meta.id is not None
    lib_q, root_q = await _seed_library(
        repo,
        tmp_path,
        name="q",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"thumb_download_strategy": "quality"},
    )
    lib_p, root_p = await _seed_library(
        repo,
        tmp_path,
        name="p",
        metadata_id=meta.id,
        number="MIDV-123",
        metadata_override={"thumb_download_strategy": "priority"},
    )
    assert lib_q.id is not None and lib_p.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result_q = await handler.handle(OrganizePayload(library_id=lib_q.id, path=str(root_q)))
    result_p = await handler.handle(OrganizePayload(library_id=lib_p.id, path=str(root_p)))
    assert result_q.success and result_p.success
    assert result_q.result is not None and result_q.result.failed == 0
    assert result_p.result is not None and result_p.result.failed == 0

    # quality 库: thumb 与 fanart 取高分辨率候选.
    thumb_q = root_q / "MIDV-123" / "thumb.jpg"
    assert Image.open(thumb_q).size == (800, 538)
    assert _pixel_close(thumb_q, (0, 128, 0))
    fanart_q = root_q / "MIDV-123" / "fanart.jpg"
    assert Image.open(fanart_q).size == (800, 538)
    assert _pixel_close(fanart_q, (0, 128, 0))
    # 剧照是集合: quality 不改变张数, 该站全部候选都落盘.
    extrafanart_q = sorted((root_q / "MIDV-123" / "extrafanart").iterdir())
    assert len(extrafanart_q) == 2
    assert Image.open(extrafanart_q[0]).size == (460, 460)
    assert _pixel_close(extrafanart_q[0], (128, 0, 128))
    assert Image.open(extrafanart_q[1]).size == (500, 440)
    assert _pixel_close(extrafanart_q[1], (255, 165, 0))

    # priority 库: thumb / fanart 均取首个候选.
    thumb_p = root_p / "MIDV-123" / "thumb.jpg"
    assert Image.open(thumb_p).size == (600, 600)
    assert _pixel_close(thumb_p, (0, 0, 255))
    fanart_p = root_p / "MIDV-123" / "fanart.jpg"
    assert Image.open(fanart_p).size == (600, 600)
    assert _pixel_close(fanart_p, (0, 0, 255))
    extrafanart_p = sorted((root_p / "MIDV-123" / "extrafanart").iterdir())
    assert len(extrafanart_p) == 2
    assert Image.open(extrafanart_p[0]).size == (460, 460)
    assert _pixel_close(extrafanart_p[0], (128, 0, 128))
    assert Image.open(extrafanart_p[1]).size == (500, 440)
    assert _pixel_close(extrafanart_p[1], (255, 165, 0))


# --- R2 IMPL-09: 预告片是视频, 不参与图片画质择优 ---

_VIDEO_FIXTURE_DIR = Path(__file__).parent.parent / "media" / "fixtures"
_TRAILER_SMALL = (_VIDEO_FIXTURE_DIR / "trailer_small.mp4").read_bytes()
_TRAILER_LARGE = (_VIDEO_FIXTURE_DIR / "trailer_large.mp4").read_bytes()
_TRAILER_URLS = ["https://s/trailer-small.mp4", "https://s/trailer-large.mp4"]


class VideoClient:
    """按 URL 写出真实 mp4 字节的下载桩: 预告片候选借此给出可区分的字节量.

    未映射的 URL (封面等图片) 写出占位图, 与既有图片桩一致.
    """

    def __init__(self, videos: dict[str, bytes], images: dict[str, tuple[tuple[int, int], str]] | None = None) -> None:
        self._videos = videos
        self._images = images or {}
        self.downloaded: list[str] = []

    async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
        self.downloaded.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = self._videos.get(url)
        if payload is not None:
            dest.write_bytes(payload)
            return True
        size, color = self._images.get(url, ((800, 538), "blue"))
        Image.new("RGB", size, color).save(dest)
        return True

    async def get_filesize(self, url: str, **kwargs: object) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs: object) -> str:
        return url


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_trailer_takes_first_candidate_under_quality(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """库级 quality 下预告片仍取顺序首个可获取候选, 且不下载全部候选; 图片候选照常择优.

    预告片是视频: 画质择优的比较键 (分辨率面积 → 字节数) 对它无意义, 若参与择优会退化为
    「下载全部候选选字节最多的」, 既改来源优先级又多付下载量.
    """
    stub = VideoClient(
        {
            "https://s/trailer-small.mp4": _TRAILER_SMALL,
            "https://s/trailer-large.mp4": _TRAILER_LARGE,
        },
        _THUMB_CANDIDATE_IMAGES,
    )
    client = cast("WebClient", stub)
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        thumb_urls=["https://s/t-low.jpg", "https://s/t-high.jpg"],
        trailer_urls=list(_TRAILER_URLS),
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

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success
    assert result.result is not None and result.result.failed == 0

    # 封面仍是图片: quality 照常择优 (高清候选胜出).
    thumb = root / "MIDV-123" / "thumb.jpg"
    assert Image.open(thumb).size == (800, 538)
    assert _pixel_close(thumb, (0, 128, 0))

    trailer = root / "MIDV-123" / "trailer.mp4"
    assert trailer.read_bytes() == _TRAILER_SMALL  # 首个候选, 非字节最多的
    assert "https://s/trailer-large.mp4" not in stub.downloaded  # 不下载后续候选
    assert stub.downloaded.count("https://s/trailer-small.mp4") == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_extrafanart_keeps_site_priority_under_quality(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """库级 quality 下剧照的站点优先级不变: 首个有产物的站点胜出, 取该站全部候选."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/dmm1.jpg": ((460, 460), "purple"),
                "https://s/dmm2.jpg": ((500, 440), "orange"),
                "https://s/javdb1.jpg": ((640, 480), "green"),
            }
        ),
    )
    meta = await repo.upsert_metadata(
        number="MIDV-123",
        content_type=ContentType.CENSORED,
        extrafanart_urls={"dmm": ["https://s/dmm1.jpg", "https://s/dmm2.jpg"], "javdb": ["https://s/javdb1.jpg"]},
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

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success

    # dmm 站点在先且有产物: 只取 dmm 的两张, javdb 的剧照不落盘.
    extrafanart = sorted((root / "MIDV-123" / "extrafanart").iterdir())
    assert len(extrafanart) == 2
    assert _pixel_close(extrafanart[0], (128, 0, 128))
    assert _pixel_close(extrafanart[1], (255, 165, 0))
    assert await resource_store.get_by_url("https://s/javdb1.jpg") is None


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_behind_scenes_kept_under_quality(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """库级 quality 下幕后图同样按集合取全: 普通剧照与幕后图各按站点取全部候选."""
    client = cast(
        "WebClient",
        MappedClient(
            {
                "https://s/e1.jpg": ((460, 460), "purple"),
                "https://s/e2.jpg": ((500, 440), "orange"),
                "https://s/b1.jpg": ((640, 480), "green"),
                "https://s/b2.jpg": ((640, 480), "blue"),
            }
        ),
    )
    meta = await repo.upsert_metadata(
        number=_BEHIND_NUMBER,
        content_type=ContentType.CENSORED,
        extrafanart_urls={"dmm": ["https://s/e1.jpg", "https://s/e2.jpg"]},
        behind_scenes={"javdb": ["https://s/b1.jpg", "https://s/b2.jpg"]},
    )
    assert meta.id is not None
    lib, root = await _seed_library(
        repo,
        tmp_path,
        name="q",
        metadata_id=meta.id,
        number=_BEHIND_NUMBER,
        metadata_override={"thumb_download_strategy": "quality", "extra_fanarts_behind_scenes": True},
    )
    assert lib.id is not None

    handler = OrganizeHandler(repo, HotSettings(), resource_store, web_client=client)
    result = await handler.handle(OrganizePayload(library_id=lib.id, path=str(root)))
    assert result.success

    # 普通剧照两张在前, 幕后图两张接续编号: quality 不减张.
    extrafanart = sorted((root / _BEHIND_NUMBER / "extrafanart").iterdir())
    assert len(extrafanart) == 4
    assert _pixel_close(extrafanart[0], (128, 0, 128))
    assert _pixel_close(extrafanart[1], (255, 165, 0))
    assert _pixel_close(extrafanart[2], (0, 128, 0))
    assert _pixel_close(extrafanart[3], (0, 0, 255))


@pytest.mark.asyncio(loop_scope="function")
async def test_acquire_candidates_local_edge_cases(resource_store: ResourceStore, tmp_path: Path) -> None:
    """候选获取分派的边界: 单候选、全部失败、内部 URL —— quality 与 priority 结果一致."""
    client = cast("WebClient", MappedClient({"https://s/one.jpg": ((640, 480), "green")}))
    urls = ["https://s/one.jpg"]

    for quality in (False, True):
        got = await _acquire_candidates_local(urls, resource_store, client, quality=quality)
        assert got is not None
        assert Image.open(got).size == (640, 480)

    # 全部候选获取失败: 返回 None (不抛错).
    class DeadClient:
        async def download(self, url: str, dest: Path, **kwargs) -> bool:
            return False

        async def get_filesize(self, url: str, **kwargs):
            return None

        async def resolve_final_url(self, url: str, **kwargs) -> str:
            return url

    dead = await _acquire_candidates_local(
        ["https://s/dead.jpg"], resource_store, cast("WebClient", DeadClient()), quality=True
    )
    assert dead is None

    # 内部 URL (已落盘派生资源) 在 quality 下按本地文件参与择优, 不发外部请求.
    internal = await _store_derived(
        resource_store, "https://s/one.jpg", "right:legacy:0.7000", size=(376, 538), color="red"
    )
    got_internal = await _acquire_candidates_local([internal], resource_store, client, quality=True)
    assert got_internal is not None
    with Image.open(got_internal) as img:
        assert img.size == (376, 538)
