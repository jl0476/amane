"""识别词 ParseRuleSet 表测试: 作用域分桶合成、扁平匹配序列、RE2 引擎边界.

规则只覆盖类型判定; 番号段先于路径段, 段内 created_at 升序 (同刻按 id), 首条命中即生效.
"""

import time
from datetime import UTC, datetime
from typing import NamedTuple

import pytest

from amane.parsing import ContentType, parse_file_info
from amane.parsing.rules import (
    PATTERN_MAX_LENGTH,
    InvalidRuleError,
    ParseRuleSet,
    ParseRuleSpec,
    RuleKind,
    apply_rules,
    compile_rule_pattern,
)


def _ts(minute: int) -> datetime:
    return datetime(2026, 1, 1, 0, minute, tzinfo=UTC)


def _spec(
    kind: RuleKind,
    content_type: ContentType,
    pattern: str,
    *,
    created_at: datetime,
    id: int,
    library_scope: bool = False,
    enabled: bool = True,
) -> ParseRuleSpec:
    return ParseRuleSpec(
        kind=kind,
        content_type=content_type,
        pattern=pattern,
        library_scope=library_scope,
        enabled=enabled,
        created_at=created_at,
        id=id,
    )


def _number(pattern: str, content_type: ContentType, *, minute: int, id: int, **kw: bool) -> ParseRuleSpec:
    return _spec(RuleKind.NUMBER_KEYWORD, content_type, pattern, created_at=_ts(minute), id=id, **kw)


def _path(pattern: str, content_type: ContentType, *, minute: int, id: int, **kw: bool) -> ParseRuleSpec:
    return _spec(RuleKind.PATH_KEYWORD, content_type, pattern, created_at=_ts(minute), id=id, **kw)


class _OrderCase(NamedTuple):
    path: str
    expected: ContentType


def test_rule_set_flat_order_and_first_hit() -> None:
    """番号段全部先于路径段 (不看 created_at); 段内 created_at 升序, 同刻按 id 升序; 首条命中即生效."""
    rules = ParseRuleSet.from_specs(
        [
            # 路径段规则 created_at 最早, 但番号段命中时不得生效
            _path("tokyo", ContentType.UNCENSORED, minute=0, id=3),
            _number("MIDV", ContentType.CHINESE, minute=10, id=1),
            _number("MIDV", ContentType.WESTERN, minute=20, id=2),
            # 同刻: id 小者生效
            _number("ABC", ContentType.WESTERN, minute=30, id=5),
            _number("ABC", ContentType.CHINESE, minute=30, id=4),
        ]
    )

    cases = [
        _OrderCase("/data/tokyo/MIDV-123.mp4", ContentType.CHINESE),
        _OrderCase("/data/MIDV-123.mp4", ContentType.CHINESE),
        _OrderCase("/data/ABC-123.mp4", ContentType.CHINESE),
        # 番号规则未命中: 路径段生效 (大小写不敏感)
        _OrderCase("/data/TOKYO/SSIS-456.mp4", ContentType.UNCENSORED),
        _OrderCase("/data/plain/SSIS-456.mp4", ContentType.CENSORED),
    ]
    for case in cases:
        info = parse_file_info(case.path, rules=rules)
        assert info.content_type is case.expected, case.path


def test_same_pattern_two_types_earlier_wins() -> None:
    """同一 pattern 分属 chinese / western 两个桶 → 扁平序列里 created_at 早者生效."""
    rules = ParseRuleSet.from_specs(
        [
            _number("ABP", ContentType.WESTERN, minute=20, id=2),
            _number("ABP", ContentType.CHINESE, minute=10, id=1),
        ]
    )
    info = parse_file_info("/data/ABP-123.mp4", rules=rules)
    assert info.number == "ABP-123"
    assert info.content_type is ContentType.CHINESE


