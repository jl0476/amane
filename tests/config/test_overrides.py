"""ConfigResolver 四层合成与覆盖 payload 契约 (规格 §3.1.1 / §3.1.3).

覆盖行与库行由用例经裸 session 直接插入: 本文件只测合成与校验语义,
不测建库 / 覆盖 CRUD 的入口 (分别见 tests/db 与 tests/api).
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from amane.config import HotSettings, MetadataConfig, NamingConfig, WatermarkConfig
from amane.config.overrides import (
    SECTION_MODELS,
    ConfigResolver,
    overlay_value,
    validate_override_payload,
)
from amane.db.engine import get_session
from amane.db.models import CONFIG_OVERRIDE_SCOPE_LIBRARY, ConfigOverride, Library
from amane.db.repository import Repository
from amane.enums import CropStrategy, WatermarkCorner, WatermarkKind
from amane.library import DEFAULT_SUBTITLE_EXTENSIONS
from amane.media.crop import resolve_crop_plan
from amane.parsing import ContentType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_GLOBAL_NAMING = NamingConfig(
    video_template="{global}/{number}.{ext}",
    link_template="global-link/{number}.url",
    thumb_template="global-thumb/{number}.jpg",
)
_SETTINGS = HotSettings(naming=_GLOBAL_NAMING)

#: 四层合成断言关注的投影键.
_PROJECTION_KEYS = ("video_template", "thumb_template", "subtitle_extensions", "link_template")

_DEFAULT_ROW = {"video_template": "{db}/{number}.{ext}", "thumb_template": {}, "subtitle_extensions": [".db"]}
_TASK_ROW = {"video_template": "{task}/{number}.{ext}"}


def _library(**overrides: Any) -> Library:
    """库行样例: 名单含 video/thumb/subtitle_extensions, thumb_template=None 是显式取空."""
    fields: dict[str, Any] = {
        "name": "lib",
        "path": "/m",
        "video_template": "{lib}/{number}.{ext}",
        "thumb_template": None,
        "subtitle_extensions": [".lib"],
        "template_overrides": ["video_template", "thumb_template", "subtitle_extensions"],
    }
    fields.update(overrides)
    return Library(**fields)


async def _seed(engine: AsyncEngine, library: Library, rows: dict[str, dict[str, Any]]) -> Library:
    """插入库行与覆盖行 (section → payload), 返回带 id 的库行."""
    async with get_session(engine) as session:
        session.add(library)
        await session.flush()
        assert library.id is not None
        for section, payload in rows.items():
            session.add(
                ConfigOverride(
                    scope_type=CONFIG_OVERRIDE_SCOPE_LIBRARY,
                    scope_id=library.id,
                    section=section,
                    payload=payload,
                )
            )
        await session.commit()
    return library


def _projection(resolved: NamingConfig) -> dict[str, Any]:
    dump = resolved.model_dump()
    return {key: dump[key] for key in _PROJECTION_KEYS}


@pytest_asyncio.fixture
async def resolver_env(tmp_path: Path) -> AsyncIterator[tuple[Repository, AsyncEngine]]:
    """create_all 建表的独立文件库, 返回 (Repository, engine)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resolver.db'}")
    repo = Repository(engine)
    await repo.initialize()
    yield repo, engine
    await engine.dispose()


def test_section_whitelist_matches_implemented_sections():
    """白名单恰好是五个已实现 section, 且模型都拒绝未知键."""
    assert set(SECTION_MODELS) == {"common", "naming", "nfo", "metadata", "watermark"}
    for model in SECTION_MODELS.values():
        assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(
    ("base", "patch", "expected"),
    [
        pytest.param({"a": {"x": 1}}, {"a": {"y": 2}}, {"a": {"x": 1, "y": 2}}, id="dict_deep_merge"),
        pytest.param(
            {"a": {"x": {"deep": 1}}},
            {"a": {"x": {"deeper": 2}}},
            {"a": {"x": {"deep": 1, "deeper": 2}}},
            id="dict_nested_deep_merge",
        ),
        pytest.param({"a": [1, 2]}, {"a": [3]}, {"a": [3]}, id="list_replaced_wholesale"),
        pytest.param({"a": "old"}, {"a": None}, {"a": None}, id="none_is_explicit_value"),
        pytest.param({"a": 1}, {"b": 2}, {"a": 1, "b": 2}, id="new_key_appended"),
        pytest.param({"a": {"x": 1}}, {"a": "scalar"}, {"a": "scalar"}, id="dict_replaced_by_scalar"),
    ],
)
def test_overlay_value(base: dict[str, Any], patch: dict[str, Any], expected: dict[str, Any]) -> None:
    """dict 值逐键深合并, 其余 (含 list 与 None) 整体替换."""
    assert overlay_value(base, patch) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"link_template": None}, id="nullable_template_field_null"),
        pytest.param({"video_template": None}, id="required_field_null"),
        pytest.param({"unknown_field": "x"}, id="unknown_key"),
    ],
)
async def test_payload_rejects_null_and_unknown_keys(
    resolver_env: tuple[Repository, AsyncEngine], payload: dict[str, Any]
) -> None:
    repo, _ = resolver_env
    resolver = ConfigResolver(_SETTINGS, repo)
    with pytest.raises(ValidationError):
        await resolver.resolve(NamingConfig, payload=payload)


