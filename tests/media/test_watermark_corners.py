"""水印角位: 值域扩展 AUTO、stack 布局解析与显式角位钉死, golden 逐字节回归."""

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from amane.config.manager import WatermarkConfig
from amane.enums import WatermarkCorner, WatermarkKind
from amane.media.images import apply_cover_watermarks
from amane.parsing import Mosaic

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "watermark_golden"

_ALL_TOP_LEFT: dict[WatermarkKind, WatermarkCorner] = dict.fromkeys(WatermarkKind, WatermarkCorner.TOP_LEFT)
_ALL_AUTO: dict[WatermarkKind, WatermarkCorner] = dict.fromkeys(WatermarkKind, WatermarkCorner.AUTO)

_FOUR_CORNERS: dict[WatermarkKind, WatermarkCorner] = {
    WatermarkKind.SUBTITLE: WatermarkCorner.TOP_LEFT,
    WatermarkKind.UNCENSORED: WatermarkCorner.TOP_RIGHT,
    WatermarkKind.CRACKED: WatermarkCorner.BOTTOM_RIGHT,
    WatermarkKind.DEFINITION: WatermarkCorner.BOTTOM_LEFT,
}
# 与 _FOUR_CORNERS 等价, 仅中字改走 AUTO: stack 下解析为 TOP_LEFT, 产物应逐字节一致.
_MIXED_AUTO: dict[WatermarkKind, WatermarkCorner] = {
    **_FOUR_CORNERS,
    WatermarkKind.SUBTITLE: WatermarkCorner.AUTO,
}


def _expected(name: str) -> bytes:
    return (_FIXTURE_ROOT / "expected" / f"{name}.jpg").read_bytes()


def _paint(
    tmp_path: Path, name: str, *, corners: Mapping[WatermarkKind, WatermarkCorner] | None = None
) -> tuple[bool, bytes]:
    """对 golden 输入副本按给定角位叠印 (相位: 中字 / 无码 / 破解 / 4K 全命中), 返回 (是否落盘, 产物字节)."""
    work = tmp_path / f"{name}.jpg"
    shutil.copy(_FIXTURE_ROOT / "inputs" / f"{name}.jpg", work)
    painted = apply_cover_watermarks(
        work,
        has_subtitle=True,
        uncensored=True,
        mosaic=Mosaic.CRACKED,
        definition="4K",
        corners=corners,
    )
    return painted, work.read_bytes()


def test_corners_default_unchanged(tmp_path: Path) -> None:
    """corners 默认全 TOP_LEFT (现状值), 其产物与 golden 基准逐字节一致."""
    corners = WatermarkConfig().corners
    assert corners == _ALL_TOP_LEFT
    painted, out = _paint(tmp_path, "default-scale-all-phases", corners=corners)
    assert painted is True
    assert out == _expected("default-scale-all-phases")


@pytest.mark.parametrize("name", ["four-corners", "small-four-corners"])
def test_explicit_corner_pins_stamp(name: str, tmp_path: Path) -> None:
    """显式角位钉死 (现状语义): 四角钉死的产物与 golden 基准逐字节一致 (含触 _STAMP_MIN_HEIGHT 的小图)."""
    painted, out = _paint(tmp_path, name, corners=_FOUR_CORNERS)
    assert painted is True
    assert out == _expected(name)


def test_auto_resolves_stack_top_left(tmp_path: Path) -> None:
    """stack 布局下 AUTO = TOP_LEFT: "auto" 字面量归一化为枚举, 全 AUTO 产物与默认 golden 逐字节一致."""
    config = WatermarkConfig(corners=dict.fromkeys(WatermarkKind, "auto"))  # type: ignore[arg-type]
    assert config.corners == _ALL_AUTO
    painted, out = _paint(tmp_path, "default-scale-all-phases", corners=_ALL_AUTO)
    assert painted is True
    assert out == _expected("default-scale-all-phases")


def test_mixed_auto_and_explicit(tmp_path: Path) -> None:
    """混合配置: AUTO 相位按 stack 解析, 产物与四角全显式钉死逐字节一致."""
    painted, out = _paint(tmp_path, "four-corners", corners=_MIXED_AUTO)
    assert painted is True
    assert out == _expected("four-corners")


@pytest.mark.parametrize(
    "value",
    [
        "middle",
        "AUTO",
        "autos",
        5,
        True,
        None,
        ["top_left"],
    ],
)
def test_invalid_corner_value_rejected(value: object) -> None:
    """非枚举成员或非 "auto" 字面量 → ValidationError."""
    with pytest.raises(ValidationError):
        WatermarkConfig(corners={"subtitle": value})  # type: ignore[arg-type]
