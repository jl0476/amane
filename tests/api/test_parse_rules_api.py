"""识别词管理 API (规格 §3.1.6): CRUD、RE2 校验 422、重复键 409、CRUD 后 ParseRuleSet 缓存失效."""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient

from amane.app.runtime import AppRuntime
from amane.parsing import ContentType, parse_file_info

_GLOBAL_RULE: dict[str, Any] = {
    "scope_type": "global",
    "kind": "number_keyword",
    "content_type": "chinese",
    "pattern": "MIDV",
}


def _rule(**over: Any) -> dict[str, Any]:
    body = dict(_GLOBAL_RULE)
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_parse_rules_crud(client: AsyncClient, seed_library: object) -> None:
    """POST 落库 → GET 回读 → PUT 更新 → DELETE 移除; 全局 scope_id 恒为 0."""
    created = await client.post("parse-rules", json=_rule())
    assert created.status_code == 201
    row = created.json()
    assert row["scope_type"] == "global"
    assert row["scope_id"] == 0
    assert row["kind"] == "number_keyword"
    assert row["content_type"] == "chinese"
    assert row["pattern"] == "MIDV"
    assert row["enabled"] is True
    rule_id: int = row["id"]

    library_rule = await client.post(
        "parse-rules",
        json=_rule(scope_type="library", scope_id=1, kind="path_keyword", content_type="western", pattern="tokyo hot"),
    )
    assert library_rule.status_code == 201
    library_rule_id: int = library_rule.json()["id"]
    assert library_rule.json()["scope_id"] == 1

    listed = await client.get("parse-rules")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["pattern"] for item in items] == ["MIDV", "tokyo hot"]

    updated = await client.put(f"parse-rules/{rule_id}", json=_rule(pattern="SSIS", content_type="western"))
    assert updated.status_code == 200
    assert updated.json()["pattern"] == "SSIS"
    assert updated.json()["content_type"] == "western"

    deleted = await client.delete(f"parse-rules/{library_rule_id}")
    assert deleted.status_code == 204
    rest = (await client.get("parse-rules")).json()["items"]
    assert [item["pattern"] for item in rest] == ["SSIS"]

    missing = await client.delete(f"parse-rules/{library_rule_id}")
    assert missing.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"pattern": "r:([unclosed"}, id="正则编译失败"),
        pytest.param({"pattern": "r:(a)\\1"}, id="反向引用"),
        pytest.param({"pattern": "r:(?<=x)y"}, id="环视 lookbehind"),
        pytest.param({"pattern": "r:(?!x)y"}, id="环视 lookahead"),
        pytest.param({"pattern": "a" * 257}, id="字面超长"),
        pytest.param({"pattern": "r:" + "a" * 257}, id="正则超长"),
        pytest.param({"pattern": "r:"}, id="空正则"),
        pytest.param({"pattern": ""}, id="空字面"),
        pytest.param({"content_type": "fc2"}, id="类型值域外 fc2"),
        pytest.param({"content_type": "hentai"}, id="类型值域外 hentai"),
        pytest.param({"scope_type": "watch_dir"}, id="作用域预留值"),
        pytest.param({"kind": "text_keyword"}, id="未知 kind"),
        pytest.param({"scope_type": "library", "scope_id": 999}, id="库不存在"),
    ],
)
async def test_create_rejects_invalid(client: AsyncClient, payload: dict[str, Any]) -> None:
    """RE2 编译失败 / 反向引用 / 环视 / 超长 / 值域外 / 预留作用域 / 库不存在 一律 422, 不落库."""
    resp = await client.post("parse-rules", json=_rule(**payload))
    assert resp.status_code == 422
    assert (await client.get("parse-rules")).json()["items"] == []


@pytest.mark.asyncio
async def test_duplicate_key_rejected(client: AsyncClient, seed_library: object) -> None:
    """UNIQUE(scope_type, scope_id, kind, content_type, pattern): POST 重复 409, PUT 撞既有键同样 409."""
    first = await client.post("parse-rules", json=_rule())
    assert first.status_code == 201
    duplicate = await client.post("parse-rules", json=_rule())
    assert duplicate.status_code == 409

    target = await client.post("parse-rules", json=_rule(scope_type="library", scope_id=1))
    assert target.status_code == 201
    mover = await client.post(
        "parse-rules", json=_rule(scope_type="library", scope_id=1, pattern="SSIS", content_type="chinese")
    )
    assert mover.status_code == 201
    collision = await client.put(f"parse-rules/{mover.json()['id']}", json=_rule(scope_type="library", scope_id=1))
    assert collision.status_code == 409


@pytest.mark.asyncio
async def test_update_rejects_invalid_pattern(client: AsyncClient, seed_library: object) -> None:
    """PUT 是写操作: 非法 pattern 422 且行保持原样."""
    created = await client.post("parse-rules", json=_rule())
    rule_id: int = created.json()["id"]

    rejected = await client.put(f"parse-rules/{rule_id}", json=_rule(pattern="r:("))
    assert rejected.status_code == 422
    row = (await client.get("parse-rules")).json()["items"][0]
    assert row["pattern"] == "MIDV"


@pytest.mark.asyncio
async def test_crud_invalidates_rule_set_cache(client: AsyncClient, app: FastAPI, seed_library: object) -> None:
    """规则 CRUD 后 ParseRuleSet 缓存失效: runtime.parse_rules_for 的匹配效果随行变化."""
    runtime: AppRuntime = app.state.runtime  # type: ignore[attr-defined]

    created = await client.post("parse-rules", json=_rule(pattern="MIDV", content_type="chinese"))
    rule_id: int = created.json()["id"]
    rules = await runtime.parse_rules_for(1)
    assert parse_file_info("/data/MIDV-123.mp4", rules=rules).content_type is ContentType.CHINESE

    updated = await client.put(f"parse-rules/{rule_id}", json=_rule(pattern="SSIS", content_type="western"))
    assert updated.status_code == 200
    rules = await runtime.parse_rules_for(1)
    assert parse_file_info("/data/MIDV-123.mp4", rules=rules).content_type is ContentType.CENSORED
    assert parse_file_info("/data/SSIS-456.mp4", rules=rules).content_type is ContentType.WESTERN

    deleted = await client.delete(f"parse-rules/{rule_id}")
    assert deleted.status_code == 204
    rules = await runtime.parse_rules_for(1)
    assert parse_file_info("/data/SSIS-456.mp4", rules=rules).content_type is ContentType.CENSORED
