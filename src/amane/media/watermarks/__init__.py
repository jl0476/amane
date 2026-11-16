"""封面角标 PNG 查找链: custom_folder → 样式目录 → 旧位置 → 内置包.

文件名与 FileInfo 相位对齐 (全小写): subtitle / uncensored / cracked / leaked,
以及分辨率 `definition.casefold()` (内置覆盖 DEFINITION_VALUES 的每一档).
缺文件则跳过该枚, 不回退文字. 有码 / VR / 3D 无对应相位, 不内置.

查找链 (custom_folder 非 None 时完全替代 — data_dir 系各级不参与):
1. `{custom_folder}/{stem}.png`
2. `{data_dir_root}/watermarks/{style}/{stem}.png`  仅 style != "default"
3. `{data_dir_root}/watermarks/{stem}.png`          仅 style == "default" (旧位置, 兼容存量)
4. 内置包 (仅 default 一套)
用户文件损坏记 warning 回退下一级; 目录缺失等同无文件.
"""

from importlib.resources import files
from pathlib import Path

import structlog
from PIL import Image

logger = structlog.get_logger()

USER_DIRNAME = "watermarks"
DEFAULT_STYLE = "default"

_STEM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def is_stamp_stem(stem: str) -> bool:
    return bool(stem) and all(ch in _STEM_CHARS for ch in stem)


def load_stamp(
    stem: str,
    data_dir_root: Path | None = None,
    custom_folder: Path | None = None,
    style: str = DEFAULT_STYLE,
) -> Image.Image | None:
    """按查找链取章图; 都没有则 None."""
    if not is_stamp_stem(stem):
        return None
    name = f"{stem}.png"
    if custom_folder is not None:
        # 第 1 级: 完全替代 — 缺失/损坏都只回退内置, 不落回 data_dir 系各级.
        loaded = _open_user(custom_folder / name)
        return loaded if loaded is not None else _load_builtin(name)
    if data_dir_root is not None:
        # 第 2 / 3 级互斥: 样式子目录仅 style != default 参与 (旧位置不兜底), 旧位置仅 default 参与.
        user_root = data_dir_root / USER_DIRNAME
        path = user_root / style / name if style != DEFAULT_STYLE else user_root / name
        loaded = _open_user(path)
        if loaded is not None:
            return loaded
    return _load_builtin(name)  # 第 4 级


def _open_user(path: Path) -> Image.Image | None:
    """用户级章图: 不存在返回 None (该级跳过); 损坏记 warning 由调用方回退下一级."""
    if not path.is_file():
        return None
    loaded = _open_rgba(path)
    if loaded is None:
        logger.warning("user watermark unreadable, falling back", path=str(path))
    return loaded


def _load_builtin(name: str) -> Image.Image | None:
    ref = files(__name__).joinpath(name)
    if not ref.is_file():
        return None
    try:
        with ref.open("rb") as fh, Image.open(fh) as img:
            return img.convert("RGBA")
    except Exception as e:
        logger.warning("builtin watermark unreadable", name=name, error=str(e))
        return None


def _open_rgba(path: Path) -> Image.Image | None:
    try:
        with Image.open(path) as img:
            return img.convert("RGBA")
    except Exception as e:
        logger.warning("watermark open failed", path=str(path), error=str(e))
        return None
