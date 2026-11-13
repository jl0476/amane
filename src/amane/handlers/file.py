import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import HotSettings, NamingConfig, NfoConfig, OrganizeCommonConfig, WatermarkConfig
from ..config.overrides import ConfigResolver
from ..db.models import Library, MediaFile
from ..db.parse_rules import ParseRuleProvider
from ..db.repo_types import MediaFileUpdates
from ..enums import ActorGender, DownloadableResource, LinkMode, OrganizeJournalPhase
from ..library import MEDIA_EXTENSIONS, LibraryFileKind, LibraryScan, ancestor_dirs, prune_empty_dirs
from ..library.rules import is_in_trash
from ..media import NfoOptions, ResourceStore, allows_poster_crop, apply_cover_watermarks_from_info, crop_poster
from ..media import write_nfo as write_nfo_file
from ..media.resource_store import internal_url_hash
from ..net.http import WebClient
from ..observability.models import OrganizeConflict, OrganizeConflictReason
from ..organize import (
    MoveMode,
    OverwriteResult,
    PlaceOutcome,
    ResolvedPaths,
    SubtitleConflict,
    TargetState,
    current_task_id,
    discover_subtitles,
    execute_organize,
    overwrite_target,
    place_subtitles,
    recover_organize_journals,
    render_extrafanart_names,
    render_strm_content,
    resolve_paths,
    sidecar_template,
    target_state,
    video_dest,
)
from ..organize import (
    OrganizeResult as PlacementResult,
)
from ..organize.link import create_video_link
from ..parsing import ContentType, FileInfo, ParseRuleSet, parse_file_info
from ..utils.path import existing_disk_path as existing_disk_path_sync
from ..utils.path import is_descendant, nfc_path, path_is_under
from ..utils.threads import existing_disk_path, in_thread, path_is_dir
from ._common import LibraryTaskLocks, refresh_external_subtitle
from .models import (
    ORGANIZE_CONFLICT_LIMIT,
    CleanupPayload,
    CleanupResult,
    OrganizePayload,
    OrganizeResult,
)
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.models import Metadata
    from ..db.repository import Repository
    from ..media import ResourceStore
    from ..net.http import WebClient

logger = structlog.get_logger()


@dataclass
class FileOperationsResult:
    outcome: PlaceOutcome
    dest: Path | None = None
    """PLACED 是落盘路径; FAILED 时可能仍带路径, 以便回写 MediaFile.path 补链接."""
    conflict_target: Path | None = None
    """CONFLICT 时被占用的目标路径."""
    subtitle_conflicts: list[SubtitleConflict] = field(default_factory=list)
    error: str | None = None
    overwritten: bool = False
    """PLACED 且经覆写协议落盘; 索引接管已在覆写协议内完成, 常规提交对该行是同值幂等重写."""
    skipped_images: int = 0
    """overwrite_image=False 时目标已有图片而跳过的张数."""


@dataclass
class EffectiveOrganizeConfig:
    """任务入口解析一次的生效配置; 下层经参数消费, 不得回读库行覆盖已解析结果 (规格 3.1.3)."""

    naming: NamingConfig
    common: OrganizeCommonConfig
    nfo_options: NfoOptions


async def resolve_effective_config(
    repo: Repository,
    config: HotSettings,
    library: Library,
    payload: OrganizePayload,
) -> EffectiveOrganizeConfig:
    """四层合成 common / naming / nfo; 第 4 层只读 payload.explicit_fields 列出的字段.

    explicit_fields 是入队前持久化的用户显式字段名单 (JSON 往返后 ``model_fields_set`` 不可靠).
    名单与 payload 字段集及非 None 值取交集后才进入合成层: 带外写入的畸形名单 (未知键或 None 值)
    在此剔除, 不使合成失败.
    """
    resolver = ConfigResolver(config, repo)
    payload_fields = type(payload).model_fields
    explicit = {
        name: value
        for name in payload.explicit_fields
        if name in payload_fields
        and name in NamingConfig.model_fields
        and (value := getattr(payload, name)) is not None
    }
    naming = await resolver.resolve(NamingConfig, scope=library, library=library, payload=explicit)
    common = await resolver.resolve(OrganizeCommonConfig, scope=library, library=library)
    nfo = await resolver.resolve(NfoConfig, scope=library, library=library)
    return EffectiveOrganizeConfig(naming=naming, common=common, nfo_options=NfoOptions(**nfo.model_dump()))


