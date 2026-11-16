"""水印布局: clockwise 轮转与 start / offset / gap 可配, golden 逐字节回归."""

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest
from PIL import Image
from pydantic import ValidationError

from amane.config.manager import WatermarkConfig
from amane.enums import WatermarkCorner, WatermarkKind
from amane.media.images import _clockwise_corners, _paste_stamps, apply_cover_watermarks
from amane.parsing import Mosaic

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "watermark_golden"

_ALL_AUTO: dict[WatermarkKind, WatermarkCorner] = dict.fromkeys(WatermarkKind, WatermarkCorner.AUTO)
# _stamp_stems 相位序: 中字 / 无码 / 破解 / 分辨率.
_PHASE_KINDS: tuple[WatermarkKind, ...] = (
    WatermarkKind.SUBTITLE,
    WatermarkKind.UNCENSORED,
    WatermarkKind.CRACKED,
    WatermarkKind.DEFINITION,
)
# clockwise 轮转的角位序 (顺时针一圈).
_WHEEL: tuple[WatermarkCorner, ...] = (
    WatermarkCorner.TOP_LEFT,
    WatermarkCorner.TOP_RIGHT,
    WatermarkCorner.BOTTOM_RIGHT,
    WatermarkCorner.BOTTOM_LEFT,
)
# 轮转期望表 (硬编码, 防实现自证): 相位 i → wheel[(start + i) % 4].
_ROTATION_TABLE: dict[int, list[WatermarkCorner]] = {
    0: [_WHEEL[0], _WHEEL[1], _WHEEL[2], _WHEEL[3]],
    1: [_WHEEL[1], _WHEEL[2], _WHEEL[3], _WHEEL[0]],
    2: [_WHEEL[2], _WHEEL[3], _WHEEL[0], _WHEEL[1]],
    3: [_WHEEL[3], _WHEEL[0], _WHEEL[1], _WHEEL[2]],
}
_RED = (255, 0, 0)
_BLUE = (0, 0, 255)


def _expected(name: str) -> bytes:
    return (_FIXTURE_ROOT / "expected" / f"{name}.jpg").read_bytes()


def _paint(
    tmp_path: Path,
    label: str,
    *,
    corners: Mapping[WatermarkKind, WatermarkCorner] | None = None,
    layout: Literal["stack", "clockwise"] = "stack",
    start: int = 0,
    gap: int | None = None,
    name: str = "default-scale-all-phases",
) -> bytes:
    """对 golden 输入副本按给定布局参数叠印 (相位: 中字 / 无码 / 破解 / 4K 全命中), 返回产物字节."""
    work = tmp_path / f"{label}.jpg"
    shutil.copy(_FIXTURE_ROOT / "inputs" / f"{name}.jpg", work)
    painted = apply_cover_watermarks(
        work,
        has_subtitle=True,
        uncensored=True,
        mosaic=Mosaic.CRACKED,
        definition="4K",
        corners=corners,
        layout=layout,
        start=start,
        gap=gap,
    )
    assert painted is True
    return work.read_bytes()


def test_clockwise_rotation_table() -> None:
    """相位→角位轮转表: AUTO 相位按相位序顺时针轮转, start 平移起始角, 超出四角回绕."""
    for start, expected in _ROTATION_TABLE.items():
        assert _clockwise_corners([WatermarkCorner.AUTO] * 4, start) == expected
    # 不足一圈: 从 start 起连取.
    assert _clockwise_corners([WatermarkCorner.AUTO] * 2, 1) == _ROTATION_TABLE[1][:2]
    # 超出一圈: 回绕后落点与同角首枚相同, 由分组叠放承担.
    assert _clockwise_corners([WatermarkCorner.AUTO] * 5, 0) == _ROTATION_TABLE[0] + [_WHEEL[0]]


