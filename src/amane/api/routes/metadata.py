from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import TypeAdapter

from ...aggregate import compute_merge_updates
from ...config import NamingConfig, NfoConfig
from ...config.overrides import ConfigResolver
from ...db.embeddings import EmbeddingKind
from ...db.models import Metadata, MetadataSortField, SavedQueryEntity, SortOrder, TaskType
from ...db.repo_types import WriteMode
from ...db.repos.media import file_phase_of
from ...enums import ActorGender
from ...handlers import ScrapePayload
from ...media import NfoOptions, manual_crop_image, write_nfo
from ...organize.path_templates import resolve_sidecar_paths
from ...parsing import FilePhaseSummary as ParsedFilePhase
from ...parsing import parse_file_info, summarize_file_phases
from ...semantic import SemanticUnavailable
from ...utils.dates import normalize_calendar_date
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models import (
    CommentResponse,
    CropPosterRequest,
    FilePhaseSummary,
    MergeRequest,
    MetadataBatchDeleteResponse,
    MetadataBatchIdsRequest,
    MetadataBatchScrapeRequest,
    MetadataBatchScrapeResponse,
    MetadataDetailResponse,
    MetadataListResponse,
    MetadataLocksRequest,
    MetadataMergeEntriesRequest,
    MetadataResponse,
    MetadataUserTagsRequest,
    NfoRewriteFailure,
    NfoRewriteResponse,
    PartialMetadata,
    SemanticHitInfo,
    UserTagLinksResponse,
    UserTagResponse,
    to_media_file_response,
)
from ..support.metadata_filters import MetadataFilters, parse_metadata_filters, require_known_definition
from .saved_queries import resolve_saved_query_id_subquery

if TYPE_CHECKING:
    from ...db.repo_types import MetadataFields

logger = structlog.get_logger()

router = APIRouter(prefix="/metadata", tags=["metadata"])


async def build_metadata_responses(repo: RepoDep, items: Sequence[Metadata]) -> list[MetadataResponse]:
    """列表与语义检索共用: 补 file_count / file_phase 聚合."""
    summaries = await repo.summarize_media_by_metadata(items)
    responses: list[MetadataResponse] = []
    for m in items:
        summary = summaries.get(m.id) if m.id is not None else None
        response = to_resp(MetadataResponse, m)
        if summary is not None:
            response = response.model_copy(
                update={
                    "file_count": summary.file_count,
                    "file_phase": _file_phase_resp(summary.phase),
                }
            )
        responses.append(response)
    return responses


def _file_phase_resp(summary: ParsedFilePhase) -> FilePhaseSummary:
    return FilePhaseSummary(
        has_subtitle=summary.has_subtitle,
        censored=summary.censored,
        uncensored=summary.uncensored,
        mosaics=list(summary.mosaics),
        definition=summary.definition,
    )


@router.get("/schema")
async def get_metadata_schema() -> dict:
    return TypeAdapter(PartialMetadata).json_schema()


