"""水印样式目录化: 四级查找链逐级命中、custom_folder 完全替代、旧位置兼容与 style 校验."""

from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from amane.config.manager import WatermarkConfig
from amane.media.images import apply_cover_watermarks
from amane.media.watermarks import load_stamp

# 无内置章图的合法 stem: 命中哪一级由该级文件颜色唯一确定, 未命中即 None.
_STEM = "bogus"
_STYLE_NAME = "blue"
_CUSTOM = (255, 0, 0, 255)
_STYLE = (0, 128, 0, 255)
_LEGACY = (0, 0, 255, 255)
_INPUT_COLOR = (128, 128, 128)


def _place(directory: Path, stem: str, color: tuple[int, int, int, int]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 8), color).save(directory / f"{stem}.png")


def _builtin_bytes(stem: str = "subtitle") -> bytes:
    builtin = load_stamp(stem, None)
    assert builtin is not None
    return builtin.tobytes()


@pytest.mark.parametrize(
    ("custom", "style", "place_style", "place_legacy", "stem", "expected"),
    [
        # 第 1 级: custom_folder 与样式目录 / 旧位置同在场 (custom_folder+style 组合行) 仍取 custom.
        ("with-file", _STYLE_NAME, True, True, _STEM, _CUSTOM),
        ("with-file", "default", True, True, _STEM, _CUSTOM),
        # 第 2 级: style ≠ default 走样式子目录, 胜旧位置.
        ("none", _STYLE_NAME, True, True, _STEM, _STYLE),
        # 第 3 级: style = default 走旧位置, watermarks/default/ 子目录不参与.
        ("none", "default", True, True, _STEM, _LEGACY),
        # 第 1 级目录在但无此文件 → 直接第 4 级内置, data_dir 系各级不参与 (完全替代).
        ("empty", _STYLE_NAME, True, True, "subtitle", "builtin"),
        # 第 4 级: 无任何用户文件 → 内置包.
        ("none", _STYLE_NAME, False, False, "subtitle", "builtin"),
    ],
)
def test_lookup_chain_levels(
    custom: str,
    style: str,
    place_style: bool,
    place_legacy: bool,
    stem: str,
    expected: object,
    tmp_path: Path,
) -> None:
    """四级链逐级命中: 其余级文件在场时命中级仍由链序唯一确定, 以像素颜色区分."""
    data_dir = tmp_path / "data"
    if place_style:
        _place(data_dir / "watermarks" / style, stem, _STYLE)
    if place_legacy:
        _place(data_dir / "watermarks", stem, _LEGACY)
    custom_folder: Path | None = None
    if custom == "with-file":
        custom_folder = tmp_path / "custom"
        _place(custom_folder, stem, _CUSTOM)
    elif custom == "empty":
        custom_folder = tmp_path / "custom"
        custom_folder.mkdir()

    img = load_stamp(stem, data_dir, custom_folder, style)
    if expected == "builtin":
        assert img is not None
        assert img.tobytes() == _builtin_bytes(stem)
    elif expected is None:
        assert img is None
    else:
        assert img is not None
        assert img.getpixel((0, 0)) == expected


def test_default_style_uses_legacy_dir(tmp_path: Path) -> None:
    """style=default 兼容旧位置 {data_dir}/watermarks/{stem}.png; "default" 不作样式子目录参与."""
    data_dir = tmp_path / "data"
    _place(data_dir / "watermarks", "subtitle", _LEGACY)
    _place(data_dir / "watermarks" / "default", "subtitle", _STYLE)
    img = load_stamp("subtitle", data_dir, None, "default")
    assert img is not None
    assert img.getpixel((0, 0)) == _LEGACY


def test_non_default_style_skips_legacy(tmp_path: Path) -> None:
    """style≠default 旧位置不兜底: 章图仅旧位置 → 不命中 (无内置 stem 得 None, 有内置 stem 得内置)."""
    data_dir = tmp_path / "data"
    _place(data_dir / "watermarks", _STEM, _LEGACY)
    assert load_stamp(_STEM, data_dir, None, _STYLE_NAME) is None
    _place(data_dir / "watermarks", "subtitle", _LEGACY)
    img = load_stamp("subtitle", data_dir, None, _STYLE_NAME)
    assert img is not None
    assert img.tobytes() == _builtin_bytes()


