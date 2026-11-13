"""命名模板后端抽象 (规格 §3.2.1): TemplateBackend Protocol、自研语法包装与运行时分派.

自研后端 render 经由 PathEngine 渲染路径 (截断长字段 + 段规范化), validate 委托 validate_template
做保存白名单校验, resolve 在渲染结果之上执行逃逸防护 (相对模板写出路径必须在库内, 绝对模板必须
在本库或 safe_dirs 内). Jinja2 后端见 template_jinja.py; ``template_backend`` 按生效语法选后端,
是全部渲染入口 (resolve_paths / resolve_subtitle_path / render_strm_content / render_extrafanart_names /
render_path_template) 的统一分派点.

STRM 正文与路径渲染语义不同 (不折叠空段、不截断、保留 ``https://``), 由 ``render_strm`` 承载,
两套后端各自实现同一语义.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from .template import (
    PLACEHOLDERS,
    PathEngine,
    StrmEngine,
    TemplateContext,
    TemplatePlaceholder,
    TemplateSyntax,
    validate_template,
)
from .template_jinja import JinjaTemplateBackend

NATIVE_VIDEO_TEMPLATE_DEFAULT = "{studio}/{number}/{number}[-CD{cd?}][-{sub?}].{ext}"
"""自研语法的默认视频模板. 渲染期回退与 schema 下发取同一常量 (path_templates.py 转出)."""

type SyntaxName = Literal["native", "jinja"]
"""模板语法名; 与 ``NamingConfig.syntax`` 同值域."""

SYNTAXES: tuple[SyntaxName, ...] = ("native", "jinja")
"""可用语法清单; schema 下发与遍历用, 顺序即下发顺序."""

__all__ = [
    "NATIVE_VIDEO_TEMPLATE_DEFAULT",
    "SYNTAXES",
    "NativeTemplateBackend",
    "SyntaxName",
    "TemplateBackend",
    "TemplatePlaceholder",
    "TemplateSyntax",
    "template_backend",
]


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

    def render_strm(self, source: str, ctx: TemplateContext) -> str:
        return StrmEngine(source).render(ctx)

    def validate(self, source: str) -> None:
        validate_template(source)

    def describe(self) -> TemplateSyntax:
        return TemplateSyntax(
            name="native",
            default_video_template=NATIVE_VIDEO_TEMPLATE_DEFAULT,
            placeholders=tuple(TemplatePlaceholder(name=name, variable=name) for name in (*PLACEHOLDERS, "dir")),
        )


def template_backend(syntax: SyntaxName) -> NativeTemplateBackend | JinjaTemplateBackend:
    """按生效语法返回渲染后端; 两个实现无状态, 共享单例 (Jinja 后端持有沙箱环境)."""
    match syntax:
        case "native":
            return _NATIVE_BACKEND
        case "jinja":
            return _JINJA_BACKEND
    raise ValueError(f"未知模板语法: {syntax!r}")


_NATIVE_BACKEND = NativeTemplateBackend()
_JINJA_BACKEND = JinjaTemplateBackend()
