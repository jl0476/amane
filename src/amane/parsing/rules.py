"""识别词规则: ParseRuleSet 的作用域合成与扁平匹配 (规格 §3.1.6).

规则只覆盖类型判定, 不改写番号. 匹配序列在构造时一次性合成:
按 ``(kind, content_type)`` 分桶, 库级桶有启用规则则整体替换全局同桶,
之后排成单一序列 (番号段全部先于路径段, 段内 ``created_at`` 升序、同刻按 ``id`` 升序),
逐条尝试, 首条命中即生效.

匹配引擎为 RE2 (``google-re2``): 匹配时间与输入长度成线性关系, 从引擎层消除灾难回溯;
反向引用 / 环视等 RE2 不支持的构造在编译期即拒 (保存与构造都经 ``compile_rule_pattern`` 校验),
Python ``re`` 不参与识别词匹配. ``parsing/`` 保持纯函数: 本模块不做 I/O,
规则集由调用方从 DB 行构造 (``ParseRuleSpec`` 为行投影) 后传入;
对 ``file_info`` 只做类型引用 (运行时不导入), 避免与其形成导入环.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import re2

if TYPE_CHECKING:
    from .file_info import ContentType, FileInfo

PATTERN_MAX_LENGTH = 256
"""pattern 长度上限; 定位是防滥用而非防回溯 (回溯由 RE2 线性时间保证)."""

REGEX_PREFIX = "r:"
"""正则前缀: 带此前缀的 pattern 按 RE2 正则编译, 其余按字面关键词 (大小写不敏感) 匹配."""


class RuleKind(StrEnum):
    """识别词的两段: 番号关键词先于路径关键词, 是唯一显式优先级."""

    NUMBER_KEYWORD = "number_keyword"
    """匹配已识别的番号."""
    PATH_KEYWORD = "path_keyword"
    """匹配完整路径 (含文件名)."""


class InvalidRuleError(ValueError):
    """识别词非法 (超长 / 空串 / 正则编译失败 / RE2 不支持的构造); API 层映射为 422."""


def compile_rule_pattern(pattern: str) -> re2._Regexp | None:
    """校验并编译识别词 pattern; 字面关键词返回 None. 非法抛 ``InvalidRuleError``."""
    if len(pattern) > PATTERN_MAX_LENGTH:
        raise InvalidRuleError(f"识别词 pattern 超过 {PATTERN_MAX_LENGTH} 字符上限")
    if not pattern.startswith(REGEX_PREFIX):
        if not pattern.strip():
            raise InvalidRuleError("识别词 pattern 不能为空")
        return None
    body = pattern[len(REGEX_PREFIX) :]
    if not body:
        raise InvalidRuleError("正则识别词缺少表达式")
    try:
        return re2.compile(body)
    except re2.error as exc:
        message = exc.args[0] if exc.args else exc
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        raise InvalidRuleError(f"识别词正则无法编译 (RE2 不支持的构造或语法错误): {message}") from exc


@dataclass(frozen=True)
class ParseRuleSpec:
    """单条识别词的纯数据投影 (DB 行 → 本结构), 是 ``ParseRuleSet`` 合成的输入."""

    kind: RuleKind
    content_type: ContentType
    pattern: str
    created_at: datetime
    """扁平排序主键: 段内升序."""
    id: int = 0
    """排序 tie-break: 同 created_at 按 id 升序."""
    library_scope: bool = False
    """库级作用域; False 为全局."""
    enabled: bool = True


@dataclass(frozen=True)
class _LiteralMatcher:
    """字面关键词: 双方 casefold 后做子串匹配."""

    keyword: str

    def matches(self, text: str) -> bool:
        return self.keyword in text.casefold()


@dataclass(frozen=True)
class _RegexMatcher:
    """r: 前缀的 RE2 正则: 按原始字符串匹配, 大小写语义由 pattern 自行声明 (?i)."""

    regex: re2._Regexp

    def matches(self, text: str) -> bool:
        return self.regex.search(text) is not None


@dataclass(frozen=True)
class _Rule:
    """已编译的识别词; 合成与排序已在 ``ParseRuleSet`` 构造时完成."""

    kind: RuleKind
    content_type: ContentType
    matcher: _LiteralMatcher | _RegexMatcher

    @classmethod
    def from_spec(cls, spec: ParseRuleSpec) -> _Rule:
        compiled = compile_rule_pattern(spec.pattern)
        matcher = _RegexMatcher(compiled) if compiled is not None else _LiteralMatcher(spec.pattern.casefold())
        return cls(kind=spec.kind, content_type=spec.content_type, matcher=matcher)


@dataclass(frozen=True)
class ParseRuleSet:
    """已合成、已排序的识别词序列 (番号段 → 路径段); 匹配只依据此序列, 不再接触作用域概念."""

    rules: tuple[_Rule, ...] = ()

    @classmethod
    def from_specs(cls, specs: Sequence[ParseRuleSpec]) -> ParseRuleSet:
        """分桶合成 (库级覆盖全局) 后按扁平序列编译排序; 非法 pattern 抛 ``InvalidRuleError``."""
        buckets: dict[tuple[RuleKind, ContentType], list[ParseRuleSpec]] = {}
        for spec in specs:
            if not spec.enabled:
                continue
            buckets.setdefault((spec.kind, spec.content_type), []).append(spec)

        chosen: list[ParseRuleSpec] = []
        for bucket in buckets.values():
            library_rows = [spec for spec in bucket if spec.library_scope]
            chosen.extend(library_rows or [spec for spec in bucket if not spec.library_scope])

        def order_key(spec: ParseRuleSpec) -> tuple[datetime, int]:
            return (spec.created_at, spec.id)

        number_rules = sorted((spec for spec in chosen if spec.kind is RuleKind.NUMBER_KEYWORD), key=order_key)
        path_rules = sorted((spec for spec in chosen if spec.kind is RuleKind.PATH_KEYWORD), key=order_key)
        return cls(rules=tuple(_Rule.from_spec(spec) for spec in (*number_rules, *path_rules)))


def apply_rules(path: str | Path | None, info: FileInfo, rules: ParseRuleSet) -> FileInfo:
    """按扁平序列逐条尝试, 首条命中即把类型置为该规则的 ``content_type``.

    番号段匹配 ``info.number`` (未识别出番号时跳过); 路径段匹配调用方传入的原始路径字符串
    (不是 ``path_key`` 归一后的形式, 归一会改变大小写), ``path=None`` 时路径段整体跳过.
    """
    path_text = None if path is None else str(path)
    for rule in rules.rules:
        if rule.kind is RuleKind.NUMBER_KEYWORD:
            if info.number is not None and rule.matcher.matches(info.number):
                return replace(info, content_type=rule.content_type)
        elif path_text is not None and rule.matcher.matches(path_text):
            return replace(info, content_type=rule.content_type)
    return info
