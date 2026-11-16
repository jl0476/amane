"""水印逐类开关: golden 逐字节基准、kinds 默认值、相位过滤、配置校验."""

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

import pytest
from pydantic import ValidationError

from amane.config.manager import WatermarkConfig
from amane.enums import WatermarkCorner, WatermarkKind
from amane.media.images import apply_cover_watermarks
from amane.parsing import Mosaic

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "watermark_golden"
_ALL_TRUE: dict[WatermarkKind, bool] = dict.fromkeys(WatermarkKind, True)

_FOUR_CORNERS: dict[WatermarkKind, WatermarkCorner] = {
    WatermarkKind.SUBTITLE: WatermarkCorner.TOP_LEFT,
    WatermarkKind.UNCENSORED: WatermarkCorner.TOP_RIGHT,
    WatermarkKind.CRACKED: WatermarkCorner.BOTTOM_RIGHT,
    WatermarkKind.DEFINITION: WatermarkCorner.BOTTOM_LEFT,
}


class _Phases(NamedTuple):
    has_subtitle: bool
    uncensored: bool
    mosaic: Mosaic | None
    definition: str | None


_ALL_HIT_CRACKED = _Phases(True, True, Mosaic.CRACKED, "4K")
_ALL_HIT_LEAKED = _Phases(True, True, Mosaic.LEAKED, "1080p")
_NO_HIT = _Phases(False, False, None, None)

# 与 fixtures/watermark_golden/README.md 的清单一致; corners/kinds 是各用例叠印配置.
_GOLDEN_CASES: list[
    tuple[str, _Phases, dict[WatermarkKind, WatermarkCorner] | None, dict[WatermarkKind, bool] | None]
] = [
    ("default-scale-all-phases", _ALL_HIT_CRACKED, None, None),
    ("multi-phase-leaked", _ALL_HIT_LEAKED, None, None),
    ("four-corners", _ALL_HIT_CRACKED, _FOUR_CORNERS, None),
    ("sub40-unclamped", _ALL_HIT_CRACKED, None, None),
    ("small-boundary", _ALL_HIT_CRACKED, None, None),
    ("small-four-corners", _ALL_HIT_CRACKED, _FOUR_CORNERS, None),
    ("poster-single-phase", _Phases(True, False, None, None), None, None),
    # 显式全 true 的 kinds dict 也必须产出与基线一致的默认行为.
    ("default-scale-all-phases", _ALL_HIT_CRACKED, None, _ALL_TRUE),
]


def _paint(
    tmp_path: Path,
    name: str,
    phases: _Phases,
    *,
    corners: Mapping[WatermarkKind, WatermarkCorner] | None = None,
    kinds: Mapping[WatermarkKind, bool] | None = None,
) -> tuple[bool, bytes]:
    """对 golden 输入副本按给定配置叠印, 返回 (是否落盘, 产物字节)."""
    work = tmp_path / f"{name}.jpg"
    shutil.copy(_FIXTURE_ROOT / "inputs" / f"{name}.jpg", work)
    painted = apply_cover_watermarks(
        work,
        has_subtitle=phases.has_subtitle,
        uncensored=phases.uncensored,
        mosaic=phases.mosaic,
        definition=phases.definition,
        corners=corners,
        kinds=kinds,
    )
    return painted, work.read_bytes()


@pytest.mark.parametrize(("name", "phases", "corners", "kinds"), _GOLDEN_CASES)
def test_golden_baseline_unchanged(
    name: str,
    phases: _Phases,
    corners: dict[WatermarkKind, WatermarkCorner] | None,
    kinds: dict[WatermarkKind, bool] | None,
    tmp_path: Path,
) -> None:
    """默认配置产物与 c412b72 基线逐字节一致 (含小图下限边界与显式全 true kinds)."""
    painted, out = _paint(tmp_path, name, phases, corners=corners, kinds=kinds)
    assert painted is True
    assert out == (_FIXTURE_ROOT / "expected" / f"{name}.jpg").read_bytes()


