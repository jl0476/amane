"""路径模板折叠空段、按 Windows 创建结果规范化每个路径段, 并约束写出路径. 填值时截断 title / actor / actors / actress / actresses. STRM 正文不折叠、不截断 (保留 `https://`), 不检查 safe_dirs.

自研语法扩展: filter 链 `{name:filter(arg)}` (分隔符是 `:`, `|` 仍只做值映射)、降级链
`{a:default(b:default("x"))}`、条件块 `{% if a,b %}…{% else %}…{% endif %}` (条件是逗号分隔的
变量名, 全部有值才真, 无 elif 与运算符). 「有值」判定见 `_has_value`; filter 集合与语义见
`_apply_filter`. 渲染不校验名字: 未知占位符仍输出 `Unknown`; 保存路径用 `validate_template`
按白名单拒绝."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, TypedDict, cast

from ..enums import ActorGender
from ..parsing.file_info import (
    CONTENT_TYPE_VALUES,
    DEFINITION_VALUES,
    MOSAIC_NATURE_VALUES,
    FileInfo,
    mosaic_nature,
    split_number,
)
from ..utils.path import is_any_descendant, is_descendant

if TYPE_CHECKING:
    from ..db.models import Metadata

_UNKNOWN = "Unknown"
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# 条件块的条件名: 占位符同款形状 (标识符字符加 ``?``).
_CONDITION_NAME = re.compile(r"[\w?]+")
PATH_FIELD_MAX_BYTES = 200
PATH_FIELD_ELLIPSIS = "…"
_CLIP_KEYS = frozenset({"title", "actor", "actors", "actress", "actresses"})
_ELLIPSIS_BYTES = len(PATH_FIELD_ELLIPSIS.encode("utf-8"))

# cd? / sub? / mosaic? / def? 不是合法标识符, 只能用函数式 TypedDict.
TemplateVariables = TypedDict(
    "TemplateVariables",
    {
        "number": str,
        "prefix": str,
        "suffix": str,
        "title": str,
        "actor": str,
        "actors": str,
        "actress": str,
        "actresses": str,
        "studio": str,
        "publisher": str,
        "series": str,
        "year": str,
        "release": str,
        "ext": str,
        "raw_dir": str,
        "raw_name": str,
        "raw_relpath": str,
        "cd?": str,
        "sub?": str,
        "content_type": str,
        "mosaic?": str,
        "def?": str,
        "video_dir": str,
        "video_name": str,
        "video_relpath": str,
        "link_dir": str,
        "link_name": str,
        "raw_srt_name": str,
        "index": str,
        "index_padded": str,
    },
)
PLACEHOLDERS: tuple[str, ...] = tuple(TemplateVariables.__annotations__)

# 有闭合取值的占位符: 映射表的 key 必须是规范值, 否则写入 422. 未列入的占位符 (如 cd?) 不校验 key.
PLACEHOLDER_MAP_KEYS: dict[str, tuple[str, ...]] = {
    "content_type": CONTENT_TYPE_VALUES,
    "mosaic?": MOSAIC_NATURE_VALUES,
    "def?": DEFINITION_VALUES,
    "sub?": ("C",),
}


@dataclass(frozen=True, slots=True)
class _Literal:
    text: str


@dataclass(frozen=True, slots=True)
class _FilterCall:
    """单个 filter: 名字与括号参数; `pos` 是名字在模板源里的偏移."""

    name: str
    args: tuple[_FilterArg, ...] = ()
    pos: int = 0


@dataclass(frozen=True, slots=True)
class _Expr:
    """变量名加 filter 链: 占位符主体与 filter 参数共用同一形状."""

    name: str
    filters: tuple[_FilterCall, ...] = ()
    pos: int = 0


type _FilterArg = str | int | _Expr


@dataclass(frozen=True, slots=True)
class _Placeholder:
    expr: _Expr
    mapping: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _Group:
    children: tuple[_Node, ...]
    wrap: bool


@dataclass(frozen=True, slots=True)
class _IfBlock:
    """条件块: 条件变量全部有值时渲染 body, 否则渲染 orelse (无 else 分支时为 None)."""

    condition: tuple[str, ...]
    body: tuple[_Node, ...]
    orelse: tuple[_Node, ...] | None
    pos: int


type _Node = _Literal | _Placeholder | _Group | _IfBlock


def _parse_condition(text: str) -> tuple[str, ...]:
    """条件块的变量名列表: 逗号分隔, 每个名字形状与占位符一致."""
    if not text.strip():
        raise ValueError("路径模板里的条件块缺少条件")
    names: list[str] = []
    for piece in text.split(","):
        name = piece.strip()
        if _CONDITION_NAME.fullmatch(name) is None:
            raise ValueError(f"路径模板里的条件名无效: {name!r}")
        names.append(name)
    return tuple(names)


def _parse_placeholder_mapping(name: str, spec: str) -> tuple[tuple[str, str], ...]:
    if not spec.strip():
        raise ValueError("路径模板里有空的占位符映射")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    allowed = PLACEHOLDER_MAP_KEYS.get(name)
    for item in spec.split(","):
        if "=" not in item:
            raise ValueError("路径模板里的占位符映射无效")
        key, value = item.split("=", 1)
        key = key.strip()
        if key in seen:
            raise ValueError(f"路径模板里的映射键重复: {key!r}")
        if key and allowed is not None and key not in allowed:
            raise ValueError(f"{{{name}}} 的映射键未知: {key!r}")
        seen.add(key)
        pairs.append((key, value.strip()))
    return tuple(pairs)


def _split_mapping(body: str) -> tuple[str, str | None]:
    """按首个顶层 ``|`` 切出值映射, 返回 (表达式, 映射文本或 None).

    顶层指 filter 参数的引号与括号之外: 引号内的 ``|`` 是参数字面量, 不参与切分.
    首个 ``:`` 之前是变量名区间, 引号与括号按字面处理, 纯值映射占位符的切分结果与过去一致.
    """
    in_quote = False
    depth = 0
    in_filter = False
    for idx, char in enumerate(body):
        if in_quote:
            if char == '"':
                in_quote = False
        elif in_filter:
            if char == '"':
                in_quote = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "|" and depth <= 0:
                return body[:idx], body[idx + 1 :]
        elif char == ":":
            in_filter = True
        elif char == "|":
            return body[:idx], body[idx + 1 :]
    return body, None


def _parse_expr(text: str, base: int, empty_error: str) -> _Expr:
    """解析 ``var(:filter)*`` 表达式; ``base`` 是 text 在模板源里的绝对偏移."""
    boundary = text.find(":")
    var_text = text if boundary < 0 else text[:boundary]
    name = var_text.strip()
    if not name:
        raise ValueError(empty_error)
    pos = base + (len(var_text) - len(var_text.lstrip()))
    filters: list[_FilterCall] = []
    idx = boundary
    while 0 <= idx < len(text):
        if text[idx] != ":":
            raise ValueError("路径模板的占位符表达式无效")
        idx += 1
        name_start = idx
        while idx < len(text) and text[idx] not in "(:":
            idx += 1
        raw = text[name_start:idx]
        filter_name = raw.strip()
        if not filter_name:
            raise ValueError("路径模板里的 filter 名为空")
        fpos = base + name_start + (len(raw) - len(raw.lstrip()))
        args: tuple[_FilterArg, ...] = ()
        if idx < len(text) and text[idx] == "(":
            args, idx = _parse_filter_args(text, idx + 1, base)
            if idx < len(text) and text[idx] != ":":
                raise ValueError("路径模板的占位符表达式无效")
        filters.append(_FilterCall(filter_name, args, fpos))
    return _Expr(name, tuple(filters), pos)


def _parse_filter_args(text: str, idx: int, base: int) -> tuple[tuple[_FilterArg, ...], int]:
    """解析 filter 参数列表; ``idx`` 指向 ``(`` 之后, 返回参数与闭括号之后的偏移."""
    args: list[_FilterArg] = []
    while True:
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            raise ValueError("路径模板里的 filter 参数未闭合")
        char = text[idx]
        if char == ")":
            return tuple(args), idx + 1
        if char == ",":
            raise ValueError("路径模板里有空的 filter 参数")
        if char == '"':
            end = text.find('"', idx + 1)
            if end < 0:
                raise ValueError("路径模板里的字符串参数未闭合")
            args.append(text[idx + 1 : end])
            idx = end + 1
        else:
            arg, idx = _parse_bare_arg(text, idx, base)
            args.append(arg)
        # 参数之后必须紧跟 `,` 或 `)`, 尾逗号视为空参数.
        if idx >= len(text):
            raise ValueError("路径模板里的 filter 参数未闭合")
        if text[idx] == ",":
            idx += 1
            if idx >= len(text) or text[idx] == ")":
                raise ValueError("路径模板里有空的 filter 参数")
        elif text[idx] != ")":
            raise ValueError("路径模板里的 filter 参数缺少逗号分隔")


def _parse_bare_arg(text: str, idx: int, base: int) -> tuple[_FilterArg, int]:
    """未加引号的参数: 整数字面量或嵌套表达式; 顶层 ``,`` 与 ``)`` 终止参数.

    嵌套表达式的括号与引号计入层级, 因此 `{a:default(b:default("x"))}` 的内层括号不会提前闭合参数.
    """
    start = idx
    depth = 0
    in_quote = False
    while idx < len(text):
        char = text[idx]
        if in_quote:
            if char == '"':
                in_quote = False
        elif char == '"':
            in_quote = True
        elif char == "(":
            depth += 1
        elif depth > 0 and char == ")":
            depth -= 1
        elif depth == 0 and char in ",)":
            break
        idx += 1
    if in_quote:
        raise ValueError("路径模板里的字符串参数未闭合")
    if depth > 0:
        raise ValueError("路径模板里的 filter 参数未闭合")
    raw = text[start:idx]
    token = raw.strip()
    if not token:
        raise ValueError("路径模板里有空的 filter 参数")
    if token.isdigit():
        return int(token), idx
    if token.startswith(":"):
        raise ValueError("路径模板里的 filter 参数无效")
    arg_base = base + start + (len(raw) - len(raw.lstrip()))
    return _parse_expr(token, arg_base, "路径模板里的 filter 参数无效"), idx


class Parser:
    """`{name}` 占位符 + filter 链 + 可选 `|k=v` 值映射 + `[...]` / `[[...]]` 可选组 + `{% if %}` 条件块.

    `[...]` 组界不输出; 组内**直接**占位符全部为空串则整组丢弃, 有一个非空则渲染
    (空的那个输出空串). 嵌套组各自判断, 不并入外层.
    `[[...]]` 同样, 有值时把结果包一层 ``[]``.
    名字里的 ``?`` 只是标识符的一部分, 没有运算含义.
    `{name|原值=输出}` 在查出值之后替换; `{name|=缺省}` 将空源值映成缺省.
    filter 链用 ``:`` 分隔 (``|`` 已被值映射占用), 从左到右应用; 带参 filter 用括号,
    字符串字面量必须加双引号. 值映射先于 filter 应用.
    条件块 `{% if a,b %}…{% else %}…{% endif %}`: 条件是逗号分隔的变量名, 全部有值才真;
    无 elif 与运算符, 块可嵌套.
    """

    def __init__(self, src: str) -> None:
        self.src = src
        self.i = 0
        # 条件块正文解析的出口指令 ({% else %} 或 {% endif %}); 由 _parse_if_block 读取.
        self._block_tag = ""

    def parse(self) -> tuple[_Node, ...]:
        return tuple(self._parse_nodes(None))

    def _parse_nodes(self, closer: str | None, block: bool = False) -> list[_Node]:
        nodes: list[_Node] = []
        buf: list[str] = []

        def flush() -> None:
            if buf:
                nodes.append(_Literal("".join(buf)))
                buf.clear()

        while self.i < len(self.src):
            if closer is not None and self.src.startswith(closer, self.i):
                flush()
                self.i += len(closer)
                return nodes
            if self.src.startswith("{%", self.i):
                flush()
                pos = self.i
                tag = self._read_block_tag()
                if tag in ("else", "endif"):
                    if block:
                        self._block_tag = tag
                        return nodes
                    raise ValueError(f"路径模板里的 {{{tag}}} 缺少对应的 {{% if %}}")
                nodes.append(self._parse_if_block(tag, pos))
                continue
            if self.src.startswith("[[", self.i):
                flush()
                self.i += 2
                nodes.append(_Group(tuple(self._parse_nodes("]]")), wrap=True))
                continue
            if self.src[self.i] == "[":
                flush()
                self.i += 1
                nodes.append(_Group(tuple(self._parse_nodes("]")), wrap=False))
                continue
            if self.src[self.i] == "{":
                flush()
                nodes.append(self._parse_placeholder())
                continue
            if self.src[self.i] == "]":
                raise ValueError("路径模板里的 ] 没有匹配的 [")
            buf.append(self.src[self.i])
            self.i += 1
        flush()
        if closer is not None:
            raise ValueError("路径模板里的可选组未闭合")
        if block:
            raise ValueError("路径模板里的条件块未闭合")
        return nodes

    def _read_block_tag(self) -> str:
        """读取并跳过 ``{% … %}`` 指令, 返回指令内容; 调用方已确认当前位于 ``{%``."""
        start = self.i + 2
        end = self.src.find("%}", start)
        if end < 0:
            raise ValueError("路径模板里的条件块指令未闭合")
        self.i = end + 2
        return self.src[start:end].strip()

    def _parse_if_block(self, tag: str, pos: int) -> _IfBlock:
        """``tag`` 是已读取的 if 指令内容; 解析条件、正文与可选 else 分支.

        正文解析在 {% else %} / {% endif %} 处返回并把指令留在 ``_block_tag``;
        嵌套块由正文里的同一入口递归消费自己的分支.
        """
        head, _, cond_text = tag.partition(" ")
        if head != "if":
            raise ValueError("路径模板里的条件块指令无效")
        condition = _parse_condition(cond_text)
        body = self._parse_nodes(None, block=True)
        tag_end = self._block_tag
        orelse: tuple[_Node, ...] | None = None
        if tag_end == "else":
            orelse = tuple(self._parse_nodes(None, block=True))
            if self._block_tag != "endif":
                raise ValueError("路径模板里出现重复的 {% else %}")
        return _IfBlock(condition, tuple(body), orelse, pos)

    def _parse_placeholder(self) -> _Placeholder:
        self.i += 1
        start = self.i
        while self.i < len(self.src) and self.src[self.i] != "}":
            if self.src[self.i] == "{":
                raise ValueError("路径模板的占位符里嵌套了花括号")
            self.i += 1
        if self.i >= len(self.src):
            raise ValueError("路径模板里的占位符未闭合")
        body = self.src[start : self.i]
        self.i += 1
        if not body:
            raise ValueError("路径模板里有空的占位符")
        expr_text, map_text = _split_mapping(body)
        expr = _parse_expr(expr_text, start, "路径模板里有空的占位符")
        mapping = _parse_placeholder_mapping(expr.name, map_text) if map_text is not None else ()
        return _Placeholder(expr, mapping)


def _direct_placeholders(nodes: Sequence[_Node]) -> tuple[_Placeholder, ...]:
    return tuple(node for node in nodes if isinstance(node, _Placeholder))


def _lookup(name: str, variables: dict[str, str]) -> str:
    if name in variables:
        return variables[name]
    return _UNKNOWN


# filter 集合与参数个数. 渲染与保存校验共用同一张表.
_FILTER_ARITY: dict[str, int] = {
    "default": 1,
    "upper": 0,
    "lower": 0,
    "trim": 0,
    "truncate": 1,
    "split": 1,
    "first": 0,
    "last": 0,
    "replace": 2,
}
_FILTER_NAMES = frozenset(_FILTER_ARITY)


def _has_value(value: str | list[str]) -> bool:
    """Amane 的「有值」判定: 空串 / 空列表 / "Unknown" / "None" 均为无值, 其余有值."""
    if isinstance(value, list):
        return bool(value)
    return value not in ("", _UNKNOWN, "None")


def _utf8_prefix(data: bytes) -> str:
    """解码为合法 UTF-8 的最长前缀, 不切开多字节字符."""
    while data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            data = data[:-1]
    return ""


def _truncate_utf8(value: str, limit: int) -> str:
    """按 UTF-8 字节截断, 不切开多字节字符, 无省略号."""
    if limit < 0:
        raise ValueError("路径模板里的 filter truncate 的字节数不能为负")
    return _utf8_prefix(value.encode("utf-8")[:limit])


def _eval_arg(arg: _FilterArg, variables: dict[str, str]) -> str:
    """求 filter 参数的值: 字符串字面量原样; 表达式按变量取值后应用其 filter 链."""
    if isinstance(arg, str):
        return arg
    if isinstance(arg, int):
        raise ValueError("路径模板里的 filter 参数不能是整数字面量")
    result = _apply_filters(arg.filters, variables, _lookup(arg.name, variables))
    if not isinstance(result, str):
        raise ValueError("路径模板里的 filter 参数以 split 结尾, 链尾必须是 first 或 last")
    return result


def _apply_filter(call: _FilterCall, variables: dict[str, str], value: str | list[str]) -> str | list[str]:
    if call.name not in _FILTER_NAMES:
        raise ValueError(f"路径模板里的 filter 未知: {call.name!r}")
    expected = _FILTER_ARITY[call.name]
    if len(call.args) != expected:
        raise ValueError(f"路径模板里的 filter {call.name!r} 需要 {expected} 个参数")
    if call.name == "first" or call.name == "last":
        if not isinstance(value, list):
            raise ValueError(f"路径模板里的 filter {call.name!r} 只能作用于 split 的结果")
        if not value:
            return ""
        return value[0] if call.name == "first" else value[-1]
    if isinstance(value, list):
        raise ValueError(f"路径模板里的 filter {call.name!r} 不能作用于列表: split 之后只能使用 first 或 last")
    if call.name == "default":
        if _has_value(value):
            return value
        return _eval_arg(call.args[0], variables)
    if call.name == "upper":
        return value.upper()
    if call.name == "lower":
        return value.lower()
    if call.name == "trim":
        return value.strip()
    if call.name == "truncate":
        limit = call.args[0]
        if not isinstance(limit, int):
            raise ValueError("路径模板里的 filter truncate 的参数必须是整数字面量")
        return _truncate_utf8(value, limit)
    if call.name == "replace":
        return value.replace(_eval_arg(call.args[0], variables), _eval_arg(call.args[1], variables))
    separator = _eval_arg(call.args[0], variables)
    if not separator:
        raise ValueError("路径模板里的 filter split 的分隔符不能为空")
    return value.split(separator)


def _apply_filters(chain: tuple[_FilterCall, ...], variables: dict[str, str], value: str) -> str | list[str]:
    result: str | list[str] = value
    for call in chain:
        result = _apply_filter(call, variables, result)
    return result


def _resolve(node: _Placeholder, variables: dict[str, str]) -> str:
    """查出值后按映射改写, 再从左到右应用 filter 链. 空源只在写出 `{name|=缺省}` 时变成非空, 否则仍为空, 可选组省略."""
    raw = _lookup(node.expr.name, variables)
    mapped = dict(node.mapping)
    result = _apply_filters(node.expr.filters, variables, mapped.get(raw, raw))
    if not isinstance(result, str):
        raise ValueError(f"路径模板里的占位符 {{{node.expr.name}}} 的 filter 链以 split 结尾, 链尾必须是 first 或 last")
    return result


def _render_nodes(nodes: Sequence[_Node], variables: dict[str, str]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, _Literal):
            parts.append(node.text)
        elif isinstance(node, _Placeholder):
            parts.append(_resolve(node, variables))
        elif isinstance(node, _IfBlock):
            if all(_has_value(_lookup(item, variables)) for item in node.condition):
                parts.append(_render_nodes(node.body, variables))
            elif node.orelse is not None:
                parts.append(_render_nodes(node.orelse, variables))
        else:
            slots = _direct_placeholders(node.children)
            if slots and all(_resolve(item, variables) == "" for item in slots):
                continue
            inner = _render_nodes(node.children, variables)
            parts.append(f"[{inner}]" if node.wrap else inner)
    return "".join(parts)


def _expr_uses(expr: _Expr, name: str) -> bool:
    """表达式是否取用该占位符: 变量名本身或任一 filter 参数表达式命中都算."""
    if expr.name == name:
        return True
    return any(isinstance(arg, _Expr) and _expr_uses(arg, name) for call in expr.filters for arg in call.args)


def _nodes_use_placeholder(nodes: Sequence[_Node], name: str) -> bool:
    for node in nodes:
        if isinstance(node, _Placeholder) and _expr_uses(node.expr, name):
            return True
        if isinstance(node, _Group) and _nodes_use_placeholder(node.children, name):
            return True
        if isinstance(node, _IfBlock):
            if name in node.condition:
                return True
            if _nodes_use_placeholder(node.body, name):
                return True
            if node.orelse is not None and _nodes_use_placeholder(node.orelse, name):
                return True
    return False


# 占位符白名单: schema 占位符加运行时别名 dir. 保存路径用它拒绝拼写错误, 渲染不受约束.
_VALID_NAMES = frozenset(PLACEHOLDERS) | {"dir"}


def validate_template(source: str) -> None:
    """保存与切换路径的白名单校验: 占位符名、filter 名、参数形状与 filter 链类型.

    校验错误指出名称与在模板源里的位置. 渲染不做这些检查, 未知占位符仍输出 `Unknown`.
    """
    _validate_nodes(Parser(source).parse())


def _validate_nodes(nodes: Sequence[_Node]) -> None:
    for node in nodes:
        if isinstance(node, _Placeholder):
            _validate_expr(node.expr)
        elif isinstance(node, _Group):
            _validate_nodes(node.children)
        elif isinstance(node, _IfBlock):
            for item in node.condition:
                if item not in _VALID_NAMES:
                    raise ValueError(f"路径模板在位置 {node.pos} 使用未知占位符: {{{item}}}")
            _validate_nodes(node.body)
            if node.orelse is not None:
                _validate_nodes(node.orelse)


def _validate_expr(expr: _Expr) -> None:
    if expr.name not in _VALID_NAMES:
        raise ValueError(f"路径模板在位置 {expr.pos} 使用未知占位符: {{{expr.name}}}")
    after_split = False
    for call in expr.filters:
        _validate_filter(call, after_split)
        after_split = call.name == "split"
    if after_split:
        raise ValueError(f"路径模板在位置 {expr.pos} 的 filter 链以 split 结尾, 链尾必须是 first 或 last")


def _validate_filter(call: _FilterCall, after_split: bool) -> None:
    if call.name not in _FILTER_NAMES:
        raise ValueError(f"路径模板在位置 {call.pos} 使用未知 filter: {call.name!r}")
    expected = _FILTER_ARITY[call.name]
    if len(call.args) != expected:
        if expected == 0:
            raise ValueError(f"路径模板在位置 {call.pos} 的 filter {call.name!r} 不接受参数")
        raise ValueError(f"路径模板在位置 {call.pos} 的 filter {call.name!r} 需要 {expected} 个参数")
    if after_split and call.name not in ("first", "last"):
        raise ValueError(
            f"路径模板在位置 {call.pos} 的 filter {call.name!r} 不能作用于 split 的结果: split 之后只能使用 first 或 last"
        )
    if call.name in ("first", "last") and not after_split:
        raise ValueError(f"路径模板在位置 {call.pos} 的 filter {call.name!r} 只能作用于 split 的结果")
    if call.name == "truncate":
        if not isinstance(call.args[0], int):
            raise ValueError(f"路径模板在位置 {call.pos} 的 filter truncate 的参数必须是整数字面量")
    else:
        for arg in call.args:
            if isinstance(arg, int):
                raise ValueError(f"路径模板在位置 {call.pos} 的 filter {call.name!r} 不接受整数字面量")
    if call.name == "split" and isinstance(call.args[0], str) and not call.args[0]:
        raise ValueError(f"路径模板在位置 {call.pos} 的 filter split 的分隔符不能为空")
    for arg in call.args:
        if isinstance(arg, _Expr):
            _validate_expr(arg)


def _clip_field(value: str) -> str:
    """按 UTF-8 字节截断并追加省略号, 不得切开多字节字符. 省略号计入上限."""
    data = value.encode("utf-8")
    if len(data) <= PATH_FIELD_MAX_BYTES:
        return value
    budget = PATH_FIELD_MAX_BYTES - _ELLIPSIS_BYTES
    if budget <= 0:
        return PATH_FIELD_ELLIPSIS
    return _utf8_prefix(data[:budget]) + PATH_FIELD_ELLIPSIS


def _safe(value: str | None) -> str | None:
    """元数据字段写入路径段或模板正文: 控制字符与目录分隔符替换为空格, Windows 保留字符删除.

    控制字符留在名字里会让 Windows 创建失败 (ERROR_INVALID_NAME), 出现在 STRM 正文里会截断内容.
    """
    if not value:
        return None
    return (
        _CONTROL_CHARS.sub(" ", value)
        .replace("/", " ")
        .replace("\\", " ")
        .replace(":", " ")
        .replace("*", "")
        .replace("?", "")
        .replace('"', "")
        .replace("<", "")
        .replace(">", "")
        .replace("|", "")
        .strip()
    )


def _lexical_abs(path: Path) -> Path:
    """转为绝对路径并消除 ``.`` / ``..``, 但不跟随符号链接."""
    return Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100


def _relpath_or_empty(path: Path | None, library_root: Path | None) -> str:
    """路径相对库根目录的 POSIX 路径; 缺任一侧或不在库根之下时为空串.

    按字面路径求相对, 不解析符号链接: 源在库内的符号链接目录下时仍保留源路径形式.
    """
    if path is None or library_root is None:
        return ""
    try:
        rel = _lexical_abs(path).relative_to(_lexical_abs(library_root))
    except ValueError:
        return ""
    return rel.as_posix() if rel.parts else ""


def video_relpath(dest: Path, library_root: Path) -> str:
    """视频必须在本库内; 返回相对库根目录的 POSIX 路径."""
    rel = _relpath_or_empty(dest, library_root)
    if not rel:
        raise ValueError(f"视频目标路径 {_lexical_abs(dest)} 不在库根 {_lexical_abs(library_root)} 之下")
    return rel


def actress_names(actors: Sequence[str], genders: Mapping[str, ActorGender] | None = None) -> list[str]:
    """按 ``Metadata.actors`` 顺序排除明确 ``male``; 未入表或 ``unknown`` 保留."""
    if not actors:
        return []
    if genders is None:
        return list(actors)
    return [name for name in actors if genders.get(name, ActorGender.UNKNOWN) is not ActorGender.MALE]


def _build_variables(
    metadata: Metadata,
    ext: str = "",
    source_path: Path | None = None,
    file_info: FileInfo | None = None,
    cd: int | None = None,
    actor_genders: Mapping[str, ActorGender] | None = None,
    library_root: Path | None = None,
) -> TemplateVariables:
    year = metadata.release[:4] if metadata.release and len(metadata.release) >= 4 else None
    actor = metadata.actors[0] if metadata.actors else None
    actresses = actress_names(metadata.actors, actor_genders)
    actress = actresses[0] if actresses else None
    source_dir = source_path.parent if source_path else None
    raw_dir = source_dir.name if source_dir else ""
    raw_name = source_path.stem if source_path else ""
    if cd is None and file_info is not None:
        cd = file_info.cd
    prefix, suffix = split_number(metadata.number) if metadata.number else ("", "")

    return {
        "number": metadata.number,
        "prefix": prefix,
        "suffix": suffix,
        "title": _safe(metadata.title) or metadata.number,
        "actor": _safe(actor) or _UNKNOWN,
        "actors": ",".join(metadata.actors) if metadata.actors else _UNKNOWN,
        "actress": _safe(actress) or _UNKNOWN,
        "actresses": ",".join(actresses) if actresses else _UNKNOWN,
        "studio": _safe(metadata.studio) or _UNKNOWN,
        "publisher": _safe(metadata.publisher) or _UNKNOWN,
        "series": _safe(metadata.series) or _UNKNOWN,
        "year": year or _UNKNOWN,
        "release": _safe(metadata.release) or _UNKNOWN,
        "ext": ext,
        "raw_dir": raw_dir,
        "raw_name": raw_name,
        "raw_relpath": _relpath_or_empty(source_path, library_root),
        "cd?": str(cd) if cd is not None else "",
        "sub?": "C" if file_info is not None and file_info.has_subtitle else "",
        "content_type": str(metadata.content_type),
        "mosaic?": str(nature)
        if (nature := mosaic_nature(file_info.mosaic if file_info else None, metadata.content_type))
        else "",
        "def?": file_info.definition if file_info is not None and file_info.definition else "",
        "video_dir": _UNKNOWN,
        "video_name": _UNKNOWN,
        "video_relpath": _UNKNOWN,
        "link_dir": _UNKNOWN,
        "link_name": _UNKNOWN,
        "raw_srt_name": _UNKNOWN,
        "index": _UNKNOWN,
        "index_padded": _UNKNOWN,
    }


@dataclass
class TemplateContext:
    """占位符取值. 相位顺序: metadata/file → apply_video → apply_link → (字幕再写入 ext / raw_srt_name)."""

    variables: dict[str, str]
    library_root: Path | None = None
    dest: Path | None = None

    @classmethod
    def from_mapping(cls, variables: dict[str, str]) -> TemplateContext:
        return cls(variables=variables)

    @classmethod
    def from_metadata(
        cls,
        metadata: Metadata,
        *,
        ext: str = "",
        source_path: Path | None = None,
        file_info: FileInfo | None = None,
        cd: int | None = None,
        actor_genders: Mapping[str, ActorGender] | None = None,
        library_root: Path | None = None,
    ) -> TemplateContext:
        built = _build_variables(metadata, ext, source_path, file_info, cd, actor_genders, library_root)
        variables = cast(dict[str, str], {**built})
        # {dir} 是 {raw_dir} 的别名, 不下发 schema.
        variables["dir"] = built["raw_dir"]
        return cls(variables=variables, library_root=library_root)

    def apply_video(self, dest: Path, library_root: Path) -> None:
        self.dest = dest
        self.library_root = library_root
        self.variables["video_dir"] = str(dest.parent)
        self.variables["video_name"] = dest.stem
        self.variables["video_relpath"] = _relpath_or_empty(dest, library_root)

    def apply_link(self, link: Path | None) -> None:
        if link is not None:
            self.variables["link_dir"] = str(link.parent)
            self.variables["link_name"] = link.stem
            return
        self.variables["link_dir"] = self.variables.get("video_dir", "")
        self.variables["link_name"] = self.variables.get("video_name", "")

    def apply_subtitle(self, subtitle_source: Path) -> None:
        self.variables["ext"] = subtitle_source.suffix.lstrip(".")
        self.variables["raw_srt_name"] = subtitle_source.stem


def _template_keeps_absolute(template: str, variables: dict[str, str]) -> bool:
    """渲染后是否应保留绝对路径 (模板本身以 /、盘符、或绝对占位符开头)."""
    s = template.lstrip()
    if s.startswith(("/", "\\")):
        return True
    if _DRIVE.match(s):
        return True
    if s.startswith("{") and "}" in s:
        name = s[1 : s.index("}")].split("|", 1)[0].split(":", 1)[0].strip()
        val = variables.get(name, "")
        if val.startswith(("/", "\\")) or _DRIVE.match(val):
            return True
    return False


def _clean_segment(part: str) -> str:
    """路径段的字面值: 控制字符替换为空格, 去掉段尾的 ``.`` 与空格.

    Windows 创建时静默丢弃段尾的 ``.`` 与空格, 而 ``os.path.abspath`` 的剥除规则与之不同 (多段路径里的
    ``studio...`` 原样保留), 索引因此记录一个磁盘上不存在的名字. 先按创建结果对齐, 记录与磁盘便一致.
    ``.`` 与 ``..`` 原样保留, 逃逸检查依赖它们.
    """
    if part in (".", ".."):
        return part
    return _CONTROL_CHARS.sub(" ", part).rstrip(" .")


def _normalize_path_segments(rendered: str, *, keep_absolute: bool) -> str:
    """规范化每个路径段的字面值, 并丢弃空段: 空占位符不能把相对模板变成绝对路径.

    锚 (UNC 共享 / 盘符 / 根) 由 ``PureWindowsPath`` 从渲染结果切出并原样保留, 规范化只作用于锚之后.
    逐段重拼会把 UNC 的 ``\\\\`` 压成单个 ``/``, 而该结果在 Windows 上无盘符, 会被当成相对路径重新拼回
    库根, 触发误报的逃逸.
    """
    posix = rendered.replace("\\", "/")
    anchor = PureWindowsPath(rendered).anchor if keep_absolute else ""
    parts = [_clean_segment(part) for part in posix[len(anchor) :].split("/")]
    parts = [part for part in parts if part]
    if not anchor:
        return "/".join(parts)
    return PureWindowsPath(anchor, *parts).as_posix()


class TemplateEngine:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tree = Parser(source).parse()

    def uses(self, name: str) -> bool:
        return _nodes_use_placeholder(self.tree, name)

    def fill(self, ctx: TemplateContext) -> str:
        return _render_nodes(self.tree, ctx.variables)

    def clean(self, filled: str, ctx: TemplateContext) -> str:
        return filled

    def render(self, ctx: TemplateContext) -> str:
        return self.clean(self.fill(ctx), ctx)


class PathEngine(TemplateEngine):
    """路径输出: 填值时截断 title / actor / actors / actress / actresses, 按 Windows 创建结果规范化每个路径段, 再按库根目录 / safe_dirs 写成字面绝对路径."""

    def fill(self, ctx: TemplateContext) -> str:
        variables = {name: _clip_field(value) if name in _CLIP_KEYS else value for name, value in ctx.variables.items()}
        return _render_nodes(self.tree, variables)

    def clean(self, filled: str, ctx: TemplateContext) -> str:
        return _normalize_path_segments(filled, keep_absolute=_template_keeps_absolute(self.source, ctx.variables))

    def resolve(self, ctx: TemplateContext, base_path: Path, safe_dirs: Sequence[Path] | None) -> Path:
        """渲染并得到字面绝对路径, 强制约束在允许的边界内.

        边界规则:
        - 相对路径模板: 相对 base_path 解析, 必须是 base_path 的后代 (ALLOW_ALL 也不例外).
        - 绝对路径模板: 必须位于 base_path 或 safe_dirs 任一目录之下.

        渲染时用字面路径, 仅折叠 ``.`` / ``..`` 不跟随链接.
        """
        rendered = self.render(ctx)
        path = Path(rendered)
        if path.is_absolute():
            candidate = _lexical_abs(path)
            if safe_dirs is None:
                return candidate
            allowed_roots = [base_path, *safe_dirs]
            if not is_any_descendant(candidate, *allowed_roots):
                followed = candidate.resolve()
                raise ValueError(f"路径越界: 渲染结果 {followed} 超出基准目录 {base_path} 与安全目录")
            return candidate
        candidate = _lexical_abs(base_path / path)
        if not is_descendant(candidate, base_path):
            followed = candidate.resolve()
            raise ValueError(f"路径越界: 渲染结果 {followed} 超出基准目录 {base_path}")
        return candidate


class StrmEngine(TemplateEngine):
    """STRM 正文: 不折叠空段, 不截断 title / actor / actors / actress / actresses. 引用 `{video_relpath}` 时视频必须在本库内."""

    def clean(self, filled: str, ctx: TemplateContext) -> str:
        return filled if filled.endswith("\n") else f"{filled}\n"

    def render(self, ctx: TemplateContext) -> str:
        if self.uses("video_relpath"):
            if ctx.dest is None or ctx.library_root is None:
                raise ValueError("video_relpath 需要 dest 与 library_root")
            video_relpath(ctx.dest, ctx.library_root)
        return super().render(ctx)
