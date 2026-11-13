"""配置 API 端点的集成测试"""

import asyncio
from typing import TYPE_CHECKING

import pytest

from amane.config import HotSettings
from amane.organize.template_jinja import JINJA_TEMPLATE_DEFAULTS, JINJA_VIDEO_TEMPLATE_DEFAULT
from tests.api.conftest import hot_for_tests

if TYPE_CHECKING:
    from httpx2 import AsyncClient

_JINJA_NAMING = {"syntax": "jinja", "video_template": JINJA_VIDEO_TEMPLATE_DEFAULT, **JINJA_TEMPLATE_DEFAULTS}


class TestConfigHttp:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_get_patch_schema(self, client: AsyncClient):
        resp = await client.get("config")
        assert resp.status_code == 200
        assert HotSettings.model_validate(resp.json()) == hot_for_tests()
        data = resp.json()
        assert isinstance(data["scraping"]["download_resources"], list)
        assert isinstance(data["worker"]["concurrency"], int)
        assert isinstance(data["worker"]["poll_interval"], float)

        schema = await client.get("config/schema")
        assert schema.status_code == 200
        assert schema.json()["type"] == "object"
        assert "properties" in schema.json()

        empty = await client.patch("config", json={})
        assert empty.status_code == 200
        assert HotSettings.model_validate(empty.json()) == hot_for_tests()
        assert (await client.patch("config", json={"worker": {"concurrency": 999}})).status_code == 422

        patched = await client.patch(
            "config",
            json={
                "network": {"proxy": "http://new"},
                "worker": {"concurrency": 8},
                "scraping": {
                    "crop_poster": False,
                    "field_priority": {"actors": ["javdb", "theporndb"]},
                    "field_blacklist": {"title": ["javbus"]},
                },
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["worker"]["concurrency"] == 8
        assert body["scraping"]["crop_poster"] is False
        assert body["scraping"]["field_priority"]["actors"] == ["javdb", "theporndb"]
        assert body["scraping"]["field_blacklist"]["title"] == ["javbus"]

        await client.patch("config", json={"network": {"proxy": "socks5://test"}})
        got = await client.get("config")
        assert got.json()["network"]["proxy"] == "socks5://test"
        assert got.json()["worker"]["concurrency"] == 8


class TestConfigNamingValidation:
    """PATCH /config 的 naming 节执行语法感知校验 (规格 3.2.3): 残留即 422 且不落库."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_switch_with_native_default_templates_rejected(self, client: AsyncClient):
        """全局语法 native→jinja 而全局模板仍为 native 默认字面量 → 422 含模板字段与残留, 全局配置不变."""
        resp = await client.patch("config", json={"naming": {"syntax": "jinja"}})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "video_template" in detail
        assert "{studio}" in detail

        got = (await client.get("config")).json()
        assert got["naming"]["syntax"] == "native"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_switch_with_jinja_templates_accepted(self, client: AsyncClient):
        """全局模板随切换一并写入合法 Jinja 默认 → 200, 全局语法与模板更新."""
        resp = await client.patch("config", json={"naming": _JINJA_NAMING})
        assert resp.status_code == 200

        got = (await client.get("config")).json()
        assert got["naming"]["syntax"] == "jinja"
        assert got["naming"]["video_template"] == JINJA_VIDEO_TEMPLATE_DEFAULT

    @pytest.mark.asyncio(loop_scope="function")
    async def test_same_syntax_template_residual_rejected(self, client: AsyncClient):
        """语法未变但写入的模板携带另一语法残留 → 422 含模板字段与残留, 全局配置不变."""
        assert (await client.patch("config", json={"naming": _JINJA_NAMING})).status_code == 200

        resp = await client.patch("config", json={"naming": {"video_template": "{studio}/{number}.{ext}"}})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "video_template" in detail
        assert "{studio}" in detail

        got = (await client.get("config")).json()
        assert got["naming"]["video_template"] == JINJA_VIDEO_TEMPLATE_DEFAULT


class TestConfigRebuild:
    """PUT /config 触发 runtime rebuild + event broadcast"""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_update_triggers_worker_rebuild(self, client: AsyncClient, app):
        old_concurrency = app.state.runtime.worker._concurrency

        resp = await client.patch("config", json={"worker": {"concurrency": 3}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["concurrency"] == 3

        new_concurrency = app.state.runtime.worker._concurrency
        assert new_concurrency == 3
        assert new_concurrency != old_concurrency

    @pytest.mark.asyncio(loop_scope="function")
    async def test_update_preserves_worker_functionality(self, client: AsyncClient, safe_path):
        scan_dir = safe_path / "after-rebuild"
        scan_dir.mkdir()

        await client.patch("config", json={"worker": {"concurrency": 5}})

        lib = (await client.post("libraries", json={"path": str(scan_dir)})).json()
        resp = await client.post("tasks", json={"type": "refresh", "library_id": lib["id"]})
        task_id = resp.json()["id"]

        for _ in range(40):
            check = await client.get(f"tasks/{task_id}")
            if check.json()["status"] in ("done", "failed"):
                break
            await asyncio.sleep(0.05)

        final = await client.get(f"tasks/{task_id}")
        assert final.json()["status"] == "done"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_r18_dsn_change_rebuilds_read_engine(self, client: AsyncClient, app):
        runtime = app.state.runtime
        assert runtime.r18_db is None

        resp = await client.patch(
            "config", json={"r18": {"dsn": "postgresql://u:p@127.0.0.1:5432/postgres", "db_name": "r18dev"}}
        )
        assert resp.status_code == 200

        assert runtime.r18_db is not None
        assert runtime.r18_handle is not None
        assert runtime.r18_handle.engine is runtime.r18_db
        crawler = await runtime.factory.get("r18dev")
        assert crawler is not None
        assert crawler._db is runtime.r18_db

    @pytest.mark.asyncio(loop_scope="function")
    async def test_browser_change_rebuilds_pool_and_unrelated_change_keeps_it(self, client: AsyncClient, app):
        runtime = app.state.runtime
        pool = runtime.browser
        assert pool is not None

        resp = await client.patch("config", json={"network": {"browser": {"backend": "solver"}}})
        assert resp.status_code == 200

        rebuilt = runtime.browser
        assert rebuilt is not None
        assert rebuilt is not pool

        # 旧池随使用它的退役 worker 排空后关闭
        await asyncio.gather(*list(runtime._retire_tasks), return_exceptions=True)
        assert pool._closed is True

        resp = await client.patch("config", json={"worker": {"concurrency": 4}})
        assert resp.status_code == 200
        assert runtime.browser is rebuilt
        assert rebuilt._closed is False
