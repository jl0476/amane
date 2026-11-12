"""自研模板语法扩展测试: filter 链、降级链, 以及既有值映射与可选组的不变性."""

import pytest

from amane.organize.template import PathEngine, StrmEngine, TemplateContext, TemplateEngine, validate_template


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
    ("{title:truncate(x)}", {"t": "abcdef"}),  # truncate 参数不是整数字面量
    ('{title:truncate("4")}', {"t": "abcdef"}),
    ("{title:default(20)}", {"t": ""}),  # default 不接受整数字面量
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


# 条件块: 条件是逗号分隔的变量名, 全部有值才真; 「有值」判定与 filter 一致.
_IF_CASES = [
    ("{% if a %}X{% endif %}", {"a": "v"}, "X"),
    ("{% if a %}X{% endif %}", {"a": ""}, ""),
    ("{% if a %}X{% endif %}", {"a": "Unknown"}, ""),
    ("{% if a %}X{% endif %}", {"a": "None"}, ""),
    ("{% if a %}X{% endif %}", {}, ""),
    ("{% if a %}X{% else %}Y{% endif %}", {"a": "v"}, "X"),
    ("{% if a %}X{% else %}Y{% endif %}", {"a": ""}, "Y"),
    ("{% if a,b %}X{% else %}Y{% endif %}", {"a": "1", "b": "2"}, "X"),
    ("{% if a,b %}X{% else %}Y{% endif %}", {"a": "1", "b": ""}, "Y"),
    ("{% if a,b %}X{% else %}Y{% endif %}", {"a": "", "b": "2"}, "Y"),
    ("{% if a,b %}X{% else %}Y{% endif %}", {"a": "1", "b": "Unknown"}, "Y"),
    # 块内可以放占位符、filter、可选组与嵌套条件块.
    ("{% if a %}{number}{% endif %}", {"a": "1", "number": "N-1"}, "N-1"),
    ("{% if a %}[-{b}]{% endif %}", {"a": "1", "b": ""}, ""),
    ('{% if a %}{number:upper}{% else %}{c:default("z")}{% endif %}', {"a": "", "c": ""}, "z"),
    ("{% if sub? %}C{% endif %}", {"sub?": "C"}, "C"),
    ("{% if sub? %}C{% endif %}", {"sub?": ""}, ""),
    ("{% if a %}A{% if b %}B{% else %}b{% endif %}{% endif %}", {"a": "1", "b": "2"}, "AB"),
    ("{% if a %}A{% if b %}B{% else %}b{% endif %}{% endif %}", {"a": "1", "b": ""}, "Ab"),
    ("{% if a %}A{% if b %}B{% else %}b{% endif %}{% endif %}", {"a": ""}, ""),
    ("[{% if a %}x{% endif %}]", {"a": "1"}, "x"),
    ("[{% if a %}x{% endif %}]", {"a": ""}, ""),
]

# 未闭合块、孤立分支、空条件与非法条件名都是解析错误.
_INVALID_BLOCK_CASES = [
    "{% if a %}X",
    "{% if a %}X{% else %}Y",
    "{% if a %}X{% if b %}Y{% endif %}",
    "{% if a X",
    "{% else %}X{% endif %}",
    "{% endif %}",
    "{% if %}X{% endif %}",
    "{% if a b %}X{% endif %}",
    "{% if a %}X{% else %}Y{% else %}Z{% endif %}",
    "{% iff a %}X{% endif %}",
    "{% if a,b, %}X{% endif %}",
    "{% elif x %}X{% endif %}",
    "{% if a == b %}X{% endif %}",
    "{% if f(x) %}X{% endif %}",
    "{% if a,,b %}X{% endif %}",
]


@pytest.mark.parametrize(("template", "variables", "expected"), _IF_CASES)
def test_if_block_truthiness(template: str, variables: dict[str, str], expected: str) -> None:
    assert _render(template, variables) == expected


@pytest.mark.parametrize("template", _INVALID_BLOCK_CASES)
def test_if_block_rejects_invalid(template: str) -> None:
    with pytest.raises(ValueError, match="路径模板"):
        _render(template, {})