async def execute_file_operations(
    media_file: MediaFile,
    metadata: Metadata,
    paths: ResolvedPaths,
    move_mode: MoveMode,
    resource_store: ResourceStore,
    file_info: FileInfo,
    download_images: bool = True,
    write_nfo: bool = True,
    copy_resources: Sequence[DownloadableResource] | None = None,
    web_client: WebClient | None = None,
    config: HotSettings | None = None,
    library: Library | None = None,
    safe_dirs: Sequence[Path] | None = (),
    watermark_dir: Path | None = None,
    actor_genders: dict[str, ActorGender] | None = None,
    naming: NamingConfig | None = None,
    nfo_options: NfoOptions | None = None,
    overwrite_video: bool = False,
    overwrite_image: bool = True,
    repo: Repository | None = None,
    task_id: int | None = None,
) -> FileOperationsResult:
    """``file_info`` 由渲染路径的那一次 ``parse_file_info`` 产出, 必须带上 ``has_external_subtitle``.

    未带库列时函数不报错, 直接少打中字角标 — 调用方负责构造.
    ``naming`` 非 None 时模板类字段 (strm 正文 / 链接模式 / 字幕扩展名 / 剧照文件名) 取自生效配置,
    不回读库行 (规格 3.1.3). ``overwrite_video`` / ``overwrite_image`` 是覆写策略开关:
    ``overwrite_video`` 接入 OCCUPIED 分派 (False 维持冲突跳过, True 进入覆写协议, 规格 3.4.2),
    ``overwrite_image`` 接入图片写入. 覆写协议需要 ``repo`` (journal 与索引接管) 与 ``library``
    (库边界); 缺任一开关即开时按失败处理. ``task_id`` 用于备份 / 临时文件命名与 journal 追查.
    """
    source_path = await existing_disk_path(Path(media_file.path))
    if source_path is None:
        logger.warning("source file missing", path=media_file.path)
        return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=f"源文件不存在: {media_file.path}")

    # 目标被别的文件占用时立即返回: 不下载图片, 不写附属文件, 不改库内任何路径.
    # 读不出目标状态时同样立即返回, 但记为失败而不是冲突.
    # 目标路径必须与 execute_organize 同一算法: 模板写死扩展名时, 模板渲染结果与真实目标不同.
    dest = video_dest(paths.video.parent, paths.video.stem, source_path)
    # 整理同一目标前就地恢复: 该目标有未完成的覆写时先按恢复矩阵处理, 占用判定基于恢复后的磁盘事实.
    if repo is not None and await repo.get_organize_journal_by_target(str(dest)) is not None:
        recovered = await recover_organize_journals(repo, target_path=str(dest))
        logger.info("organize journal in-place recovery finished", dest=str(dest), rows=recovered)
    check = await target_state(source_path, dest)
    if check.state is TargetState.OCCUPIED and not overwrite_video:
        logger.warning("organize target occupied", source=str(source_path), dest=str(dest))
        return FileOperationsResult(outcome=PlaceOutcome.CONFLICT, conflict_target=dest)
    if check.state is TargetState.UNKNOWN:
        logger.warning("organize target state unknown", source=str(source_path), dest=str(dest), error=check.error)
        return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=check.error)

    # 发现字幕: 必须在视频移动前检查同目录 (覆写协议在阶段 B 移走源文件).
    subtitles: list[Path] = []
    if library is not None:
        extensions = naming.subtitle_extensions if naming is not None else library.subtitle_extensions
        subtitles = await discover_subtitles(source_path, extensions, file_info)

    # 覆写协议: 前置检查是该预检的延伸 — 目标必须是库内视频文件且不同于源 (OCCUPIED 已排除同文件).
    overwrite_result: OverwriteResult | None = None
    if check.state is TargetState.OCCUPIED:
        precondition = _overwrite_precondition(dest, library)
        if precondition is not None:
            return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=precondition)
        if repo is None or library is None:
            return FileOperationsResult(outcome=PlaceOutcome.FAILED, error="覆写需要数据库与媒体库上下文")
        overwrite_result = await overwrite_target(
            source=source_path,
            target=dest,
            mode=move_mode,
            repo=repo,
            media_file=media_file,
            task_id=task_id,
        )
        if overwrite_result.outcome is not PlaceOutcome.PLACED:
            logger.warning(
                "overwrite not placed", source=str(source_path), dest=str(dest), outcome=str(overwrite_result.outcome)
            )
            if overwrite_result.outcome is PlaceOutcome.CONFLICT:
                return FileOperationsResult(outcome=PlaceOutcome.CONFLICT, conflict_target=dest)
            return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=overwrite_result.error)

    # 下载图片; 水印打在库路径副本上, 不能修改 Resource 原图.
    skipped_images = 0
    if web_client and download_images:
        logger.debug("downloading images via store", number=metadata.number)
        kinds = set(copy_resources) if copy_resources is not None else set(DownloadableResource)
        skipped_images = await _download_images_via_store(
            metadata,
            resource_store,
            web_client,
            paths,
            config,
            kinds,
            file_info=file_info,
            content_type=metadata.content_type,
            watermark_dir=watermark_dir,
            library=library,
            naming=naming,
            overwrite_image=overwrite_image,
        )

    if overwrite_result is not None:
        org_result = PlacementResult(outcome=PlaceOutcome.PLACED, dest=dest)
    else:
        org_result = await execute_organize(
            source=source_path,
            target_dir=paths.video.parent,
            target_stem=paths.video.stem,
            mode=move_mode,
        )

    if org_result.outcome is PlaceOutcome.CONFLICT:
        return FileOperationsResult(outcome=PlaceOutcome.CONFLICT, conflict_target=org_result.dest)

    # 写链接; 失败仍带 dest, 以便回写 MediaFile.path.
    if org_result.outcome is PlaceOutcome.PLACED and org_result.dest and paths.link is not None:
        mode = (
            naming.link_mode
            if naming is not None
            else (LinkMode(library.link_mode) if library is not None else LinkMode.STRM)
        )
        strm_template = (
            naming.strm_content_template
            if naming is not None
            else (library.strm_content_template if library is not None else None)
        )
        strm_content: str | None = None
        if mode == LinkMode.STRM and library is not None:
            try:
                strm_content = render_strm_content(
                    strm_template,
                    org_result.dest,
                    Path(library.path),
                    metadata,
                    source_path=source_path,
                    file_info=file_info,
                    link=paths.link,
                    actor_genders=actor_genders,
                    syntax=naming.syntax if naming is not None else "native",
                )
            except ValueError as e:
                return FileOperationsResult(outcome=PlaceOutcome.FAILED, dest=org_result.dest, error=str(e))
        link_result = await create_video_link(org_result.dest, paths.link, mode, content=strm_content)
        if link_result.outcome is not PlaceOutcome.PLACED:
            return FileOperationsResult(outcome=PlaceOutcome.FAILED, dest=org_result.dest, error=link_result.error)
        # 链接路径持久化: resolve_paths 每次按当前模板重渲染, 历史落盘位置只能靠该列 (规格 3.4.4).
        if repo is not None and media_file.id is not None:
            try:
                await repo.update_media_file(media_file.id, link_path=str(link_result.dest))
            except Exception as e:
                logger.warning("link_path persist failed", dest=str(link_result.dest), error=str(e), exc_info=True)

    # 写入 NFO.
    if org_result.outcome is PlaceOutcome.PLACED and org_result.dest and write_nfo:
        await write_nfo_file(metadata, paths.nfo, nfo_options if nfo_options is not None else NfoOptions())

    # 字幕按模板落到 video_dest 侧.
    subtitle_conflicts: list[SubtitleConflict] = []
    if org_result.outcome is PlaceOutcome.PLACED and org_result.dest and library is not None and subtitles:
        subtitle_conflicts = await place_subtitles(
            subtitles,
            video_source=source_path,
            video_dest=org_result.dest,
            library=library,
            metadata=metadata,
            file_info=file_info,
            mode=move_mode,
            safe_dirs=safe_dirs,
            link_dir=paths.link.parent if paths.link is not None else None,
            link_name=paths.link.stem if paths.link is not None else None,
            actor_genders=actor_genders,
            naming=naming,
        )

    if org_result.outcome is PlaceOutcome.PLACED:
        logger.debug("file operations done", number=metadata.number, dest=str(org_result.dest), mode=str(move_mode))
        return FileOperationsResult(
            outcome=PlaceOutcome.PLACED,
            dest=org_result.dest,
            subtitle_conflicts=subtitle_conflicts,
            overwritten=overwrite_result is not None,
            skipped_images=skipped_images,
        )
    logger.debug("file operations failed", number=metadata.number, error=org_result.error)
    return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=org_result.error)


