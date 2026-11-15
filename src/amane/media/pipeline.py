"""刮削期按 ``download_resources`` 下载 / 裁剪 / 就地超分, 返回应写入 metadata 的 URL.

与整理到媒体库路径无关. 任一步失败不抛, 仅降级.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import DownloadableResource
from ..enums import CropStrategy
from ..parsing import ContentType
from ..sr import get_preset_meta, run_SR
from .crop import CropPlan, acquire_face_crop, format_crop_args, resolve_crop_plan
from .images import (
    CropRotation,
    crop_box,
    crop_poster,
    format_crop_box_args,
    needs_upscale,
    probe_oriented_size,
    probe_size,
    rotated_size,
    validate_crop_box,
)
from .resource_store import RESOURCE_URL_PREFIX

if TYPE_CHECKING:
    from ..config import HotSettings, MetadataConfig, SrConfig
    from ..db.models import Resource
    from ..net.http import WebClient
    from .resource_store import ResourceStore

logger = structlog.get_logger()


@dataclass
class MaterializedImages:
    # fanart 不是 metadata 字段 (整理时按 JAV 约定取 thumb), 不在此返回.

    poster_urls: list[str]
    thumb_urls: list[str]
    trailer_urls: list[str]


def _internal_url(store: ResourceStore, locator_url: str) -> str:
    return f"{RESOURCE_URL_PREFIX}/{store.url_hash(locator_url)}"


def _success_first(urls: list[str], succeeded: set[str]) -> list[str]:
    # 成功者保序前置, 失败者保序置于尾部. succeeded 为空时幂等返回原序.
    return [u for u in urls if u in succeeded] + [u for u in urls if u not in succeeded]


def _winner_first(urls: list[str], succeeded: set[str], winner: str | None) -> list[str]:
    """成功者保序, 择优胜者置于首位.

    quality 路径的胜者是本轮实际落盘的产物, 落库列表的首项必须与之一致, 否则展示与后续读取
    仍取到低清候选; 其余成功候选保持原相对顺序, 失败者沉底. winner 为 None (非 quality 路径
    或择优失败) 时退化为 :func:`_success_first`.
    """
    if winner is None or winner not in succeeded:
        return _success_first(urls, succeeded)
    rest = [u for u in urls if u in succeeded and u != winner]
    failed = [u for u in urls if u not in succeeded]
    return [winner, *rest, *failed]


def sr_args_dict(cfg: SrConfig) -> dict:
    pm = get_preset_meta(cfg.preset)
    return {"preset": cfg.preset, "tool": pm.tool, "model": pm.model, "scale": pm.scale}


async def _maybe_upscale(
    store: ResourceStore,
    resource: Resource,
    config: HotSettings,
    data_dir: Path,
) -> None:
    if not config.sr.enabled:
        return
    full = store.full_path(resource)
    size = probe_size(full)
    file_bytes = full.stat().st_size if full.exists() else 0
    if not needs_upscale(
        size,
        file_bytes,
        max_dim_threshold=config.sr.max_dim_threshold,
        max_bytes_threshold=config.sr.max_bytes_threshold,
    ):
        return

    async def producer(src: Path, out: Path) -> bool:
        result = await run_SR(src, out, config.sr, data_dir)
        return result.success

    await store.upscale_in_place(resource, sr_args_dict(config.sr), producer)


async def materialize_images(
    poster_urls: list[str],
    thumb_urls: list[str],
    trailer_urls: list[str],
    store: ResourceStore,
    client: WebClient,
    config: HotSettings,
    data_dir: Path,
    *,
    content_type: ContentType,
    metadata_cfg: MetadataConfig,
    extrafanart_urls: dict[str, list[str]] | None = None,
    behind_scenes: list[str] | None = None,
) -> MaterializedImages:
    """仅下载集合内的类型; 未选中的保留聚合 URL.

    死 URL 保序置于尾部, 供来源恢复后重试.
    extrafanart 仅下载到 Resource, 站点分组结构不重排.
    幕后图 (规格 §3.5): 全局开关 ``metadata_cfg.extra_fanarts_behind_scenes`` 开启时并入 extrafanart
    物化集合; 关闭时仅持久化 URL, 整理期按库开关现场获取.
    海报裁切按 ``metadata_cfg`` 的策略矩阵解析 (`media/crop.py::resolve_crop_plan`);
    ``executed=NONE`` 表示候选原样入选, 不执行像素裁剪.
    thumb / poster 候选获取按 ``metadata_cfg.thumb_download_strategy`` 分派 (R2 PLAN-09):
    quality 先全候选独占临时择优, 胜者才是后续裁剪判定与像素执行的输入; 落库的 poster 列表
    首项为胜者, 其余成功候选保持原相对顺序, 否则展示与后续读取仍取到低清候选.
    """
    kinds = set(config.scraping.download_resources)
    crop_enabled = config.scraping.crop_poster
    quality = metadata_cfg.thumb_download_strategy == "quality"

    # 下载 thumb.
    thumb_ok: set[str] = set()
    thumb_local: Path | None = None
    thumb_src: str | None = None
    if DownloadableResource.thumb in kinds:
        if quality:
            best = await store.acquire_best(thumb_urls, client, task_token="materialize")
            if best.success and best.path is not None and best.used_url is not None:
                thumb_ok = set(thumb_urls) - set(best.failed or ())
                thumb_local, thumb_src = best.path, best.used_url
                res = await store.get_by_url(best.used_url)
                if res:
                    await _maybe_upscale(store, res, config, data_dir)
        else:
            for url in thumb_urls:
                local = await store.acquire(url, client)
                if local:
                    thumb_ok.add(url)
                    if thumb_local is None:
                        thumb_local, thumb_src = local, url
                        res = await store.get_by_url(url)
                        if res:
                            await _maybe_upscale(store, res, config, data_dir)

    # 下载 poster; 候选偏矮则从 thumb 裁剪.
    poster_ok: set[str] = set()
    poster_candidate_local: Path | None = None
    poster_winner: str | None = None
    result_poster_urls = list(poster_urls)
    if DownloadableResource.poster in kinds:
        if quality:
            best = await store.acquire_best(poster_urls, client, task_token="materialize")
            if best.success and best.path is not None:
                poster_ok = set(poster_urls) - set(best.failed or ())
                poster_candidate_local = best.path
                poster_winner = best.used_url
        else:
            for url in poster_urls:
                local = await store.acquire(url, client)
                if local:
                    poster_ok.add(url)
                    if poster_candidate_local is None:
                        poster_candidate_local = local

        cand_size = probe_size(poster_candidate_local) if poster_candidate_local else None

        def crop_plan(thumb_size: tuple[int, int] | None) -> CropPlan:
            return resolve_crop_plan(
                content_type,
                strategy_map=metadata_cfg.crop_type,
                aspect=metadata_cfg.crop_aspect,
                low_res_height=metadata_cfg.crop_low_res_height,
                poster_ratio=config.scraping.poster_ratio,
                skip_ratio=config.scraping.poster_crop_skip_ratio,
                thumb_size=thumb_size,
                candidate_size=cand_size,
            )

        # 裁剪需要 thumb 本地文件; 若未选 thumb 下载, 为裁切临时获取 thumb (quality 下同样先择优).
        # 策略矩阵未请求像素裁剪的类型 (requested=none) 不临时下载.
        if thumb_local is None and thumb_urls and crop_enabled and crop_plan(None).requested is not CropStrategy.NONE:
            if quality:
                best = await store.acquire_best(thumb_urls, client, task_token="materialize")
                if best.success and best.path is not None and best.used_url is not None:
                    thumb_local, thumb_src = best.path, best.used_url
            else:
                for url in thumb_urls:
                    local = await store.acquire(url, client)
                    if local:
                        thumb_local, thumb_src = local, url
                        break

        thumb_size = probe_size(thumb_local) if thumb_local else None
        plan = crop_plan(thumb_size)

        if crop_enabled and plan.executed is not CropStrategy.NONE and thumb_local:
            if plan.executed is CropStrategy.FACE:
                # Face 请求走检测先于键构造流程 (`media/crop.py::acquire_face_crop`): 无人脸或后端
                # 不可用降级 right, 键编码与像素执行恒一致.
                crop_res = await acquire_face_crop(
                    store,
                    thumb_src or "",
                    thumb_local,
                    plan=plan,
                    aspect=metadata_cfg.crop_aspect,
                    jpeg_quality=config.scraping.jpeg_quality,
                )
            else:
                ratio = plan.ratio
                args = format_crop_args(plan.executed, metadata_cfg.crop_aspect, ratio)
                local_thumb = thumb_local

                async def crop_producer(dest: Path) -> bool:
                    return crop_poster(local_thumb, dest, poster_ratio=ratio, jpeg_quality=config.scraping.jpeg_quality)

                crop_res = await store.acquire_derived(thumb_src or "", "crop", args, crop_producer)
            if crop_res:
                await _maybe_upscale(store, crop_res, config, data_dir)
                result_poster_urls = [_internal_url(store, crop_res.url)]
            else:
                result_poster_urls = _winner_first(poster_urls, poster_ok, poster_winner)
        else:
            # executed=NONE: 候选原样入选, 不执行像素裁剪; 对候选就地超分.
            result_poster_urls = _winner_first(poster_urls, poster_ok, poster_winner)
            if poster_candidate_local:
                # quality 的落盘产物是胜者, 超分针对胜者; 落选者即便有旧缓存记录也不参与.
                targets = [poster_winner] if poster_winner is not None else poster_urls
                for url in targets:
                    res = await store.get_by_url(url)
                    if res:
                        await _maybe_upscale(store, res, config, data_dir)
                        break

    # 下载预告片.
    trailer_ok: set[str] = set()
    if DownloadableResource.trailer in kinds:
        for url in trailer_urls:
            local = await store.acquire(url, client)
            if local:
                trailer_ok.add(url)

    # 下载剧照: 按站点优先级, 有结果即停.
    if DownloadableResource.extrafanart in kinds and extrafanart_urls:
        priority = list(extrafanart_urls.keys())
        await store.acquire_extrafanart(extrafanart_urls, priority, client)

    # 幕后图: 单独一组获取, 不受普通剧照「有结果即停」影响 (规格 §3.5).
    if DownloadableResource.extrafanart in kinds and metadata_cfg.extra_fanarts_behind_scenes and behind_scenes:
        await store.acquire_extrafanart({"behind_scenes": behind_scenes}, ["behind_scenes"], client)

    out = MaterializedImages(
        poster_urls=result_poster_urls,
        thumb_urls=_success_first(thumb_urls, thumb_ok),
        trailer_urls=_success_first(trailer_urls, trailer_ok),
    )
    logger.debug(
        "images materialized",
        kinds=sorted(kinds),
        poster=out.poster_urls[:1],
        thumb=out.thumb_urls[:1],
        trailer=out.trailer_urls[:1],
    )
    return out


async def manual_crop_image(
    source_url: str,
    box: tuple[int, int, int, int],
    store: ResourceStore,
    client: WebClient,
    config: HotSettings,
    data_dir: Path,
    *,
    subject: str,
    rotation: CropRotation = 0,
) -> str:
    """手动裁切源图, 返回内部 URL. 源可为外部 http(s) 或 ``/api/resources/{hash}``.

    先按 EXIF 转正、再顺时针旋转 ``rotation``, 之后按 ``box`` 裁切; 失败抛 ``ValueError``
    (消息可直接作 API detail); ``subject`` 组装提示文案 (如「封面图」「头像」).
    """
    source = await store.resolve_source(source_url, client)
    if source is None:
        raise ValueError(f"无法获取{subject}")

    size = probe_oriented_size(source.path)
    if size is None:
        raise ValueError(f"{subject}无法读取")
    if not validate_crop_box(box, rotated_size(size, rotation)):
        raise ValueError("裁切区域无效")

    args = format_crop_box_args(*box, rotation=rotation)
    jpeg_quality = config.scraping.jpeg_quality

    async def producer(dest: Path) -> bool:
        return crop_box(source.path, dest, box, rotation=rotation, jpeg_quality=jpeg_quality)

    crop_res = await store.acquire_derived(source.locator, "crop", args, producer)
    if crop_res is None:
        raise ValueError("裁切失败")

    await _maybe_upscale(store, crop_res, config, data_dir)
    return _internal_url(store, crop_res.url)