def test_library_scope_replaces_global_per_bucket() -> None:
    """库级 (kind, content_type) 桶有启用规则 → 整体替换全局同桶; 其余桶用全局; 禁用行不算启用规则."""
    specs = [
        # (number, western) 桶: 被库级同桶整体替换
        _number("MIDV", ContentType.WESTERN, minute=0, id=1),
        # (path, uncensored) 桶: 库级无此桶 → 全局生效
        _path("tokyo", ContentType.UNCENSORED, minute=0, id=2),
        # (number, uncensored) 桶: 库级该桶只有禁用行 → 全局生效
        _number("HEYZO", ContentType.UNCENSORED, minute=0, id=3),
        _number("SSIS", ContentType.WESTERN, minute=10, id=4, library_scope=True),
        # 禁用行: 该桶视为「库级无启用规则」
        _number("DISABLED", ContentType.AMATEUR, minute=10, id=5, library_scope=True, enabled=False),
    ]
    rules = ParseRuleSet.from_specs(specs)

    cases = [
        # (number, western) 桶只剩库级: 全局 MIDV 被排除, 库级 SSIS 生效
        _OrderCase("/data/MIDV-123.mp4", ContentType.CENSORED),
        _OrderCase("/data/SSIS-456.mp4", ContentType.WESTERN),
        # 其余桶用全局
        _OrderCase("/data/tokyo/ABC-123.mp4", ContentType.UNCENSORED),
        _OrderCase("/data/HEYZO-123.mp4", ContentType.UNCENSORED),
    ]
    for case in cases:
        info = parse_file_info(case.path, rules=rules)
        assert info.content_type is case.expected, case.path


def test_disabled_rule_never_matches() -> None:
    """全局禁用行同样不参与匹配与合成."""
    rules = ParseRuleSet.from_specs(
        [
            _number("MIDV", ContentType.CHINESE, minute=0, id=1, enabled=False),
            _path("tokyo", ContentType.UNCENSORED, minute=0, id=2, enabled=False),
        ]
    )
    assert parse_file_info("/data/tokyo/MIDV-123.mp4", rules=rules).content_type is ContentType.CENSORED


def test_apply_rules_needs_path_for_path_rules() -> None:
    """apply_rules(path=None) 跳过路径规则, 番号规则照常; 路径匹配对字面关键词 casefold."""
    rules = ParseRuleSet.from_specs(
        [
            _number("midv", ContentType.CHINESE, minute=0, id=1),
            _path("TOKYO HOT", ContentType.UNCENSORED, minute=0, id=2),
        ]
    )
    info = parse_file_info(text="MIDV-123")

    # path=None: 路径规则不参与, 番号规则命中 (字面关键词大小写不敏感)
    applied = apply_rules(None, info, rules)
    assert applied.content_type is ContentType.CHINESE

    # 路径规则: 字面关键词 casefold 后做子串匹配
    no_number = parse_file_info(text="unrecognized string")
    by_path = apply_rules("/data/Tokyo Hot/ABC-123.mp4", no_number, rules)
    assert by_path.content_type is ContentType.UNCENSORED
    # 大小写归一只发生在匹配实现内, FileInfo 原始内容不变
    assert by_path.number == no_number.number

    # 未命中任何规则 → 原样返回
    untouched = apply_rules("/data/other/ABC-123.mp4", no_number, rules)
    assert untouched == no_number


def test_apply_rules_empty_set_is_noop() -> None:
    info = parse_file_info(text="MIDV-123")
    assert apply_rules(None, info, ParseRuleSet()) == info


def test_parse_file_info_accepts_rules() -> None:
    """path 分支在返回前应用规则; text= 分支只应用番号规则, 跳过路径规则."""
    rules = ParseRuleSet.from_specs(
        [
            _number("MIDV", ContentType.CHINESE, minute=0, id=1),
            _path("tokyo", ContentType.UNCENSORED, minute=0, id=2),
        ]
    )

    # path 分支: 番号规则覆盖内置判定 (含目录关键词 欧美 → western 的内置结果)
    from_path = parse_file_info("/data/欧美/MIDV-123.mp4", rules=rules)
    assert from_path.number == "MIDV-123"
    assert from_path.content_type is ContentType.CHINESE

    # text 分支: path=None, 路径规则跳过
    from_text = parse_file_info(text="MIDV-123 tokyo", rules=rules)
    assert from_text.number == "MIDV-123"
    assert from_text.content_type is ContentType.CHINESE