def _overwrite_precondition(target: Path, library: Library | None) -> str | None:
    """覆写协议的阶段 A 前置检查延伸: 目标必须是库内视频文件; 返回 None 表示通过."""
    if library is not None and not is_descendant(target, Path(library.path)):
        return f"覆写目标不在媒体库内, 拒绝覆写: {target}"
    if target.suffix.lower() not in MEDIA_EXTENSIONS:
        return f"覆写目标不是视频文件, 拒绝覆写: {target}"
    return None


async def apply_file_operations(
    repo: Repository,
    media_file_id: int | None,
    metadata: Metadata,
    config: HotSettings,
    resource_store: ResourceStore,
    *,
    write_nfo: bool = True,
    copy_resources: Sequence[DownloadableResource] | None = None,
    web_client: WebClient | None = None,
    safe_dirs: Sequence[Path] | None = (),
    watermark_dir: Path | None = None,
    naming: NamingConfig | None = None,
    nfo_options: NfoOptions | None = None,
    overwrite_video: bool = False,
    overwrite_image: bool = True,
    task_id: int | None = None,
    rules: ParseRuleSet | None = None,
) -> FileOperationsResult | None:
    """缺少 media_file_id 或对应记录时返回 None (跳过, 不视为失败).
    Library 由 MediaFile.library_id 派生.

    ``naming`` / ``nfo_options`` / 覆写开关是任务入口解析好的生效配置, 原样下传, 不在此回读库行.
    ``rules`` 是该库的识别词集 (调用方注入).
    """
    if media_file_id is None:
        return None
    media_file = await repo.get_media_file(media_file_id)
    if media_file is None:
        return None
    library = await repo.get_library(media_file.library_id)
    if library is None:
        return None

    # 渲染路径后执行落盘.
    ext = Path(media_file.path).suffix.lstrip(".")
    file_info = parse_file_info(media_file.path, has_external_subtitle=media_file.has_external_subtitle, rules=rules)
    actor_genders = {a.name: a.gender for a in await repo.get_actors_by_names(metadata.actors)}
    paths = resolve_paths(
        library,
        metadata,
        ext=ext,
        file_info=file_info,
        source_path=Path(media_file.path),
        safe_dirs=safe_dirs,
        actor_genders=actor_genders,
        naming=naming,
        move_mode=library.move_mode,
    )
    return await execute_file_operations(
        media_file,
        metadata,
        paths,
        library.move_mode,
        resource_store,
        file_info,
        download_images=True,
        write_nfo=write_nfo,
        copy_resources=copy_resources,
        web_client=web_client,
        config=config,
        library=library,
        safe_dirs=safe_dirs,
        watermark_dir=watermark_dir,
        actor_genders=actor_genders,
        naming=naming,
        nfo_options=nfo_options,
        overwrite_video=overwrite_video,
        overwrite_image=overwrite_image,
        repo=repo,
        task_id=task_id,
    )