@pytest.mark.asyncio
async def test_override_row_null_rejected(resolver_env: tuple[Repository, AsyncEngine]) -> None:
    """覆盖表读出的 payload 同样过契约: 直接落库的 null 行在解析期被拒."""
    repo, engine = resolver_env
    library = await _seed(engine, _library(), {"naming": {"thumb_template": None}})
    resolver = ConfigResolver(_SETTINGS, repo)
    with pytest.raises(ValidationError):
        await resolver.resolve(NamingConfig, scope=library, library=library)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        pytest.param({"link_template": {}}, "link_template"),
        pytest.param({"thumb_template": {}}, "thumb_template"),
        pytest.param({"strm_content_template": {}}, "strm_content_template"),
    ],
)
async def test_empty_dict_maps_to_none(
    resolver_env: tuple[Repository, AsyncEngine], payload: dict[str, Any], field: str
) -> None:
    """{} 是「显式取空」哨兵: 映射为 None, 而不是继承."""
    repo, _ = resolver_env
    resolver = ConfigResolver(_SETTINGS, repo)
    resolved = await resolver.resolve(NamingConfig, payload=payload)
    assert getattr(resolved, field) is None


@pytest.mark.asyncio
async def test_missing_key_inherits_global(resolver_env: tuple[Repository, AsyncEngine]) -> None:
    """键缺失表示继承: 未出现的字段沿用全局值."""
    repo, _ = resolver_env
    resolver = ConfigResolver(_SETTINGS, repo)
    resolved = await resolver.resolve(NamingConfig, payload={"video_template": "{task}/{number}.{ext}"})
    assert resolved.video_template == "{task}/{number}.{ext}"
    assert resolved.link_template == "global-link/{number}.url"
    assert resolved.thumb_template == "global-thumb/{number}.jpg"


