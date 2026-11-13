"""命名试算端点 (规格 §3.2.4): 与 resolve_paths 同一代码路径, 纯计算不落盘."""

import os
from pathlib import Path
from typing import Any

import pytest
from httpx2 import AsyncClient

from amane.db.models import MediaFileStatus, Metadata
from amane.organize import resolve_paths
from amane.parsing import parse_file_info


async def _create_library(client: AsyncClient, safe_path: Path, name: str, **extra: Any) -> int:
    target = safe_path / name
    target.mkdir(exist_ok=True)
    resp = await client.post("libraries", json={"path": str(target), "scan": False, **extra})
    assert resp.status_code == 201
    library_id: int = resp.json()["id"]
    return library_id


async def _seed_scraped_file(client: AsyncClient, repo: Any, safe_path: Path, name: str, **library_extra: Any) -> int:
    """建库 + 已刮削文件, 返回 media_file_id."""
    library_id = await _create_library(client, safe_path, name, **library_extra)
    src = safe_path / name / "incoming" / "NSFS-039.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"video")
    meta = await repo.upsert_metadata(number="NSFS-039", studio="Studio")
    assert meta.id is not None
    media = await repo.create_media_file(
        library_id, path=str(src), number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id
    )
    assert media.id is not None
    return media.id


@pytest.mark.asyncio
async def test_preview_renders_all_products(client: AsyncClient, repo: Any, safe_path: Path) -> None:
    """全部产物路径与 resolve_paths 对同一输入的输出一致 (同一代码路径)."""
    library_id = await _create_library(client, safe_path, "preview", video_template="{studio}/{number}/{number}.{ext}")
    src = safe_path / "preview" / "incoming" / "NSFS-039.mp4"
    resp = await client.post(
        "libraries/naming/preview",
        json={
            "library_id": library_id,
            "number": "NSFS-039",
            "studio": "Studio",
            "title": "标题",
            "release": "2024-01-15",
            "source_path": str(src),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["number"] == "NSFS-039"
    for key in ("video", "thumb", "poster", "fanart", "nfo", "trailer", "subtitle"):
        assert body[key], key
    assert body["video"] == str(safe_path / "preview" / "Studio" / "NSFS-039" / "NSFS-039.mp4")

    # 与 resolve_paths 对同一输入一致
    library = await repo.get_library(library_id)
    assert library is not None
    expected = resolve_paths(
        library,
        Metadata(number="NSFS-039", studio="Studio", title="标题", release="2024-01-15"),
        ext="mp4",
        source_path=src,
        file_info=parse_file_info(str(src)),
        safe_dirs=None,
    )
    assert body["video"] == str(expected.video)
    assert body["thumb"] == str(expected.thumb)
    assert body["poster"] == str(expected.poster)
    assert body["fanart"] == str(expected.fanart)
    assert body["nfo"] == str(expected.nfo)
    assert body["trailer"] == str(expected.trailer)
    # 不落盘
    assert not (safe_path / "preview" / "Studio").exists()


@pytest.mark.asyncio
async def test_preview_renders_jinja_naming(client: AsyncClient, repo: Any, safe_path: Path) -> None:
    """生效 naming.syntax=jinja 时试算走 Jinja 后端; 请求 syntax 可显式指定."""
    library_id = await _create_library(client, safe_path, "jinja")
    await repo.upsert_config_override(
        library_id,
        "naming",
        {"syntax": "jinja", "video_template": "{{ studio }}/{{ number }}.{{ ext }}"},
    )
    src = safe_path / "jinja" / "incoming" / "NSFS-039.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"v")
    resp = await client.post(
        "libraries/naming/preview",
        json={
            "library_id": library_id,
            "syntax": "jinja",
            "number": "NSFS-039",
            "studio": "Studio",
            "source_path": str(src),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["video"] == str(safe_path / "jinja" / "Studio" / "NSFS-039.mp4")


@pytest.mark.asyncio
async def test_preview_supports_media_file_id_and_global_default(
    client: AsyncClient, repo: Any, safe_path: Path
) -> None:
    """media_file_id 取真实文件与元数据; 无 library_id 用全局默认模板试算."""
    media_file_id = await _seed_scraped_file(client, repo, safe_path, "real")

    resp = await client.post("libraries/naming/preview", json={"media_file_id": media_file_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["number"] == "NSFS-039"
    assert body["video"] == str(safe_path / "real" / "Studio" / "NSFS-039" / "NSFS-039.mp4")

    # 无 library_id (也无 media_file_id): 全局默认模板, 相对库根为 /
    resp = await client.post(
        "libraries/naming/preview",
        json={"number": "ABC-123", "studio": "StudioX", "source_path": "/media/115/ABC-123-U.mp4"},
    )
    assert resp.status_code == 200
    expected = Path(os.path.abspath("/StudioX/ABC-123/ABC-123.mp4"))  # noqa: PTH100 — 与实现的 lexical abspath 同口径
    assert Path(resp.json()["video"]) == expected

    # 不存在的 media_file_id → 404
    resp = await client.post("libraries/naming/preview", json={"media_file_id": 99999})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_respects_library_naming_override(client: AsyncClient, repo: Any, safe_path: Path) -> None:
    """库级 naming 覆盖参与试算 (与整理的生效配置同源)."""
    library_id = await _create_library(client, safe_path, "override")
    await repo.upsert_config_override(library_id, "naming", {"video_template": "{studio}-ov/{number}.{ext}"})

    resp = await client.post(
        "libraries/naming/preview",
        json={
            "library_id": library_id,
            "number": "NSFS-039",
            "studio": "Studio",
            "source_path": "/media/115/NSFS-039.mp4",
        },
    )
    assert resp.status_code == 200
    assert Path(resp.json()["video"]) == Path(safe_path) / "override" / "Studio-ov" / "NSFS-039.mp4"


INVALID_PREVIEW_CASES = [
    pytest.param(
        {},
        {"number": ""},
        200,
        "video",
        "Unknown",
        id="空番号-全局默认模板回退 Unknown",
    ),
    pytest.param(
        {"video_template": "{number}"},
        {"number": ""},
        422,
        "detail",
        "视频模板解析为库根目录",
        id="空番号-视频模板退化为库根目录",
    ),
    pytest.param(
        {"override": {"syntax": "jinja", "video_template": "{% if %}"}},
        {"number": "NSFS-039", "studio": "Studio"},
        422,
        "detail",
        "模板 video_template",
        id="非法 Jinja 模板语法",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("seed", "body", "expect_status", "expect_field", "expect_needle"), INVALID_PREVIEW_CASES)
async def test_preview_invalid_inputs(
    client: AsyncClient,
    repo: Any,
    safe_path: Path,
    seed: dict[str, Any],
    body: dict[str, Any],
    expect_status: int,
    expect_field: str,
    expect_needle: str,
) -> None:
    """空番号按 Unknown 回退渲染 (200); 模板渲染失败统一 422, detail 指明模板与原因."""
    if seed:
        library_id = await _create_library(
            client, safe_path, "invalid", **{k: v for k, v in seed.items() if k != "override"}
        )
        if "override" in seed:
            await repo.upsert_config_override(library_id, "naming", seed["override"])
        body["library_id"] = library_id
    resp = await client.post("libraries/naming/preview", json=body)
    assert resp.status_code == expect_status
    assert expect_needle in resp.json()[expect_field]


@pytest.mark.asyncio
async def test_preview_reports_parsed_fields(client: AsyncClient, safe_path: Path) -> None:
    """响应带解析结果: 番号 / 类型 / 马赛克 / 中字 / 分辨率."""
    resp = await client.post(
        "libraries/naming/preview",
        json={"number": "MIDV-123-C", "source_path": "/media/115/MIDV-123-C-1080p.mp4"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["number"] == "MIDV-123"
    assert body["subtitle_tag"] == "C"
    assert body["content_type"]
    assert "mosaic" in body and "definition" in body
    # 路径按模板渲染: 番号来自源文件名解析, 中字标记进可选组
    assert body["video"].endswith("MIDV-123-C.mp4")
