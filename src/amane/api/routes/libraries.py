from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Response
from pydantic import ValidationError

from ...config import NamingConfig, NfoConfig, OrganizeCommonConfig
from ...config.overrides import ConfigResolver, section_model, validate_override_payload
from ...db.models import TEMPLATE_OVERRIDE_FIELDS, Library, Metadata, TaskType
from ...handlers import RefreshPayload, ScanMode
from ...organize.path_templates import (
    NAMING_TEMPLATE_FIELDS,
    resolve_paths,
    resolve_subtitle_path,
    validate_naming_templates,
)
from ...parsing import parse_file_info
from ...parsing.file_info import mosaic_nature
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models import (
    EffectiveConfigResponse,
    LibraryCreateRequest,
    LibraryListResponse,
    LibraryOverrideResponse,
    LibraryOverridesResponse,
    LibraryResponse,
    LibraryUpdateRequest,
    NamingPreviewRequest,
    NamingPreviewResponse,
    PathTemplateSchemaResponse,
    path_template_schema,
)
from ..support.path_validation import validate_directory_path

if TYPE_CHECKING:
    from ...db.repo_types import LibraryUpdates

logger = structlog.get_logger()

router = APIRouter(prefix="/libraries", tags=["libraries"])


@router.get("/path-template-schema")
async def get_path_template_schema() -> PathTemplateSchemaResponse:
    """与 ORGANIZE 实际使用的默认值同源."""
    return path_template_schema()


