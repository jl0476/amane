"""命名模板后端抽象: TemplateBackend Protocol (render / validate / describe) 与自研语法的包装实现.

自研后端 render 经由 PathEngine 渲染路径 (截断长字段 + 段规范化), validate 委托 validate_template
做保存白名单校验, resolve 在渲染结果之上执行逃逸防护 (相对模板写出路径必须在库内, 绝对模板必须
在本库或 safe_dirs 内). Jinja2 后端见 template_jinja.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .template import PLACEHOLDERS, PathEngine, TemplateContext, validate_template

NATIVE_VIDEO_TEMPLATE_DEFAULT = "{studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}"
"""自研语法的默认视频模板. path_templates.py::VIDEO_TEMPLATE_DEFAULT 是渲染期取用的同一字面量, 两处必须一致."""


@dataclass(frozen=True, slots=True)
class TemplatePlaceholder:
    """占位符与其在语法内的引擎变量名: 保存模板用占位符名, 渲染取值用变量名."""

    name: str
    variable: str


@dataclass(frozen=True, slots=True)
class TemplateSyntax:
    """语法描述: 语法名、默认视频模板与占位符到变量名的对应, 供语法切换校验与 schema 下发."""

    name: str
    default_video_template: str
    placeholders: tuple[TemplatePlaceholder, ...]


class TemplateBackend(Protocol):
    """渲染命名模板."""

    def render(self, source: str, variables: Mapping[str, str]) -> str: ...

    def validate(self, source: str) -> None: ...

    def describe(self) -> TemplateSyntax: ...


class NativeTemplateBackend:
    """自研语法后端: 包装 template.py 的 PathEngine 与 validate_template."""

    def render(self, source: str, variables: Mapping[str, str]) -> str:
        return PathEngine(source).render(TemplateContext.from_mapping(dict(variables)))

    def resolve(self, source: str, ctx: TemplateContext, base_path: Path, safe_dirs: Sequence[Path] | None) -> Path:
        return PathEngine(source).resolve(ctx, base_path, safe_dirs)

    def validate(self, source: str) -> None:
        validate_template(source)

    def describe(self) -> TemplateSyntax:
        return TemplateSyntax(
            name="native",
            default_video_template=NATIVE_VIDEO_TEMPLATE_DEFAULT,
            placeholders=tuple(TemplatePlaceholder(name=name, variable=name) for name in (*PLACEHOLDERS, "dir")),
        )