def test_invalid_regex_rejected_and_length_capped() -> None:
    """RE2 编译失败抛 InvalidRuleError (API 层映射 422); pattern 超 256 字符拒绝; 字面 pattern 返回 None."""
    with pytest.raises(InvalidRuleError):
        compile_rule_pattern("r:([unclosed")
    with pytest.raises(InvalidRuleError):
        compile_rule_pattern("r:")
    with pytest.raises(InvalidRuleError):
        compile_rule_pattern("")
    with pytest.raises(InvalidRuleError):
        compile_rule_pattern("a" * (PATTERN_MAX_LENGTH + 1))
    with pytest.raises(InvalidRuleError):
        compile_rule_pattern("r:" + "a" * (PATTERN_MAX_LENGTH + 1))

    assert compile_rule_pattern("MIDV") is None
    assert compile_rule_pattern("r:ab+") is not None

    # 构造 ParseRuleSet 时同样拒绝非法 pattern
    with pytest.raises(InvalidRuleError):
        ParseRuleSet.from_specs([_path("r:([unclosed", ContentType.WESTERN, minute=0, id=1)])
    with pytest.raises(InvalidRuleError):
        ParseRuleSet.from_specs([_number("a" * (PATTERN_MAX_LENGTH + 1), ContentType.WESTERN, minute=0, id=1)])


class _RejectCase(NamedTuple):
    pattern: str
    reason: str


@pytest.mark.parametrize(
    "case",
    [
        _RejectCase("r:(a)\\1", "反向引用"),
        _RejectCase("r:(?<=x)y", "环视 lookbehind"),
        _RejectCase("r:(?!x)y", "环视 lookahead"),
    ],
)
def test_re2_rejects_unsupported_constructs(case: _RejectCase) -> None:
    """RE2 不支持的构造 (反向引用 / 环视) 编译即拒, 语义为 422."""
    with pytest.raises(InvalidRuleError):
        compile_rule_pattern(case.pattern)


class _CaseSemantics(NamedTuple):
    pattern: str
    path: str
    matched: bool


@pytest.mark.parametrize(
    "case",
    [
        # 正则按原始字符串 search, 大小写敏感; 大小写不敏感由 pattern 自行声明 (?i)
        _CaseSemantics("r:abc", "/data/ABC-123.mp4", False),
        _CaseSemantics("r:abc", "/data/abc-123.mp4", True),
        _CaseSemantics("r:(?i)abc", "/data/ABC-123.mp4", True),
        # 反衬: 字面关键词始终 casefold 不敏感
        _CaseSemantics("abc", "/data/ABC-123.mp4", True),
    ],
)
def test_regex_case_semantics(case: _CaseSemantics) -> None:
    """钉定大小写语义: 正则敏感 (需 (?i) 自声明), 字面关键词不敏感; 重构若把正则也 casefold 则此处变红."""
    rules = ParseRuleSet.from_specs([_path(case.pattern, ContentType.WESTERN, minute=0, id=1)])
    info = apply_rules(case.path, parse_file_info(text="MIDV-123"), rules)
    assert (info.content_type is ContentType.WESTERN) is case.matched, case.pattern


def test_catastrophic_pattern_is_linear() -> None:
    """(a|aa)+$ 在 RE2 下编译通过且对长近似匹配输入线性时间完成 (Python re 会指数回溯)."""
    pattern = "r:(a|aa)+$"
    rules = ParseRuleSet.from_specs([_path(pattern, ContentType.WESTERN, minute=0, id=1)])
    assert compile_rule_pattern(pattern) is not None

    no_match = "/" + "a" * 1000 + "b"
    started = time.perf_counter()
    info = apply_rules(no_match, parse_file_info(text="MIDV-123"), rules)
    elapsed = time.perf_counter() - started
    assert info.content_type is not ContentType.WESTERN
    assert elapsed < 1.0

    match_path = "/" + "a" * 1000
    started = time.perf_counter()
    info = apply_rules(match_path, parse_file_info(text="MIDV-123"), rules)
    elapsed = time.perf_counter() - started
    assert info.content_type is ContentType.WESTERN
    assert elapsed < 1.0