@pytest.mark.asyncio
async def test_empty_dict_sentinel_round_trips_through_table(
    resolver_env: tuple[Repository, AsyncEngine],
) -> None:
    """payload 落库存 {} 哨兵, 解析时映射为 None."""
    repo, engine = resolver_env
    library = await _seed(engine, _library(), {"naming": {"thumb_template": {}}})
    resolver = ConfigResolver(_SETTINGS, repo)
    resolved = await resolver.resolve(NamingConfig, scope=library, library=library)
    assert resolved.thumb_template is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("library_kwargs", "row_payload", "task_payload", "expected"),
    [
        pytest.param(
            None,
            None,
            None,
            {
                "video_template": "{global}/{number}.{ext}",
                "thumb_template": "global-thumb/{number}.jpg",
                "subtitle_extensions": list(DEFAULT_SUBTITLE_EXTENSIONS),
                "link_template": "global-link/{number}.url",
            },
            id="global_only",
        ),
        pytest.param(
            {},
            None,
            None,
            {
                "video_template": "{lib}/{number}.{ext}",
                "thumb_template": None,
                "subtitle_extensions": [".lib"],
                "link_template": "global-link/{number}.url",
            },
            id="library_row_in_list_wins_and_none_is_explicit",
        ),
        pytest.param(
            {"template_overrides": []},
            None,
            None,
            {
                "video_template": "{global}/{number}.{ext}",
                "thumb_template": "global-thumb/{number}.jpg",
                "subtitle_extensions": list(DEFAULT_SUBTITLE_EXTENSIONS),
                "link_template": "global-link/{number}.url",
            },
            id="library_row_out_of_list_yields_to_global",
        ),
        pytest.param(
            {},
            _DEFAULT_ROW,
            None,
            {
                "video_template": "{db}/{number}.{ext}",
                "thumb_template": None,
                "subtitle_extensions": [".db"],
                "link_template": "global-link/{number}.url",
            },
            id="override_table_beats_library_row",
        ),
        pytest.param(
            {},
            {},
            None,
            {
                "video_template": "{lib}/{number}.{ext}",
                "thumb_template": None,
                "subtitle_extensions": [".lib"],
                "link_template": "global-link/{number}.url",
            },
            id="empty_override_row_equals_inheritance",
        ),
        pytest.param(
            {},
            _DEFAULT_ROW,
            _TASK_ROW,
            {
                "video_template": "{task}/{number}.{ext}",
                "thumb_template": None,
                "subtitle_extensions": [".db"],
                "link_template": "global-link/{number}.url",
            },
            id="task_payload_beats_override_table",
        ),
    ],
)
async def test_four_layer_synthesis(
    resolver_env: tuple[Repository, AsyncEngine],
    library_kwargs: dict[str, Any] | None,
    row_payload: dict[str, Any] | None,
    task_payload: dict[str, Any] | None,
    expected: dict[str, Any],
) -> None:
    """全局 < 库行 (仅名单内) < 覆盖表 < 任务; {} 哨兵与 list 整体替换随层生效."""
    repo, engine = resolver_env
    library = None
    if library_kwargs is not None:
        rows = {"naming": row_payload} if row_payload is not None else {}
        library = await _seed(engine, _library(**library_kwargs), rows)

    resolver = ConfigResolver(_SETTINGS, repo)
    resolved = await resolver.resolve(
        NamingConfig,
        scope=library,
        library=library,
        payload=task_payload,
    )
    assert _projection(resolved) == expected


@pytest.mark.parametrize("section", ["download", "provider", "parser"])
def test_validate_rejects_reserved_sections(section: str) -> None:
    """白名单外的预留 section 一律拒绝; parser 不是 section; watermark 已激活不再在此列."""
    with pytest.raises(ValueError, match="未实现"):
        validate_override_payload(section, {})


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param({"video_template": None}, "null", id="null_rejected"),
        pytest.param({"no_such_field": "x"}, None, id="unknown_key_rejected"),
        pytest.param({"write_nfo": []}, None, id="type_mismatch_rejected"),
        pytest.param({"video_template": {}}, None, id="empty_dict_on_required_field_rejected"),
    ],
)
def test_validate_rejects_invalid_payloads(payload: dict[str, Any], match: str | None) -> None:
    """null / 未知键 / 类型不符 / 不可空字段上的 {} 都被拒, 合法 {} 哨兵通过."""
    with pytest.raises(ValidationError, match=match):
        validate_override_payload("naming", payload)
    validate_override_payload("naming", {"thumb_template": {}})


# --- 任务 4: metadata 节激活 (规格 §3.1) ---


def test_metadata_config_defaults() -> None:
    """MetadataConfig 默认值 (规格 §3.1 表): 稀疏默认矩阵 / legacy / priority / None / False."""
    config = MetadataConfig()
    assert config.crop_type == {
        ContentType.CENSORED: CropStrategy.RIGHT,
        ContentType.UNCENSORED: CropStrategy.RIGHT,
    }
    assert all(isinstance(strategy, CropStrategy) for strategy in config.crop_type.values())
    assert config.crop_aspect == "legacy"
    assert config.thumb_download_strategy == "priority"
    assert config.crop_low_res_height is None
    assert config.extra_fanarts_behind_scenes is False


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"crop_type": {}}, id="empty_dict_sentinel_not_for_crop_type"),
        pytest.param({"crop_type": {"mosaic": "right"}}, id="unknown_content_type_key"),
        pytest.param({"crop_type": {"censored": "bogus"}}, id="unknown_strategy_value"),
        pytest.param({"crop_type": None}, id="null_rejected"),
        pytest.param({"crop_aspect": "cinema"}, id="unknown_aspect"),
        pytest.param({"thumb_download_strategy": "fastest"}, id="unknown_download_strategy"),
        pytest.param({"crop_low_res_height": "high"}, id="non_int_threshold"),
        pytest.param({"no_such_field": True}, id="unknown_key"),
    ],
)
def test_metadata_payload_rejects_invalid(payload: dict[str, Any]) -> None:
    """crop_type 值 {} / 未知 ContentType 键 / 非法策略值 / null / 越界字面量 / 未知键全部拒绝."""
    with pytest.raises(ValidationError):
        validate_override_payload("metadata", payload)


