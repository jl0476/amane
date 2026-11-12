"""模板后端抽象与 Jinja2 后端测试: 沙箱拒绝、兼容 filter 语义、变量映射、两引擎一致性与逃逸防护."""

from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

from amane.organize.backend import NativeTemplateBackend, TemplateBackend, TemplateSyntax
from amane.organize.path_templates import VIDEO_TEMPLATE_DEFAULT
from amane.organize.template import PLACEHOLDERS, TemplateContext
from amane.organize.template_jinja import VARIABLE_MAP, JinjaTemplateBackend


def _native() -> NativeTemplateBackend:
    return NativeTemplateBackend()


def _jinja() -> JinjaTemplateBackend:
    return JinjaTemplateBackend()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """媒体库根目录 (tmp_path 已 resolve, 无符号链接残留, 保证 resolve 后路径不变形)."""
    return tmp_path.resolve() / "media"


@pytest.fixture
def other(tmp_path: Path) -> Path:
    """媒体根之外的目录, 作绝对模板写出路径 / safe_dirs (多盘分存场景)."""
    return tmp_path.resolve() / "out"


# SandboxedEnvironment: 下划线属性访问在读取时即拒绝; 普通方法调用不受影响.
_UNSAFE_ATTR_CASES = [
    "{{ studio.__class__ }}",
    "{{ studio.__doc__ }}",
    '{{ studio | attr("__class__") }}',
    '{{ studio["__class__"] }}',
]


@pytest.mark.parametrize("template", _UNSAFE_ATTR_CASES)
def test_jinja_backend_renders_and_rejects_attr(template: str) -> None:
    backend = _jinja()
    assert (
        backend.render("{{ studio }}/{{ number }}.{{ ext }}", {"studio": "S", "number": "N", "ext": "mp4"}) == "S/N.mp4"
    )
    assert backend.render("{{ studio.lower() }}", {"studio": "S"}) == "s"
    with pytest.raises(SecurityError):
        backend.render(template, {"studio": "S"})


def test_missing_variable_renders_empty() -> None:
    """变量缺失渲染为空串: 与自研引擎的 Unknown 回退是有意差异 (规格 3.2.2)."""
    assert _jinja().render("[{{ missing }}]", {}) == "[]"


# (template, variables, expected). default 按自研「有值」判定 (原生 default 只对 undefined 生效,
# 覆盖不了空串与 "Unknown"); truncate 按 UTF-8 字节计, 不追加省略号; 其余 filter 与自研语义一致.
_COMPAT_FILTER_CASES = [
    ('{{ a | default("x") }}', {"a": "v"}, "v"),
    ('{{ a | default("x") }}', {"a": ""}, "x"),
    ('{{ a | default("x") }}', {"a": "Unknown"}, "x"),
    ('{{ a | default("x") }}', {"a": "None"}, "x"),
    ('{{ a | default("x") }}', {}, "x"),
    ('{{ a | default(b | default("未知")) }}', {"a": "A", "b": "B"}, "A"),
    ('{{ a | default(b | default("未知")) }}', {"a": "", "b": "B"}, "B"),
    ('{{ a | default(b | default("未知")) }}', {"a": ""}, "未知"),
    ('{{ a | default(b | default("未知")) }}', {}, "未知"),
    # 「日」3 字节, 截 4 字节只保留「日」; 原生 truncate 按字符计数并带省略号, 不合此语义.
    ("{{ title | truncate(4) }}", {"title": "日本語テスト"}, "日"),
    ("{{ title | truncate(4) }}", {"title": "abcdef"}, "abcd"),
    ("{{ title | truncate(0) }}", {"title": "abc"}, ""),
    ("{{ title | truncate(99) }}", {"title": "abc"}, "abc"),
    ("{{ title | upper | truncate(20) }}", {"title": "x" * 30}, "X" * 20),
    ("{{ title | upper }}", {"title": "abc"}, "ABC"),
    ("{{ title | lower }}", {"title": "AbC"}, "abc"),
    ("{{ title | trim }}", {"title": "  x "}, "x"),
    ('{{ t | replace("a", "b") }}', {"t": "banana"}, "bbnbnb"),
    ('{{ t | split(",") | first }}', {"t": "A,B"}, "A"),
    ('{{ t | split(",") | last }}', {"t": "A,B"}, "B"),
    ('{{ t | split("-") | first }}', {"t": "A"}, "A"),
]


