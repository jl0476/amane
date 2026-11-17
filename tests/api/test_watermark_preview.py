"""水印预览端点 (规格 §3.6): 库级生效配置 × 请求相位的只读试算.

验收面: 200 image/jpeg 魔数、相位与 kinds 过滤随请求变化、库级覆盖生效、
源缺失 404、叠印失败 422 带原因、不落库不写 Resource.
"""

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from httpx2 import AsyncClient
from PIL import Image

if TYPE_CHECKING:
    from fastapi import FastAPI


PREVIEW_URL = "https://s/wm-preview.jpg"


class _ImageClient:
    """只为 ResourceStore 供图: download 写出指定字节, 其余方法用不到."""

    def __init__(self, content: bytes | None = None) -> None:
        self._content = content

    async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._content is not None:
            dest.write_bytes(self._content)
        else:
            Image.new("RGB", (200, 280), "blue").save(dest)
        return True

    async def resolve_final_url(self, url: str, **kwargs: object) -> str:
        return url


def _write_red_stamp(tmp_path: Path) -> None:
    """在 data_dir 旧位置放一枚不透明红章 (default 样式, 查找链第 3 级)."""
    watermark_dir = tmp_path / "data" / "watermarks"
    watermark_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(watermark_dir / "uncensored.png")


async def _seed_resource(app: FastAPI, url: str = PREVIEW_URL, content: bytes | None = None) -> Path:
    store = app.state.runtime.resource_store
    source = await store.acquire(url, _ImageClient(content))
    assert source is not None
    return source


async def _create_library(client: AsyncClient, safe_path: Path, name: str, **extra: Any) -> int:
    target = safe_path / name
    target.mkdir(exist_ok=True)
    resp = await client.post("libraries", json={"path": str(target), "scan": False, **extra})
    assert resp.status_code == 201
    return int(resp.json()["id"])


def _phase(**overrides: Any) -> dict[str, Any]:
    """相位四元组全量显式 (与端点契约一致): 缺字段即 422."""
    phase: dict[str, Any] = {"has_subtitle": False, "uncensored": False, "mosaic": None, "definition": None}
    phase.update(overrides)
    return phase


async def _preview(client: AsyncClient, library_id: int, url: str, phase: dict[str, Any]) -> Any:
    return await client.post(
        f"libraries/{library_id}/preview/watermark",
        json={"url": url, "phase": phase},
    )


def _pixel(body: bytes, x: int, y: int) -> tuple[int, ...]:
    with Image.open(io.BytesIO(body)) as img:
        value = img.convert("RGB").getpixel((x, y))
    assert isinstance(value, tuple)
    return value


def _is_red(pixel: tuple[int, ...]) -> bool:
    """采样点落在红章上 (蓝底图的红章呈高 R 低 B)."""
    return pixel[0] > 180 and pixel[2] < 80