async def commit_organized_media_file(
    repo: Repository,
    media: MediaFile,
    placed: Path,
    library_root: Path,
) -> None:
    """整理后的路径仍在本库内才改 path; 已离开本库且源路径不在磁盘上则删行.

    目标路径已被另一行占用时删本行; 占用行缺少刮削字段则从本行补上.
    """
    if media.id is None:
        return
    library = await repo.get_library(media.library_id)
    if not is_descendant(placed, library_root):
        if await existing_disk_path(Path(media.path), follow_symlinks=False) is None:
            await repo.delete_media_file(media.id)
        return

    occupant = await repo.get_media_file_by_path(str(placed))
    if occupant is None or occupant.id == media.id:
        updated = await repo.update_media_file(media.id, path=str(placed))
        if updated is not None and library is not None:
            await refresh_external_subtitle(repo, updated, library)
        return
    if occupant.id is None:
        return
    occupant_updates: MediaFileUpdates = {}
    if occupant.metadata_id is None and media.metadata_id is not None:
        occupant_updates["metadata_id"] = media.metadata_id
        occupant_updates["status"] = media.status
    if occupant.oshash is None and media.oshash is not None:
        occupant_updates["oshash"] = media.oshash
    if occupant.number is None and media.number is not None:
        occupant_updates["number"] = media.number
    if occupant_updates:
        await repo.update_media_file(occupant.id, **occupant_updates)
    await repo.delete_media_file(media.id)
    if library is not None:
        await refresh_external_subtitle(repo, occupant, library)


async def _resolve_local(url: str, store: ResourceStore, client: WebClient) -> Path | None:
    """内部 `/api/resources/{hash}` 与外部 URL 均由 store 解析 (命中缓存直出)."""
    return await store.acquire(url, client)


async def _acquire_first_local(urls: list[str], store: ResourceStore, client: WebClient) -> Path | None:
    for url in urls:
        path = await _resolve_local(url, store, client)
        if path:
            return path
    return None


def _copy_into_library(src: Path, dst: Path) -> None:
    """复制到媒体库, 元数据复制失败不算失败.

    成功路径保持 copy2 的全部行为 (Windows 的 CopyFile2 与其余平台的 copyfile + copystat).
    网盘 / FUSE 挂载 (CloudDrive2 等) 常对 utime、chmod 返回 EPERM: copy2 在内容写入之后
    抛错, 而元数据缺失不影响产物可用, 不应让整个整理任务失败.
    内容失败时记录两次尝试的原文, 并原样抛出回退那次的错误.
    """
    try:
        shutil.copy2(src, dst)
        return
    except OSError as first:
        # 内容可能已经写好, 重拷一次以区分内容失败与元数据失败: 内容失败会在此再次抛出.
        try:
            shutil.copyfile(src, dst)
        except OSError as second:
            # 回退复制经 CRT 打开文件, Win32 码会被压成 EINVAL, 两次的原文都要留下.
            logger.error(
                "library copy failed",
                src=str(src),
                dst=str(dst),
                copy2_error=str(first),
                copyfile_error=str(second),
            )
            raise
    try:
        shutil.copystat(src, dst)
    except OSError as exc:
        logger.debug("copy metadata skipped", src=str(src), dst=str(dst), error=str(exc))