def test_custom_folder_highest_priority(tmp_path: Path) -> None:
    """custom_folder 优先于一切: 样式目录与旧位置文件同在场仍取 custom_folder (完全替代)."""
    data_dir = tmp_path / "data"
    _place(data_dir / "watermarks" / _STYLE_NAME, "subtitle", _STYLE)
    _place(data_dir / "watermarks", "subtitle", _LEGACY)
    custom_folder = tmp_path / "custom"
    _place(custom_folder, "subtitle", _CUSTOM)
    for style in ("default", _STYLE_NAME):
        img = load_stamp("subtitle", data_dir, custom_folder, style)
        assert img is not None
        assert img.getpixel((0, 0)) == _CUSTOM, style


@pytest.mark.parametrize("setup", ["no-watermarks-dir", "no-style-dir", "empty-style-dir"])
def test_style_dir_missing_skips_level(tmp_path: Path, setup: str) -> None:
    """样式目录缺失或空目录 → 该级跳过, 回退内置."""
    data_dir = tmp_path / "data"
    if setup != "no-watermarks-dir":
        (data_dir / "watermarks").mkdir(parents=True)
    if setup == "empty-style-dir":
        (data_dir / "watermarks" / _STYLE_NAME).mkdir()
    img = load_stamp("subtitle", data_dir, None, _STYLE_NAME)
    assert img is not None
    assert img.tobytes() == _builtin_bytes()


def test_custom_folder_missing_skips(tmp_path: Path) -> None:
    """custom_folder 指向不存在目录 → 第 1 级跳过 (等同无文件); data_dir 系各级仍不参与."""
    data_dir = tmp_path / "data"
    _place(data_dir / "watermarks" / _STYLE_NAME, "subtitle", _STYLE)
    _place(data_dir / "watermarks", "subtitle", _LEGACY)
    img = load_stamp("subtitle", data_dir, tmp_path / "missing", _STYLE_NAME)
    assert img is not None
    assert img.tobytes() == _builtin_bytes()


@pytest.mark.parametrize("level", ["custom", "style", "legacy"])
def test_corrupt_user_file_falls_back(tmp_path: Path, level: str) -> None:
    """用户文件损坏 → 记 warning 回退下一级 (现状语义), 三级皆终至内置."""
    data_dir = tmp_path / "data"
    custom_folder = tmp_path / "custom"
    target, style, folder = {
        "custom": (custom_folder, "default", custom_folder),
        "style": (data_dir / "watermarks" / _STYLE_NAME, _STYLE_NAME, None),
        "legacy": (data_dir / "watermarks", "default", None),
    }[level]
    target.mkdir(parents=True)
    (target / "subtitle.png").write_bytes(b"garbage")
    img = load_stamp("subtitle", data_dir, folder, style)
    assert img is not None
    assert img.tobytes() == _builtin_bytes()


def test_invalid_style_rejected() -> None:
    """style 字符集 `^[a-z0-9_-]+$` 拒绝 → ValidationError (API 层映射 422); 合法值与默认值放行."""
    for style in ("Blue", "", "a b", "old/red", "old.red", "默认", "Old_Red"):
        with pytest.raises(ValidationError, match="style"):
            WatermarkConfig(style=style)
    assert WatermarkConfig().style == "default"
    assert WatermarkConfig().custom_folder is None
    for style in ("default", "old_red", "v-2"):
        assert WatermarkConfig(style=style).style == style


def test_apply_cover_watermarks_style_passthrough(tmp_path: Path) -> None:
    """images 层双根透传: data_dir+style 与 custom_folder 经 apply_cover_watermarks 命中对应级."""
    data_dir = tmp_path / "data"
    custom_folder = tmp_path / "custom"
    _place(data_dir / "watermarks" / _STYLE_NAME, "subtitle", _STYLE)
    _place(data_dir / "watermarks", "subtitle", _LEGACY)
    _place(custom_folder, "subtitle", _CUSTOM)
    cases: list[tuple[Path | None, Path | None, str, tuple[int, int, int, int]]] = [
        (data_dir, None, _STYLE_NAME, _STYLE),
        (data_dir, None, "default", _LEGACY),
        (None, custom_folder, "default", _CUSTOM),
    ]
    for i, (data_dir_arg, folder, style, color) in enumerate(cases):
        path = tmp_path / f"case-{i}.jpg"
        Image.new("RGB", (200, 200), _INPUT_COLOR).save(path)
        painted = apply_cover_watermarks(
            path,
            has_subtitle=True,
            uncensored=False,
            mosaic=None,
            definition=None,
            data_dir=data_dir_arg,
            custom_folder=folder,
            style=style,
        )
        assert painted is True, style
        with Image.open(path) as out:
            pixel = out.convert("RGB").getpixel((10, 10))
        assert isinstance(pixel, tuple)
        channels = [int(v) for v in pixel[:3]]
        assert all(abs(c - want) <= 12 for c, want in zip(channels, color[:3], strict=True)), style