@router.get("")
async def list_metadata(
    repo: RepoDep,
    runtime: RuntimeDep,
    filters: Annotated[MetadataFilters, Depends(parse_metadata_filters)],
    search: Annotated[str | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    sort_by: Annotated[MetadataSortField, Query(description="Sort field")] = MetadataSortField.UPDATED_AT,
    order: Annotated[SortOrder, Query(description="Sort order")] = SortOrder.DESC,
    semantic_query: Annotated[
        str | None, Query(min_length=1, max_length=200, description="按画面描述排序; 提供时忽略 sort_by / order")
    ] = None,
    semantic_kind: Annotated[Literal["all", "cover", "still"], Query(description="参与语义排序的图片来源")] = "all",
) -> MetadataListResponse:
    definition = require_known_definition(filters.definition)
    id_subquery_sql = None
    if filters.saved_query_id is not None:
        id_subquery_sql = await resolve_saved_query_id_subquery(repo, filters.saved_query_id, SavedQueryEntity.METADATA)
    if semantic_query is not None:
        return await _semantic_list(
            runtime=runtime,
            repo=repo,
            filters=filters,
            search=search,
            semantic_query=semantic_query,
            semantic_kind=semantic_kind,
            definition=definition,
            id_subquery_sql=id_subquery_sql,
            offset=offset,
            limit=limit,
        )
    items, total = await repo.list_metadata(
        keyword=search,
        offset=offset,
        limit=limit,
        sort_by=sort_by,
        order=order,
        actor_ids=filters.actor_id,
        director_ids=filters.director_id,
        tag_ids=filters.tag_id,
        studio_ids=filters.studio_id,
        publisher_ids=filters.publisher_id,
        series_ids=filters.series_id,
        user_tag_ids=filters.user_tag_id,
        library_ids=filters.library_id,
        has_files=filters.has_files,
        has_subtitle=filters.has_subtitle,
        vr=filters.vr,
        mosaic=filters.mosaic,
        uncensored=filters.uncensored,
        definition=definition,
        content_type=filters.content_type,
        id_subquery_sql=id_subquery_sql,
    )
    return MetadataListResponse(items=await build_metadata_responses(repo, items), total=total)


async def _semantic_list(
    *,
    runtime: RuntimeDep,
    repo: RepoDep,
    filters: MetadataFilters,
    search: str | None,
    semantic_query: str,
    semantic_kind: Literal["all", "cover", "still"],
    definition: str | None,
    id_subquery_sql: str | None,
    offset: int,
    limit: int,
) -> MetadataListResponse:
    """语义参数出现时接管排序: 候选集来自同一套筛选, 返回结构与普通列表一致."""
    service = runtime.semantic
    if service is None or not service.enabled:
        raise HTTPException(status_code=503, detail="语义检索未启用")
    allowed_ids = None
    if filters.active or search:
        allowed_ids = await repo.list_metadata_ids(
            keyword=search,
            actor_ids=filters.actor_id,
            director_ids=filters.director_id,
            tag_ids=filters.tag_id,
            studio_ids=filters.studio_id,
            publisher_ids=filters.publisher_id,
            series_ids=filters.series_id,
            user_tag_ids=filters.user_tag_id,
            library_ids=filters.library_id,
            has_files=filters.has_files,
            has_subtitle=filters.has_subtitle,
            vr=filters.vr,
            mosaic=filters.mosaic,
            uncensored=filters.uncensored,
            definition=definition,
            content_type=filters.content_type,
            id_subquery_sql=id_subquery_sql,
        )
    kind = None if semantic_kind == "all" else EmbeddingKind(semantic_kind)
    try:
        hits = await service.search(semantic_query, limit, kind, allowed_ids, offset)
        total = await service.count(kind, allowed_ids)
    except SemanticUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not hits:
        return MetadataListResponse(items=[], total=total)
    ids = [hit.metadata_id for hit in hits]
    items, _ = await repo.list_metadata(ids=ids, limit=len(ids))
    by_id = {item.id: item for item in items}
    ordered = [by_id[hit.metadata_id] for hit in hits if hit.metadata_id in by_id]
    responses = {response.id: response for response in await build_metadata_responses(repo, ordered)}
    return MetadataListResponse(
        items=[responses[hit.metadata_id] for hit in hits if hit.metadata_id in responses],
        total=total,
        semantic_hits=[
            SemanticHitInfo(
                id=hit.metadata_id,
                kind=hit.kind,
                resource_url=hit.resource_url,
                similarity=hit.score,
            )
            for hit in hits
            if hit.metadata_id in responses
        ],
    )


@router.post("/batch/delete")
async def batch_delete_metadata(req: MetadataBatchIdsRequest, repo: RepoDep) -> MetadataBatchDeleteResponse:
    """级联行为与单条删除一致."""
    deleted, missing = await repo.batch_delete_metadata(req.ids)
    logger.info("metadata batch deleted", deleted=deleted, missing=missing)
    return MetadataBatchDeleteResponse(deleted=deleted, missing=missing)


@router.post("/batch/scrape", status_code=202)
async def batch_scrape_metadata(req: MetadataBatchScrapeRequest, repo: RepoDep) -> MetadataBatchScrapeResponse:
    """以各自 number 重新刮削."""
    missing = 0
    payloads: list[ScrapePayload] = []
    for metadata_id in req.ids:
        metadata = await repo.get_metadata(metadata_id)
        if metadata is None:
            missing += 1
            continue
        payloads.append(
            ScrapePayload(
                number=metadata.number,
                content_type=req.content_type if req.content_type is not None else metadata.content_type,
                use_cache=req.use_cache,
            )
        )
    # 批量提交动辄数百条, 逐条提交会重复付出事务开销
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads)
    task_ids = [task.id for task in tasks if task.id is not None]
    logger.info("metadata batch scrape submitted", submitted=len(task_ids), missing=missing)
    return MetadataBatchScrapeResponse(submitted=len(task_ids), missing=missing, task_ids=task_ids)


