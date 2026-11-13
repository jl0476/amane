from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from ...config import NamingConfig, NfoConfig, OrganizeCommonConfig
from ...db import Library
from ...enums import DownloadableResource, LibraryAutomation, LibraryIngest, LinkMode, MoveMode
from ...library import (
    DEFAULT_SUBTITLE_EXTENSIONS,
    DEFAULT_TRAILER_PATTERN,
    BlacklistPattern,
    MinFileSize,
    SubtitleExtensions,
    TrailerPattern,
    resolve_ingest_cloud_path,
)
from ...organize.backend import SYNTAXES, template_backend
from ...organize.path_templates import (
    EXTRAFANART_NAME_TEMPLATE_DEFAULT,
    EXTRAFANART_TEMPLATE_DEFAULT,
    FANART_TEMPLATE_DEFAULT,
    NFO_TEMPLATE_DEFAULT,
    PLACEHOLDER_MAP_KEYS,
    PLACEHOLDERS,
    POSTER_TEMPLATE_DEFAULT,
    SUBTITLE_TEMPLATE_DEFAULT,
    THUMB_TEMPLATE_DEFAULT,
    TRAILER_TEMPLATE_DEFAULT,
    VIDEO_TEMPLATE_DEFAULT,
    ExtrafanartNameTemplate,
    PathTemplate,
    sidecar_defaults,
)
from ...organize.strm_content import StrmContentTemplate
from ...parsing import ContentType
from ...utils.model import create_partial_model, subset_of


class LibraryCreateRequest(BaseModel):
    name: str | None = None
    """显示名; 留空则取路径 basename."""
    path: str
    automation: LibraryAutomation = LibraryAutomation.SCRAPE
    auto_organize: bool = False
    ingest: LibraryIngest = LibraryIngest.NATIVE
    cloud_path: str | None = None
    """CloudDrive 虚拟路径 (POSIX, 如 /115open/云下载). ingest=clouddrive 时必填."""
    recursive: bool = True
    patterns: list[str] = []
    move_mode: MoveMode = MoveMode.MOVE
    video_template: PathTemplate = VIDEO_TEMPLATE_DEFAULT
    link_template: PathTemplate | None = None
    link_mode: LinkMode = LinkMode.STRM
    strm_content_template: StrmContentTemplate | None = None
    thumb_template: PathTemplate | None = None
    poster_template: PathTemplate | None = None
    fanart_template: PathTemplate | None = None
    extrafanart_template: PathTemplate | None = None
    extrafanart_name_template: ExtrafanartNameTemplate | None = None
    nfo_template: PathTemplate | None = None
    trailer_template: PathTemplate | None = None
    subtitle_template: PathTemplate | None = None
    subtitle_extensions: SubtitleExtensions = Field(default_factory=lambda: list(DEFAULT_SUBTITLE_EXTENSIONS))
    write_nfo: bool = True
    copy_resources: list[DownloadableResource] = Field(default_factory=lambda: list(DownloadableResource))
    trailer_pattern: TrailerPattern = DEFAULT_TRAILER_PATTERN
    blacklist_patterns: list[BlacklistPattern] = []
    """文件名正则列表; 命中任一则扫描/监控跳过, 并作为无效文件进入清理清单."""
    min_file_size: MinFileSize = 0
    """最小视频大小 (字节). 小于此值的扫描视频跳过入库, 并作为无效文件进入清理清单. 0 关闭."""
    scan: bool = True

    @model_validator(mode="after")
    def _cloud_path_for_ingest(self) -> Self:
        self.cloud_path = resolve_ingest_cloud_path(self.ingest, self.cloud_path)
        return self


if TYPE_CHECKING:
    type LibraryUpdateRequest = Library

# 外部可写字段: 除主键 id 与 template_overrides 名单外的全部库配置列.
# template_overrides 只能经由请求中显式给出的模板字段追加 (路由按 model_fields_set 计算), 不允许直接改写.
LibraryUpdateRequest = create_partial_model(
    Library, ignore_fields=("id", "template_overrides"), partial_cls_name="LibraryUpdateRequest"
)


class LibraryResponse(BaseModel):
    id: int
    name: str
    path: str
    automation: LibraryAutomation
    auto_organize: bool
    ingest: LibraryIngest
    cloud_path: str | None = None
    recursive: bool
    patterns: list[str] = []
    move_mode: MoveMode
    video_template: str
    link_template: str | None = None
    link_mode: LinkMode
    strm_content_template: str | None = None
    thumb_template: str | None = None
    poster_template: str | None = None
    fanart_template: str | None = None
    extrafanart_template: str | None = None
    extrafanart_name_template: str | None = None
    nfo_template: str | None = None
    trailer_template: str | None = None
    subtitle_template: str | None = None
    subtitle_extensions: list[str]
    write_nfo: bool
    copy_resources: list[DownloadableResource]
    template_overrides: list[str]
    trailer_pattern: str
    blacklist_patterns: list[str]
    min_file_size: int


class LibraryListResponse(BaseModel):
    items: list[LibraryResponse]


class LibraryOverrideResponse(BaseModel):
    """单个 section 的覆盖; payload 为稀疏 JSON, 可空模板字段的显式取空以 {} 原样呈现."""

    section: str
    payload: dict[str, Any] = {}