@pytest.mark.parametrize(("template", "variables", "expected"), _COMPAT_FILTER_CASES)
def test_compat_filters_match_native_semantics(template: str, variables: dict[str, str], expected: str) -> None:
    assert _jinja().render(template, variables) == expected


# 非法 filter 用法在渲染时报 ValueError: truncate 参数必须是整数字面量, split 分隔符不能为空.
_INVALID_FILTER_CASES = [
    ('{{ t | truncate("4") }}', {"t": "abcdef"}),
    ('{{ t | split("") }}', {"t": "a,b"}),
]


@pytest.mark.parametrize(("template", "variables"), _INVALID_FILTER_CASES)
def test_jinja_filter_rejects_invalid_args(template: str, variables: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="路径模板"):
        _jinja().render(template, variables)


# cd? / sub? / mosaic? / def? 不是合法 Jinja 标识符, 渲染前按映射改写变量键; 其余同名 (含 dir 别名).
_VARIABLE_MAPPING_CASES = [
    ("{{ cd }}/{{ sub }}", {"cd?": "1", "sub?": "C"}, "1/C"),
    ("{{ mosaic }}/{{ definition }}", {"mosaic?": "uncensored", "def?": "1080p"}, "uncensored/1080p"),
    ("{{ dir }}/{{ number }}", {"dir": "d", "number": "N"}, "d/N"),
    ("{{ title }}/{{ index_padded }}", {"title": "T", "index_padded": "01"}, "T/01"),
]


def test_variable_map_renames_exact() -> None:
    assert VARIABLE_MAP == {"cd?": "cd", "sub?": "sub", "mosaic?": "mosaic", "def?": "definition"}
    assert set(VARIABLE_MAP) <= set(PLACEHOLDERS)


@pytest.mark.parametrize(("template", "variables", "expected"), _VARIABLE_MAPPING_CASES)
def test_variable_mapping(template: str, variables: dict[str, str], expected: str) -> None:
    assert _jinja().render(template, variables) == expected
    # `?` 不是合法 Jinja 标识符: 模板里写 `{{ cd? }}` 在解析期报错.
    with pytest.raises(ValueError, match="语法无效"):
        _jinja().render("{{ cd? }}", {})


# 分集 / 中字真值组合全覆盖: 两套默认模板渲染出的路径必须完全相同.
_EQUIVALENCE_CASES = [("1", "C"), ("1", ""), ("", "C"), ("", "")]


@pytest.mark.parametrize(("cd", "sub"), _EQUIVALENCE_CASES)
def test_two_engines_agree_on_default_templates(cd: str, sub: str) -> None:
    variables = {
        "studio": "Studio",
        "number": "ABC-1",
        "title": "标" * 200,
        "actor": "A" * 300,
        "ext": "mp4",
        "cd?": cd,
        "sub?": sub,
    }
    native = _native().render(_native().describe().default_video_template, variables)
    jinja = _jinja().render(_jinja().describe().default_video_template, variables)
    assert native == jinja


# 同语义的双语法模板对: 变量齐全时 (只排除变量缺失差异) 两引擎渲染结果一致, 覆盖截断与全部兼容 filter.
_SHARED_SEMANTICS_CASES = [
    ("{title}/{number}", "{{ title }}/{{ number }}", {"title": "T", "number": "N"}),
    ("[{title}]/{number}", "{% if title %}{{ title }}/{% endif %}{{ number }}", {"title": "T", "number": "N"}),
    ("[{title}]/{number}", "{% if title %}{{ title }}/{% endif %}{{ number }}", {"title": "", "number": "N"}),
    ("{title:upper:trim}", "{{ title | upper | trim }}", {"title": " t "}),
    ('{t:default("x")}', '{{ t | default("x") }}', {"t": ""}),
    ('{t:default("x")}', '{{ t | default("x") }}', {"t": "v"}),
    ('{t:replace("a", "b")}', '{{ t | replace("a", "b") }}', {"t": "banana"}),
    ('{t:split(","):first}', '{{ t | split(",") | first }}', {"t": "A,B"}),
    ("{title:truncate(4)}", "{{ title | truncate(4) }}", {"title": "日本語テスト"}),
    ("{title}", "{{ title }}", {"title": "标" * 200}),
    # 条件块的「有值」判定一致: "Unknown" / "None" 与缺失同判无值 (变量缺失的差异只在取值输出).
    ("{% if studio %}X{% else %}Y{% endif %}", "{% if studio %}X{% else %}Y{% endif %}", {"studio": "Unknown"}),
    ("{% if studio %}X{% else %}Y{% endif %}", "{% if studio %}X{% else %}Y{% endif %}", {"studio": "None"}),
    ("{% if studio %}X{% else %}Y{% endif %}", "{% if studio %}X{% else %}Y{% endif %}", {}),
    # 前导空白文本块之后的绝对取值占位符: 两引擎同样跳过空白判锚, 规范化同样因锚不可见而剥掉前导 /.
    ("  {video_dir}/thumb.jpg", "  {{ video_dir }}/thumb.jpg", {"video_dir": "/mnt/lib/x"}),
]


