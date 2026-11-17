from pydantic import BaseModel

from ...parsing import Mosaic


class WatermarkPreviewPhase(BaseModel):
    """水印预览的相位四元组: 与 `FileInfo` 同口径, 调用方显式给定.

    端点不做资源反查 (预览是配置试算工具, 相位由前端从条目当前信息填充);
    四个字段都必须显式出现在请求体里, 可空字段以 null 表示「无」.
    """

    has_subtitle: bool
    uncensored: bool
    mosaic: Mosaic | None
    definition: str | None


class WatermarkPreviewRequest(BaseModel):
    """水印预览入参 (规格 §3.6): 内部资源 URL + 显式相位."""

    url: str
    phase: WatermarkPreviewPhase