# 保存路径的白名单校验: 未知占位符与未知 filter 必须指出名称与位置.
_VALIDATE_REJECT_CASES = [
    "{typo}",
    "a{typo}b",
    "{ typo }",
    "{title:unknownf}",
    "{studio:default(publisher:unknownf)}",
    "{studio:default(typo2)}",
    "{% if condx %}X{% endif %}",
    '{actors:split(","):upper}',
    '{actors:split(",")}',
    "{actors:first}",
    "{title:default(20)}",
    "{title:truncate(x)}",
    '{title:truncate("4")}',
    '{title:replace("a")}',
    "{title:upper(1)}",
    "{index:02}",
]

_VALIDATE_ACCEPT_CASES = [
    "{dir}",
    "{dir:upper}",
    "{% if dir %}X{% endif %}",
    "{studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}",
    '{actors:split(","):first:upper}',
    '{studio:default(publisher:default("x"))}',
    "{title|truncate=10}",
    "{title:trim:truncate(10)}",
    "{video_relpath}",
    "{index_padded}",
]


@pytest.mark.parametrize("template", _VALIDATE_REJECT_CASES)
def test_validate_rejects_unknown_placeholder_and_filter(template: str) -> None:
    with pytest.raises(ValueError, match="路径模板"):
        validate_template(template)


@pytest.mark.parametrize("template", _VALIDATE_ACCEPT_CASES)
def test_validate_accepts_dir_alias(template: str) -> None:
    validate_template(template)


def test_validate_error_names_position() -> None:
    with pytest.raises(ValueError) as exc:
        validate_template("ab{typo}cd")
    message = str(exc.value)
    assert "typo" in message
    assert "位置 3" in message
    with pytest.raises(ValueError) as exc:
        validate_template("{title:unknownf}")
    message = str(exc.value)
    assert "unknownf" in message
    assert "位置" in message


def test_filter_keeps_absolute_anchor() -> None:
    """绝对取值占位符带 filter 时, 渲染结果的路径锚仍按绝对路径保留."""
    ctx = TemplateContext.from_mapping({"video_dir": "/media/lib/video"})
    engine = PathEngine("{video_dir:trim}/thumb.jpg")
    assert engine.render(ctx) == "/media/lib/video/thumb.jpg"


# uses() 必须穿透条件块: 条件名、分支内占位符与 filter 参数表达式都算取用.
# 消费方: StrmEngine 的 {video_relpath} 检查与剧照文件名的序号 / 路径占位符检查.
_USES_CASES = [
    ("{% if a %}X{% endif %}", "a", True),
    ("{% if a %}{b}{% endif %}", "b", True),
    ("{% if a %}X{% else %}{b}{% endif %}", "b", True),
    ("{% if a %}{% if b %}{c}{% endif %}{% endif %}", "c", True),
    ("{% if a %}{b}{% endif %}", "c", False),
    ("{a:default(b)}", "b", True),
    ("{a}", "b", False),
]


@pytest.mark.parametrize(("template", "name", "expected"), _USES_CASES)
def test_uses_penetrates_condition_block(template: str, name: str, expected: bool) -> None:
    assert TemplateEngine(template).uses(name) == expected


def test_strm_video_relpath_inside_condition_block() -> None:
    """{video_relpath} 被条件块包裹时, STRM 渲染仍要求 dest 与 library_root."""
    engine = StrmEngine("{% if x %}{video_relpath}{% endif %}")
    with pytest.raises(ValueError, match="video_relpath"):
        engine.render(TemplateContext.from_mapping({"x": "1"}))


# 条件块开头的模板: 绝对锚按实际渲染分支的首个占位符判定, 不得剥掉.
_ANCHOR_CASES = [
    (
        "{% if link_dir %}{link_dir}/{video_name}{% endif %}",
        {"link_dir": "/mnt/lib/x", "video_name": "a"},
        "/mnt/lib/x/a",
    ),
    (
        "{% if raw_relpath %}{raw_relpath}/{video_name}{% else %}{link_dir}/{video_name}{% endif %}",
        {"raw_relpath": "", "link_dir": "/mnt/lib/x", "video_name": "a"},
        "/mnt/lib/x/a",
    ),
    ("{% if link_dir %}{link_dir}/{video_name}{% endif %}", {"link_dir": "", "video_name": "a"}, ""),
    ("{video_dir}/{video_name}", {"video_dir": "/mnt/lib/x", "video_name": "a"}, "/mnt/lib/x/a"),
]


@pytest.mark.parametrize(("template", "variables", "expected"), _ANCHOR_CASES)
def test_if_block_keeps_absolute_anchor(template: str, variables: dict[str, str], expected: str) -> None:
    assert PathEngine(template).render(TemplateContext.from_mapping(variables)) == expected
