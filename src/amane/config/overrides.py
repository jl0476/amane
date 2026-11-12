"""配置覆盖层: payload 契约、section 白名单与四层生效配置合成 (规格 §3.1.1 / §3.1.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, RootModel, model_validator

from .manager import HotSettings, NamingConfig, NfoConfig, OrganizeCommonConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..db.models import Library
    from ..db.repository import Repository

type Scope = Library
"""覆盖作用域; 本阶段只有 library, watch_dir 预留 (写入被拒), 对应 config_overrides 的 scope_type/scope_id."""

# db 类型仅作标注 (config 不得在运行期导入 db: db 经 aggregate 反向依赖 config.manager, 会成环).


class _SectionBinding(NamedTuple):
    name: str
    global_layer: Callable[[HotSettings], BaseModel]


_SECTION_BINDINGS: dict[type[BaseModel], _SectionBinding] = {
    OrganizeCommonConfig: _SectionBinding("common", lambda settings: settings.common),
    NamingConfig: _SectionBinding("naming", lambda settings: settings.naming),
    NfoConfig: _SectionBinding("nfo", lambda settings: settings.nfo),
}

SECTION_MODELS: dict[str, type[BaseModel]] = {binding.name: section for section, binding in _SECTION_BINDINGS.items()}
"""section 白名单 → 模型. download / watermark / metadata / provider 预留, parser 不是 section, 写入一律拒绝."""


def _normalize_payload(data: object) -> object:
    """payload 预变换: null 一律拒绝; 顶层空对象映射为 None (「显式取空」哨兵). 其余交由模型校验."""
    if not isinstance(data, dict):
        return data
    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            raise ValueError(f"覆盖 payload 不允许 null 值: {key}")
        if isinstance(value, dict):
            if not value:
                normalized[key] = None
            else:
                _reject_nested_null(value, key)
                normalized[key] = value
            continue
        normalized[key] = value
    return normalized


def _reject_nested_null(value: dict[str, Any], key: str) -> None:
    for nested_key, nested in value.items():
        if nested is None:
            raise ValueError(f"覆盖 payload 不允许 null 值: {key}.{nested_key}")


class OverridePayload(RootModel[dict[str, Any]]):
    """覆盖 payload 的值域契约: 禁止 null (「继承」与「显式关闭」无法同时表达); {} 表示显式取空."""

    @model_validator(mode="before")
    @classmethod
    def _enforce_contract(cls, data: object) -> object:
        return _normalize_payload(data)


def _section_model(section: str) -> type[BaseModel]:
    model = SECTION_MODELS.get(section)
    if model is None:
        raise ValueError(f"section {section!r} 未实现, 可用: {', '.join(sorted(SECTION_MODELS))}")
    return model


def validate_override_payload(section: str, payload: Mapping[str, Any]) -> None:
    """覆盖写入前校验: section 白名单 + payload 契约 + section 模型 (未知键与类型不符在此报错)."""
    model = _section_model(section)
    normalized = OverridePayload.model_validate(dict(payload)).root
    model.model_validate(normalized)


def overlay_value(base: Any, patch: Any) -> Any:
    """逐层合成的基本规则: dict 值逐键深合并, 其余 (含 list 与 None) 整体替换."""
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            merged[key] = overlay_value(merged[key], value) if key in merged else value
        return merged
    return patch


def _apply_layer(merged: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        merged[key] = overlay_value(merged[key], value) if key in merged else value


class ConfigResolver:
    """把全局配置、库行与覆盖合成生效配置.

    四层由低到高: 全局 HotSettings → Library 行 (仅 template_overrides 名单内字段, None 也是显式值)
    → config_overrides 表 → 任务 payload (只含调用方筛出的用户显式字段).
    未登记的 section 类型没有全局层与覆盖表层, 由库行与 payload 直接构造.
    ``repo`` 须提供 ``get_config_override_payload(library_id, section)`` (即 amane.db.repository.Repository).
    """

    def __init__(self, settings: HotSettings, repo: Repository) -> None:
        self._settings = settings
        self._repo = repo

    async def resolve[S: BaseModel](
        self,
        section: type[S],
        *,
        scope: Scope | None = None,
        library: Library | None = None,
        payload: dict[str, Any] | None = None,
    ) -> S:
        """返回该 section 的生效模型; 各层 payload 均过 null 拒绝与 {}→None 契约."""
        binding = _SECTION_BINDINGS.get(section)
        merged: dict[str, Any] = {}
        if binding is not None:
            merged.update(binding.global_layer(self._settings).model_dump())

        if library is not None:
            # 字段名来自持久化名单, 取值属动态字段访问 (类型禁令允许的动态访问范围).
            _apply_layer(
                merged,
                {name: getattr(library, name) for name in library.template_overrides if name in section.model_fields},
            )

        if scope is not None and binding is not None and scope.id is not None:
            stored = await self._repo.get_config_override_payload(scope.id, binding.name)
            if stored is not None:
                _apply_layer(merged, self._normalize(stored))

        if payload is not None:
            _apply_layer(merged, self._normalize(payload))

        return section.model_validate(merged)

    @staticmethod
    def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
        return OverridePayload.model_validate(dict(payload)).root
