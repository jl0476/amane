"""识别词管理端点的请求与响应模型 (规格 §3.1.6).

值域校验 (类型五值 / pattern 长度与 RE2 编译) 在 repo 写路径, 本层只做结构校验;
作用域类型是 Literal: watch_dir 等预留值在请求构造期即 422.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ...parsing import ContentType, RuleKind


class ParseRuleUpsertRequest(BaseModel):
    """POST 与 PUT 同形 (整体替换); scope_type=global 时 scope_id 恒按 0 落库.

    值与 ``PARSE_RULE_SCOPE_GLOBAL`` / ``PARSE_RULE_SCOPE_LIBRARY`` 一致; Literal 不能引用变量.
    """

    scope_type: Literal["global", "library"]
    scope_id: int = Field(default=0, ge=0)
    kind: RuleKind
    content_type: ContentType
    pattern: str
    enabled: bool = True


class ParseRuleResponse(BaseModel):
    id: int
    scope_type: str
    scope_id: int
    kind: RuleKind
    content_type: ContentType
    pattern: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ParseRuleListResponse(BaseModel):
    items: list[ParseRuleResponse]