@router.post("/naming/preview")
async def preview_naming(req: NamingPreviewRequest, repo: RepoDep, config: ConfigDep) -> NamingPreviewResponse:
    """命名模板试算 (规格 3.2.4): 与整理走同一渲染代码路径 (resolve_paths + TemplateContext), 纯计算不落盘.

    相对模板以库根为基准; 无 library_id 时以 ``/`` 为基准按全局默认模板试算.
    模板无法渲染 (语法无效 / 渲染退化到库根) → 422, detail 指明模板与原因.
    """
    metadata, source_path, library_id = await _preview_inputs(req, repo)
    library = await repo.get_library(library_id) if library_id is not None else None
    if library is not None:
        naming = await ConfigResolver(config.hot, repo).resolve(NamingConfig, scope=library, library=library)
        base_library = library
    else:
        naming = config.hot.naming
        base_library = Library(name="preview", path="/")
    if req.syntax is not None and req.syntax != naming.syntax:
        naming = naming.model_copy(update={"syntax": req.syntax})

    file_info = parse_file_info(str(source_path) if source_path is not None else req.number)
    try:
        # 保存校验先行: 语法类错误由 validate_naming_templates 带上模板字段名再抛出.
        validate_naming_templates(
            naming.syntax, {field: getattr(naming, field) for field in NAMING_TEMPLATE_FIELDS}, probe=False
        )
        paths = resolve_paths(
            base_library,
            metadata,
            ext=source_path.suffix.lstrip(".") if source_path is not None else "mp4",
            source_path=source_path,
            file_info=file_info,
            safe_dirs=None,
            naming=naming,
        )
        # 字幕按 `{源文件主干}.srt` 的假想字幕源渲染, 与整理时最常见的同名字幕一致;
        # 番号全空时源名回退 Unknown, 与渲染引擎的占位符回退一致, 避免空路径名报错.
        subtitle_source = (
            source_path if source_path is not None else Path(req.number or metadata.number or "Unknown")
        ).with_suffix(".srt")
        subtitle = resolve_subtitle_path(
            base_library,
            metadata,
            subtitle_source,
            video_dir=paths.video.parent,
            video_name=paths.video.stem,
            video_dest=paths.video,
            source_path=source_path,
            file_info=file_info,
            safe_dirs=None,
            naming=naming,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return NamingPreviewResponse(
        number=metadata.number,
        content_type=str(metadata.content_type),
        mosaic=str(nature) if (nature := mosaic_nature(file_info.mosaic, metadata.content_type)) else "",
        subtitle_tag="C" if file_info.has_subtitle else "",
        definition=file_info.definition or "",
        video=str(paths.video),
        thumb=str(paths.thumb),
        poster=str(paths.poster),
        fanart=str(paths.fanart),
        nfo=str(paths.nfo),
        trailer=str(paths.trailer),
        subtitle=str(subtitle),
    )


async def _preview_inputs(req: NamingPreviewRequest, repo: RepoDep) -> tuple[Metadata, Path | None, int | None]:
    """试算入参组装: media_file_id 优先取真实文件与元数据, 否则按请求字段构造."""
    if req.media_file_id is not None:
        media_file = await repo.get_media_file(req.media_file_id)
        if media_file is None:
            raise HTTPException(status_code=404, detail=f"媒体文件 {req.media_file_id} 不存在")
        metadata = await repo.get_metadata(media_file.metadata_id) if media_file.metadata_id is not None else None
        if metadata is None:
            metadata = Metadata(number=media_file.number or "")
        return metadata, Path(media_file.path), req.library_id if req.library_id is not None else media_file.library_id
    metadata = Metadata(
        number=req.number,
        title=req.title,
        actors=req.actors,
        studio=req.studio,
        release=req.release,
        content_type=req.content_type,
    )
    source_path = Path(req.source_path) if req.source_path else None
    if source_path is not None:
        parsed = parse_file_info(str(source_path))
        if parsed.number:
            metadata.number = parsed.number
    return metadata, source_path, req.library_id


@router.get("")
async def list_libraries(repo: RepoDep) -> LibraryListResponse:
    items = await repo.list_libraries()
    return LibraryListResponse(items=[to_resp(LibraryResponse, lib) for lib in items])


@router.post("", status_code=201)
async def create_library(req: LibraryCreateRequest, repo: RepoDep, runtime: RuntimeDep) -> LibraryResponse:
    # 校验路径: 须存在、是目录、位于 safe_dirs 内
    await validate_directory_path(req.path, runtime.safe_dirs)

    name = req.name or Path(req.path).name

    # 请求模型由 FastAPI 直接从 JSON 构造, model_fields_set 如实反映请求体里的键;
    # 显式给出的模板字段 (含显式 null) 追加进名单, 否则建库时自定义的模板会被视为继承.
    template_overrides = sorted(req.model_fields_set & set(TEMPLATE_OVERRIDE_FIELDS))

    try:
        lib = await repo.create_library(
            name=name,
            path=req.path,
            automation=req.automation,
            auto_organize=req.auto_organize,
            ingest=req.ingest,
            cloud_path=req.cloud_path,
            recursive=req.recursive,
            patterns=req.patterns,
            move_mode=req.move_mode,
            video_template=req.video_template,
            link_template=req.link_template,
            link_mode=req.link_mode,
            strm_content_template=req.strm_content_template,
            thumb_template=req.thumb_template,
            poster_template=req.poster_template,
            fanart_template=req.fanart_template,
            extrafanart_template=req.extrafanart_template,
            extrafanart_name_template=req.extrafanart_name_template,
            nfo_template=req.nfo_template,
            trailer_template=req.trailer_template,
            subtitle_template=req.subtitle_template,
            subtitle_extensions=req.subtitle_extensions,
            write_nfo=req.write_nfo,
            copy_resources=req.copy_resources,
            trailer_pattern=req.trailer_pattern,
            blacklist_patterns=req.blacklist_patterns,
            min_file_size=req.min_file_size,
            template_overrides=template_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    assert lib.id is not None

    logger.info(
        "library created",
        library_id=lib.id,
        name=name,
        path=req.path,
        automation=req.automation,
        ingest=req.ingest,
    )

    if runtime.watcher_service:
        runtime.watcher_service.sync_library(lib)

    # 提交初始 Refresh 任务
    if req.scan:
        await repo.create_task(
            TaskType.REFRESH,
            RefreshPayload(
                library_id=lib.id,
                recursive=req.recursive,
                patterns=req.patterns,
                # 用落库后的路径: 入口已把库路径解析为真实路径, 首次扫描必须与它同一形式.
                path=lib.path,
                scan={ScanMode.add},
                scrape=set(),
            ),
        )

    return to_resp(LibraryResponse, lib)


@router.get("/{library_id}")
async def get_library(library_id: int, repo: RepoDep) -> LibraryResponse:
    lib = await repo.get_library(library_id)
    if lib is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return to_resp(LibraryResponse, lib)


@router.patch("/{library_id}")
async def update_library(
    library_id: int,
    req: LibraryUpdateRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> LibraryResponse:
    updates = cast("LibraryUpdates", req.model_dump(exclude_unset=True))
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的字段")

    # 显式给出的模板字段追加进名单 (repo 侧与既有名单取并集); 判定依据同建库: model_fields_set.
    explicit_templates = sorted(req.model_fields_set & set(TEMPLATE_OVERRIDE_FIELDS))
    if explicit_templates:
        updates["template_overrides"] = explicit_templates

    # 仅 path 被显式更新时才校验
    if "path" in updates and updates["path"] is not None:
        await validate_directory_path(updates["path"], runtime.safe_dirs)

    try:
        lib = await repo.update_library(library_id, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if lib is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    logger.info("library updated", library_id=library_id, fields=list(updates.keys()))

    watch_fields = {
        "automation",
        "ingest",
        "cloud_path",
        "path",
        "recursive",
        "patterns",
        "trailer_pattern",
        "blacklist_patterns",
        "min_file_size",
    }
    if runtime.watcher_service and watch_fields & updates.keys():
        runtime.watcher_service.sync_library(lib)
    # 清单记录生成时的库根: 路径改掉后按新库根重解释相对路径会删错文件.
    if "path" in updates:
        runtime.inventory_store.drop_library(library_id)

    return to_resp(LibraryResponse, lib)


@router.delete("/{library_id}", status_code=204)
async def delete_library(library_id: int, repo: RepoDep, runtime: RuntimeDep):
    """级联删除该库 MediaFile (library_id 非空 FK). 仅删除数据库索引, 不动磁盘文件."""
    existing = await repo.list_libraries()
    if not any(lib.id == library_id for lib in existing):
        raise HTTPException(status_code=404, detail="媒体库不存在")

    # 先停止监控, 避免删库后 watcher 仍为已删库 id 创建悬空 MediaFile
    if runtime.watcher_service:
        runtime.watcher_service.remove_library(library_id)

    deleted_media = await repo.delete_library(library_id)
    runtime.inventory_store.drop_library(library_id)
    logger.info("library deleted", library_id=library_id, deleted_media=deleted_media)
    return Response(status_code=204)


async def _require_library(library_id: int, repo: RepoDep) -> Library:
    library = await repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return library


@router.get("/{library_id}/overrides")
async def list_library_overrides(library_id: int, repo: RepoDep) -> LibraryOverridesResponse:
    """该库的全部覆盖, 按 section 分组; payload 为稀疏 JSON, {} 哨兵原样返回."""
    await _require_library(library_id, repo)
    rows = await repo.list_config_overrides(library_id)
    return LibraryOverridesResponse(overrides={row.section: row.payload for row in rows})


async def _validate_naming_override(
    library: Library, payload: dict[str, Any], repo: RepoDep, config: ConfigDep
) -> None:
    """naming 覆盖的语法感知校验 (规格 3.2.2 / 3.2.3).

    PUT 是整体替换: 以「本次 payload 顶掉既有覆盖行」合成生效 naming. 两个分支都执行保存校验
    (未知占位符 / 未知 filter → 422 + 名称与位置) 加渲染探针 (另一语法的残留同样拒绝):
    语法切换时对生效链全部模板校验, 语法未变时只对写入的模板字段校验.
    """
    resolver = ConfigResolver(config.hot, repo)
    old_naming = await resolver.resolve(NamingConfig, scope=library, library=library)
    new_naming = await resolver.resolve(NamingConfig, library=library, payload=payload)
    # 写入字段取自请求 payload; 「显式取空」哨兵 {} 与解析期一致地映射为 None.
    written: dict[str, str | None] = {
        field: None if payload[field] == {} else payload[field] for field in NAMING_TEMPLATE_FIELDS if field in payload
    }
    try:
        if new_naming.syntax != old_naming.syntax:
            validate_naming_templates(
                new_naming.syntax,
                {field: getattr(new_naming, field) for field in NAMING_TEMPLATE_FIELDS},
                probe=True,
            )
        else:
            validate_naming_templates(new_naming.syntax, written, probe=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/{library_id}/overrides/{section}")
async def put_library_override(
    library_id: int,
    section: str,
    payload: dict[str, Any],
    repo: RepoDep,
    config: ConfigDep,
) -> LibraryOverrideResponse:
    """整体替换某 section 的覆盖 payload; 校验失败 422 且不落库."""
    library = await _require_library(library_id, repo)
    try:
        validate_override_payload(section, payload)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if section == "naming":
        await _validate_naming_override(library, payload, repo, config)
    row = await repo.upsert_config_override(library_id, section, dict(payload))
    logger.info("library override updated", library_id=library_id, section=section)
    return LibraryOverrideResponse(section=row.section, payload=row.payload)


@router.delete("/{library_id}/overrides/{section}", status_code=204)
async def delete_library_override(library_id: int, section: str, repo: RepoDep) -> Response:
    """清空某 section 的覆盖 (回到继承); 预留节 422, 无覆盖行同样 204, 清空语义幂等."""
    await _require_library(library_id, repo)
    try:
        # DELETE 同为写操作: 白名单校验与 PUT 同判 (规格 §3.1.4).
        section_model(section)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await repo.delete_config_override(library_id, section)
    logger.info("library override deleted", library_id=library_id, section=section)
    return Response(status_code=204)


@router.get("/{library_id}/effective-config")
async def get_effective_config(library_id: int, repo: RepoDep, config: ConfigDep) -> EffectiveConfigResponse:
    """common / naming / nfo 三节的四层合成结果 (全局 → 库行名单内 → 覆盖表; 无任务层)."""
    library = await _require_library(library_id, repo)
    resolver = ConfigResolver(config.hot, repo)
    return EffectiveConfigResponse(
        common=await resolver.resolve(OrganizeCommonConfig, scope=library, library=library),
        naming=await resolver.resolve(NamingConfig, scope=library, library=library),
        nfo=await resolver.resolve(NfoConfig, scope=library, library=library),
    )
