"""Library.template_overrides 列与历史库迁移 (规格 §3.1.5).

历史库迁移把全部 15 个模板类字段钉进名单: 库行值 (含 None) 就是迁移时刻的生效输入,
全量保留保证落盘不变; 新建库名单为空, 显式给出的模板字段经 API 路由追加.
落盘级验收 (真实磁盘产物路径) 依赖任务层接线, 在此以 ConfigResolver 的生效值为准.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from alembic import command
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from amane.api.routes import API_PREFIX
from amane.config import HotSettings, NamingConfig
from amane.config.overrides import ConfigResolver
from amane.db.engine import get_session
from amane.db.models import CONFIG_OVERRIDE_SCOPE_LIBRARY, TEMPLATE_OVERRIDE_FIELDS, ConfigOverride
from amane.db.repository import Repository
from tests.api.conftest import make_app
from tests.helpers import alembic_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import ModuleType

    from sqlalchemy.ext.asyncio import AsyncEngine

_PREVIOUS = "9a398bb6ae4d"
_TARGET = "679e7b412ab5"


def _load_migration() -> ModuleType:
    """加载本迁移模块, 直调其数据迁移函数 (versions/ 不是包, 须按文件加载)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "src/amane/db/migrations/versions/679e7b412ab5_library_template_overrides.py"
    )
    spec = importlib.util.spec_from_file_location("amane_test_template_overrides_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_INSERT = (
    "INSERT INTO libraries (name, path, automation, recursive, patterns, move_mode, video_template, "
    "write_nfo, copy_resources, trailer_pattern, blacklist_patterns, subtitle_extensions, min_file_size) "
    "VALUES (:name, :path, 'SCRAPE', 1, '[]', 'MOVE', '{number}.{ext}', 1, "
    "'[\"thumb\"]', '(?i)trailer', '[]', '[\".srt\"]', 0)"
)