class LibraryOverridesResponse(BaseModel):
    """该库的全部覆盖, 按 section 分组; 未写覆盖的 section 不出现."""

    overrides: dict[str, dict[str, Any]] = {}


class EffectiveConfigResponse(BaseModel):
    """GET /api/libraries/{id}/effective-config: 三节四层合成的生效配置 (无任务层)."""

    common: OrganizeCommonConfig
    naming: NamingConfig
    nfo: NfoConfig


@subset_of(Library, covariant=True)
class OptionalPathTemplateDefaults(BaseModel):
    """附属模板缺省 (Library 对应列为 None 时 ORGANIZE 使用; 剧照文件名在写盘时按张数渲染).

    取值是代码字面量, 两套语法各有一份 (Jinja 默认不是合法的自研语法模板), 不做语法校验.
    """

    thumb_template: str
    poster_template: str
    fanart_template: str
    extrafanart_template: str
    extrafanart_name_template: str
    nfo_template: str
    trailer_template: str
    subtitle_template: str


OPTIONAL_TEMPLATE_DEFAULTS = OptionalPathTemplateDefaults(
    thumb_template=THUMB_TEMPLATE_DEFAULT,
    poster_template=POSTER_TEMPLATE_DEFAULT,
    fanart_template=FANART_TEMPLATE_DEFAULT,
    extrafanart_template=EXTRAFANART_TEMPLATE_DEFAULT,
    extrafanart_name_template=EXTRAFANART_NAME_TEMPLATE_DEFAULT,
    nfo_template=NFO_TEMPLATE_DEFAULT,
    trailer_template=TRAILER_TEMPLATE_DEFAULT,
    subtitle_template=SUBTITLE_TEMPLATE_DEFAULT,
)


class PathTemplatePlaceholder(BaseModel):
    name: str
    map_keys: list[str] = Field(
        default_factory=list,
        description="有闭合取值时列出规范 key, 供 `{name|k=v}` 映射校验与 UI 提示. 空则不校验映射 key.",
    )


class PathTemplateSyntaxVariable(BaseModel):
    """占位符在对应语法下的变量名: Jinja 映射 4 个带 `?` 的占位符, 其余同名."""

    name: str
    variable: str


class PathTemplateSyntaxInfo(BaseModel):
    """一套语法的默认视频模板、附属模板默认与占位符名 → 变量名映射, 供前端切换语法时替换默认."""

    name: str
    default_video_template: str
    default_optional_templates: OptionalPathTemplateDefaults
    """附属模板在该语法下的内建默认: 与渲染期未设置时的回退同源 (规格 3.2.3)."""
    placeholders: list[PathTemplateSyntaxVariable]


class PathTemplateSchemaResponse(BaseModel):
    """与 ORGANIZE 实际使用的默认值同源; 路径模板由 resolve_paths 解析, 剧照文件名模板由写盘阶段渲染."""

    video_default: str
    optional_defaults: OptionalPathTemplateDefaults
    placeholders: list[PathTemplatePlaceholder]
    subtitle_extensions_default: list[str]
    syntaxes: list[PathTemplateSyntaxInfo]


def path_template_schema() -> PathTemplateSchemaResponse:
    return PathTemplateSchemaResponse(
        video_default=VIDEO_TEMPLATE_DEFAULT,
        optional_defaults=OPTIONAL_TEMPLATE_DEFAULTS,
        placeholders=[
            PathTemplatePlaceholder(name=name, map_keys=list(PLACEHOLDER_MAP_KEYS.get(name, ())))
            for name in (*PLACEHOLDERS, "dir")
        ],
        subtitle_extensions_default=list(DEFAULT_SUBTITLE_EXTENSIONS),
        syntaxes=[
            PathTemplateSyntaxInfo(
                name=described.name,
                default_video_template=described.default_video_template,
                default_optional_templates=OptionalPathTemplateDefaults(**sidecar_defaults(syntax)),
                placeholders=[
                    PathTemplateSyntaxVariable(name=p.name, variable=p.variable) for p in described.placeholders
                ],
            )
            for syntax in SYNTAXES
            for described in (template_backend(syntax).describe(),)
        ],
    )


class NamingPreviewRequest(BaseModel):
    """命名试算入参 (规格 3.2.4): 不落盘, 走与整理相同的 resolve_paths 代码路径.

    ``media_file_id`` 用真实文件与已刮削的元数据; 两者都缺省时用请求字段构造.
    ``library_id`` 缺省用全局默认模板; ``syntax`` 缺省用生效语法.
    """

    library_id: int | None = None
    media_file_id: int | None = None
    syntax: Literal["native", "jinja"] | None = None
    number: str = ""
    source_path: str = ""
    content_type: ContentType = ContentType.CENSORED
    title: str = ""
    actors: list[str] = []
    studio: str = ""
    release: str = ""


class NamingPreviewResponse(BaseModel):
    """解析结果 + 各产物渲染路径; subtitle 按 `{源文件主干}.srt` 的假想字幕渲染."""

    number: str
    content_type: str
    mosaic: str
    subtitle_tag: str
    definition: str
    video: str
    thumb: str
    poster: str
    fanart: str
    nfo: str
    trailer: str
    subtitle: str