def test_clockwise_explicit_occupies_slot(tmp_path: Path) -> None:
    """显式角位钉死不轮转, 但占据其角位的轮转位置: 后继 AUTO 相位跳过被占角."""
    # 中字钉在右上 (非其轮转位): 后继 AUTO 从 start 起轮转并跳过右上.
    assert _clockwise_corners([_WHEEL[1], WatermarkCorner.AUTO, WatermarkCorner.AUTO, WatermarkCorner.AUTO], 0) == [
        _WHEEL[1],
        _WHEEL[0],
        _WHEEL[2],
        _WHEEL[3],
    ]
    # 轮转指针不因显式相位前进: 破解钉在右下后, 后继 AUTO 从指针处继续.
    assert _clockwise_corners([WatermarkCorner.AUTO, _WHEEL[2], WatermarkCorner.AUTO, WatermarkCorner.AUTO], 0) == [
        _WHEEL[0],
        _WHEEL[2],
        _WHEEL[1],
        _WHEEL[3],
    ]
    # 端到端: clockwise 产物与「期望角位显式钉死」逐字节一致.
    mixed = {**_ALL_AUTO, WatermarkKind.SUBTITLE: WatermarkCorner.TOP_RIGHT}
    pinned: dict[WatermarkKind, WatermarkCorner] = {
        WatermarkKind.SUBTITLE: WatermarkCorner.TOP_RIGHT,
        WatermarkKind.UNCENSORED: WatermarkCorner.TOP_LEFT,
        WatermarkKind.CRACKED: WatermarkCorner.BOTTOM_RIGHT,
        WatermarkKind.DEFINITION: WatermarkCorner.BOTTOM_LEFT,
    }
    clockwise = _paint(tmp_path, "cw-occupy", corners=mixed, layout="clockwise")
    explicit = _paint(tmp_path, "occupy-pinned", corners=pinned)
    assert clockwise == explicit


@pytest.mark.parametrize("start", sorted(_ROTATION_TABLE))
def test_start_shifts_origin(tmp_path: Path, start: int) -> None:
    """start 平移起始角: clockwise + 全 AUTO 的产物与「轮转表期望角位显式钉死」逐字节一致."""
    pinned: dict[WatermarkKind, WatermarkCorner] = dict(zip(_PHASE_KINDS, _ROTATION_TABLE[start], strict=True))
    clockwise = _paint(tmp_path, f"cw-start{start}", corners=_ALL_AUTO, layout="clockwise", start=start)
    explicit = _paint(tmp_path, f"pin-start{start}", corners=pinned)
    assert clockwise == explicit


def test_offset_moves_anchor() -> None:
    """offset_x/offset_y 在 pad 落点上向内叠加: 四角落点像素断言 (0 = 现状落点)."""
    width = height = 100
    stamp_w, stamp_h = 10, 6
    for offset_x, offset_y in [(0, 0), (3, 5)]:
        img = Image.new("RGB", (width, height), _BLUE)
        stamp = Image.new("RGBA", (stamp_w, stamp_h), (*_RED, 255))
        grouped = {
            corner: [stamp]
            for corner in (
                WatermarkCorner.TOP_LEFT,
                WatermarkCorner.TOP_RIGHT,
                WatermarkCorner.BOTTOM_LEFT,
                WatermarkCorner.BOTTOM_RIGHT,
            )
        }
        _paste_stamps(img, grouped, pad=4, gap=4, offset_x=offset_x, offset_y=offset_y)
        pad_x, pad_y = 4 + offset_x, 4 + offset_y
        origins = {
            WatermarkCorner.TOP_LEFT: (pad_x, pad_y),
            WatermarkCorner.TOP_RIGHT: (width - pad_x - stamp_w, pad_y),
            WatermarkCorner.BOTTOM_LEFT: (pad_x, height - pad_y - stamp_h),
            WatermarkCorner.BOTTOM_RIGHT: (width - pad_x - stamp_w, height - pad_y - stamp_h),
        }
        for x, y in origins.values():
            assert img.getpixel((x, y)) == _RED
            assert img.getpixel((x + stamp_w - 1, y + stamp_h - 1)) == _RED
            assert img.getpixel((x - 1, y)) == _BLUE
            assert img.getpixel((x + stamp_w, y)) == _BLUE
            assert img.getpixel((x, y - 1)) == _BLUE
            assert img.getpixel((x, y + stamp_h)) == _BLUE


