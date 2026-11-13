"""organize journal 表

Revision ID: b2637f502a07
Revises: f4a7968d2458
Create Date: 2026-11-13 11:53:00.351608

覆写操作日志 (规格 3.4.2), UNIQUE(source_path, target_path). autogenerate 顺带检出的
ix_metadata_number 表达式索引与 tasks/schedules 的 StrEnum 类型差异均为 SQLite 反射噪声
(见 9a398bb6ae4d 自检), 不进本迁移.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2637f502a07"
down_revision: str | None = "f4a7968d2458"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organize_journal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("mode", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_file_id", sa.Integer(), nullable=True),
        sa.Column("source_oshash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("source_size", sa.Integer(), nullable=True),
        sa.Column("source_metadata_id", sa.Integer(), nullable=True),
        sa.Column("source_number", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("target_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("target_file_id", sa.Integer(), nullable=True),
        sa.Column("backup_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("phase", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("updated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_path", "target_path", name="uq_organize_journal_source_target"),
    )
    op.create_index(op.f("ix_organize_journal_phase"), "organize_journal", ["phase"], unique=False)
    op.create_index(op.f("ix_organize_journal_task_id"), "organize_journal", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_organize_journal_task_id"), table_name="organize_journal")
    op.drop_index(op.f("ix_organize_journal_phase"), table_name="organize_journal")
    op.drop_table("organize_journal")
