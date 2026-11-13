"""parse_rules 表的读写与值域校验 (规格 §3.1.6).

写路径统一校验: pattern 经 RE2 编译 (parsing.rules.compile_rule_pattern), 类型限
RULE_CONTENT_TYPES 值域; 重复键以 UNIQUE 约束拦截, IntegrityError 由 API 层映射 409.
"""

from datetime import UTC, datetime

from sqlalchemy import and_, func, or_
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ...parsing import ContentType, RuleKind, compile_rule_pattern
from ..models import (
    PARSE_RULE_SCOPE_GLOBAL,
    PARSE_RULE_SCOPE_LIBRARY,
    RULE_CONTENT_TYPES,
    Library,
    ParseRule,
)
from .base import RepositoryMixinBase


def _validate_fields(content_type: ContentType, pattern: str) -> None:
    """类型值域与 pattern 编译校验; 非法抛 ValueError (API 层映射 422)."""
    if content_type not in RULE_CONTENT_TYPES:
        allowed = ", ".join(item.value for item in RULE_CONTENT_TYPES)
        raise ValueError(f"识别词类型不在值域内: {content_type.value} (允许: {allowed})")
    compile_rule_pattern(pattern)


async def _validated_scope(session: AsyncSession, scope_type: str, scope_id: int) -> int:
    """作用域校验: 全局恒为 0; 库级必须指向存在的库. watch_dir 等预留值拒绝."""
    if scope_type == PARSE_RULE_SCOPE_GLOBAL:
        return 0
    if scope_type == PARSE_RULE_SCOPE_LIBRARY:
        if scope_id < 1 or await session.get(Library, scope_id) is None:
            raise ValueError(f"识别词作用域库不存在: {scope_id}")
        return scope_id
    raise ValueError(f"识别词作用域类型不支持: {scope_type}")


class ParseRulesRepoMixin(RepositoryMixinBase):
    async def list_parse_rules(self) -> list[ParseRule]:
        """全部规则行, 按 id 升序保证响应稳定."""
        async with self._session() as session:
            stmt = select(ParseRule).order_by(col(ParseRule.id))
            return list((await session.exec(stmt)).all())

    async def list_parse_rules_for_library(self, library_id: int) -> list[ParseRule]:
        """该库相关行 = 全局行 + 该库的库级行, 按 id 升序."""
        async with self._session() as session:
            stmt = (
                select(ParseRule)
                .where(
                    or_(
                        col(ParseRule.scope_type) == PARSE_RULE_SCOPE_GLOBAL,
                        and_(
                            col(ParseRule.scope_type) == PARSE_RULE_SCOPE_LIBRARY,
                            col(ParseRule.scope_id) == library_id,
                        ),
                    )
                )
                .order_by(col(ParseRule.id))
            )
            return list((await session.exec(stmt)).all())

    async def parse_rules_version(self, library_id: int) -> tuple[int, datetime | None]:
        """该库相关行 (含全局) 的版本键 ``(count, max(updated_at))``; 无行时 max 为 None."""
        async with self._session() as session:
            stmt = select(func.count(), func.max(col(ParseRule.updated_at))).where(
                or_(
                    col(ParseRule.scope_type) == PARSE_RULE_SCOPE_GLOBAL,
                    and_(
                        col(ParseRule.scope_type) == PARSE_RULE_SCOPE_LIBRARY,
                        col(ParseRule.scope_id) == library_id,
                    ),
                )
            )
            row = (await session.exec(stmt)).one()
            return int(row[0]), row[1]

    async def get_parse_rule(self, rule_id: int) -> ParseRule | None:
        async with self._session() as session:
            return await session.get(ParseRule, rule_id)

    async def create_parse_rule(
        self,
        *,
        scope_type: str,
        scope_id: int,
        kind: RuleKind,
        content_type: ContentType,
        pattern: str,
        enabled: bool = True,
    ) -> ParseRule:
        _validate_fields(content_type, pattern)
        async with self._session() as session:
            row = ParseRule(
                scope_type=scope_type,
                scope_id=await _validated_scope(session, scope_type, scope_id),
                kind=kind,
                content_type=content_type,
                pattern=pattern,
                enabled=enabled,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def update_parse_rule(
        self,
        rule_id: int,
        *,
        scope_type: str,
        scope_id: int,
        kind: RuleKind,
        content_type: ContentType,
        pattern: str,
        enabled: bool = True,
    ) -> ParseRule | None:
        """整体替换; 不存在返回 None. 重复键以 UNIQUE 拦截 (IntegrityError 上抛)."""
        _validate_fields(content_type, pattern)
        async with self._session() as session:
            row = await session.get(ParseRule, rule_id)
            if row is None:
                return None
            row.scope_type = scope_type
            row.scope_id = await _validated_scope(session, scope_type, scope_id)
            row.kind = kind
            row.content_type = content_type
            row.pattern = pattern
            row.enabled = enabled
            row.updated_at = datetime.now(UTC)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def delete_parse_rule(self, rule_id: int) -> bool:
        """无行返回 False, 调用方映射 404."""
        async with self._session() as session:
            row = await session.get(ParseRule, rule_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True