@router.post("/batch/user-tags")
async def batch_metadata_user_tags(req: MetadataUserTagsRequest, repo: RepoDep) -> UserTagLinksResponse:
    """把一组用户标签应用到一组影片; 未知标签 id 返回 404, 不存在的影片 id 计入 missing."""
    try:
        result = await repo.apply_metadata_user_tags(req.ids, req.user_tag_ids, action=req.action)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    logger.info(
        "metadata user tags applied",
        action=req.action,
        changed=result.changed,
        unchanged=result.unchanged,
        missing=result.missing,
    )
    return UserTagLinksResponse(changed=result.changed, unchanged=result.unchanged, missing=result.missing)


@router.post("/merge-entries")
async def merge_metadata_entries(req: MetadataMergeEntriesRequest, repo: RepoDep) -> MetadataResponse:
    """把来源条目的 raw 快照补入目标并删除来源: 目标字段不变, 关联记录迁入目标.

    ``rescrape`` 要求合并提交后再按目标番号入队 SCRAPE: 只有 raw 已落库, 复用快照的聚合
    才能按当前优先级重建字段与站点级字典.
    """
    try:
        metadata = await repo.merge_metadata_entries(req.target_id, req.source_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    rescrape_task_id: int | None = None
    if req.rescrape:
        task = await repo.create_task(
            TaskType.SCRAPE,
            ScrapePayload(number=metadata.number, content_type=metadata.content_type),
        )
        rescrape_task_id = task.id
    logger.info(
        "metadata entries merged",
        target_id=req.target_id,
        source_ids=req.source_ids,
        rescrape_task_id=rescrape_task_id,
    )
    return to_resp(MetadataResponse, metadata)


@router.get("/{metadata_id}")
async def get_metadata(metadata_id: int, repo: RepoDep) -> MetadataDetailResponse:
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    # 关联文件 / 用户 tag / 评论 / 分类 id
    files = await repo.get_media_by_metadata_id(metadata_id)
    user_tags = await repo.list_metadata_user_tags(metadata_id)
    comments = await repo.list_comments(metadata_id)
    (
        actor_ids,
        actor_genders,
        director_ids,
        tag_ids,
        studio_id,
        publisher_id,
        series_id,
    ) = await repo.resolve_metadata_facet_ids(metadata)
    return MetadataDetailResponse(
        metadata=to_resp(MetadataResponse, metadata).model_copy(
            update={
                "file_count": len(files),
                "file_phase": _file_phase_resp(
                    summarize_file_phases((file_phase_of(f) for f in files), metadata.content_type)
                ),
            }
        ),
        files=[to_media_file_response(f, metadata.content_type) for f in files],
        user_tags=[to_resp(UserTagResponse, t) for t in user_tags],
        comments=[to_resp(CommentResponse, c) for c in comments],
        actor_ids=actor_ids,
        actor_genders=actor_genders,
        director_ids=director_ids,
        tag_ids=tag_ids,
        studio_id=studio_id,
        publisher_id=publisher_id,
        series_id=series_id,
    )


@router.patch("/{metadata_id}")
async def update_metadata(metadata_id: int, req: PartialMetadata, repo: RepoDep) -> MetadataResponse:
    updates = cast("MetadataFields", {k: v for k, v in req.model_dump().items() if v is not None})
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的字段")
    if "release" in updates:
        raw_release = updates["release"]
        if isinstance(raw_release, str):
            normalized = normalize_calendar_date(raw_release)
            if normalized is None:
                raise HTTPException(status_code=422, detail="发行日期必须为 YYYY-MM-DD")
            updates["release"] = normalized
        else:
            raise HTTPException(status_code=422, detail="发行日期必须为 YYYY-MM-DD")
    metadata = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, **updates)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata updated", metadata_id=metadata_id, fields=list(updates.keys()))
    return to_resp(MetadataResponse, metadata)


