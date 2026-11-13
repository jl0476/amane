from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import AfterValidator

from ..enums import ActorGender, LinkMode, MoveMode
from ..parsing.file_info import FileInfo
from ..utils.path import is_descendant, path_key
from .backend import NATIVE_VIDEO_TEMPLATE_DEFAULT, SyntaxName, template_backend
from .template import PLACEHOLDER_MAP_KEYS as PLACEHOLDER_MAP_KEYS
from .template import PLACEHOLDERS as PLACEHOLDERS
from .template import Parser, PathEngine, TemplateContext
from .template_jinja import JINJA_TEMPLATE_DEFAULTS

if TYPE_CHECKING:
    from ..config import NamingConfig
    from ..db.models import Library, Metadata


@dataclass
class ResolvedPaths:
    video: Path
    thumb: Path
    poster: Path
    fanart: Path
    extrafanart_dir: Path
    nfo: Path
    trailer: Path
    link: Path | None = None
    context: TemplateContext | None = None
    """渲染剧照文件名需要的同一份取值 (含 ``{video_*}`` / ``{link_*}``); 手工构造时可为 None."""


type SidecarKind = Literal["thumb", "poster", "fanart", "extrafanart", "nfo", "trailer", "subtitle"]
"""附属产物种类; subtitle 需要字幕源文件提供 `{ext}` / `{raw_srt_name}`, 由 resolve_subtitle_path 承载."""

ALL_SIDECARS: frozenset[SidecarKind] = frozenset(("thumb", "poster", "fanart", "extrafanart", "nfo", "trailer"))
"""resolve_paths 渲染的附属全集; subtitle 不在此渲染 (无字幕源可取)."""


@dataclass
class ResolvedSidecars:
    """按需渲染的附属产物; 未列入 artifacts 的字段为 None."""

    thumb: Path | None = None
    poster: Path | None = None
    fanart: Path | None = None
    extrafanart_dir: Path | None = None
    nfo: Path | None = None
    trailer: Path | None = None
    subtitle: Path | None = None


VIDEO_TEMPLATE_DEFAULT = NATIVE_VIDEO_TEMPLATE_DEFAULT
"""自研语法默认视频模板: 单源在 backend.py (规格 3.2.1 分派落点), 此处只为既有导入面保留名字."""
THUMB_TEMPLATE_DEFAULT = "{link_dir}/thumb.jpg"
POSTER_TEMPLATE_DEFAULT = "{link_dir}/poster.jpg"
FANART_TEMPLATE_DEFAULT = "{link_dir}/fanart.jpg"
EXTRAFANART_TEMPLATE_DEFAULT = "{link_dir}/extrafanart"
EXTRAFANART_NAME_TEMPLATE_DEFAULT = "{index}.jpg"
"""剧照文件名, 写盘时逐张渲染. 补零宽度取数量位数, 见 `render_extrafanart_names`."""
# 媒体服务器按视频文件名匹配 NFO, 所以默认跟随 {video_name} (含 CD / 中字段).
NFO_TEMPLATE_DEFAULT = "{link_dir}/{video_name}.nfo"
TRAILER_TEMPLATE_DEFAULT = "{link_dir}/trailer.mp4"
SUBTITLE_TEMPLATE_DEFAULT = "{link_dir}/{raw_srt_name}.{ext}"


