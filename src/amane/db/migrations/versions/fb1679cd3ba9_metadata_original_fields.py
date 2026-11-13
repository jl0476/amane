"""metadata original fields

Revision ID: fb1679cd3ba9
Revises: fb4fd486708c
Create Date: 2026-11-13 00:35:25.801180
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fb1679cd3ba9"
down_revision: str | None = "fb4fd486708c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _recreate_number_index() -> None:
    """batch_alter_table 重建表时无法反射表达式索引, 会把 ix_metadata_number 丢成 BINARY 序;
    这里按 ``db/models.py::Metadata.__table_args__`` 的原 DDL 重建, 保证查号仍走 SEARCH."""
    op.drop_index("ix_metadata_number", table_name="metadata")
    op.create_index("ix_metadata_number", "metadata", [sa.literal_column("number COLLATE NOCASE")], unique=True)


def upgrade() -> None:
    # 自检剔除两处反射噪声: ix_metadata_number 的表达式索引与 tasks/schedules 的 StrEnum 类型差异
    # 均为 SQLite 反射极限, 非模型真实变化, 不进本迁移.
    with op.batch_alter_table("metadata") as batch_op:
        batch_op.add_column(sa.Column("original_title", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column("original_plot", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column("field_languages", sa.JSON(), server_default=sa.text("'{}'"), nullable=True))
        batch_op.add_column(sa.Column("website", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    _recreate_number_index()


def downgrade() -> None:
    with op.batch_alter_table("metadata") as batch_op:
        batch_op.drop_column("website")
        batch_op.drop_column("field_languages")
        batch_op.drop_column("original_plot")
        batch_op.drop_column("original_title")
    _recreate_number_index()
