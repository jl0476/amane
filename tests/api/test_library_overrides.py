"""库级配置覆盖 CRUD 与生效配置端点 (规格 §3.1.4).

覆盖写路径校验: section 白名单 / 禁 null / section 模型校验, 失败 422 且不落库.
effective-config 是四层合成的只读投影 (无任务层), 断言以全局默认与覆盖语义为准.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport, AsyncClient

from amane.api.routes import API_PREFIX
from amane.config import HotSettings, NamingConfig, NfoConfig, OrganizeCommonConfig
from tests.api.conftest import make_app

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
    payload = {"video_template": "{studio}/{number}.{ext}", "thumb_template": {}}

    put = await client.put(f"libraries/{library_id}/overrides/naming", json=payload)
    assert put.status_code == 200
    assert put.json() == {"section": "naming", "payload": payload}

    listed = await client.get(f"libraries/{library_id}/overrides")
    assert listed.status_code == 200
    assert listed.json() == {"overrides": {"naming": payload}}

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.status_code == 200
    naming = effective.json()["naming"]
    assert naming["video_template"] == "{studio}/{number}.{ext}"
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

    await client.put(f"{base}/naming", json={"video_template": "{publisher}/{number}.{ext}", "write_nfo": False})
    replaced = await client.put(f"{base}/naming", json={})
    assert replaced.status_code == 200
    assert replaced.json() == {"section": "naming", "payload": {}}

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"] == _GLOBAL_NAMING


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["download", "watermark", "metadata", "provider", "parser"])
async def test_write_rejects_reserved_sections(client: AsyncClient, safe_path: Path, section: str) -> None:
    """白名单外的预留 section 一律 422 (PUT 与 DELETE 同判), 不落库."""
    library_id = await _create_library(client, safe_path, "reserved")
    base = f"libraries/{library_id}/overrides"

    resp = await client.put(f"{base}/{section}", json={"overwrite_video": True})
    assert resp.status_code == 422
    assert (await client.delete(f"{base}/{section}")).status_code == 422
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
    library_id = await _create_library(client, safe_path, "pin", video_template="{series}/{number}.{ext}")
    base = f"libraries/{library_id}/overrides"

    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"]["video_template"] == "{series}/{number}.{ext}"

    await client.put(f"{base}/naming", json={"video_template": "{prefix}/{number}.{ext}"})
    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"]["video_template"] == "{prefix}/{number}.{ext}"
    # 名单内的其余字段仍按库行生效
    assert effective.json()["naming"]["thumb_template"] is None

    await client.put(f"{base}/common", json={"overwrite_video": True})
    await client.put(f"{base}/nfo", json={"votes": True})
    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["common"]["overwrite_video"] is True
    assert effective.json()["nfo"]["votes"] is True
    assert effective.json()["nfo"]["votes"] != _GLOBAL_NFO["votes"]


# --- 任务 21: 语法切换验证、保存路径校验与 schema 下发 ---


_JINJA_DEFAULTS: dict[str, Any] = {
    "video_template": "{{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}",
    "thumb_template": "{{ link_dir }}/thumb.jpg",
    "poster_template": "{{ link_dir }}/poster.jpg",
    "fanart_template": "{{ link_dir }}/fanart.jpg",
    "extrafanart_template": "{{ link_dir }}/extrafanart",
    "extrafanart_name_template": "{{ index }}.jpg",
    "nfo_template": "{{ link_dir }}/{{ video_name }}.nfo",
    "trailer_template": "{{ link_dir }}/trailer.mp4",
    "subtitle_template": "{{ link_dir }}/{{ raw_srt_name }}.{{ ext }}",
}


@pytest.mark.asyncio
async def test_switch_rejected_on_placeholder_residual(client: AsyncClient, safe_path: Path) -> None:
    """自研模板切 jinja 被拒: 422 + 指明模板与残留; 反向同理 ({{ 残留被保存校验拦下)."""
    library_id = await _create_library(client, safe_path, "switch", video_template="{studio}/{number}.{ext}")

    resp = await client.put(f"libraries/{library_id}/overrides/naming", json={"syntax": "jinja"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "video_template" in detail
    assert "{studio}" in detail
    # 未切换: 生效语法保持 native
    effective = await client.get(f"libraries/{library_id}/effective-config")
    assert effective.json()["naming"]["syntax"] == "native"

    # 反向: 目标 native 而写入的模板是 Jinja 形态 → 保存校验拒绝并指明模板
    resp = await client.put(
        f"libraries/{library_id}/overrides/naming",
        json={"syntax": "native", "video_template": "{{ studio }}/{{ number }}.{{ ext }}"},
    )
    assert resp.status_code == 422
    assert "video_template" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_switch_validates_inherited_and_fallback_templates(tmp_path: Path) -> None:
    """名单内库级模板、覆盖表模板、全局默认模板全部参与切换验证; 全部换成目标语法默认后切换成功."""
    files = tmp_path / "files"
    (files / "plain").mkdir(parents=True)
    app = make_app(HotSettings(), tmp_path / "data", tmp_path / "logs", files)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://test{API_PREFIX}/") as client:
            library_id = await _create_library(client, files, "plain")

            # 全局默认模板参与: 纯净库的 video_template 回退到全局默认 (native 字面量) → 残留被拒
            resp = await client.put(f"libraries/{library_id}/overrides/naming", json={"syntax": "jinja"})
            assert resp.status_code == 422
            assert "video_template" in resp.json()["detail"]
            assert "{studio}" in resp.json()["detail"]

            # 名单内库级模板参与: 库行模板有自研残留 → 拒
            inherited = await _create_library(client, files, "inherited", video_template="{customlib}.{ext}")
            resp = await client.put(f"libraries/{inherited}/overrides/naming", json={"syntax": "jinja"})
            assert resp.status_code == 422
            assert "{customlib}" in resp.json()["detail"]

            # 覆盖表模板参与: 本次 PUT 携带的自研模板 → 拒
            resp = await client.put(
                f"libraries/{library_id}/overrides/naming",
                json={"syntax": "jinja", "video_template": "{fromoverride}.{ext}"},
            )
            assert resp.status_code == 422
            assert "{fromoverride}" in resp.json()["detail"]

            # 全部模板随切换替换为目标语法默认 → 成功, 生效语法为 jinja
            ok = await client.put(
                f"libraries/{library_id}/overrides/naming", json={"syntax": "jinja", **_JINJA_DEFAULTS}
            )
            assert ok.status_code == 200
            effective = await client.get(f"libraries/{library_id}/effective-config")
            assert effective.json()["naming"]["syntax"] == "jinja"
            assert effective.json()["naming"]["video_template"] == _JINJA_DEFAULTS["video_template"]
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_switch_validates_dirty_global_default(tmp_path: Path) -> None:
    """全局默认模板本身含另一语法残留时同样拒绝切换."""
    files = tmp_path / "files"
    (files / "plain").mkdir(parents=True)
    app = make_app(
        HotSettings(naming=NamingConfig(video_template="{number}.{ext}")),
        tmp_path / "data",
        tmp_path / "logs",
        files,
    )
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://test{API_PREFIX}/") as client:
            library_id = await _create_library(client, files, "plain")
            resp = await client.put(f"libraries/{library_id}/overrides/naming", json={"syntax": "jinja"})
            assert resp.status_code == 422
            assert "{number}" in resp.json()["detail"]
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"video_template": "{nope}/{number}.{ext}"}, "nope"),
        ({"video_template": "{number:foo}.{ext}"}, "foo"),
        ({"subtitle_template": "{number:truncate(x)}.{ext}"}, "truncate"),
    ],
    ids=["unknown-placeholder", "unknown-filter", "bad-filter-arg"],
)
async def test_naming_override_save_rejects_bad_templates(
    client: AsyncClient, safe_path: Path, payload: dict[str, Any], expected: str
) -> None:
    """保存路径按生效语法校验模板: 未知占位符 / 未知 filter / 语法错误 → 422 + 名称与位置."""
    library_id = await _create_library(client, safe_path, "save")
    resp = await client.put(f"libraries/{library_id}/overrides/naming", json=payload)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert expected in detail
    field = next(iter(payload))
    assert field in detail


@pytest.mark.asyncio
async def test_naming_override_accepts_dir_alias_and_syntax_template(client: AsyncClient, safe_path: Path) -> None:
    """dir 别名被接受; jinja 语法下保存 jinja 模板通过."""
    library_id = await _create_library(client, safe_path, "alias")

    resp = await client.put(f"libraries/{library_id}/overrides/naming", json={"video_template": "{dir}/{number}.{ext}"})
    assert resp.status_code == 200

    resp = await client.put(
        f"libraries/{library_id}/overrides/naming",
        json={"syntax": "jinja", "video_template": "{{ studio }}/{{ number }}.{{ ext }}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_naming_override_same_syntax_save_probes_residual(client: AsyncClient, safe_path: Path) -> None:
    """同语法保存对写入的模板字段执行同一渲染探针: jinja 生效下携带自研占位符模板 → 422 不落库."""
    library_id = await _create_library(client, safe_path, "residual")
    base = f"libraries/{library_id}/overrides/naming"
    assert (await client.put(base, json={"syntax": "jinja", **_JINJA_DEFAULTS})).status_code == 200

    resp = await client.put(base, json={"syntax": "jinja", "video_template": "{studio}/{number}.{ext}"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "video_template" in detail
    assert "{studio}" in detail

    # 被拒的字段不落库, 生效模板保持切换时的取值; 合法 Jinja 模板仍可保存
    effective = (await client.get(f"libraries/{library_id}/effective-config")).json()
    assert effective["naming"]["syntax"] == "jinja"
    assert effective["naming"]["video_template"] == _JINJA_DEFAULTS["video_template"]
    ok = await client.put(base, json={"syntax": "jinja", "video_template": "{{ studio }}/{{ number }}.{{ ext }}"})
    assert ok.status_code == 200
    effective = (await client.get(f"libraries/{library_id}/effective-config")).json()
    assert effective["naming"]["video_template"] == "{{ studio }}/{{ number }}.{{ ext }}"


@pytest.mark.asyncio
async def test_schema_endpoint_lists_syntaxes(client: AsyncClient) -> None:
    """path-template-schema 新增 syntaxes: 两套默认视频模板与各自占位符 (含 Jinja 变量映射与 dir 别名)."""
    resp = await client.get("libraries/path-template-schema")
    assert resp.status_code == 200
    body = resp.json()

    syntaxes = {item["name"]: item for item in body["syntaxes"]}
    assert set(syntaxes) == {"native", "jinja"}
    assert syntaxes["native"]["default_video_template"] == body["video_default"]
    assert syntaxes["jinja"]["default_video_template"] == (
        "{{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}"
    )
    native_vars = {p["name"]: p["variable"] for p in syntaxes["native"]["placeholders"]}
    jinja_vars = {p["name"]: p["variable"] for p in syntaxes["jinja"]["placeholders"]}
    assert "dir" in native_vars and "dir" in jinja_vars
    assert jinja_vars["cd?"] == "cd"
    assert jinja_vars["sub?"] == "sub"
    assert jinja_vars["mosaic?"] == "mosaic"
    assert jinja_vars["def?"] == "definition"
    assert jinja_vars["number"] == "number"
    assert len(native_vars) == len(jinja_vars)
    # 顶层占位符清单补上 dir 别名
    assert "dir" in {p["name"] for p in body["placeholders"]}
