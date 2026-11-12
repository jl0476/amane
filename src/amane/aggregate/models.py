from dataclasses import dataclass, field

from ..crawlers.models import FilmActor


@dataclass
class SourcedScore:
    # 各爬虫负责归一化到 0-100.
    site: str
    score: float


@dataclass
class AggregatedMetadata:
    number: str

    title: str | None = None
    studio: str | None = None
    publisher: str | None = None
    release: str | None = None
    runtime: int | None = None
    series: str | None = None
    plot: str | None = None
    # 源站原文双轨: 与 title / plot 平行的独立字段, 翻译只改写后者.
    original_title: str | None = None
    original_plot: str | None = None
    actors: list[FilmActor] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)

    poster_urls: list[str] = field(default_factory=list)
    thumb_urls: list[str] = field(default_factory=list)
    trailer_urls: list[str] = field(default_factory=list)

    extrafanart_urls: dict[str, list[str]] = field(default_factory=dict)

    # 不同站点评分体系不同, 必须保留来源.
    scores: list[SourcedScore] = field(default_factory=list)

    external_ids: dict[str, str] = field(default_factory=dict)
    source_urls: dict[str, str] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    # 逐字段语言声明: 键为字段名, 值为该字段取值语言; 只记录来源声明过的字段.
    field_languages: dict[str, str] = field(default_factory=dict)
    # 主来源详情页地址: 取标题胜出来源的 source_url, 标题来源无地址时留空, 不沿链回退.
    website: str | None = None


@dataclass
class AggregateResult:
    metadata: AggregatedMetadata
    field_sources: dict[str, str]
    failed_sites: list[str]
    sites_queried: list[str]
    raw: dict[str, dict]
    log: str