@in_thread
def _place_library_images(
    paths: ResolvedPaths,
    *,
    thumb_local: Path | None,
    poster_local: Path | None,
    trailer_local: Path | None,
    extrafanart_targets: Sequence[tuple[str, Path]],
    kinds: set[DownloadableResource],
    config: HotSettings | None,
    file_info: FileInfo | None,
    content_type: ContentType,
    watermark_dir: Path | None,
    overwrite_image: bool = True,
) -> int:
    """复制封面 / 海报 / 预告片 / extrafanart 到库路径, 返回因目标已有产物而跳过的张数.

    ``overwrite_image=False`` 时目标已有图片则整块跳过 (fanart 与 thumb 同源, 随 thumb 一同跳过),
    记入 skipped; True (默认) 维持覆盖写, 不判断目标上是否已有产物.
    水印只作用于本轮写出的图, 跳过的既有图不重编码.
    """
    skipped = 0
    thumb_written = False
    poster_written = False
    if thumb_local is not None and DownloadableResource.thumb in kinds:
        if not overwrite_image and paths.thumb.exists():
            skipped += 1
        else:
            paths.thumb.parent.mkdir(parents=True, exist_ok=True)
            _copy_into_library(thumb_local, paths.thumb)
            paths.fanart.parent.mkdir(parents=True, exist_ok=True)
            _copy_into_library(thumb_local, paths.fanart)
            thumb_written = True

    if DownloadableResource.poster in kinds:
        if not overwrite_image and paths.poster.exists():
            skipped += 1
        elif poster_local is not None:
            paths.poster.parent.mkdir(parents=True, exist_ok=True)
            _copy_into_library(poster_local, paths.poster)
            poster_written = True
        elif (
            thumb_local is not None
            and config is not None
            and config.scraping.crop_poster
            and allows_poster_crop(content_type)
        ):
            paths.poster.parent.mkdir(parents=True, exist_ok=True)
            crop_poster(
                paths.thumb,
                paths.poster,
                poster_ratio=config.scraping.poster_ratio,
                jpeg_quality=config.scraping.jpeg_quality,
            )
            poster_written = True

    if trailer_local is not None and DownloadableResource.trailer in kinds:
        if not overwrite_image and paths.trailer.exists():
            skipped += 1
        else:
            paths.trailer.parent.mkdir(parents=True, exist_ok=True)
            _copy_into_library(trailer_local, paths.trailer)

    if extrafanart_targets and DownloadableResource.extrafanart in kinds:
        paths.extrafanart_dir.mkdir(parents=True, exist_ok=True)
        for name, p in extrafanart_targets:
            target = paths.extrafanart_dir / name
            if not overwrite_image and target.exists():
                skipped += 1
                continue
            _copy_into_library(p, target)

    # 叠加水印 (thumb / poster); 跳过的产物不重写, 水印也随之不动.
    # overwrite_image=True 时保持对既有图叠印: 覆盖写路径的产物也算本轮写的.
    wm = config.watermark if config is not None else WatermarkConfig()
    if wm.enabled and file_info is not None:
        jpeg_quality = config.scraping.jpeg_quality if config is not None else 95
        if (thumb_written or overwrite_image) and paths.thumb.exists():
            apply_cover_watermarks_from_info(
                paths.thumb,
                file_info,
                content_type,
                jpeg_quality=jpeg_quality,
                watermark_dir=watermark_dir,
                scale=wm.scale,
                corners=wm.corners,
            )
        if (poster_written or overwrite_image) and paths.poster.exists():
            apply_cover_watermarks_from_info(
                paths.poster,
                file_info,
                content_type,
                jpeg_quality=jpeg_quality,
                watermark_dir=watermark_dir,
                scale=wm.scale,
                corners=wm.corners,
            )
    return skipped


async def _download_images_via_store(
    metadata: Metadata,
    store: ResourceStore,
    client: WebClient,
    paths: ResolvedPaths,
    config: HotSettings | None,
    kinds: set[DownloadableResource],
    file_info: FileInfo | None,
    content_type: ContentType,
    watermark_dir: Path | None = None,
    library: Library | None = None,
    naming: NamingConfig | None = None,
    overwrite_image: bool = True,
) -> int:
    """优先用 Resource 已有文件; 缺失则现场 acquire. 返回因目标已有图片而跳过的张数.

    剧照文件名在这里渲染: 补零宽度取决于本轮剧照数量, 只有拿到文件列表后才定得下来.
    """
    thumb_local = None
    if DownloadableResource.thumb in kinds:
        thumb_urls = metadata.thumb_urls
        thumb_local = await _acquire_first_local(thumb_urls, store, client) if thumb_urls else None

    poster_local = None
    if DownloadableResource.poster in kinds:
        poster_urls = metadata.poster_urls
        poster_local = await _acquire_first_local(poster_urls, store, client) if poster_urls else None

    trailer_local = None
    if DownloadableResource.trailer in kinds:
        trailer_urls = metadata.trailer_urls
        trailer_local = await _acquire_first_local(trailer_urls, store, client) if trailer_urls else None

    extrafanart: list[Path] = []
    if DownloadableResource.extrafanart in kinds:
        extrafanart_by_site = metadata.extrafanart_urls
        if extrafanart_by_site:
            priority = list(extrafanart_by_site.keys())
            extrafanart = await store.acquire_extrafanart(extrafanart_by_site, priority, client)

    # 名字与源文件成对落地: 文件名模板只能在这里渲染, 宽度取决于本轮张数.
    extrafanart_targets: list[tuple[str, Path]] = []
    if extrafanart:
        template = (
            naming.extrafanart_name_template
            if naming is not None
            else (library.extrafanart_name_template if library is not None else None)
        )
        syntax = naming.syntax if naming is not None else "native"
        names = render_extrafanart_names(
            sidecar_template(template, "extrafanart_name_template", syntax),
            paths.context,
            len(extrafanart),
            syntax=syntax,
        )
        extrafanart_targets = list(zip(names, extrafanart, strict=True))

    return await _place_library_images(
        paths,
        thumb_local=thumb_local,
        poster_local=poster_local,
        trailer_local=trailer_local,
        extrafanart_targets=extrafanart_targets,
        kinds=kinds,
        config=config,
        file_info=file_info,
        content_type=content_type,
        watermark_dir=watermark_dir,
        overwrite_image=overwrite_image,
    )


