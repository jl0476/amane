"""cv2 (OpenCV) 可用性访问器: 依赖缺失时判定不可用. 使用侧经由 available()/module() 探测, 不直接导入 cv2."""

import types

cv2: types.ModuleType | None

try:
    import cv2  # ty: ignore[unresolved-import]
except ModuleNotFoundError:
    cv2 = None


def available() -> bool:
    """cv2 是否已成功导入."""
    return cv2 is not None


def module() -> types.ModuleType | None:
    """返回已导入的 cv2 模块; 不可用时为 None."""
    return cv2
