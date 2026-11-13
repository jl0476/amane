"""media file link_path

Revision ID: fedccd866f47
Revises: b2637f502a07
Create Date: 2026-11-13 12:27:11.482650

链接落盘位置列 (规格 3.4.4): 可空, 无数据回填. autogenerate 顺带检出的 ix_metadata_number
表达式索引与 tasks/schedules 的 StrEnum 类型差异均为 SQLite 反射噪声 (见 9a398bb6ae4d 自检),
不进本迁移.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fedccd866f47"
down_revision: str | None = "b2637f502a07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_files") as batch:
        batch.add_column(sa.Column("link_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_files") as batch:
        batch.drop_column("link_path")
