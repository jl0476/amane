"""config overrides table

Revision ID: 9a398bb6ae4d
Revises: fb1679cd3ba9
Create Date: 2026-11-13 03:25:22.294217

库级配置覆盖表 (稀疏 JSON payload). autogenerate 顺带检出的 ix_metadata_number 表达式索引与
tasks/schedules 的 StrEnum 类型差异均为 SQLite 反射噪声 (见 fb1679cd3ba9 自检), 不进本迁移.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a398bb6ae4d"
down_revision: str | None = "fb1679cd3ba9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("section", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("updated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_id", "section", name="uq_config_overrides_scope_section"),
    )


def downgrade() -> None:
    op.drop_table("config_overrides")
