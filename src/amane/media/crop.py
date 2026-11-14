"""海报裁剪策略中立面: 策略解析与产物选择判定的唯一定义, 刮削物化与整理期重选双入口共用 (设计规格 §3.2/§3.3)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..enums import CropStrategy
from ..parsing import ContentType

CropAspect = Literal["legacy", "emby", "full"]

# crop_aspect 的固定产物比例: emby 海报 2:3, full 2.12:3.
_EMBY_RATIO = 2 / 3
_FULL_RATIO = 2.12 / 3

# 默认策略矩阵, strategy_map 缺键时回退: 仅封面为横版夹页的内容类型执行右侧裁剪, 其余类型保持候选原样.
_DEFAULT_STRATEGIES: dict[ContentType, CropStrategy] = {
    ContentType.CENSORED: CropStrategy.RIGHT,
    ContentType.UNCENSORED: CropStrategy.RIGHT,
}


@dataclass(frozen=True)
class CropPlan:
    """单次裁剪的完整决策: 请求策略、实际执行与产物比例."""

    requested: CropStrategy
    """配置请求的策略."""

    executed: CropStrategy
    """实际执行的像素策略; NONE 表示候选原样入选, 不执行像素裁剪."""

    ratio: float
    """产物宽高比 (w/h), 由 crop_aspect 解析."""

    low_res_height: int | None
    """候选质量底线阈值; None 表示默认态判定 (skip_ratio 短路)."""


def _resolve_ratio(aspect: CropAspect, poster_ratio: float) -> float:
    """legacy 沿用传入的 poster_ratio (含用户自定义值); emby 与 full 为固定比例."""
    if aspect == "legacy":
        return poster_ratio
    if aspect == "full":
        return _FULL_RATIO
    return _EMBY_RATIO


def _select_candidate(
    thumb_size: tuple[int, int] | None,
    candidate_size: tuple[int, int] | None,
    *,
    skip_ratio: float,
) -> bool:
    """默认态的候选选择: True 表示入选候选原样. 尺寸口径与 images.should_crop_poster 一致."""
    if thumb_size is None:
        return True
    if candidate_size is None:
        return False
    thumb_height = thumb_size[1]
    candidate_height = candidate_size[1]
    if thumb_height <= 0 or candidate_height <= 0:
        return True
    return candidate_height / thumb_height >= skip_ratio


def resolve_crop_plan(
    content_type: ContentType,
    *,
    strategy_map: Mapping[ContentType, CropStrategy],
    aspect: CropAspect,
    low_res_height: int | None,
    poster_ratio: float,
    skip_ratio: float,
    thumb_size: tuple[int, int] | None,
    candidate_size: tuple[int, int] | None,
) -> CropPlan:
    """解析单次裁剪决策 (规格 §3.3 默认态): 策略读取 strategy_map, 缺键回退默认矩阵 (censored/uncensored=right, 其余=none).

    产物选择: 无 thumb 或候选 b/h ≥ skip_ratio → 候选原样 (executed=NONE);
    无候选或 b/h < skip_ratio → 执行裁剪 (executed=requested).
    skip_ratio 为既有可配置阈值 (0.5–1.0), 由调用方显式传入.
    """
    requested = strategy_map.get(content_type, _DEFAULT_STRATEGIES.get(content_type, CropStrategy.NONE))
    if requested is CropStrategy.NONE or _select_candidate(thumb_size, candidate_size, skip_ratio=skip_ratio):
        executed = CropStrategy.NONE
    else:
        executed = requested
    return CropPlan(
        requested=requested,
        executed=executed,
        ratio=_resolve_ratio(aspect, poster_ratio),
        low_res_height=low_res_height,
    )
