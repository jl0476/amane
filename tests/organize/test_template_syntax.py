"""自研模板语法扩展测试: filter 链、降级链, 以及既有值映射与可选组的不变性."""

import pytest

from amane.organize.template import TemplateContext, TemplateEngine


def _render(template: str, variables: dict[str, str]) -> str:
    return TemplateEngine(template).render(TemplateContext.from_mapping(variables))


# (template, variables, expected). filter 从左到右依次应用; truncate 按 UTF-8 字节计, 不追加省略号.
_FILTER_CASES = [
    ("{title:upper}", {"title": "abc"}, "ABC"),
    ("{title:lower}", {"title": "AbC"}, "abc"),
    ("{title:trim}", {"title": "  x "}, "x"),
    ("{title:upper:trim}", {"title": " abc "}, "ABC"),
    ('{t:replace("a", "b")}', {"t": "banana"}, "bbnbnb"),
    ('{t:split(","):first}', {"t": "A,B"}, "A"),
    ('{t:split(","):last}', {"t": "A,B"}, "B"),
    ('{t:split("-"):first}', {"t": "A"}, "A"),
    ("{title:upper:truncate(20)}", {"title": "x" * 30}, "X" * 20),
    ("{title:upper:truncate(4)}", {"title": "abcdef"}, "ABCD"),
    # 多字节字符不得切开: 「日」3 字节, 截 4 字节只保留「日」.
    ("{title:truncate(4)}", {"title": "日本語テスト"}, "日"),
    ("{title:truncate(0)}", {"title": "abc"}, ""),
    ("{title:truncate(99)}", {"title": "abc"}, "abc"),
    # 空值经 filter 仍为空, 可选组照常丢弃.
    ("[{t:upper}]", {"t": ""}, ""),
    ("[{t:upper}]", {"t": "x"}, "X"),
    # filter 与值映射并存: 映射先改写源值, filter 再变换.
    ("{t:upper|k=v}", {"t": "k"}, "V"),
    ("{sub?:upper|C=cn}", {"sub?": "C"}, "CN"),
]

# (template, variables, expected). 引号内的分隔符不属于结构, 引号缺失与未闭合必须报错.
_QUOTED_ARG_CASES = [
    ('{t:replace("a,b","c")}', {"t": "a,b"}, "c"),
    ('{t:replace("a:b","c")}', {"t": "a:b"}, "c"),
    ('{t:replace("a)","c")}', {"t": "a)"}, "c"),
    ('{t:split("|"):last}', {"t": "a|b"}, "b"),
]

# 降级链: 「有值」判定为 None / 空串 / 空列表 / "Unknown" / "None" 之外; default 覆盖 "Unknown".
_DEFAULT_CASES = [
    ('{a:default("x")}', {"a": "v"}, "v"),
    ('{a:default("x")}', {"a": ""}, "x"),
    ('{a:default("x")}', {"a": "Unknown"}, "x"),
    ('{a:default("x")}', {"a": "None"}, "x"),
    ('{a:default("x")}', {}, "x"),
    ('{a:default(b:default("未知"))}', {"a": "A", "b": "B"}, "A"),
    ('{a:default(b:default("未知"))}', {"a": "", "b": "B"}, "B"),
    ('{a:default(b:default("未知"))}', {"a": "", "b": ""}, "未知"),
    # b 缺失时取值回退 "Unknown", 仍按无值继续向内降级.
    ('{a:default(b:default("未知"))}', {"a": ""}, "未知"),
    ('{studio:default("未分类")}', {"studio": "Unknown"}, "未分类"),
]

# 值映射与可选组的行为一字不改: `{name|k=v|upper}` 的 `v|upper` 仍是映射值, 映射值里的 `:` 不是 filter.
_MAPPING_CASES = [
    ("{title|truncate=10}", {"title": "abc"}, "abc"),
    ("{studio|Unknown=未分类}", {"studio": "Unknown"}, "未分类"),
    ("{studio|=缺省}", {"studio": ""}, "缺省"),
    ("{sub?|C=中字}", {"sub?": "C"}, "中字"),
    ("{sub?|C=中字}", {"sub?": ""}, ""),
    ("{title|k=v|upper}", {"title": "k"}, "v|upper"),
    ("{t|a=12:30}", {"t": "a"}, "12:30"),
    ("{number}[-CD{cd?}][-{sub?}].{ext}", {"number": "ABC-1", "cd?": "2", "sub?": "", "ext": "mp4"}, "ABC-1-CD2.mp4"),
    ("{number}[-CD{cd?}][-{sub?}].{ext}", {"number": "ABC-1", "cd?": "", "sub?": "", "ext": "mp4"}, "ABC-1.mp4"),
    ("[[x{sub?}]]", {"sub?": "C"}, "[xC]"),
    ("[[x{sub?}]]", {"sub?": ""}, ""),
]

# 非法语法与非法组合在渲染时报 ValueError.
_INVALID_RENDER_CASES = [
    ("{t:unknownf}", {"t": "x"}),  # filter 名未知
    ("{t:}", {"t": "x"}),  # filter 名为空
    ("{t:upper(}", {"t": "x"}),  # 参数括号未闭合
    ('{t:replace("a,b}', {"t": "x"}),  # 引号未闭合
    ("{t:replace(}", {"t": "x"}),  # 括号后无参数也无闭括号
    ('{t:replace("a"x, "b")}', {"t": "x"}),  # 参数之间缺逗号
    ("{t:replace(a,,b)}", {"t": "x"}),  # 空参数
    ('{t:split(",")}', {"t": "a,b"}),  # 链尾是 split, 渲染结果不是字符串
    ("{actors:first}", {"actors": "A,B"}),  # first 只能作用于 split 的结果
    ('{t:split(","):upper}', {"t": "a,b"}),  # split 之后只能使用 first / last
    ("{t:truncate(x)}", {"t": "abcdef"}),  # truncate 参数不是整数字面量
    ('{t:truncate("4")}', {"t": "abcdef"}),
    ("{t:default(20)}", {"t": ""}),  # default 不接受整数字面量
    ('{t:split("")}', {"t": "a"}),  # 空分隔符
]


@pytest.mark.parametrize(("template", "variables", "expected"), _FILTER_CASES)
def test_filter_chain_with_colon(template: str, variables: dict[str, str], expected: str) -> None:
    assert _render(template, variables) == expected


@pytest.mark.parametrize(("template", "variables", "expected"), _QUOTED_ARG_CASES)
def test_quoted_args_allow_comma(template: str, variables: dict[str, str], expected: str) -> None:
    assert _render(template, variables) == expected


@pytest.mark.parametrize(("template", "variables", "expected"), _DEFAULT_CASES)
def test_default_chain_nested(template: str, variables: dict[str, str], expected: str) -> None:
    assert _render(template, variables) == expected


@pytest.mark.parametrize(("template", "variables", "expected"), _MAPPING_CASES)
def test_value_mapping_unaffected(template: str, variables: dict[str, str], expected: str) -> None:
    assert _render(template, variables) == expected


@pytest.mark.parametrize(("template", "variables"), _INVALID_RENDER_CASES)
def test_filter_render_rejects_invalid(template: str, variables: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="路径模板"):
        _render(template, variables)