def normalize_link_template(value: str | None) -> str | None:
    """空白 link_template 视为未设置 (不创建链接)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


_SIDE_CAR_FALLBACKS: dict[str, tuple[str, str]] = {
    "thumb_template": (THUMB_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["thumb_template"]),
    "poster_template": (POSTER_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["poster_template"]),
    "fanart_template": (FANART_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["fanart_template"]),
    "extrafanart_template": (EXTRAFANART_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["extrafanart_template"]),
    "extrafanart_name_template": (
        EXTRAFANART_NAME_TEMPLATE_DEFAULT,
        JINJA_TEMPLATE_DEFAULTS["extrafanart_name_template"],
    ),
    "nfo_template": (NFO_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["nfo_template"]),
    "trailer_template": (TRAILER_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["trailer_template"]),
    "subtitle_template": (SUBTITLE_TEMPLATE_DEFAULT, JINJA_TEMPLATE_DEFAULTS["subtitle_template"]),
}
"""附属模板的内建默认: 每套语法各一份字面量, 未设置时按生效语法回退, 保证任一语法下都能渲染."""


def sidecar_template(value: str | None, field: str, syntax: SyntaxName) -> str:
    """附属模板渲染取值: 未设置时回退到该语法的内建默认."""
    if value is not None:
        return value
    native, jinja = _SIDE_CAR_FALLBACKS[field]
    return jinja if syntax == "jinja" else native


def sidecar_defaults(syntax: SyntaxName) -> dict[str, str]:
    """附属模板字段在该语法下的内建默认 (规格 3.2.3): schema 下发与渲染回退共用同一份."""
    return {field: sidecar_template(None, field, syntax) for field in _SIDE_CAR_FALLBACKS}


def _syntax_of(naming: NamingConfig | None) -> SyntaxName:
    return naming.syntax if naming is not None else "native"


def validate_path_template(value: str) -> str:
    """空串合法."""
    Parser(value).parse()
    return value


# 常规取值含目录分隔符的占位符. `{raw_relpath}` / `{video_relpath}` 在源文件与链接都不在本库时是空串,
# 但按取值逐个判断不值得: 静态拒绝一条规则就够, 运行时检查兜住剩下的.
_PATH_VALUED_PLACEHOLDERS = ("video_dir", "link_dir", "video_relpath", "raw_relpath")

_INDEX_PLACEHOLDERS = ("index", "index_padded")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


def _extrafanart_index_placeholder(template: str) -> str | None:
    engine = PathEngine(template)
    return next((name for name in _INDEX_PLACEHOLDERS if engine.uses(name)), None)


def _unsupported_index_placeholder(template: str) -> str | None:
    """带宽度写法的序号占位符 (`{index:02}`), 名字本身不是可用占位符."""
    for body in _PLACEHOLDER_RE.findall(template):
        name = body.partition("|")[0].strip()
        if name.startswith("index") and name not in _INDEX_PLACEHOLDERS:
            return name
    return None


def validate_extrafanart_name_template(value: str) -> str:
    """剧照文件名必须落在目录内, 且带序号占位符, 否则多张剧照写成一个文件. 空串表示用默认值."""
    if not value.strip():
        return value
    Parser(value).parse()
    if "/" in value or "\\" in value:
        raise ValueError("剧照文件名模板只能是文件名, 不能含目录分隔符")
    unsupported = _unsupported_index_placeholder(value)
    if unsupported is not None:
        raise ValueError(f"剧照文件名模板不支持 {{{unsupported}}}: 补零用 {{index_padded}}, 宽度按张数自动推导")
    if _extrafanart_index_placeholder(value) is None:
        raise ValueError("剧照文件名模板必须含 {index} 或 {index_padded}")
    engine = PathEngine(value)
    busy = next((name for name in _PATH_VALUED_PLACEHOLDERS if engine.uses(name)), None)
    if busy is not None:
        raise ValueError(f"剧照文件名模板不能使用 {{{busy}}}: 它的取值是路径, 写不出文件名")
    return value


PathTemplate = Annotated[str, AfterValidator(validate_path_template)]
ExtrafanartNameTemplate = Annotated[str, AfterValidator(validate_extrafanart_name_template)]


def render_extrafanart_names(
    template: str, context: TemplateContext | None, count: int, syntax: SyntaxName = "native"
) -> list[str]:
    """按 1..count 渲染文件名; 宽度取数量位数, 同目录内字典序与序号一致.

    宽度按本轮数量算, 数量跨位数时全部名字变宽, 旧名字成为孤儿.
    结果不含目录: 由调用方拼到 ``ResolvedPaths.extrafanart_dir`` 上.
    """
    if count <= 0:
        return []
    if context is None:
        raise ValueError("剧照文件名需要路径渲染上下文")
    backend = template_backend(syntax)
    width = len(str(count))
    names: list[str] = []
    for index in range(1, count + 1):
        variables = {**context.variables, "index": str(index), "index_padded": f"{index:0{width}d}"}
        name = backend.render(template, variables)
        # 隐藏文件与无主干的名字媒体服务器不认, 分隔符会写到目录之外.
        if not name or name.startswith(".") or "/" in name or "\\" in name:
            raise ValueError(f"剧照文件名模板 {template!r} 渲染结果不是合法的文件名: {name!r}")
        if name in names:
            # 映射语法可以把不同序号映到同一个名字, 静默覆盖会让剧照少几张.
            raise ValueError(f"剧照文件名模板 {template!r} 渲染出重复名字: {name!r} (第 {index} 张)")
        names.append(name)
    return names


def render_path_template(template: str, variables: dict[str, str], syntax: SyntaxName = "native") -> str:
    """路径试算入口: 按生效语法分派渲染 (规格 3.2.1)."""
    return template_backend(syntax).render(template, variables)


def resolve_paths(
    library: Library,
    metadata: Metadata,
    ext: str = "",
    cd: int | None = None,
    source_path: Path | None = None,
    file_info: FileInfo | None = None,
    safe_dirs: Sequence[Path] | None = (),
    actor_genders: Mapping[str, ActorGender] | None = None,
    *,
    naming: NamingConfig | None = None,
    move_mode: MoveMode = MoveMode.MOVE,
) -> ResolvedPaths:
    """``safe_dirs is None`` 时不限制绝对模板写出的路径; 相对模板仍须在 base_path 下.

    ``naming`` 非 None 时全部模板字段取自该生效配置, 库行不再参与 (规格 3.1.3);
    None 时维持按库行取值.
    ``move_mode`` 为 inplace 时视频目标即源路径, video_template 被忽略, 也跳过
    「视频模板解析为库根目录」守卫 (规格 3.4.1); 附属产物按模板落在视频所在目录.
    """
    base_path = Path(library.path)
    ctx = _video_context(
        library,
        metadata,
        video_path=None,
        source_path=source_path,
        ext=ext,
        file_info=file_info,
        cd=cd,
        actor_genders=actor_genders,
    )
    backend = template_backend(_syntax_of(naming))

    # 先渲染视频, 注入 `{video_*}` 后再渲染链接与刮削产物.
    if move_mode is MoveMode.INPLACE:
        if source_path is None:
            raise ValueError("原地整理需要 source_path: 视频目标即源路径")
        video = source_path
    else:
        video_template = naming.video_template if naming is not None else library.video_template
        video = backend.resolve(video_template, ctx, base_path, safe_dirs)
        if path_key(video) == path_key(base_path):
            # 空模板或唯一占位符取值落空时, 视频路径退化为库根目录; 后续会以与原因无关的错误失败, 或按库根名在库外落盘.
            raise ValueError(f"视频模板解析为库根目录: {video_template!r}")
    ctx.apply_video(video, base_path)
    link = _resolve_link_path(library, ctx, base_path, safe_dirs, naming=naming)
    ctx.apply_link(link)

    sidecars = resolve_sidecar_paths(
        library,
        metadata,
        consume_path=link if link is not None else video,
        video_path=video,
        source_path=source_path,
        ext=ext,
        cd=cd,
        file_info=file_info,
        safe_dirs=safe_dirs,
        actor_genders=actor_genders,
        artifacts=ALL_SIDECARS,
        naming=naming,
    )

    # 全集传入: 六个附属产物必然都有值.
    assert sidecars.thumb is not None and sidecars.poster is not None and sidecars.fanart is not None
    assert sidecars.extrafanart_dir is not None and sidecars.nfo is not None and sidecars.trailer is not None
    return ResolvedPaths(
        video=video,
        thumb=sidecars.thumb,
        poster=sidecars.poster,
        fanart=sidecars.fanart,
        extrafanart_dir=sidecars.extrafanart_dir,
        nfo=sidecars.nfo,
        trailer=sidecars.trailer,
        link=link,
        context=ctx,
    )


def _video_context(
    library: Library,
    metadata: Metadata,
    *,
    video_path: Path | None,
    source_path: Path | None,
    ext: str,
    file_info: FileInfo | None,
    cd: int | None,
    actor_genders: Mapping[str, ActorGender] | None,
) -> TemplateContext:
    """上下文构造的唯一入口 (规格 3.4.4): resolve_paths 与 resolve_sidecar_paths 共用, 不得各写一份.

    ``video_path`` 非 None 时注入 ``{video_*}``; resolve_paths 传 None (视频尚未渲染, 链接模板
    不能先看到 ``{link_*}``), 渲染后自行 apply_video.
    """
    base_path = Path(library.path)
    ctx = TemplateContext.from_metadata(
        metadata,
        ext=ext,
        source_path=source_path,
        file_info=file_info,
        cd=cd,
        actor_genders=actor_genders,
        library_root=base_path,
    )
    if video_path is not None:
        ctx.apply_video(video_path, base_path)
    return ctx


def resolve_sidecar_paths(
    library: Library,
    metadata: Metadata,
    *,
    consume_path: Path,
    video_path: Path,
    source_path: Path | None = None,
    ext: str = "",
    cd: int | None = None,
    file_info: FileInfo | None = None,
    safe_dirs: Sequence[Path] | None = (),
    actor_genders: Mapping[str, ActorGender] | None = None,
    artifacts: Collection[SidecarKind] = ALL_SIDECARS,
    naming: NamingConfig | None = None,
) -> ResolvedSidecars:
    """渲染 ``artifacts`` 指定的附属产物, 不渲染视频与链接模板 (规格 3.4.4).

    ``video_path`` 用于注入 ``{video_*}``; ``consume_path`` 用于注入 ``{link_dir}`` / ``{link_name}``
    (有链接), 无链接时传 ``video_path`` 即退化为视频侧. ``source_path`` 是原始文件路径,
    ``{raw_dir}`` / ``{raw_name}`` / ``{raw_relpath}`` 由它派生 (template.py::_build_variables),
    必须传入, 否则含 raw_* 的附属模板渲染结果与整理时不同. artifacts 之外的产物不参与求值:
    不解析模板、不执行 resolve、不触发任何校验, 返回值对应字段为 None.
    subtitle 需要字幕源文件提供 ``{ext}`` / ``{raw_srt_name}``, 由 resolve_subtitle_path 承载.
    ``naming`` 与 ``artifacts`` 正交: naming 决定用哪套模板 (含语法), artifacts 决定渲染哪些产物.
    """
    syntax = _syntax_of(naming)
    backend = template_backend(syntax)
    ctx = _video_context(
        library,
        metadata,
        video_path=video_path,
        source_path=source_path,
        ext=ext,
        file_info=file_info,
        cd=cd,
        actor_genders=actor_genders,
    )
    ctx.apply_link(consume_path)
    base_path = Path(library.path)

    def _render(kind: str, field: str) -> Path | None:
        if kind not in artifacts:
            return None
        value = getattr(naming, field) if naming is not None else getattr(library, field)
        return backend.resolve(sidecar_template(value, field, syntax), ctx, base_path, safe_dirs)

    return ResolvedSidecars(
        thumb=_render("thumb", "thumb_template"),
        poster=_render("poster", "poster_template"),
        fanart=_render("fanart", "fanart_template"),
        extrafanart_dir=_render("extrafanart", "extrafanart_template"),
        nfo=_render("nfo", "nfo_template"),
        trailer=_render("trailer", "trailer_template"),
    )


def _resolve_link_path(
    library: Library,
    ctx: TemplateContext,
    base_path: Path,
    safe_dirs: Sequence[Path] | None,
    *,
    naming: NamingConfig | None = None,
) -> Path | None:
    """结果必须在库外: 库外指链接文件自身所在的位置, 判定只解析父目录链.

    链接文件已指向库内视频时, 判定的是文件落在哪里, 不是它指向的视频, 因此仍然通过校验.
    此时 `{video_dir}` / `{video_name}` / `{video_relpath}` 已注入, `{link_dir}` / `{link_name}` 尚未注入.
    """
    template = normalize_link_template(naming.link_template if naming is not None else library.link_template)
    if template is None:
        return None
    link = template_backend(_syntax_of(naming)).resolve(template, ctx, base_path, safe_dirs)
    link_mode = naming.link_mode if naming is not None else library.link_mode
    if link_mode == LinkMode.STRM:
        link = link.with_suffix(".strm")
    if is_descendant(link.parent, base_path):
        raise ValueError(f"链接模板必须解析到库根之外: {link} 位于 {base_path} 之下")
    return link


def resolve_subtitle_path(
    library: Library,
    metadata: Metadata,
    subtitle_source: Path,
    *,
    video_dir: Path,
    link_dir: Path | None = None,
    video_name: str = "",
    video_dest: Path | None = None,
    link_name: str | None = None,
    source_path: Path | None = None,
    file_info: FileInfo | None = None,
    safe_dirs: Sequence[Path] | None = (),
    actor_genders: Mapping[str, ActorGender] | None = None,
    naming: NamingConfig | None = None,
) -> Path:
    """`{ext}` / `{raw_srt_name}` 取自该字幕源文件; `{raw_name}` / `{raw_dir}` / `{raw_relpath}` 仍是视频源.
    `{video_dir}` / `{video_name}` 为整理后视频父目录与文件名 (不含扩展名);
    `{video_relpath}` 为整理后视频相对库根目录的路径 (`video_dest` 未传时为空);
    `{link_dir}` / `{link_name}` 为链接父目录与文件名 (未设置链接时分别与视频侧相同).
    默认模板保持原文件名与扩展名.

    ``naming`` 非 None 时 subtitle_template 取自该生效配置, 库行不再参与 (规格 3.1.3).
    """
    base_path = Path(library.path)
    ctx = TemplateContext.from_metadata(
        metadata,
        source_path=source_path,
        file_info=file_info,
        actor_genders=actor_genders,
        library_root=base_path,
    )
    if video_dest is not None:
        ctx.apply_video(video_dest, base_path)
    else:
        ctx.variables["video_dir"] = str(video_dir)
        ctx.variables["video_name"] = video_name
        ctx.variables["video_relpath"] = ""
    ctx.apply_link(None)
    if link_dir is not None:
        ctx.variables["link_dir"] = str(link_dir)
    if link_name is not None:
        ctx.variables["link_name"] = link_name
    ctx.apply_subtitle(subtitle_source)
    syntax = _syntax_of(naming)
    template = sidecar_template(
        naming.subtitle_template if naming is not None else library.subtitle_template, "subtitle_template", syntax
    )
    return template_backend(syntax).resolve(template, ctx, base_path, safe_dirs)


# --- 语法切换验证与保存路径校验 (规格 3.2.2 / 3.2.3) ---

NAMING_TEMPLATE_FIELDS: tuple[str, ...] = (
    "video_template",
    "link_template",
    "strm_content_template",
    "thumb_template",
    "poster_template",
    "fanart_template",
    "extrafanart_template",
    "extrafanart_name_template",
    "nfo_template",
    "trailer_template",
    "subtitle_template",
)
"""naming section 中参与渲染的模板字段; video 必有值, link / strm 未设置时不渲染, 附属未设置按语法回退."""

# 样例取值覆盖全部 30 占位符 + dir 别名: 取占位符名本身 (非空、无花括号), 探针只关心残留.
PROBE_VARIABLES: dict[str, str] = {name: name for name in (*PLACEHOLDERS, "dir")}

# 未替换的残留: 自研占位符形态, 或 Jinja 的 `{{` / `{%` 前缀. 样例取值不含花括号, 渲染结果中出现即语法不匹配.
_RESIDUAL_RE = re.compile(r"\{\{|\{%|\{[\w?]+\}")


def validate_naming_templates(syntax: SyntaxName, values: Mapping[str, str | None], *, probe: bool) -> None:
    """naming 模板的语法感知校验 (规格 3.2.2 / 3.2.3).

    每个非 None 模板先过目标引擎的保存校验 (未知占位符 / 未知 filter / 语法形状);
    ``probe=True`` (语法切换) 再做渲染探针: 用覆盖全部占位符的样例取值渲染, 残留
    `{ident}` / `{{` / `{%` 判失败 —— Jinja 把自研占位符当普通文本, 「能解析」不证明模板有效.
    失败抛 ValueError, 消息指明模板字段与残留位置; None 跳过 (运行时不启用该模板).
    """
    backend = template_backend(syntax)
    for field in NAMING_TEMPLATE_FIELDS:
        if field not in values or values[field] is None:
            continue
        source = values[field]
        if not isinstance(source, str):
            raise ValueError(f"模板 {field} 必须是字符串")
        try:
            backend.validate(source)
            if probe:
                rendered = backend.render(source, PROBE_VARIABLES)
                residual = _RESIDUAL_RE.search(rendered)
                if residual:
                    raise ValueError(
                        f"渲染结果存在未替换的残留 {residual.group()!r} (位置 {residual.start()}): 模板语法与目标不符"
                    )
        except ValueError as exc:
            raise ValueError(f"模板 {field}: {exc}") from exc