def test_gap_override(tmp_path: Path) -> None:
    """gap 可配: None = 现状推导 (与 golden 逐字节); 显式值生效 (同值等价现状, 异值改变落点)."""
    assert WatermarkConfig().gap is None
    # target_h = round(538 × 0.08) = 43, 现状推导 gap = max(4, 43 // 10) = 4.
    golden = _expected("default-scale-all-phases")
    assert _paint(tmp_path, "gap-none", gap=None) == golden
    assert _paint(tmp_path, "gap-4", gap=4) == golden
    assert _paint(tmp_path, "gap-40", gap=40) != golden
    # 单元层: 同角第二枚的落点间距 = gap.
    stamp = Image.new("RGBA", (10, 6), (*_RED, 255))
    for gap, second_top in [(4, 14), (12, 22)]:
        img = Image.new("RGB", (60, 60), _BLUE)
        _paste_stamps(img, {WatermarkCorner.TOP_LEFT: [stamp, stamp]}, pad=4, gap=gap)
        assert img.getpixel((5, 4)) == _RED
        assert img.getpixel((5, 9)) == _RED
        assert img.getpixel((5, second_top - 1)) == _BLUE
        assert img.getpixel((5, second_top)) == _RED


def test_gap_small_image_lower_bound(tmp_path: Path) -> None:
    """target_h < 40 小图: gap=None 推导含 max(4, …) 下限 — 与 golden 逐字节, 且高于无下限的商值."""
    # 640×480 → target_h = 38, 无下限推导 38 // 10 = 3, 下限抬高到 4.
    golden = _expected("sub40-unclamped")
    assert _paint(tmp_path, "sub-none", gap=None, name="sub40-unclamped") == golden
    assert _paint(tmp_path, "sub-3", gap=3, name="sub40-unclamped") != golden
    assert _paint(tmp_path, "sub-4", gap=4, name="sub40-unclamped") == golden


@pytest.mark.parametrize("start", [1, 2, 3])
def test_stack_ignores_start(tmp_path: Path, start: int) -> None:
    """stack 忽略 start: 产物与现状 golden 逐字节一致."""
    assert _paint(tmp_path, f"stack-start{start}", start=start) == _expected("default-scale-all-phases")


def test_clockwise_all_corners_pinned_falls_back() -> None:
    """四角全被显式钉死时 AUTO 相位回退: 从 start 起始角向内叠 (同角第二枚规则, 由分组叠放承担)."""
    pins = [_WHEEL[0], _WHEEL[1], _WHEEL[2], _WHEEL[3]]
    for start in range(4):
        assert _clockwise_corners([*pins, WatermarkCorner.AUTO], start) == [*pins, _WHEEL[start]]
    assert _clockwise_corners([*pins, WatermarkCorner.AUTO, WatermarkCorner.AUTO], 1) == [
        *pins,
        _WHEEL[1],
        _WHEEL[1],
    ]
    # 钉死顺序与轮转序错开时同样回退到 start 起始角.
    assert _clockwise_corners([_WHEEL[1], _WHEEL[2], _WHEEL[3], _WHEEL[0], WatermarkCorner.AUTO], 0) == [
        _WHEEL[1],
        _WHEEL[2],
        _WHEEL[3],
        _WHEEL[0],
        _WHEEL[0],
    ]


def test_invalid_layout_rejected() -> None:
    """新字段默认值 = 现状行为; 边界合法值放行; 非法 layout / 负 offset / 负 gap / start 越界拒绝."""
    base = WatermarkConfig()
    assert (base.layout, base.offset_x, base.offset_y, base.gap, base.start) == ("stack", 0, 0, None, 0)
    valid = WatermarkConfig(layout="clockwise", offset_x=0, offset_y=0, gap=0, start=3)
    assert (valid.layout, valid.gap, valid.start) == ("clockwise", 0, 3)
    invalid: tuple[tuple[str, object], ...] = (
        ("layout", "diagonal"),
        ("layout", "CLOCKWISE"),
        ("layout", None),
        ("offset_x", -1),
        ("offset_y", -1),
        ("gap", -1),
        ("start", 4),
        ("start", -1),
    )
    for field, value in invalid:
        with pytest.raises(ValidationError):
            WatermarkConfig(**{field: value})  # type: ignore[arg-type]
