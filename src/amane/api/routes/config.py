from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ...config import HotSettings, NamingConfig
from ...events import Event, EventType
from ...organize.path_templates import NAMING_TEMPLATE_FIELDS, validate_naming_templates
from ..deps import ConfigDep, PluginManagerDep, RuntimeDep

logger = structlog.get_logger()

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_config(config: ConfigDep) -> HotSettings:
    return config.hot


def _validate_naming_patch(req: dict[str, Any], old: NamingConfig, new: NamingConfig) -> None:
    """naming 节写入的语法感知校验 (规格 3.2.3), 与库级覆盖端点同一错误形态.

    语法切换或模板字段有写入时, 对合并后的生效模板集 (旧值 ∪ 本次写入) 做保存校验加渲染探针,
    残留即拒绝: 全局层是运行时分派语法的源头, 放行会让继承全局模板的库经另一语法后端渲染出字面量.
    """
    patch = req.get("naming")
    if not isinstance(patch, dict):
        return
    if new.syntax == old.syntax and not (set(NAMING_TEMPLATE_FIELDS) & patch.keys()):
        return
    validate_naming_templates(new.syntax, {field: getattr(new, field) for field in NAMING_TEMPLATE_FIELDS}, probe=True)


@router.patch("")
async def update_config(req: dict[str, Any], config: ConfigDep, runtime: RuntimeDep) -> HotSettings:
    """先用当前会话的插件目录校验新路由和插件配置, 再持久化."""
    changed_sections = list(req.keys())
    logger.info("config update requested", sections=changed_sections)

    try:
        preview = config.preview(req)
        _validate_naming_patch(req, config.hot.naming, preview.naming)
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
