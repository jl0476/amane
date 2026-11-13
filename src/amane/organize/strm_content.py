"""STRM 正文不折叠空段, 以便保留 `https://`. 空模板写一行视频绝对路径."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator

from ..enums import ActorGender
from .backend import SyntaxName, template_backend
from .template import Parser, TemplateContext

if TYPE_CHECKING:
    from ..db.models import Metadata
    from ..parsing.file_info import FileInfo


def normalize_strm_content_template(value: str | None) -> str | None:
    """空白 strm_content_template 视为未设置 (写视频绝对路径)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def validate_strm_content_template(value: str) -> str:
    """必须为单行; 空白合法; 占位符语法与路径模板相同."""
    if "\n" in value or "\r" in value:
        raise ValueError("STRM 内容模板必须是单行")
    Parser(value).parse()
    return value


StrmContentTemplate = Annotated[str, AfterValidator(validate_strm_content_template)]


def render_strm_content(
    template: str | None,
    dest: Path,
    library_root: Path,
    metadata: Metadata,
    *,
    source_path: Path | None = None,
    file_info: FileInfo | None = None,
    link: Path | None = None,
    actor_genders: Mapping[str, ActorGender] | None = None,
    syntax: SyntaxName = "native",
) -> str:
    """空模板写 dest 的字面绝对路径.
    模板引用 `{video_relpath}` 且整理后的路径不在本库内时抛 ValueError, 不写出错误正文.

    ``syntax`` 是生效 ``NamingConfig.syntax``: 正文渲染属运行时分派的六个入口之一 (规格 3.2.1),
    两套后端的 STRM 语义一致 (不折叠空段 / 不截断长字段).
    """
    normalized = normalize_strm_content_template(template)
    if normalized is None:
        return f"{dest}\n"
    ctx = TemplateContext.from_metadata(
        metadata,
        ext=dest.suffix.lstrip("."),
        source_path=source_path,
        file_info=file_info,
        actor_genders=actor_genders,
        library_root=library_root,
    )
    ctx.apply_video(dest, library_root)
    ctx.apply_link(link)
    rendered = template_backend(syntax).render_strm(normalized, ctx)
    return rendered if rendered.endswith("\n") else f"{rendered}\n"