# scale=0.2, 源图 200x280: 章高 56, pad=7, 章宽 112 → 左上章覆盖 (7..119, 7..63).
_SAMPLE = (20, 20)


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_returns_jpeg(app: FastAPI, client: AsyncClient, safe_path: Path, tmp_path: Path) -> None:
    """200 + image/jpeg + JPEG 魔数; 相位命中章时输出已叠章."""
    _write_red_stamp(tmp_path)
    source = await _seed_resource(app)
    library_id = await _create_library(client, safe_path, "lib")
    put = await client.put(f"libraries/{library_id}/overrides/watermark", json={"enabled": True, "scale": 0.2})
    assert put.status_code == 200

    resp = await _preview(client, library_id, PREVIEW_URL, _phase(uncensored=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.content[:2] == b"\xff\xd8"
    assert resp.content != source.read_bytes()
    assert _is_red(_pixel(resp.content, *_SAMPLE))


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_phase_input_changes_output(app: FastAPI, client: AsyncClient, safe_path: Path) -> None:
    """相位四元组随请求变化 → 叠印相位变化: 只开中字与只开无码出不同章."""
    await _seed_resource(app)
    library_id = await _create_library(client, safe_path, "lib")
    put = await client.put(f"libraries/{library_id}/overrides/watermark", json={"enabled": True})
    assert put.status_code == 200

    outputs = {
        name: (await _preview(client, library_id, PREVIEW_URL, _phase(**marks))).content
        for name, marks in {
            "subtitle": {"has_subtitle": True},
            "uncensored": {"uncensored": True},
            "both": {"has_subtitle": True, "uncensored": True},
            "cracked": {"mosaic": "cracked"},
        }.items()
    }
    assert outputs["subtitle"] != outputs["uncensored"]
    assert outputs["subtitle"] != outputs["cracked"]
    assert outputs["uncensored"] != outputs["cracked"]
    # 同角两枚向内叠: 双相位输出与其单相位子集都不同
    assert outputs["both"] != outputs["subtitle"]
    assert outputs["both"] != outputs["uncensored"]


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_kinds_filter_changes_output(app: FastAPI, client: AsyncClient, safe_path: Path) -> None:
    """kinds=false 的相位在预览中同样不取章: 同相位、不同 kinds → 输出不同."""
    await _seed_resource(app)
    same_phase = _phase(has_subtitle=True, uncensored=True)
    plain_id = await _create_library(client, safe_path, "plain")
    filtered_id = await _create_library(client, safe_path, "filtered")
    for lib_id, payload in (
        (plain_id, {"enabled": True}),
        (filtered_id, {"enabled": True, "kinds": {"subtitle": False}}),
    ):
        put = await client.put(f"libraries/{lib_id}/overrides/watermark", json=payload)
        assert put.status_code == 200

    plain = await _preview(client, plain_id, PREVIEW_URL, same_phase)
    filtered = await _preview(client, filtered_id, PREVIEW_URL, same_phase)
    assert plain.status_code == 200 and filtered.status_code == 200
    assert plain.content != filtered.content


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_uses_library_effective(
    app: FastAPI, client: AsyncClient, safe_path: Path, tmp_path: Path
) -> None:
    """库级覆盖影响预览输出: 继承全局 (水印关) 不带章, 覆盖开启的库带章."""
    _write_red_stamp(tmp_path)
    await _seed_resource(app)
    default_id = await _create_library(client, safe_path, "default")
    stamped_id = await _create_library(client, safe_path, "stamped")
    put = await client.put(f"libraries/{stamped_id}/overrides/watermark", json={"enabled": True, "scale": 0.2})
    assert put.status_code == 200

    phase = _phase(uncensored=True)
    plain = await _preview(client, default_id, PREVIEW_URL, phase)
    stamped = await _preview(client, stamped_id, PREVIEW_URL, phase)
    assert plain.status_code == 200 and stamped.status_code == 200
    assert plain.content != stamped.content
    assert not _is_red(_pixel(plain.content, *_SAMPLE))
    assert _is_red(_pixel(stamped.content, *_SAMPLE))


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_missing_source_404(client: AsyncClient, safe_path: Path) -> None:
    """源资源不存在与库不存在都是 404."""
    library_id = await _create_library(client, safe_path, "lib")
    resp = await _preview(client, library_id, "https://s/absent.jpg", _phase())
    assert resp.status_code == 404
    resp = await _preview(client, 4242, "https://s/absent.jpg", _phase())
    assert resp.status_code == 404


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_failure_422(app: FastAPI, client: AsyncClient, safe_path: Path) -> None:
    """源不可读 (非图像字节) → 422, detail 带原因不返回半成品."""
    await _seed_resource(app, content=b"definitely not an image")
    library_id = await _create_library(client, safe_path, "lib")
    put = await client.put(f"libraries/{library_id}/overrides/watermark", json={"enabled": True})
    assert put.status_code == 200

    resp = await _preview(client, library_id, PREVIEW_URL, _phase(uncensored=True))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "水印预览失败" in detail
    assert detail != "水印预览失败"  # 原因非空


@pytest.mark.asyncio(loop_scope="function")
async def test_preview_does_not_touch_store(app: FastAPI, client: AsyncClient, safe_path: Path) -> None:
    """预览不落库不写 Resource: store 行集合与源文件字节在预览前后不变."""
    source = await _seed_resource(app)
    store = app.state.runtime.resource_store
    rows_before = sorted(r.file_path for r in await store.list_all())
    source_bytes = source.read_bytes()

    library_id = await _create_library(client, safe_path, "lib")
    put = await client.put(f"libraries/{library_id}/overrides/watermark", json={"enabled": True, "scale": 0.2})
    assert put.status_code == 200

    resp = await _preview(client, library_id, PREVIEW_URL, _phase(uncensored=True))
    assert resp.status_code == 200
    # 输出已叠章, 但源文件与 store 行原样
    assert resp.content != source_bytes
    assert source.read_bytes() == source_bytes
    assert sorted(r.file_path for r in await store.list_all()) == rows_before
