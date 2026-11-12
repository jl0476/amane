"""ParseRuleSet 提供者: 按库缓存识别词集 (含全局合成).

版本键取该库相关行 (含全局) 的 ``(count, max(updated_at))`` — 行未变化时复用已合成的
``ParseRuleSet``, 行变化时重建. 规则是 DB 行, 缓存不随 ``AppRuntime._rebuild`` 重建
(那由配置文件变化触发); 规则 CRUD 由调用方显式 ``invalidate``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..parsing import ContentType, ParseRuleSet, ParseRuleSpec, RuleKind
from .models import PARSE_RULE_SCOPE_LIBRARY

if TYPE_CHECKING:
    from .models import ParseRule
    from .repository import Repository


def _spec(row: ParseRule) -> ParseRuleSpec:
    """DB 行 → 纯数据投影; parsing 包不导入 DB, 行到 spec 的映射留在本侧."""
    return ParseRuleSpec(
        kind=RuleKind(row.kind),
        content_type=ContentType(row.content_type),
        pattern=row.pattern,
        created_at=row.created_at,
        id=row.id if row.id is not None else 0,
        library_scope=row.scope_type == PARSE_RULE_SCOPE_LIBRARY,
        enabled=row.enabled,
    )


class ParseRuleProvider:
    """按库提供 ``ParseRuleSet``; 五个生产入口注入与 API 校验共用一个实例."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._cache: dict[int, tuple[tuple[int, datetime | None], ParseRuleSet]] = {}

    async def for_library(self, library_id: int) -> ParseRuleSet:
        """该库的识别词集 (全局 + 库级, 经分桶合成); 行版本变化时重建."""
        version = await self._repo.parse_rules_version(library_id)
        cached = self._cache.get(library_id)
        if cached is not None and cached[0] == version:
            return cached[1]
        rows = await self._repo.list_parse_rules_for_library(library_id)
        rules = ParseRuleSet.from_specs([_spec(row) for row in rows])
        self._cache[library_id] = (version, rules)
        return rules

    def invalidate(self) -> None:
        """规则 CRUD 后清空全部库的缓存."""
        self._cache.clear()
