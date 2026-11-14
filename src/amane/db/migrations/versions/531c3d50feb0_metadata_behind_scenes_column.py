"""metadata behind_scenes column

Revision ID: 531c3d50feb0
Revises: fedccd866f47
Create Date: 2026-11-15 03:32:14.086712
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "531c3d50feb0"
down_revision: str | None = "fedccd866f47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 自检剔除 autogenerate 反射噪声: ix_metadata_number 的表达式索引差异与 tasks/schedules 的
    # StrEnum 类型差异均为 SQLite 反射极限, 非模型真实变化, 不进本迁移.
    # ADD COLUMN 不重建表, 表达式索引保持原定义 (先例 e4f7dd7d4711).
    op.add_column("metadata", sa.Column("behind_scenes", sa.JSON(), server_default=sa.text("'{}'"), nullable=True))


def downgrade() -> None:
    op.drop_column("metadata", "behind_scenes")