@router.put("/{metadata_id}/locks")
async def set_metadata_locks(metadata_id: int, req: MetadataLocksRequest, repo: RepoDep) -> MetadataResponse:
    """整体替换锁定字段集合."""
    metadata = await repo.set_metadata_locks(metadata_id, req.fields)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata locks updated", metadata_id=metadata_id, fields=[str(field) for field in req.fields])
    return to_resp(MetadataResponse, metadata)


@router.delete("/{metadata_id}", status_code=204)
async def delete_metadata(metadata_id: int, repo: RepoDep) -> None:
    deleted = await repo.delete_metadata(metadata_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata deleted", metadata_id=metadata_id)


@router.post("/{metadata_id}/crop-poster")
async def crop_poster_from_thumb(
    metadata_id: int,
    req: CropPosterRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> MetadataResponse:
    """从封面 (thumb) 按像素框裁切海报, 写入派生 Resource 并更新 poster_urls.

    先按 EXIF 转正、再顺时针旋转 ``req.rotation``, 框相对旋转后的图像.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    if not metadata.thumb_urls:
        raise HTTPException(status_code=400, detail="无封面图可裁切")

    # 按像素框裁切封面, 写入派生 Resource
    thumb_url = metadata.thumb_urls[0]
    box = (req.left, req.top, req.right, req.bottom)
    try:
        poster_url = await manual_crop_image(
            thumb_url,
            box,
            runtime.resource_store,
            runtime.web_client,
            runtime.config.hot,
            runtime.config.cold.data_dir,
            subject="封面图",
            rotation=req.rotation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 写回 poster_urls
    updated = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, poster_urls=[poster_url])
    assert updated is not None
    logger.info(
        "poster cropped",
        metadata_id=metadata_id,
        box=box,
        rotation=req.rotation,
        poster_url=poster_url,
    )
    return to_resp(MetadataResponse, updated)


class _NfoPairingMismatch(ValueError):
    """重写出的 NFO 与消费文件不同目录或不同主干."""


async def _rewrite_media_nfo(
    metadata: Metadata,
    media_path: str,
    link_path: str | None,
    has_external_subtitle: bool,
    library_id: int,
    *,
    repo: RepoDep,
    resolver: ConfigResolver,
    safe_dirs: Sequence[Path] | None,
    actor_genders: Mapping[str, ActorGender] | None,
) -> Path:
    """按已落盘路径重写单份文件的 NFO (规格 3.4.4): 只渲染 NFO, 不重算视频与链接位置.

    消费路径取持久化的 ``link_path``; 链接文件已不在盘上时按无链接处理 (回退视频路径),
    该列是只读消费, 不改写. ``source_path`` 取落盘路径: 原始路径未持久化, 含 ``raw_*`` 的
    附属模板在重写时无法还原整理时的取值, 因此本端点只渲染 NFO (默认模板不含 ``raw_*``).
    """
    library = await repo.get_library(library_id)
    if library is None:
        raise ValueError(f"归属库 {library_id} 不存在")
    video_path = Path(media_path)
    consume_path = Path(link_path) if link_path is not None else video_path
    if link_path is not None and not consume_path.exists():
        # 链接文件已不存在: 读取侧按无链接处理, 不改写 link_path 列 (规格 3.4.4).
        consume_path = video_path
    naming = await resolver.resolve(NamingConfig, scope=library, library=library)
    nfo_config = await resolver.resolve(NfoConfig, scope=library, library=library)
    file_info = parse_file_info(media_path, has_external_subtitle=has_external_subtitle)
    sidecars = resolve_sidecar_paths(
        library,
        metadata,
        consume_path=consume_path,
        video_path=video_path,
        source_path=video_path,
        ext=video_path.suffix.lstrip("."),
        file_info=file_info,
        safe_dirs=safe_dirs,
        actor_genders=actor_genders,
        artifacts=("nfo",),
        naming=naming,
    )
    nfo = sidecars.nfo
    if nfo is None:  # artifacts 含 nfo, 理论不可达; 渲染失败以 ValueError 抛出.
        raise ValueError("NFO 模板未渲染出路径")
    if nfo.parent != consume_path.parent or nfo.stem != consume_path.stem:
        raise _NfoPairingMismatch(
            f"NFO 目标路径与消费文件不配对: 实际 {nfo} (stem {nfo.stem!r}), "
            f"期望与 {consume_path} 同目录同主干 (stem {consume_path.stem!r})"
        )
    if not await write_nfo(metadata, nfo, NfoOptions(**nfo_config.model_dump())):
        # write_nfo 对写盘异常返回 False 而不抛出: 目录创建失败、权限不足、磁盘满都到不了
        # 这里的调用方, 必须转成 OSError 才能落入端点的逐文件失败分支.
        raise OSError(f"NFO 写盘失败: {nfo}")
    return nfo


@router.put("/{metadata_id}/nfo")
async def rewrite_nfo(metadata_id: int, repo: RepoDep, config: ConfigDep, runtime: RuntimeDep) -> NfoRewriteResponse:
    """NFO 单独重写 (规格 3.4.4): 以已落盘路径为基准渲染并覆盖写, 供只改模板后修复既有 NFO.

    条目的每份文件独立处理: 失败不回滚成功者, 失败清单随响应返回; 全部失败且含配对
    不一致时整体 422 并报出实际与期望配对. 生效配置 (naming / nfo 开关) 按各文件归属库合成.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    files = await repo.get_media_by_metadata_id(metadata_id)
    if not files:
        raise HTTPException(status_code=409, detail="无本地文件，无法重写 NFO")
    actor_genders = {a.name: a.gender for a in await repo.get_actors_by_names(metadata.actors)}
    resolver = ConfigResolver(config.hot, repo)
    rewritten: list[str] = []
    failures: list[NfoRewriteFailure] = []
    mismatches: list[str] = []
    for media in files:
        try:
            nfo = await _rewrite_media_nfo(
                metadata,
                media.path,
                media.link_path,
                media.has_external_subtitle,
                media.library_id,
                repo=repo,
                resolver=resolver,
                safe_dirs=runtime.safe_dirs,
                actor_genders=actor_genders,
            )
            rewritten.append(str(nfo))
        except _NfoPairingMismatch as exc:
            mismatches.append(str(exc))
            failures.append(NfoRewriteFailure(path=media.path, error=str(exc)))
        except (ValueError, OSError) as exc:
            failures.append(NfoRewriteFailure(path=media.path, error=str(exc)))
    if not rewritten and mismatches:
        # 配对不一致是模板与落盘状态的契约违背, 不是环境故障: 整体 422, 逐文件报出实际与期望.
        raise HTTPException(status_code=422, detail="；".join(mismatches))
    return NfoRewriteResponse(rewritten=rewritten, failures=failures)


@router.post("/{metadata_id}/merge")
async def merge_metadata(metadata_id: int, req: MergeRequest, repo: RepoDep) -> MetadataResponse:
    if not req.selections:
        raise HTTPException(status_code=400, detail="未提供选择项")

    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    # 按 selections 从 raw 合并字段
    try:
        updates = compute_merge_updates(metadata.raw, metadata.field_sources, req.selections)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if not updates:
        raise HTTPException(status_code=400, detail="没有有效的选择项")

    # 写回元数据
    updated = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, **cast("MetadataFields", updates))
    assert updated is not None
    logger.info("metadata merged", metadata_id=metadata_id, selections=req.selections)
    return to_resp(MetadataResponse, updated)
