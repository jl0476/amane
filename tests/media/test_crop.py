"""测试裁剪策略中立面: 策略解析、默认态判定与 cv2 可用性访问器."""

import subprocess
import sys
import types
from enum import StrEnum
from typing import Literal

import pytest

from amane.enums import CropStrategy
from amane.media import optional_cv2
from amane.media.crop import CropPlan, resolve_crop_plan
from amane.parsing import ContentType

# 基线尺寸: thumb 为标准封面 (800, 538); 候选高 430, b/h ≈ 0.799 < 0.9, 命中裁剪路径.
_BASE_THUMB = (800, 538)
_CROP_CANDIDATE = (379, 430)


def _resolve_plan(
    content_type: ContentType = ContentType.CENSORED,
    strategy_map: dict[ContentType, CropStrategy] | None = None,
    *,
    aspect: Literal["legacy", "emby", "full"] = "legacy",
    low_res_height: int | None = None,
    poster_ratio: float = 0.7,
    skip_ratio: float = 0.9,
    thumb_size: tuple[int, int] | None = _BASE_THUMB,
    candidate_size: tuple[int, int] | None = _CROP_CANDIDATE,
) -> CropPlan:
    """resolve_crop_plan 的测试封装: 基线为有码类型 + 裁剪路径 + legacy 比例."""
    return resolve_crop_plan(
        content_type,
        strategy_map={} if strategy_map is None else strategy_map,
        aspect=aspect,
        low_res_height=low_res_height,
        poster_ratio=poster_ratio,
        skip_ratio=skip_ratio,
        thumb_size=thumb_size,
        candidate_size=candidate_size,
    )


def test_crop_strategy_in_enums_neutral_layer() -> None:
    """CropStrategy 定义于 enums 中立层; 干净进程双序导入 config 与 media 均无环 (R2 PLAN-01 回归)."""
    assert issubclass(CropStrategy, StrEnum)
    assert CropStrategy.NONE.value == "none"
    assert CropStrategy.RIGHT.value == "right"
    assert CropStrategy.FACE.value == "face"

    for statement in ("import amane.config; import amane.media", "import amane.media; import amane.config"):
        result = subprocess.run([sys.executable, "-c", statement], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"双序导入失败: {statement}\n{result.stderr}"


def test_optional_cv2_accessor_two_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """available()/module() 两态经 monkeypatch 模块存在性验证: module() 返回 None 或模块本身."""
    present = types.ModuleType("cv2")
    monkeypatch.setattr(optional_cv2, "cv2", present)
    assert optional_cv2.available() is True
    assert optional_cv2.module() is present

    monkeypatch.setattr(optional_cv2, "cv2", None)
    assert optional_cv2.available() is False
    assert optional_cv2.module() is None


def test_resolve_crop_plan_defaults() -> None:
    """空策略映射回退默认矩阵: censored/uncensored=right, 其余 5 类型=none; requested == executed."""
    expected = {
        ContentType.CENSORED: CropStrategy.RIGHT,
        ContentType.UNCENSORED: CropStrategy.RIGHT,
        ContentType.CHINESE: CropStrategy.NONE,
        ContentType.WESTERN: CropStrategy.NONE,
        ContentType.FC2: CropStrategy.NONE,
        ContentType.AMATEUR: CropStrategy.NONE,
        ContentType.HENTAI: CropStrategy.NONE,
    }
    assert len(ContentType) == 7  # 默认矩阵必须覆盖全部内容类型
    for content_type, strategy in expected.items():
        plan = _resolve_plan(content_type)
        assert plan.requested is strategy, content_type
        assert plan.executed is strategy, content_type

    # 稀疏映射: 显式条目覆盖默认矩阵, 未列出的类型回退默认.
    plan = _resolve_plan(ContentType.HENTAI, strategy_map={ContentType.HENTAI: CropStrategy.RIGHT})
    assert plan.requested is CropStrategy.RIGHT
    assert plan.executed is CropStrategy.RIGHT


@pytest.mark.parametrize(
    ("aspect", "poster_ratio", "expected_ratio"),
    [
        pytest.param("legacy", 0.7, 0.7, id="legacy-default"),
        pytest.param("legacy", 0.75, 0.75, id="legacy-custom"),
        pytest.param("emby", 0.7, 2 / 3, id="emby"),
        pytest.param("full", 0.7, 2.12 / 3, id="full"),
    ],
)
def test_legacy_ratio_uses_poster_ratio(
    aspect: Literal["legacy", "emby", "full"], poster_ratio: float, expected_ratio: float
) -> None:
    """legacy 沿用传入的 poster_ratio (含自定义 0.75); emby=2/3; full=2.12/3."""
    plan = _resolve_plan(aspect=aspect, poster_ratio=poster_ratio)
    assert plan.ratio == expected_ratio


@pytest.mark.parametrize(
    ("thumb_size", "candidate_size", "expected_executed", "desc"),
    [
        ((800, 538), (379, 538), CropStrategy.NONE, "b/h=1.0 ≥ skip_ratio: 用候选原样"),
        ((800, 538), (379, 430), CropStrategy.RIGHT, "b/h≈0.799 < skip_ratio: 执行裁剪"),
        ((800, 538), None, CropStrategy.RIGHT, "无候选: 无比较对象, 用裁剪产物"),
        (None, (379, 430), CropStrategy.NONE, "无 thumb: 不裁, 用候选原样"),
        ((800, 0), (379, 430), CropStrategy.NONE, "退化高度: 不裁, 用候选原样"),
    ],
)
def test_low_res_none_uses_skip_ratio_short_circuit(
    thumb_size: tuple[int, int] | None,
    candidate_size: tuple[int, int] | None,
    expected_executed: CropStrategy,
    desc: str,
) -> None:
    """low_res_height=None 时判定采用 skip_ratio 短路 (现状算法); 短路只影响 executed, requested 保持请求值."""
    plan = _resolve_plan(thumb_size=thumb_size, candidate_size=candidate_size)
    assert plan.requested is CropStrategy.RIGHT, desc
    assert plan.executed is expected_executed, desc


def test_skip_ratio_is_explicit_input() -> None:
    """同一组尺寸 (b/h ≈ 0.929): skip_ratio=0.9 → 用候选原样; skip_ratio=0.95 → 执行裁剪 (可配置阈值, 不固定 0.9)."""
    plan_keep = _resolve_plan(skip_ratio=0.9, thumb_size=(800, 538), candidate_size=(379, 500))
    assert plan_keep.requested is CropStrategy.RIGHT
    assert plan_keep.executed is CropStrategy.NONE

    plan_crop = _resolve_plan(skip_ratio=0.95, thumb_size=(800, 538), candidate_size=(379, 500))
    assert plan_crop.executed is CropStrategy.RIGHT
