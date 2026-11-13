"""测试 file ops 消费已物化 URL (内部派生 / 外部) + trailer 落盘."""

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from PIL import Image

from amane.api.models.media import MediaFileResponse, to_media_file_response
from amane.api.models.tasks import OrganizeSubmission
from amane.config import HotSettings, WatermarkConfig
from amane.db.models import Library, MediaFile, MediaFileStatus, Metadata
from amane.enums import DownloadableResource
from amane.handlers import OrganizeHandler, OrganizePayload
from amane.handlers.file import _copy_into_library, execute_file_operations, resolve_effective_config
from amane.media import ResourceStore
from amane.organize import MoveMode, PlaceOutcome, ResolvedPaths, resolve_paths
from amane.parsing import ContentType, parse_file_info

if TYPE_CHECKING:
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

    user_dir = tmp_path / "watermarks"
    user_dir.mkdir()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(user_dir / "subtitle.png")

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
        watermark_dir=user_dir,
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

    user_dir = tmp_path / "watermarks"
    user_dir.mkdir()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(user_dir / "subtitle.png")

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
        watermark_dir=user_dir,
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

    user_dir = tmp_path / "watermarks"
    user_dir.mkdir()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(user_dir / "subtitle.png")

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
        watermark_dir=user_dir,
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