def test_metadata_payload_accepts_sparse_crop_type() -> None:
    """稀疏 crop_type 键集非冻结 (任意 ContentType 键合法); 可空阈值字段的 {} 哨兵通过."""
    validate_override_payload("metadata", {"crop_type": {"fc2": "face"}})
    validate_override_payload("metadata", {"crop_low_res_height": {}})


@pytest.mark.parametrize("content_type", list(ContentType))
def test_metadata_default_matrix_matches_crop_fallback(content_type: ContentType) -> None:
    """R-default-matrix 守卫: config 默认矩阵喂入 resolve_crop_plan 的有效策略, 与其缺键回退矩阵一致 (防跨层漂移)."""
    config = MetadataConfig()
    with_config = resolve_crop_plan(
        content_type,
        strategy_map=config.crop_type,
        aspect=config.crop_aspect,
        low_res_height=config.crop_low_res_height,
        poster_ratio=0.7,
        skip_ratio=0.9,
        thumb_size=None,
        candidate_size=None,
    )
    fallback = resolve_crop_plan(
        content_type,
        strategy_map={},
        aspect=config.crop_aspect,
        low_res_height=config.crop_low_res_height,
        poster_ratio=0.7,
        skip_ratio=0.9,
        thumb_size=None,
        candidate_size=None,
    )
    assert with_config.requested == fallback.requested


# --- 任务 6: watermark 节激活 (规格 §3.1) ---


def test_watermark_config_defaults() -> None:
    """WatermarkConfig 默认值矩阵 (规格 §3.1 表): 全部默认组合起来 = 现状行为."""
    config = WatermarkConfig()
    assert config.enabled is False
    assert config.scale == 0.08
    assert config.kinds == dict.fromkeys(WatermarkKind, True)
    assert config.corners == dict.fromkeys(WatermarkKind, WatermarkCorner.TOP_LEFT)
    assert config.layout == "stack"
    assert config.offset_x == 0
    assert config.offset_y == 0
    assert config.gap is None
    assert config.start == 0
    assert config.style == "default"
    assert config.custom_folder is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kinds": {"subtitle": "maybe"}}, id="kinds_non_bool_string"),
        pytest.param({"kinds": {"subtitle": [True]}}, id="kinds_non_bool_container"),
        pytest.param({"corners": {"subtitle": "middle"}}, id="corners_unknown_corner"),
        pytest.param({"layout": "diagonal"}, id="layout_unknown_literal"),
        pytest.param({"scale": 0.5}, id="scale_out_of_range"),
        pytest.param({"offset_x": -1}, id="offset_x_negative"),
        pytest.param({"offset_y": -1}, id="offset_y_negative"),
        pytest.param({"gap": -1}, id="gap_negative"),
        pytest.param({"start": 4}, id="start_out_of_range"),
        pytest.param({"style": "Big Style"}, id="style_charset_rejected"),
        pytest.param({"enabled": None}, id="null_rejected"),
        pytest.param({"targets": ["bogus"]}, id="targets_unknown_member_rejected"),
        pytest.param({"no_such_field": True}, id="unknown_key"),
    ],
)
def test_watermark_invalid_payload_rejected(payload: dict[str, Any]) -> None:
    """kinds/corners/layout/offset/gap/start/style/custom_folder 越界与未知键全部拒; targets 现阶段是未知键."""
    with pytest.raises(ValidationError):
        validate_override_payload("watermark", payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"scale": 0.1}, id="plain_field"),
        pytest.param({"kinds": {"subtitle": False}}, id="sparse_kinds_keys_completed"),
        pytest.param({"corners": {"definition": "auto"}}, id="corners_auto_value"),
        pytest.param({"corners": {"gone": "top_left"}}, id="unknown_kind_key_dropped"),
        pytest.param({"gap": 0, "start": 3}, id="boundary_zero_gap_and_top_start"),
        pytest.param({"custom_folder": "/tmp/stamps"}, id="custom_folder_path"),
        pytest.param({}, id="empty_payload_inherits"),
    ],
)
def test_watermark_payload_accepts_valid(payload: dict[str, Any]) -> None:
    """合法 payload 通过: 稀疏 kinds/corners 键补齐, auto 角位与边界值放行."""
    validate_override_payload("watermark", payload)
