"""NFO 单独重写端点 (规格 3.4.4): 以已落盘路径为基准, 只渲染 NFO, 不重算视频与链接位置.

场景统一为「库内视频 + 库外链接」: link_path 已持久化且链接文件在盘, NFO 默认落在链接旁.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient

    from amane.db.repository import Repository

VIDEO_TEMPLATE = "{number}/{number}.{ext}"


async def _seed_paired_entry(repo: Repository, root: Path) -> dict[str, Any]:
    """一条已整理的条目: 视频在库内、链接在库外, 两者均已落盘且 link_path 已持久化."""
    lib_root = root / "lib"
    out = root / "emby"
    (lib_root / "MIDV-123").mkdir(parents=True)
    out.mkdir(parents=True)
    video = lib_root / "MIDV-123" / "MIDV-123.mp4"
    video.write_bytes(b"video")
    link = out / "MIDV-123.mp4"
    link.write_bytes(b"link")
    meta = await repo.upsert_metadata("MIDV-123", studio="Studio")
    assert meta.id is not None
    lib = await repo.create_library(
        name="paired",
        path=str(lib_root),
        video_template=VIDEO_TEMPLATE,
        link_template=str(out / "{number}.{ext}"),
        template_overrides=["video_template", "link_template"],
    )
    assert lib.id is not None
    media = await repo.create_media_file(lib.id, path=str(video), metadata_id=meta.id, link_path=str(link))
    assert media.id is not None
    return {
        "metadata_id": meta.id,
        "library_id": lib.id,
        "media_id": media.id,
        "lib_root": lib_root,
        "out": out,
        "nfo": out / "MIDV-123.nfo",
        "video": video,
        "link": link,
    }


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("changed", ["video_template", "link_template", "nfo_template"])
async def test_rewrite_after_template_change_stays_paired(
    client: AsyncClient, repo: Repository, tmp_path: Path, changed: str
) -> None:
    """只改一种模板后重写: NFO 仍落在已落盘的消费路径旁、同主干, 不按新模板重算位置."""
    seeded = await _seed_paired_entry(repo, tmp_path / "files")
    out2 = seeded["out"].parent / "emby2"
    templates: dict[str, str] = {
        "video_template": "{prefix}-{suffix}/renamed.{ext}",
        "link_template": str(out2 / "{number}.{ext}"),
        "nfo_template": "{link_dir}/{link_name}.nfo",
    }
    await repo.upsert_config_override(seeded["library_id"], "naming", {changed: templates[changed]})

    resp = await client.put(f"metadata/{seeded['metadata_id']}/nfo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rewritten"] == [str(seeded["nfo"])]
    assert body["failures"] == []
    nfo = seeded["nfo"]
    assert nfo.is_file()
    assert "MIDV-123" in nfo.read_text(encoding="utf-8")
    # 不按新模板重算: 视频模板 / 链接模板的新位置上没有产物.
    assert not (out2 / "MIDV-123.nfo").exists()
    assert not (seeded["lib_root"] / "renamed").exists()
    # 端点是只读消费: link_path 列原样保留.
    media = await repo.get_media_file(seeded["media_id"])
    assert media is not None
    assert media.link_path == str(seeded["link"])


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("nfo_template", "other_dir"),
    [
        ("{link_dir}/custom.nfo", False),
        ("{video_dir}/{video_name}.nfo", True),
    ],
)
async def test_rewrite_rejects_stem_mismatch(
    client: AsyncClient, repo: Repository, tmp_path: Path, nfo_template: str, other_dir: bool
) -> None:
    """模板算出的落点与消费文件不同目录或不同主干 → 422 + 实际与期望配对, 不写到别处."""
    seeded = await _seed_paired_entry(repo, tmp_path / "files")
    await repo.upsert_config_override(seeded["library_id"], "naming", {"nfo_template": nfo_template})

    resp = await client.put(f"metadata/{seeded['metadata_id']}/nfo")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "MIDV-123" in detail and str(seeded["out"]) in detail
    if other_dir:
        assert str(seeded["lib_root"] / "MIDV-123") in detail
    else:
        assert "custom" in detail
    # 不静默写到别处.
    assert not (seeded["out"] / "custom.nfo").exists()
    assert not (seeded["lib_root"] / "MIDV-123" / "MIDV-123.nfo").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_rewrite_missing_link_file_falls_back_to_video(
    client: AsyncClient, repo: Repository, tmp_path: Path
) -> None:
    """链接文件已不在盘上: 以视频为消费路径, NFO 落在视频旁, 不改写 link_path 列 (裁决 R28)."""
    seeded = await _seed_paired_entry(repo, tmp_path / "files")
    seeded["link"].unlink()

    resp = await client.put(f"metadata/{seeded['metadata_id']}/nfo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    video_nfo = seeded["lib_root"] / "MIDV-123" / "MIDV-123.nfo"
    assert body["rewritten"] == [str(video_nfo)]
    assert body["failures"] == []
    assert video_nfo.is_file()
    assert not seeded["nfo"].exists()
    media = await repo.get_media_file(seeded["media_id"])
    assert media is not None
    assert media.link_path == str(seeded["link"])


@pytest.mark.asyncio(loop_scope="function")
async def test_rewrite_no_local_file_409(client: AsyncClient, repo: Repository) -> None:
    """只有元数据没有本地文件 → 409; 未知条目 → 404."""
    meta = await repo.upsert_metadata("NOFILE-001")
    resp = await client.put(f"metadata/{meta.id}/nfo")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "无本地文件，无法重写 NFO"
    assert (await client.put("metadata/9999/nfo")).status_code == 404


@pytest.mark.asyncio(loop_scope="function")
async def test_rewrite_multi_file_partial_failure(client: AsyncClient, repo: Repository, tmp_path: Path) -> None:
    """N 个文件独立处理: 成功者不回滚, 失败者带原因进失败清单; 跨库文件各按归属库处理."""
    files_root = tmp_path / "files"
    seeded = await _seed_paired_entry(repo, files_root)
    # 同库第二份 (无链接): 消费路径回退视频侧, NFO 落在视频旁.
    cd2 = seeded["lib_root"] / "MIDV-123" / "MIDV-123-CD2.mp4"
    cd2.write_bytes(b"video")
    await repo.create_media_file(seeded["library_id"], path=str(cd2), metadata_id=seeded["metadata_id"])
    # 跨库第三份: 归属库的 NFO 模板与消费文件不同主干 → 该文件失败.
    lib2_root = files_root / "lib2"
    (lib2_root / "MIDV-456").mkdir(parents=True)
    other = lib2_root / "MIDV-456" / "MIDV-456.mp4"
    other.write_bytes(b"video")
    lib2 = await repo.create_library(
        name="broken",
        path=str(lib2_root),
        video_template=VIDEO_TEMPLATE,
        nfo_template="{link_dir}/broken.nfo",
        template_overrides=["video_template", "nfo_template"],
    )
    assert lib2.id is not None
    await repo.create_media_file(lib2.id, path=str(other), metadata_id=seeded["metadata_id"])

    resp = await client.put(f"metadata/{seeded['metadata_id']}/nfo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["rewritten"]) == [
        str(seeded["nfo"]),
        str(seeded["lib_root"] / "MIDV-123" / "MIDV-123-CD2.nfo"),
    ]
    assert seeded["nfo"].is_file()
    assert (seeded["lib_root"] / "MIDV-123" / "MIDV-123-CD2.nfo").is_file()
    assert len(body["failures"]) == 1
    failure = body["failures"][0]
    assert failure["path"] == str(other)
    assert "broken" in failure["error"] and "MIDV-456" in failure["error"]
    assert not (lib2_root / "MIDV-456" / "broken.nfo").exists()
