"""整理期海报候选重建与用户产物分类.

纯函数, 不执行任何 IO: raw 快照、锁定集合与 Resource 反解结果 (内部 URL → locator args, 经
``ResourceStore.get_by_url_hash``) 由调用方异步读取后注入. 判定次序 (规格 §3.6, 决策 13/18):
user_product 短路 → 字段链路由.

user_product 双路径: poster_urls 内部 URL 的 locator args 以 ``box:`` 开头 (手动裁剪), 或
``poster_urls`` ∈ locked_fields (PATCH 外部 URL 经 _merge_auto_locks 自动锁定); ``box:`` 判定
优先于锁定. 候选 = raw 各站点原始 poster_urls 沿当前配置的 POSTER 字段链拼接, 快照键的
``site:lang`` 语言限定形式按站点名归组. 候选只由 raw 快照经字段链路由得出: 既有 poster_urls
不贡献候选 —— 字段链已排除的来源不得因此重新参与重选. 候选为空即回退信号.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..aggregate.engine import FieldPriority, SourceName, compile_priority
from ..enums import MetadataField
from .images import CROP_BOX_ARGS_PREFIX

_POSTER_FIELD = MetadataField.POSTER_URLS


@dataclass(frozen=True)
class CandidateResult:
    """海报重选输入: 候选列表与两条信号.

    user_product 为真时调用方以 poster_urls 首项为本轮产物, 不重选 (candidates 为空);
    fallback 为真时调用方回退物化图复制路径; 两者皆假时按 candidates 顺序重选.
    """

    candidates: list[str]
    user_product: bool
    fallback: bool


def _is_manual_crop(url: str, locator_args: Mapping[str, str]) -> bool:
    """内部 URL 反解出的 locator args 以 ``box:`` 开头即为手动裁剪产物; 未反解或未派生不是."""
    args = locator_args.get(url)
    return args is not None and args.startswith(CROP_BOX_ARGS_PREFIX)


def _snapshot_poster_urls(snapshot: Mapping[str, Any]) -> list[str]:
    """站点快照的原始 poster_urls; 旧形态单值字符串宽容展开, 其余非法形态视为不提供."""
    value = snapshot.get(_POSTER_FIELD.value)
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [url for url in value if isinstance(url, str) and url]
    return []


def rebuild_poster_candidates(
    *,
    raw: Mapping[str, Mapping[str, Any]],
    poster_urls: Sequence[str],
    locator_args: Mapping[str, str],
    locked_fields: Collection[str],
    route: Sequence[SourceName],
    field_priority: FieldPriority,
    field_blacklist: Mapping[MetadataField, Sequence[SourceName]] | None = None,
) -> CandidateResult:
    """重建海报候选并给出用户产物 / 回退信号.

    参数均为调用方异步读取后的注入值: ``raw`` 为 ``Metadata.raw`` 快照; ``poster_urls`` 为
    ``Metadata.poster_urls`` 当前产物 (只用于用户产物判定, 不贡献候选); ``locator_args`` 只收录
    派生资源的内部 URL (args 取自 Resource 记录); ``locked_fields`` 为 ``Metadata.locked_fields``;
    ``route`` / ``field_priority`` / ``field_blacklist`` 为当前配置的 content_routes 与字段链配置,
    POSTER 字段链经 :func:`compile_priority` 得到, 字段链外站点不参与.

    候选拼接与聚合同口径: 相同地址只保留首次出现.
    """
    user_product = any(_is_manual_crop(url, locator_args) for url in poster_urls) or (
        _POSTER_FIELD.value in locked_fields
    )
    if user_product:
        return CandidateResult(candidates=[], user_product=True, fallback=False)

    by_site: dict[str, list[str]] = {}
    for key, snapshot in raw.items():
        # 快照键为站点名, 或多语言站点的 "site:lang" 限定形式; 站点名不含冒号.
        site = key.split(":", 1)[0]
        by_site.setdefault(site, []).extend(_snapshot_poster_urls(snapshot))

    chain = compile_priority(route, field_priority, field_blacklist)[_POSTER_FIELD]
    candidates = [url for site in chain for url in by_site.get(site, ())]
    # 候选只由 raw 快照经字段链路由得出: 既有 poster_urls 不贡献候选, 字段链已排除的来源
    # 不得因此重新参与重选 (规格 §3.6 第 3 条). 候选为空即回退现状路径 (第 4 条).
    candidates = list(dict.fromkeys(candidates))
    return CandidateResult(candidates=candidates, user_product=False, fallback=not candidates)