def test_kinds_default_all_true() -> None:
    """默认全 true; 缺 key 按开关语义补齐, 未知 key 沿 corners 先例丢弃."""
    assert WatermarkConfig().kinds == _ALL_TRUE
    # key 是字符串字面量: 模拟 API payload 的 JSON 口径, pydantic lax 模式收窄为枚举.
    config = WatermarkConfig(kinds={"subtitle": False})  # type: ignore[arg-type]
    assert config.kinds[WatermarkKind.SUBTITLE] is False
    assert config.kinds == {**_ALL_TRUE, WatermarkKind.SUBTITLE: False}
    assert WatermarkConfig(kinds={"nope": True}).kinds == _ALL_TRUE  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("gated", "remaining"),
    [
        ({WatermarkKind.SUBTITLE}, _Phases(False, True, Mosaic.CRACKED, "4K")),
        ({WatermarkKind.UNCENSORED}, _Phases(True, False, Mosaic.CRACKED, "4K")),
        ({WatermarkKind.CRACKED}, _Phases(True, True, None, "4K")),
        ({WatermarkKind.DEFINITION}, _Phases(True, True, Mosaic.CRACKED, None)),
    ],
)
def test_kinds_false_removes_stamp(gated: set[WatermarkKind], remaining: _Phases, tmp_path: Path) -> None:
    """kinds=false 的相位不出现在产物: 产物等价于关掉对应相位后全开叠印."""
    kinds = {**_ALL_TRUE, **dict.fromkeys(gated, False)}
    painted, out = _paint(tmp_path, "default-scale-all-phases", _ALL_HIT_CRACKED, kinds=kinds)
    assert painted is True
    _, expected = _paint(tmp_path, "default-scale-all-phases", remaining)
    assert out == expected


def test_kinds_multi_combination(tmp_path: Path) -> None:
    """多类组合表测试: 逐类关闭的组合产物等价于对应相位缺席; 全关则不落盘."""
    cases: list[tuple[_Phases, set[WatermarkKind], _Phases]] = [
        (
            _ALL_HIT_CRACKED,
            {WatermarkKind.UNCENSORED, WatermarkKind.DEFINITION},
            _Phases(True, False, Mosaic.CRACKED, None),
        ),
        (_ALL_HIT_CRACKED, {WatermarkKind.SUBTITLE, WatermarkKind.CRACKED}, _Phases(False, True, None, "4K")),
        (_ALL_HIT_LEAKED, {WatermarkKind.LEAKED}, _Phases(True, True, None, "1080p")),
        (_ALL_HIT_LEAKED, {WatermarkKind.LEAKED, WatermarkKind.DEFINITION}, _Phases(True, True, None, None)),
        # 全部相位关掉: 无可画角标, 不落盘.
        (_ALL_HIT_CRACKED, set(WatermarkKind), _NO_HIT),
        (_ALL_HIT_LEAKED, set(WatermarkKind), _NO_HIT),
    ]
    for phases, gated, remaining in cases:
        kinds = {**_ALL_TRUE, **dict.fromkeys(gated, False)}
        painted, out = _paint(tmp_path, "default-scale-all-phases", phases, kinds=kinds)
        expected_painted, expected = _paint(tmp_path, "default-scale-all-phases", remaining)
        assert painted is expected_painted, gated
        assert out == expected, gated


@pytest.mark.parametrize(
    "kinds",
    [
        {"subtitle": "maybe"},
        {"subtitle": [True]},
        [True],
    ],
)
def test_kinds_invalid_value_rejected(kinds: object) -> None:
    """非 bool 值或非 dict 容器 → ValidationError."""
    with pytest.raises(ValidationError):
        WatermarkConfig(kinds=kinds)  # type: ignore[arg-type]


def test_unknown_key_rejected() -> None:
    """顶层未知键 → ValidationError (extra="forbid")."""
    with pytest.raises(ValidationError):
        WatermarkConfig(no_such_key=1)  # type: ignore[arg-type]
