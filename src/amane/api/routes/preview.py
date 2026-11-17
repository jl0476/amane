"""水印预览端点 (规格 §3.6): 库级生效配置 × 请求相位的只读试算.

不落库、不写 Resource: 源图复制到临时文件叠印, 请求结束临时即删.
失败 (源不可读 / 叠印异常) 422 带原因, 不返回半成品; 响应恒为 ``image/jpeg``,
编码与真实落盘产物同走 ``media/images.py::_save_jpeg`` 语义.
"""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from fastapi import APIRouter, HTTPException, Response

from ...config import WatermarkConfig
from ...config.overrides import ConfigResolver
from ...media import apply_cover_watermarks, reencode_jpeg
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models import WatermarkPreviewRequest

logger = structlog.get_logger()

router = APIRouter(prefix="/libraries/{library_id}/preview", tags=["libraries"])


@router.post("/watermark", responses={200: {"content": {"image/jpeg": {}}}})
async def preview_watermark(
    library_id: int,
    req: WatermarkPreviewRequest,
    repo: RepoDep,
    config: ConfigDep,
    runtime: RuntimeDep,
) -> Response:
    """按库级生效 ``WatermarkConfig`` 对指定源图试算叠印结果.

    相位四元组由请求显式给定 (与 FileInfo 同口径), 端点不做资源反查;
    ``kinds`` 过滤与整理期同一实现 (media/images.py::apply_cover_watermarks).
    源缺失 404; 源不可读 / 叠印失败 422 带原因.
    """
    library = await repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    watermark = await ConfigResolver(config.hot, repo).resolve(WatermarkConfig, scope=library, library=library)
    source = await runtime.resource_store.resolve(req.url)
    if source is None:
        raise HTTPException(status_code=404, detail="预览源资源不存在")

    jpeg_quality = config.hot.scraping.jpeg_quality
    with TemporaryDirectory(prefix="watermark-preview-") as tmp:
        work = Path(tmp) / source.name
        shutil.copyfile(source, work)
        if not watermark.enabled:
            # 总开关关闭: 整理产物为原样复制, 预览输出与落盘产物同编码的 JPEG (重编码兼作可读性预检)
            reason = reencode_jpeg(work, quality=jpeg_quality)
        else:
            stamped = apply_cover_watermarks(
                work,
                has_subtitle=req.phase.has_subtitle,
                uncensored=req.phase.uncensored,
                mosaic=req.phase.mosaic,
                definition=req.phase.definition,
                jpeg_quality=jpeg_quality,
                data_dir=runtime.config.cold.data_dir,
                custom_folder=watermark.custom_folder,
                style=watermark.style,
                scale=watermark.scale,
                corners=watermark.corners,
                kinds=watermark.kinds,
                layout=watermark.layout,
                offset_x=watermark.offset_x,
                offset_y=watermark.offset_y,
                gap=watermark.gap,
                start=watermark.start,
            )
            # False 收场有两种: 相位无章可叠 (合法空预览) 或叠印失败, 重编码预检给出后者的原因
            reason = reencode_jpeg(work, quality=jpeg_quality) if not stamped else None
        if reason is not None:
            raise HTTPException(status_code=422, detail=f"水印预览失败: {reason}")
        data = work.read_bytes()

    logger.info("watermark preview", library_id=library_id, url=req.url, enabled=watermark.enabled)
    return Response(content=data, media_type="image/jpeg")