@in_thread
def _classify_indexed(path: Path, scan: LibraryScan) -> LibraryFileKind | None:
    return scan.classify(path)


class OrganizeHandler(TaskHandler[OrganizePayload, OrganizeResult]):
    """依据已有 Metadata 整理范围内的 MediaFile; 不刮削, 不修改 Metadata, 不扫描磁盘.

    同库执行期与 DELETE 共用一把锁.
    """

    def __init__(
        self,
        repo: Repository,
        config: HotSettings,
        resource_store: ResourceStore,
        web_client: WebClient | None = None,
        safe_dirs: Sequence[Path] | None = (),
        watermark_dir: Path | None = None,
        *,
        library_locks: LibraryTaskLocks | None = None,
        parse_rules: ParseRuleProvider | None = None,
    ):
        super().__init__(payload_t=OrganizePayload, result_t=OrganizeResult)
        self._repo = repo
        self._config = config
        self._web_client = web_client
        self._resource_store = resource_store
        self._safe_dirs = safe_dirs
        self._watermark_dir = watermark_dir
        self._library_locks = library_locks if library_locks is not None else LibraryTaskLocks()
        self._parse_rules = parse_rules

    async def _load_scope(self, payload: OrganizePayload, library: Library) -> list[MediaFile]:
        assert library.id is not None
        if payload.media_file_ids is not None:
            return await self._repo.list_media_files(library_id=library.id, ids=payload.media_file_ids, limit=None)
        indexed = await self._repo.list_media_files(library_id=library.id, limit=None)
        if nfc_path(payload.path) != nfc_path(library.path):
            return [mf for mf in indexed if path_is_under(mf.path, payload.path)]
        return indexed

    def parse_payload(self, raw: dict) -> OrganizePayload:
        payload = super().parse_payload(raw)
        if isinstance(raw, dict) and "explicit_fields" not in raw:
            # 升级前入队的任务没有 explicit_fields 键: 按旧语义「非 None 即显式」重建 (规格 3.1.3).
            payload.explicit_fields = [
                name for name in ("write_nfo", "copy_resources") if getattr(payload, name) is not None
            ]
        return payload

    async def handle(self, payload: OrganizePayload) -> TaskResult[OrganizeResult]:
        library = await self._repo.get_library(payload.library_id)
        if library is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")
        assert library.id is not None
        library_root = Path(library.path)
        if not await path_is_dir(library_root):
            return TaskResult(success=False, error=f"不是目录: {library.path}")
        if payload.media_file_ids is None:
            scope = Path(payload.path) if payload.path else library_root
            if nfc_path(str(scope)) != nfc_path(library.path) and not await path_is_dir(scope):
                return TaskResult(success=False, error=f"不是目录: {scope}")

        lock = await self._library_locks.get(library.id)
        async with lock:
            return await self._handle_unlocked(payload, library)

    async def _handle_unlocked(self, payload: OrganizePayload, library: Library) -> TaskResult[OrganizeResult]:
        indexed = await self._load_scope(payload, library)
        skipped = 0
        if payload.media_file_ids and not indexed:
            # 勾选快照里的行在本次运行前已消失 (自动整理的单文件范围是主要来源): 不是「空跑」, 记一次跳过.
            logger.warning("organize scope missing", library_id=library.id, media_file_ids=payload.media_file_ids)
            skipped += 1
        library_root = Path(library.path)
        media_extensions = frozenset(self._config.watcher.media_extensions) or MEDIA_EXTENSIONS
        scan = LibraryScan(
            trailer_pattern=library.trailer_pattern,
            blacklist_patterns=library.blacklist_patterns,
            min_file_size=library.min_file_size,
            media_extensions=media_extensions,
        )
        # 清理缺失索引之前先恢复本库的覆写 journal (联合审核 P1): 同文件系统 MOVE 已落位而
        # 接管失败时, 源路径已消失 —— 若先清理, 源索引行被删, 恢复将永久找不到源行.
        # 只处理本库目标 (其它库任务正在写入的 journal 不在本次锁保护范围内, 不触碰).
        await recover_organize_journals(self._repo, library_root=library_root)
        # 双保险: 仍未完成的 journal (恢复失败保留的行) 引用的源行受保护, 缺失也不清理.
        protected_source_ids = {
            row.source_file_id
            for row in await self._repo.list_organize_journals()
            if row.phase is not OrganizeJournalPhase.COMMITTED and row.source_file_id is not None
        }
        live: list[MediaFile] = []
        prune_total = len(indexed)
        if prune_total:
            await self.report_progress(0, prune_total, "prune")
            for i, mf in enumerate(indexed, start=1):
                if mf.id is not None:
                    mf_path = Path(mf.path)
                    missing = await existing_disk_path(mf_path, follow_symlinks=False) is None
                    if mf.id in protected_source_ids:
                        # 恢复所需的源行: 缺失也不删, 保留交恢复矩阵; 不进入本次整理.
                        skipped += 1
                        await self.report_progress(i, prune_total, "prune")
                        continue
                    if missing or not is_descendant(mf_path, library_root) or is_in_trash(mf_path):
                        await self._repo.delete_media_file(mf.id)
                    else:
                        kind = await _classify_indexed(mf_path, scan)
                        if kind is LibraryFileKind.UNWANTED or kind is LibraryFileKind.SKIP:
                            skipped += 1
                        else:
                            # MEDIA, 以及 classify 返回 None 的已索引行 (不在当前扩展名白名单,
                            # 例如库曾用 patterns 收过 .txt). 仍按索引落盘, 不按扫描 glob 再裁一次.
                            live.append(mf)
                await self.report_progress(i, prune_total, "prune")
        else:
            live = indexed

        organized = 0
        overwritten = 0
        conflicted = 0
        conflicts: list[OrganizeConflict] = []
        failed = 0
        total = len(live)
        # 任务入口解析一次生效配置, 向下传参; 下层不得回读库行覆盖已解析结果 (规格 3.1.3).
        effective = await resolve_effective_config(self._repo, self._config, library, payload)
        task_id = current_task_id()
        # 只有移动方式会移走源文件, 复制 / 硬链接 / 符号链接不腾空目录.
        prune_candidates: set[Path] = set()
        collect_prune = payload.prune_empty_dirs and library.move_mode is MoveMode.MOVE
        if total == 0:
            await self.report_progress(1, 1, "done")
        else:
            await self.report_progress(0, total, "organize")
            for i, media_file in enumerate(live, start=1):
                path_str = media_file.path
                file_path = Path(path_str)
                if media_file.metadata_id is None:
                    skipped += 1
                    await self.report_progress(i, total, file_path.name)
                    continue

                metadata = await self._repo.get_metadata(media_file.metadata_id)
                if metadata is None:
                    skipped += 1
                    await self.report_progress(i, total, file_path.name)
                    continue

                write_nfo = effective.naming.write_nfo
                copy_resources = effective.naming.copy_resources
                rules = (
                    await self._parse_rules.for_library(payload.library_id) if self._parse_rules is not None else None
                )
                fop_result = await apply_file_operations(
                    self._repo,
                    media_file.id,
                    metadata,
                    self._config,
                    self._resource_store,
                    write_nfo=write_nfo,
                    copy_resources=copy_resources,
                    web_client=self._web_client,
                    safe_dirs=self._safe_dirs,
                    watermark_dir=self._watermark_dir,
                    naming=effective.naming,
                    nfo_options=effective.nfo_options,
                    overwrite_video=effective.common.overwrite_video,
                    overwrite_image=effective.common.overwrite_image,
                    task_id=task_id,
                    rules=rules,
                )
                if fop_result is None:
                    skipped += 1
                    await self.report_progress(i, total, file_path.name)
                    continue

                if fop_result.dest and media_file.id is not None:
                    await commit_organized_media_file(self._repo, media_file, fop_result.dest, library_root)
                match fop_result.outcome:
                    case PlaceOutcome.PLACED:
                        organized += 1
                        if fop_result.overwritten:
                            overwritten += 1
                        skipped += fop_result.skipped_images
                        if collect_prune:
                            prune_candidates.update(ancestor_dirs(file_path, library_root=library_root))
                        # 字幕冲突的 path 是留在原处的那条字幕, 不是视频源路径.
                        conflicted += _record_conflicts(
                            conflicts,
                            [
                                OrganizeConflict(
                                    path=str(item.source),
                                    target=str(item.target),
                                    reason=OrganizeConflictReason.subtitle_target_exists,
                                )
                                for item in fop_result.subtitle_conflicts
                            ],
                        )
                    case PlaceOutcome.CONFLICT:
                        # 行不提交新路径, 留在源路径: 下次整理会再次报告, 直到用户自行处理.
                        target = fop_result.conflict_target
                        conflicted += _record_conflicts(
                            conflicts,
                            []
                            if target is None
                            else [
                                OrganizeConflict(
                                    path=path_str, target=str(target), reason=OrganizeConflictReason.target_exists
                                )
                            ],
                        )
                    case PlaceOutcome.FAILED:
                        logger.warning("organize failed", path=path_str, error=fop_result.error)
                        failed += 1
                await self.report_progress(i, total, file_path.name)
            await self.report_progress(total, total, "done")

        pruned_dirs = 0
        if prune_candidates:
            pruned_dirs = (await prune_empty_dirs(prune_candidates, library_root=library_root)).removed

        logger.info(
            "organize completed",
            path=payload.path,
            organized=organized,
            skipped=skipped,
            conflicted=conflicted,
            failed=failed,
            overwritten=overwritten,
            pruned_dirs=pruned_dirs,
        )

        return TaskResult(
            True,
            result=OrganizeResult(
                organized=organized,
                skipped=skipped,
                conflicted=conflicted,
                failed=failed,
                pruned_dirs=pruned_dirs,
                conflicts=conflicts,
                overwritten=overwritten,
            ),
        )


