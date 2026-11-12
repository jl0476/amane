"""parse rules table

Revision ID: f4a7968d2458
Revises: 679e7b412ab5
Create Date: 2026-11-13 06:29:49.943838

用户自定义识别词表 (规格 §3.1.6). autogenerate 顺带检出的 ix_metadata_number 表达式索引与
tasks/schedules 的 StrEnum 类型差异均为 SQLite 反射噪声 (见 fb1679cd3ba9 自检), 不进本迁移.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

from amane.parsing import ContentType, RuleKind

# revision identifiers, used by Alembic.
revision: str = "f4a7968d2458"
down_revision: str | None = "679e7b412ab5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "parse_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum(*RuleKind.__members__, name="rulekind"), nullable=False),
        sa.Column("content_type", sa.Enum(*ContentType.__members__, name="contenttype"), nullable=False),
        sa.Column("pattern", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("updated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type", "scope_id", "kind", "content_type", "pattern", name="uq_parse_rules_scope_rule"
        ),
    )


def downgrade() -> None:
    op.drop_table("parse_rules")