def _insert_libraries(db_path: Path, count: int) -> None:
    """升到 _PREVIOUS 后插入 count 个既有库 (thumb_template 为 NULL)."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            for index in range(count):
                conn.execute(text(_INSERT), {"name": f"lib-{index}", "path": f"/m{index}"})
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def migrated_repo(tmp_path: Path) -> AsyncIterator[tuple[Repository, AsyncEngine]]:
    """两个既有库 (thumb_template 均为 NULL) 走完 _PREVIOUS → _TARGET 迁移后的 Repository 与引擎."""
    db_path = tmp_path / "migrate.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, _PREVIOUS)
    _insert_libraries(db_path, 2)
    command.upgrade(cfg, _TARGET)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    yield Repository(engine), engine
    await engine.dispose()


async def _add_override(engine: AsyncEngine, library_id: int, payload: dict[str, Any]) -> None:
    """绕过 CRUD 入口直接落一张覆盖行 (CRUD 入口属任务 14)."""
    async with get_session(engine) as session:
        session.add(
            ConfigOverride(
                scope_type=CONFIG_OVERRIDE_SCOPE_LIBRARY,
                scope_id=library_id,
                section="naming",
                payload=payload,
            )
        )
        await session.commit()


async def _resolve_naming(repo: Repository, library_id: int, settings: HotSettings) -> NamingConfig:
    library = await repo.get_library(library_id)
    assert library is not None
    return await ConfigResolver(settings, repo).resolve(NamingConfig, scope=library, library=library)


def test_migration_pins_all_template_fields(tmp_path: Path) -> None:
    """既有库迁移后 template_overrides 含全部 15 个模板类字段名."""
    db_path = tmp_path / "migrate.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, _PREVIOUS)
    _insert_libraries(db_path, 1)
    command.upgrade(cfg, _TARGET)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT template_overrides FROM libraries")).one()
            names = (
                row.template_overrides
                if isinstance(row.template_overrides, list)
                else json.loads(row.template_overrides)
            )
            assert set(names) == set(TEMPLATE_OVERRIDE_FIELDS)
    finally:
        engine.dispose()


def test_migration_round_trip_repins_all_fields(tmp_path: Path) -> None:
    """重跑结果一致: downgrade 删列后重新 upgrade, 名单重新钉满; 已钉满的行再跑一次数据迁移不改写."""
    db_path = tmp_path / "migrate.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, _PREVIOUS)
    _insert_libraries(db_path, 1)
    command.upgrade(cfg, _TARGET)
    command.downgrade(cfg, _PREVIOUS)
    command.upgrade(cfg, _TARGET)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT template_overrides FROM libraries")).scalar_one()
        migration = _load_migration()
        # 名单已含全部字段: 跳过分支生效, 重跑不改写
        with engine.begin() as conn:
            migration._pin_existing_libraries(conn)
        with engine.connect() as conn:
            after = conn.execute(text("SELECT template_overrides FROM libraries")).scalar_one()
        names = after if isinstance(after, list) else json.loads(after)
        assert set(names) == set(TEMPLATE_OVERRIDE_FIELDS)
        assert after == before
    finally:
        engine.dispose()


def test_pin_preserves_existing_names(tmp_path: Path) -> None:
    """名单写入是逐行并集: 已有自有条目的库重跑数据迁移后条目保留, 已钉满的行不再改写."""
    db_path = tmp_path / "migrate.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, _TARGET)
    _insert_libraries(db_path, 1)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(text('UPDATE libraries SET template_overrides = \'["video_template", "custom"]\''))
        migration = _load_migration()
        with engine.begin() as conn:
            migration._pin_existing_libraries(conn)
        with engine.connect() as conn:
            names = json.loads(conn.execute(text("SELECT template_overrides FROM libraries")).scalar_one())
        assert set(names) == set(TEMPLATE_OVERRIDE_FIELDS) | {"custom"}
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_migrated_library_ignores_global_change(
    migrated_repo: tuple[Repository, AsyncEngine],
) -> None:
    """库行 thumb_template=None 被名单钉住后是显式值: 修改全局 naming.thumb_template 不改变库的生效值."""
    repo, _ = migrated_repo
    settings = HotSettings(naming=NamingConfig(thumb_template="global-thumb/{number}.jpg"))
    resolved = await _resolve_naming(repo, 1, settings)
    assert resolved.thumb_template is None
    # 名单内其余字段仍按库行生效
    assert resolved.video_template == "{number}.{ext}"


@pytest.mark.asyncio
async def test_override_beats_pinned_library_row(
    migrated_repo: tuple[Repository, AsyncEngine],
) -> None:
    """迁移后写 naming 覆盖 → 覆盖生效; 不写覆盖 → 库行生效."""
    repo, engine = migrated_repo
    await _add_override(engine, 1, {"video_template": "{override}/{number}.{ext}"})
    overridden = await _resolve_naming(repo, 1, HotSettings())
    assert overridden.video_template == "{override}/{number}.{ext}"

    inherited = await _resolve_naming(repo, 2, HotSettings())
    assert inherited.video_template == "{number}.{ext}"


@pytest.mark.asyncio
async def test_create_request_explicit_field_pins(tmp_path: Path) -> None:
    """POST /api/libraries: 显式给出的模板字段进名单; 未给出的库名单为空."""
    files = tmp_path / "files"
    with_dir = files / "with-template"
    plain_dir = files / "plain"
    with_dir.mkdir(parents=True)
    plain_dir.mkdir()

    app = make_app(HotSettings(), tmp_path / "data", tmp_path / "logs", files)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://test{API_PREFIX}/") as client:
            created = await client.post(
                "libraries",
                json={
                    "path": str(with_dir),
                    "video_template": "{custom}/{number}.{ext}",
                    "thumb_template": None,
                    "scan": False,
                },
            )
            assert created.status_code == 201
            assert set(created.json()["template_overrides"]) == {"video_template", "thumb_template"}

            plain = await client.post("libraries", json={"path": str(plain_dir), "scan": False})
            assert plain.status_code == 201
            assert plain.json()["template_overrides"] == []
    finally:
        await ctx.__aexit__(None, None, None)
