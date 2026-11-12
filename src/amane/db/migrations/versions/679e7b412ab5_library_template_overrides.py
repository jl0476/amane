"""library template overrides

Revision ID: 679e7b412ab5
Revises: 9a398bb6ae4d
Create Date: 2026-11-13 03:43:05.034872

既有库的全部 15 个模板类字段写入名单 (规格 §3.1.5): 库行值 (含 None) 就是迁移时刻的生效输入,
全量保留保证落盘结果不变. autogenerate 顺带检出的 ix_metadata_number 表达式索引与
tasks/schedules 的 StrEnum 类型差异均为 SQLite 反射噪声 (见 fb1679cd3ba9 自检), 不进本迁移.
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "679e7b412ab5"
down_revision: str | None = "9a398bb6ae4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 src/amane/db/models.py::TEMPLATE_OVERRIDE_FIELDS 同源的快照 (迁移须自包含).
_PIN_FIELDS: tuple[str, ...] = (
    "video_template",
    "link_template",
    "link_mode",
    "strm_content_template",
    "thumb_template",
    "poster_template",
    "fanart_template",
    "extrafanart_template",
    "extrafanart_name_template",
    "nfo_template",
    "trailer_template",
    "subtitle_template",
    "subtitle_extensions",
    "write_nfo",
    "copy_resources",
)


def _pin_existing_libraries(conn: sa.engine.Connection) -> None:
    """逐行把名单与全部 15 个字段名取并集后写回; 重跑结果一致, 已有自有条目保留."""
    rows = conn.execute(sa.text("SELECT id, template_overrides FROM libraries")).fetchall()
    for row in rows:
        lib_id, raw = row[0], row[1]
        current = _as_name_list(raw)
        merged = list(dict.fromkeys([*_PIN_FIELDS, *current]))
        if merged == current:
            continue
        conn.execute(
            sa.text("UPDATE libraries SET template_overrides = :names WHERE id = :id"),
            {"names": json.dumps(merged), "id": lib_id},
        )


def _as_name_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = json.loads(raw) if raw else []
    if not isinstance(raw, list):
        return []
    return [name for name in raw if isinstance(name, str)]


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("template_overrides", sa.JSON(), nullable=False, server_default="[]"),
    )
    _pin_existing_libraries(op.get_bind())


def downgrade() -> None:
    op.drop_column("libraries", "template_overrides")
