"""识别词管理端点 (规格 §3.1.6): CRUD + RE2 校验 422 + 重复键 409; 写路径后失效 ParseRuleSet 缓存."""

import structlog
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.exc import IntegrityError

from ...utils.model import to_resp
from ..deps import RepoDep, RuntimeDep
from ..models import ParseRuleListResponse, ParseRuleResponse, ParseRuleUpsertRequest

logger = structlog.get_logger()

router = APIRouter(prefix="/parse-rules", tags=["parse-rules"])

_DUPLICATE_DETAIL = "相同作用域、关键词与 pattern 的识别词已存在"


@router.get("")
async def list_parse_rules(repo: RepoDep) -> ParseRuleListResponse:
    return ParseRuleListResponse(items=[to_resp(ParseRuleResponse, row) for row in await repo.list_parse_rules()])


@router.post("", status_code=201)
async def create_parse_rule(req: ParseRuleUpsertRequest, repo: RepoDep, runtime: RuntimeDep) -> ParseRuleResponse:
    try:
        row = await repo.create_parse_rule(
            scope_type=req.scope_type,
            scope_id=req.scope_id,
            kind=req.kind,
            content_type=req.content_type,
            pattern=req.pattern,
            enabled=req.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail=_DUPLICATE_DETAIL) from exc
    runtime.invalidate_parse_rules()
    logger.info("parse rule created", rule_id=row.id, scope_type=req.scope_type, scope_id=row.scope_id)
    return to_resp(ParseRuleResponse, row)


@router.put("/{rule_id}")
async def update_parse_rule(
    rule_id: int, req: ParseRuleUpsertRequest, repo: RepoDep, runtime: RuntimeDep
) -> ParseRuleResponse:
    """整体替换; 重复键 (含 PUT 撞其它既有行) 409."""
    try:
        row = await repo.update_parse_rule(
            rule_id,
            scope_type=req.scope_type,
            scope_id=req.scope_id,
            kind=req.kind,
            content_type=req.content_type,
            pattern=req.pattern,
            enabled=req.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail=_DUPLICATE_DETAIL) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="识别词不存在")
    runtime.invalidate_parse_rules()
    logger.info("parse rule updated", rule_id=row.id)
    return to_resp(ParseRuleResponse, row)


@router.delete("/{rule_id}", status_code=204)
async def delete_parse_rule(rule_id: int, repo: RepoDep, runtime: RuntimeDep) -> Response:
    if not await repo.delete_parse_rule(rule_id):
        raise HTTPException(status_code=404, detail="识别词不存在")
    runtime.invalidate_parse_rules()
    logger.info("parse rule deleted", rule_id=rule_id)
    return Response(status_code=204)