@pytest.mark.parametrize(("native_tpl", "jinja_tpl", "variables"), _SHARED_SEMANTICS_CASES)
def test_two_engines_agree_on_shared_semantics(native_tpl: str, jinja_tpl: str, variables: dict[str, str]) -> None:
    assert _native().render(native_tpl, variables) == _jinja().render(jinja_tpl, variables)


# R17: NativeTemplateBackend.validate 委托 validate_template — 白名单行为与保存路径一致.
_NATIVE_VALIDATE_ACCEPT_CASES = [
    "{studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}",
    "{dir}",
    "{% if dir %}X{% endif %}",
    '{actors:split(","):first:upper}',
]

_NATIVE_VALIDATE_REJECT_CASES = [
    "{typo}",
    "{title:unknownf}",
    "{% if condx %}X{% endif %}",
    "{index:02}",
]


@pytest.mark.parametrize("template", _NATIVE_VALIDATE_ACCEPT_CASES)
def test_native_validate_accepts_whitelisted(template: str) -> None:
    _native().validate(template)


@pytest.mark.parametrize("template", _NATIVE_VALIDATE_REJECT_CASES)
def test_native_validate_rejects_unknown(template: str) -> None:
    with pytest.raises(ValueError, match="路径模板"):
        _native().validate(template)


# Jinja 校验: 语法错误与未定义 filter 由编译期拦截, 变量名按白名单 (映射后的占位符加 dir) 拒绝.
_JINJA_VALIDATE_ACCEPT_CASES = [
    "{{ studio }}/{{ number }}.{{ ext }}",
    "{{ dir }}",
    "{% if cd %}-CD{{ cd }}{% endif %}",
    "{{ title | truncate(4) | upper }}",
    '{{ studio | default(publisher | default("未知")) }}',
    # 合法链形状: split 之后接 first / last, first 之后接字符串 filter, replace 双参,
    # filter 参数内的嵌套链同样按形状校验.
    '{{ studio | split(",") | first }}',
    '{{ studio | split(",") | first | upper }}',
    '{{ title | replace("a", "b") }}',
    '{{ studio | default(number | split(",") | first) }}',
]

_JINJA_VALIDATE_REJECT_CASES = [
    "{{ typo }}",
    "{{ cd? }}",
    "{{ t | nosuchf }}",
    "{{ studio",
    "{% if studio %}X",
    # filter 链形状与 native 一致: split 只能与 first / last 组合, 链尾不得是 split,
    # first / last 只能作用于 split 的结果 (否则按字符串取首末项, 落盘静默垃圾).
    '{{ studio | split(",") }}',
    "{{ studio | first }}",
    '{{ studio | split(",") | upper }}',
    # 条件帧内的 filter 延迟到运行期才解析, 编译不报错, 必须由遍历拦截.
    "{% if studio | nosuchf %}X{% endif %}",
    # arity 与 native 共表: default 单参, upper 无参.
    "{{ studio | default(number, title) }}",
    "{{ title | upper(1) }}",
    # filter 参数内的嵌套链同样按形状校验.
    '{{ studio | default(number | split(",")) }}',
]


@pytest.mark.parametrize("template", _JINJA_VALIDATE_ACCEPT_CASES)
def test_jinja_validate_accepts(template: str) -> None:
    _jinja().validate(template)


@pytest.mark.parametrize("template", _JINJA_VALIDATE_REJECT_CASES)
def test_jinja_validate_rejects(template: str) -> None:
    with pytest.raises(ValueError, match="路径模板"):
        _jinja().validate(template)


def test_native_describe_matches_path_templates_default() -> None:
    syntax = _native().describe()
    assert syntax.name == "native"
    assert syntax.default_video_template == VIDEO_TEMPLATE_DEFAULT
    assert {p.name for p in syntax.placeholders} == {*PLACEHOLDERS, "dir"}
    assert all(p.variable == p.name for p in syntax.placeholders)


