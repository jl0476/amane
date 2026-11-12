"""Jinja2 命名模板后端: SandboxedEnvironment 渲染, 输出与自研引擎执行同一路径规范化与逃逸防护.

兼容 filter: default / truncate / split 自建 — 原生 default 只对 undefined 生效, 覆盖不了空串与
"Unknown"; 原生 truncate 按字符计数并追加省略号, 均不合自研语义. upper / lower / trim / replace /
first / last 用 Jinja 原生. 变量键按 `VARIABLE_MAP` 改写: `cd?` 等 4 个占位符不是合法 Jinja 标识符,
渲染前换成合法变量名, 其余同名 (含 `dir`); title / actor 等字段与自研引擎同样按 UTF-8 字节截断.
取值包成 `_TemplateValue`, 让 `{% if %}` 与自研条件块共用「有值」判定 ("Unknown" / "None" 与空串
同为无值). 变量缺失渲染为空串, 与自研引擎的 Unknown 回退是有意差异.
保存校验与自研引擎共用 `_FILTER_ARITY` 与 split / first / last 的链形状规则.

绝对锚判定与自研引擎一致: 按渲染分片顺序取首个有输出的块 — 文本块空白跳过, 表达式按渲染值判定,
空值判相对; 条件块按实际渲染分支. 分片按 AST 叶子逐片消耗, 结构超出白名单 (for / set / elif /
复合条件) 时无法与分片对齐, 回退为按渲染结果整体判定.

沙箱约束: 下划线开头的属性在读取时即拒绝 (基类默认延迟到取值时才失败), autoescape 必须关闭 —
元数据写入路径, HTML 转义会损坏路径.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from jinja2 import Template, Undefined, meta, nodes
from jinja2.exceptions import SecurityError, TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from .backend import TemplatePlaceholder, TemplateSyntax
from .template import (
    _CLIP_KEYS,
    _FILTER_ARITY,
    PLACEHOLDERS,
    TemplateContext,
    _clip_field,
    _has_value,
    _is_anchor,
    _normalize_path_segments,
    _truncate_utf8,
    resolve_rendered,
)

JINJA_VIDEO_TEMPLATE_DEFAULT = (
    "{{ studio }}/{{ number }}/{{ number }}{% if cd %}-CD{{ cd }}{% endif %}{% if sub %}-{{ sub }}{% endif %}.{{ ext }}"
)
"""Jinja 语法的默认视频模板 (规格 3.2.3 的字面量)."""

VARIABLE_MAP: Mapping[str, str] = {
    "cd?": "cd",
    "sub?": "sub",
    "mosaic?": "mosaic",
    "def?": "definition",
}
"""4 个带 ``?`` 的占位符在 Jinja 语法下的变量名; 其余占位符同名 (含 dir 别名), 见 ``describe``."""

# 校验白名单: 占位符经映射后的变量名加 dir 别名 (与自研引擎的 PLACEHOLDERS ∪ {"dir"} 对齐).
_VARIABLE_NAMES = frozenset(VARIABLE_MAP.get(name, name) for name in (*PLACEHOLDERS, "dir"))

# 锚遍历可静态求值的条件节点形状; 条件块不提供运算符之外的语法, 超出即回退.
_CONDITION_NODES = (nodes.Name, nodes.Const, nodes.Not, nodes.And, nodes.Or)


class _PathSandboxedEnvironment(SandboxedEnvironment):
    """路径渲染沙箱: 基类对不安全属性返回延迟失败的 Undefined, 这里改为读取时立即拒绝."""

    def unsafe_undefined(self, obj: object, attribute: str) -> NoReturn:
        raise SecurityError(f"路径模板禁止读取属性 {attribute!r}: 下划线开头的属性不可访问")


class _TemplateValue(str):
    """变量取值: 真值按「有值」判定, 条件块与自研引擎的语义一致 (空串 / "Unknown" / "None" 均为假)."""

    __slots__ = ()

    def __bool__(self) -> bool:
        return _has_value(self)


class _Misaligned(Exception):
    """渲染分片与 AST 叶子失去一一对应, 锚判定需要回退."""


def _has_jinja_value(value: object) -> bool:
    """「有值」判定与自研引擎一致, 另把 Undefined 与 None 视为无值."""
    if value is None or isinstance(value, Undefined):
        return False
    if isinstance(value, str | list):
        return _has_value(value)
    return bool(value)


def _coerce_text(value: object) -> str:
    """filter 入口规整: Undefined (变量缺失) 按空串处理, 与「变量缺失渲染为空串」一致."""
    if value is None or isinstance(value, Undefined):
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _default_filter(value: object, fallback: object) -> object:
    """自建 default: 空串 / 空列表 / "Unknown" / "None" / 缺失变量都取 fallback."""
    if _has_jinja_value(value):
        return value
    return fallback


def _truncate_filter(value: object, limit: object) -> str:
    """自建 truncate: 按 UTF-8 字节截断, 不切开多字节字符, 无省略号."""
    if not isinstance(limit, int):
        raise ValueError("路径模板里的 filter truncate 的参数必须是整数字面量")
    return _truncate_utf8(_coerce_text(value), limit)


def _split_filter(value: object, sep: object) -> list[str]:
    """自建 split: 拆分结果供 first / last 取首末项 (Jinja 无原生 split)."""
    separator = _coerce_text(sep)
    if not separator:
        raise ValueError("路径模板里的 filter split 的分隔符不能为空")
    return _coerce_text(value).split(separator)


def _build_environment() -> _PathSandboxedEnvironment:
    env = _PathSandboxedEnvironment(autoescape=False)
    # 不用原生 default / truncate: 语义差异见模块 docstring; 原生 split 不存在.
    env.filters["default"] = _default_filter
    env.filters["truncate"] = _truncate_filter
    env.filters["split"] = _split_filter
    return env


def _compile(env: _PathSandboxedEnvironment, source: str) -> Template:
    """编译模板: 语法错误与未定义 filter 统一转为 ValueError, 指出行号与 Jinja 的原始信息."""
    try:
        return env.from_string(source)
    except TemplateSyntaxError as error:
        raise ValueError(f"路径模板的 Jinja 语法无效 (第 {error.lineno} 行): {error.message}") from error


def _prepare_variables(variables: Mapping[str, str]) -> dict[str, str]:
    """改写变量键 (映射表), 按自研规则截断 title / actor 等长字段, 取值包成按「有值」判真值的字符串."""
    return {
        VARIABLE_MAP.get(name, name): _TemplateValue(_clip_field(value) if name in _CLIP_KEYS else value)
        for name, value in variables.items()
    }


def _walk_eligible(body: Sequence[nodes.Node]) -> bool:
    """锚遍历是否可执行: 结构只允许文本 / 输出与条件块, 条件必须是可静态求值的形状."""
    for node in body:
        if isinstance(node, nodes.Output):
            continue
        if isinstance(node, nodes.If):
            if not isinstance(node.test, _CONDITION_NODES) or node.elif_:
                return False
            if not _walk_eligible(node.body) or not _walk_eligible(node.else_):
                return False
            continue
        return False
    return True


def _condition_truthy(test: nodes.Node, jinja_vars: Mapping[str, str]) -> bool:
    """条件求值与 Jinja 渲染使用同一份变量, 保证遍历消耗的分片与实际渲染分支一致."""
    if isinstance(test, nodes.Name):
        return bool(jinja_vars.get(test.name, ""))
    if isinstance(test, nodes.Const):
        return bool(test.value)
    if isinstance(test, nodes.Not):
        return not _condition_truthy(test.node, jinja_vars)
    if isinstance(test, nodes.And):
        return _condition_truthy(test.left, jinja_vars) and _condition_truthy(test.right, jinja_vars)
    if isinstance(test, nodes.Or):
        return _condition_truthy(test.left, jinja_vars) or _condition_truthy(test.right, jinja_vars)
    raise _Misaligned


def _walk_anchor(body: Sequence[nodes.Node], chunks: Iterator[str], jinja_vars: Mapping[str, str]) -> bool | None:
    """锚遍历: True 锚 / False 相对 / None 前缀无输出. 每个叶子恰好消耗一个分片."""
    for node in body:
        if isinstance(node, nodes.Output):
            for leaf in node.nodes:
                chunk = next(chunks)
                if isinstance(leaf, nodes.TemplateData):
                    if chunk != leaf.data:
                        raise _Misaligned
                    if not leaf.data.strip():
                        continue
                    return _is_anchor(leaf.data)
                # 表达式: 取渲染值判定, 空值判相对 (空占位符不能把相对模板变成绝对路径).
                return _is_anchor(chunk)
            continue
        if isinstance(node, nodes.If):
            branch = node.body if _condition_truthy(node.test, jinja_vars) else node.else_
            if branch:
                result = _walk_anchor(branch, chunks, jinja_vars)
                if result is not None:
                    return result
            continue
    return None


def _keeps_absolute(
    env: _PathSandboxedEnvironment,
    source: str,
    jinja_vars: Mapping[str, str],
    chunks: Sequence[str],
    rendered: str,
) -> bool:
    """绝对锚判定: 遍历 AST 按渲染分片取首个有输出的块; 结构不可遍历或分片失配时按渲染结果整体判定."""
    body = env.parse(source).body
    if not _walk_eligible(body):
        return _is_anchor(rendered)
    try:
        result = _walk_anchor(body, iter(chunks), jinja_vars)
    except _Misaligned, StopIteration:
        return _is_anchor(rendered)
    return _is_anchor(rendered) if result is None else result


def _validate_filter_shapes(env: _PathSandboxedEnvironment, ast: nodes.Template) -> None:
    """全 AST 校验 filter 链, 只从链头起算 (被外层 filter 经 ``node`` 链入的节点不是链头, 由外层覆盖).
    编译只拦截输出帧内的未定义 filter, 条件帧与嵌套参数内的 filter 延迟到运行期才解析, 必须在此遍历拦截."""
    stack: list[tuple[nodes.Node, bool]] = [(ast, False)]
    while stack:
        node, inside_chain = stack.pop()
        if isinstance(node, nodes.Filter):
            if not inside_chain:
                _validate_filter_chain(env, node)
            stack.extend((child, child is node.node) for child in node.iter_child_nodes())
        else:
            stack.extend((child, False) for child in node.iter_child_nodes())


def _validate_filter_chain(env: _PathSandboxedEnvironment, node: nodes.Filter) -> None:
    """单条 filter 链的形状校验, 镜像 native `_validate_expr` / `_validate_filter`:
    成员性与参数个数按共用的 `_FILTER_ARITY`; split 之后只能 first / last, 链尾不得是 split.
    链的输入按字符串判定 (变量取值恒为 str), first / last 缺 split 前置即拒绝."""
    chain: list[nodes.Filter] = [node]
    cursor: nodes.Node | None = node.node
    while isinstance(cursor, nodes.Filter):
        chain.append(cursor)
        cursor = cursor.node
    after_split = False
    for item in reversed(chain):
        name = item.name
        line = item.lineno
        if name not in env.filters:
            raise ValueError(f"路径模板在第 {line} 行使用未定义 filter: {name!r}")
        if name in _FILTER_ARITY:
            if item.kwargs or item.dyn_args is not None or item.dyn_kwargs is not None:
                raise ValueError(f"路径模板在第 {line} 行的 filter {name!r} 不接受关键字参数")
            expected = _FILTER_ARITY[name]
            if len(item.args) != expected:
                if expected == 0:
                    raise ValueError(f"路径模板在第 {line} 行的 filter {name!r} 不接受参数")
                raise ValueError(f"路径模板在第 {line} 行的 filter {name!r} 需要 {expected} 个参数")
        if after_split and name not in ("first", "last"):
            raise ValueError(
                f"路径模板在第 {line} 行的 filter {name!r} 不能作用于 split 的结果: split 之后只能使用 first 或 last"
            )
        if name in ("first", "last") and not after_split:
            raise ValueError(f"路径模板在第 {line} 行的 filter {name!r} 只能作用于 split 的结果")
        after_split = name == "split"
    if after_split:
        raise ValueError(f"路径模板在第 {chain[0].lineno} 行的 filter 链以 split 结尾, 链尾必须是 first 或 last")


class JinjaTemplateBackend:
    """Jinja2 语法后端: 沙箱渲染, 输出经段规范化, 逃逸防护与自研引擎共用 `resolve_rendered`."""

    def __init__(self) -> None:
        self._env = _build_environment()

    def render(self, source: str, variables: Mapping[str, str]) -> str:
        template = _compile(self._env, source)
        jinja_vars = _prepare_variables(variables)
        chunks = list(template.stream(jinja_vars))
        rendered = "".join(chunks)
        return _normalize_path_segments(
            rendered, keep_absolute=_keeps_absolute(self._env, source, jinja_vars, chunks, rendered)
        )

    def resolve(self, source: str, ctx: TemplateContext, base_path: Path, safe_dirs: Sequence[Path] | None) -> Path:
        return resolve_rendered(self.render(source, ctx.variables), base_path, safe_dirs)

    def validate(self, source: str) -> None:
        """编译校验 (语法错误) 加变量名白名单与 filter 链形状检查 (成员性 / 参数个数 / split 规则)."""
        _compile(self._env, source)
        ast = self._env.parse(source)
        undeclared = meta.find_undeclared_variables(ast)
        unknown = sorted(name for name in undeclared if name not in _VARIABLE_NAMES)
        if unknown:
            raise ValueError(f"路径模板使用未知占位符: {', '.join(unknown)}")
        _validate_filter_shapes(self._env, ast)

    def describe(self) -> TemplateSyntax:
        return TemplateSyntax(
            name="jinja",
            default_video_template=JINJA_VIDEO_TEMPLATE_DEFAULT,
            placeholders=tuple(
                TemplatePlaceholder(name=name, variable=VARIABLE_MAP.get(name, name)) for name in (*PLACEHOLDERS, "dir")
            ),
        )
