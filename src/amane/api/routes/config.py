from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ...config import HotSettings, NamingConfig
from ...config.overrides import ConfigResolver
from ...events import Event, EventType
from ...organize.path_templates import NAMING_TEMPLATE_FIELDS, validate_naming_templates
from ..deps import ConfigDep, PluginManagerDep, RuntimeDep

logger = structlog.get_logger()

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_config(config: ConfigDep) -> HotSettings:
    return config.hot


async def _validate_naming_patch(
    req: dict[str, Any], old: NamingConfig, settings: HotSettings, repo: Any
) -> None:
    """naming 节写入的语法感知校验 (规格 3.2.3), 与库级覆盖端点同一错误形态.

    语法切换或模板字段有写入时, 对合并后的生效模板集 (旧值 ∪ 本次写入) 做保存校验加渲染探针,
    残留即拒绝: 全局层是运行时分派语法的源头, 放行会让继承全局模板的库经另一语法后端渲染出字面量.
    语法切换时还须校验**所有库**的生效配置 (联合审核 P2): 名单内钉住库行模板的库不受全局模板
    默认影响, 但 ``syntax`` 继承全局 —— 库行模板是另一语法形态时按错误后端渲染; 任一库校验
    失败即拒绝保存并指出库名与模板.
    """
    new = settings.naming
    patch = req.get("naming")
    if not isinstance(patch, dict):
        return
    if new.syntax == old.syntax and not (set(NAMING_TEMPLATE_FIELDS) & patch.keys()):
        return
    validate_naming_templates(new.syntax, {field: getattr(new, field) for field in NAMING_TEMPLATE_FIELDS}, probe=True)
    if new.syntax == old.syntax:
        return
    resolver = ConfigResolver(settings, repo)
    failures: list[str] = []
    for library in await repo.list_libraries():
        if library.id is None:
            continue
        naming = await resolver.resolve(NamingConfig, scope=library, library=library)
        try:
            validate_naming_templates(
                naming.syntax, {field: getattr(naming, field) for field in NAMING_TEMPLATE_FIELDS}, probe=True
            )
        except ValueError as e:
            failures.append(f"库「{library.name}」(id={library.id}): {e}")
    if failures:
        raise ValueError("语法切换将使以下库的生效模板无法按新语法渲染: " + "; ".join(failures))


@router.patch("")
async def update_config(req: dict[str, Any], config: ConfigDep, runtime: RuntimeDep) -> HotSettings:
    """先用当前会话的插件目录校验新路由和插件配置, 再持久化."""
    changed_sections = list(req.keys())
    logger.info("config update requested", sections=changed_sections)

    try:
        preview = config.preview(req)
        await _validate_naming_patch(req, config.hot.naming, preview, runtime.repo)
        if runtime.plugin_manager is not None:
            runtime.plugin_manager.validate_hot_settings(preview, require_available=False)
        config.update(req)
    except (KeyError, ValidationError, ValueError) as e:
        logger.warning("config update rejected", error=str(e), sections=changed_sections)
        raise HTTPException(status_code=422, detail=str(e)) from e

    await runtime.apply_rebuild()
    logger.info("runtime rebuilt after config update", sections=changed_sections)

    await runtime.event_bus.broadcast(Event(type=EventType.LOG, data={"type": "config.updated"}))

    return config.hot


@router.get("/schema")
async def get_config_schema(plugin_manager: PluginManagerDep) -> dict:
    return plugin_manager.augment_config_schema(HotSettings.model_json_schema())