def _record_conflicts(conflicts: list[OrganizeConflict], entries: Sequence[OrganizeConflict]) -> int:
    """登记未处理的目标, 返回条数; 列表达到上限后只计数, 差额只留在任务日志里."""
    conflicts.extend(entries[: max(0, ORGANIZE_CONFLICT_LIMIT - len(conflicts))])
    return len(entries)


def _add_resource_ref(url: str, live_urls: set[str], live_hashes: set[str]) -> None:
    if not url:
        return
    url_hash = internal_url_hash(url)
    if url_hash is not None:
        live_hashes.add(url_hash)
    else:
        live_urls.add(url)


async def _collect_live_resource_refs(repo: Repository) -> tuple[set[str], set[str]]:
    live_urls: set[str] = set()
    live_hashes: set[str] = set()
    offset = 0
    page = 500
    # 收集 Metadata 仍引用的 locator / url_hash.
    while True:
        batch, _total = await repo.list_metadata(offset=offset, limit=page)
        if not batch:
            break
        for meta in batch:
            for u in (*meta.poster_urls, *meta.thumb_urls, *meta.trailer_urls):
                _add_resource_ref(u, live_urls, live_hashes)
            for site_urls in meta.extrafanart_urls.values():
                for u in site_urls:
                    _add_resource_ref(u, live_urls, live_hashes)
        offset += len(batch)
        if len(batch) < page:
            break

    offset = 0
    # 收集 Actor 头像 URL.
    while True:
        actors = await repo.list_actors(offset=offset, limit=page)
        if not actors:
            break
        for actor in actors:
            for u in actor.image_urls or []:
                _add_resource_ref(u, live_urls, live_hashes)
        offset += len(actors)
        if len(actors) < page:
            break
    return live_urls, live_hashes


