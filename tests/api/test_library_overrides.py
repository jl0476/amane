"""库级配置覆盖 CRUD 与生效配置端点 (规格 §3.1.4).

覆盖写路径校验: section 白名单 / 禁 null / section 模型校验, 失败 422 且不落库.
effective-config 是四层合成的只读投影 (无任务层), 断言以全局默认与覆盖语义为准.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx2 import AsyncClient

from amane.config import NamingConfig, NfoConfig, OrganizeCommonConfig

_GLOBAL_NAMING = NamingConfig().model_dump(mode="json")
_GLOBAL_COMMON = OrganizeCommonConfig().model_dump(mode="json")
_GLOBAL_NFO = NfoConfig().model_dump(mode="json")


async def _create_library(client: AsyncClient, safe_path: Path, name: str, **extra: Any) -> int:
    target = safe_path / name
    target.mkdir(exist_ok=True)
    resp = await client.post("libraries", json={"path": str(target), "scan": False, **extra})
    assert resp.status_code == 201
    library_id: int = resp.json()["id"]
    return library_id


@pytest.mark.asyncio
async def test_overrides_crud_roundtrip(client: AsyncClient, safe_path: Path) -> None:
    """PUT 落库 → GET 回读 ({} 哨兵原样) → effective-config 生效 → DELETE 回到继承."""
    library_id = await _create_library(client, safe_path, "crud")
    payload = {"video_template": "{custom}/{number}.{ext}", "thumb_template": {}}

    put = await client.put(f"libraries/{library_id}/overrides/naming", json=payload)
    assert put.status_code == 200
    assert put.json() == {"section": "naming", "payload": payload}

    listed = await client.get(f"libraries/{library_id}/overrides")
    assert listed.status_code == 200
    assert listed.json() == {"overrides": {"naming": payload}}

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.status_code == 200
    naming = effective.json()["naming"]
    assert naming["video_template"] == "{custom}/{number}.{ext}"
    assert naming["thumb_template"] is None

    deleted = await client.delete(f"libraries/{library_id}/overrides/naming")
    assert deleted.status_code == 204
    assert (await client.get(f"libraries/{library_id}/overrides")).json() == {"overrides": {}}

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json() == {"common": _GLOBAL_COMMON, "naming": _GLOBAL_NAMING, "nfo": _GLOBAL_NFO}


@pytest.mark.asyncio
async def test_put_empty_payload_replaces_whole_section(client: AsyncClient, safe_path: Path) -> None:
    """PUT 是整体替换: 空 payload 落库后旧键消失, 生效值回到继承."""
    library_id = await _create_library(client, safe_path, "replace")
    base = f"libraries/{library_id}/overrides"

    await client.put(f"{base}/naming", json={"video_template": "{old}/{number}.{ext}", "write_nfo": False})
    replaced = await client.put(f"{base}/naming", json={})
    assert replaced.status_code == 200
    assert replaced.json() == {"section": "naming", "payload": {}}

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"] == _GLOBAL_NAMING


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["download", "watermark", "metadata", "provider", "parser"])
async def test_put_rejects_reserved_sections(client: AsyncClient, safe_path: Path, section: str) -> None:
    """白名单外的预留 section 一律 422, 不落库."""
    library_id = await _create_library(client, safe_path, "reserved")
    resp = await client.put(f"libraries/{library_id}/overrides/{section}", json={"overwrite_video": True})
    assert resp.status_code == 422
    assert (await client.get(f"libraries/{library_id}/overrides")).json() == {"overrides": {}}


@pytest.mark.asyncio
async def test_put_rejects_null_and_invalid_payloads(client: AsyncClient, safe_path: Path) -> None:
    """null / 未知键 / 类型不符 422 且不落库; 库不存在 404; DELETE 幂等."""
    library_id = await _create_library(client, safe_path, "invalid")
    base = f"libraries/{library_id}/overrides"

    resp = await client.put(f"{base}/naming", json={"link_template": None})
    assert resp.status_code == 422
    assert (await client.get(f"libraries/{library_id}/overrides")).json() == {"overrides": {}}

    for payload in ({"no_such_field": "x"}, {"write_nfo": []}, {"common": {"overwrite_video": True}}):
        resp = await client.put(f"{base}/naming", json=payload)
        assert resp.status_code == 422, payload
    assert (await client.get(f"libraries/{library_id}/overrides")).json() == {"overrides": {}}

    assert (await client.put("libraries/9999/overrides/naming", json={})).status_code == 404
    assert (await client.get("libraries/9999/overrides")).status_code == 404
    assert (await client.get("libraries/9999/effective-config")).status_code == 404
    assert (await client.delete("libraries/9999/overrides/naming")).status_code == 404

    # DELETE 无行也 204 (清空语义: 回到继承)
    assert (await client.delete(f"{base}/naming")).status_code == 204


@pytest.mark.asyncio
async def test_effective_config_beats_pinned_library_row(client: AsyncClient, safe_path: Path) -> None:
    """覆盖表优先于名单内的库行; common / nfo 三节各自生效."""
    library_id = await _create_library(client, safe_path, "pin", video_template="{lib}/{number}.{ext}")
    base = f"libraries/{library_id}/overrides"

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"]["video_template"] == "{lib}/{number}.{ext}"

    await client.put(f"{base}/naming", json={"video_template": "{ov}/{number}.{ext}"})
    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"]["video_template"] == "{ov}/{number}.{ext}"
    # 名单内的其余字段仍按库行生效
    assert effective.json()["naming"]["thumb_template"] is None

    await client.put(f"{base}/common", json={"overwrite_video": True})
    await client.put(f"{base}/nfo", json={"votes": True})
    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["common"]["overwrite_video"] is True
    assert effective.json()["nfo"]["votes"] is True
    assert effective.json()["nfo"]["votes"] != _GLOBAL_NFO["votes"]