def test_jinja_describe_maps_variables() -> None:
    syntax = _jinja().describe()
    assert syntax.name == "jinja"
    assert (
        syntax.default_video_template
        == "{{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}"
    )
    variables = {p.name: p.variable for p in syntax.placeholders}
    assert len(variables) == len(PLACEHOLDERS) + 1
    assert variables["cd?"] == "cd"
    assert variables["sub?"] == "sub"
    assert variables["mosaic?"] == "mosaic"
    assert variables["def?"] == "definition"
    assert variables["dir"] == "dir"
    assert all(value.isidentifier() for value in variables.values())


# 绝对锚判定与自研引擎一致: 首个有输出的块决定, 表达式按渲染值判定 (空值判相对),
# 条件块按实际渲染分支; 首个表达式取值为空时前导 / 不得把相对模板变成绝对路径.
_JINJA_ANCHOR_CASES = [
    ("{{ video_dir }}/{{ video_name }}", {"video_dir": "/mnt/lib/x", "video_name": "a"}, "/mnt/lib/x/a"),
    ("{{ raw_dir }}/{{ number }}", {"raw_dir": "", "number": "N"}, "N"),
    (
        "{% if raw_relpath %}{{ raw_relpath }}/{{ video_name }}{% else %}{{ link_dir }}/{{ video_name }}{% endif %}",
        {"raw_relpath": "", "link_dir": "/mnt/lib/x", "video_name": "a"},
        "/mnt/lib/x/a",
    ),
    ("{% if link_dir %}{{ link_dir }}/{{ video_name }}{% endif %}", {"link_dir": "", "video_name": "a"}, ""),
    ("studio/{{ number }}", {"number": "N"}, "studio/N"),
]


@pytest.mark.parametrize(("template", "variables", "expected"), _JINJA_ANCHOR_CASES)
def test_jinja_keeps_absolute_anchor(template: str, variables: dict[str, str], expected: str) -> None:
    assert _jinja().render(template, variables) == expected


def test_backends_satisfy_protocol() -> None:
    backends: tuple[TemplateBackend, ...] = (_native(), _jinja())
    for backend in backends:
        assert isinstance(backend.describe(), TemplateSyntax)
        backend.validate(backend.describe().default_video_template)


def test_resolve_relative_stays_in_base(media: Path) -> None:
    for backend, template in ((_native(), "{name}"), (_jinja(), "{{ name }}")):
        ctx = TemplateContext.from_mapping({"name": "x"})
        assert backend.resolve(template, ctx, media, ()) == media / "x"


def test_resolve_relative_escape_rejected(media: Path) -> None:
    for backend, template in ((_native(), "../{name}"), (_jinja(), "../{{ name }}")):
        ctx = TemplateContext.from_mapping({"name": "x"})
        with pytest.raises(ValueError, match="路径越界"):
            backend.resolve(template, ctx, media, ())


def test_resolve_absolute_within_base(media: Path) -> None:
    video_dir = media / "v"
    for backend, template in ((_native(), "{video_dir}/thumb.jpg"), (_jinja(), "{{ video_dir }}/thumb.jpg")):
        ctx = TemplateContext.from_mapping({"video_dir": str(video_dir)})
        assert backend.resolve(template, ctx, media, ()) == video_dir / "thumb.jpg"


def test_resolve_absolute_in_safe_dirs(media: Path, other: Path) -> None:
    for backend, template in ((_native(), "{video_dir}/thumb.jpg"), (_jinja(), "{{ video_dir }}/thumb.jpg")):
        ctx = TemplateContext.from_mapping({"video_dir": str(other / "v")})
        assert backend.resolve(template, ctx, media, (other,)) == other / "v" / "thumb.jpg"


def test_resolve_absolute_outside_rejected(media: Path, other: Path) -> None:
    for backend, template in ((_native(), "{video_dir}/thumb.jpg"), (_jinja(), "{{ video_dir }}/thumb.jpg")):
        ctx = TemplateContext.from_mapping({"video_dir": str(other / "v")})
        with pytest.raises(ValueError, match="路径越界"):
            backend.resolve(template, ctx, media, ())


def test_resolve_absolute_unrestricted_without_safe_dirs(media: Path, other: Path) -> None:
    for backend, template in ((_native(), "{video_dir}/thumb.jpg"), (_jinja(), "{{ video_dir }}/thumb.jpg")):
        ctx = TemplateContext.from_mapping({"video_dir": str(other / "v")})
        assert backend.resolve(template, ctx, media, None) == other / "v" / "thumb.jpg"