@in_thread
def _missing_media_ids(rows: Sequence[tuple[int, str]]) -> list[int]:
    """不跟随符号链接判断存在."""
    return [media_id for media_id, path in rows if existing_disk_path_sync(path, follow_symlinks=False) is None]


class CleanupHandler(TaskHandler[CleanupPayload, CleanupResult]):
    """不因缺少 MediaFile 而删除 Metadata."""

    def __init__(self, repo: Repository, resource_store: ResourceStore):
        super().__init__(payload_t=CleanupPayload, result_t=CleanupResult)
        self._repo = repo
        self._resource_store = resource_store

    async def handle(self, payload: CleanupPayload) -> TaskResult[CleanupResult]:
        files_removed = 0
        resources_removed = 0

        # 删除磁盘上不存在的 MediaFile 记录.
        if payload.remove_missing_files:
            missing_ids: list[int] = []
            offset = 0
            page = 500
            while True:
                batch = await self._repo.list_media_files(limit=page, offset=offset)
                if not batch:
                    break
                missing_ids.extend(await _missing_media_ids([(mf.id, mf.path) for mf in batch if mf.id is not None]))
                offset += len(batch)
                if len(batch) < page:
                    break
            for media_id in missing_ids:
                await self._repo.delete_media_file(media_id)
                files_removed += 1

        # 删除不被 Metadata / Actor URL 引用的 Resource.
        if payload.remove_unreferenced_resources:
            live_urls, live_hashes = await _collect_live_resource_refs(self._repo)
            resources_removed = await self._resource_store.purge_unreferenced(live_urls, live_hashes)

        logger.info("cleanup completed", files_removed=files_removed, resources_removed=resources_removed)

        return TaskResult(True, result=CleanupResult(files_removed=files_removed, resources_removed=resources_removed))
