from sqlmodel import col, select

from ...utils.path import nfc_path
from ..models import OrganizeJournal, OrganizeJournalPhase
from ..repo_types import _utcnow
from .base import RepositoryMixinBase


class OrganizeJournalRepoMixin(RepositoryMixinBase):
    """organize_journal 的最小写面: 覆写协议的 journal 读写与恢复扫描的行枚举.

    journal 行的相位推进必须独立提交 (OS 操作无法加入数据库事务), 每个方法各自提交;
    与索引接管的同事务提交见 `takeover_overwrite_target`.
    """

    async def create_organize_journal(
        self,
        *,
        task_id: int | None,
        mode: str,
        source_path: str,
        source_file_id: int,
        source_oshash: str,
        source_size: int,
        source_metadata_id: int | None,
        source_number: str | None,
        target_path: str,
        target_file_id: int | None,
        backup_path: str,
    ) -> OrganizeJournal:
        """写入 phase=prepared 的覆写日志行, 一次提交 (规格 3.4.2 阶段 A)."""
        async with self._session() as session:
            journal = OrganizeJournal(
                task_id=task_id,
                mode=mode,
                source_path=nfc_path(source_path),
                source_file_id=source_file_id,
                source_oshash=source_oshash,
                source_size=source_size,
                source_metadata_id=source_metadata_id,
                source_number=source_number,
                target_path=nfc_path(target_path),
                target_file_id=target_file_id,
                backup_path=backup_path,
                phase=OrganizeJournalPhase.PREPARED.value,
            )
            session.add(journal)
            await session.commit()
            await session.refresh(journal)
            return journal

    async def get_organize_journal(self, journal_id: int) -> OrganizeJournal | None:
        async with self._session() as session:
            return await session.get(OrganizeJournal, journal_id)

    async def get_organize_journal_by_target(self, target_path: str) -> OrganizeJournal | None:
        """同目标的进行中覆写至多一行; 分派点用它拦截并发覆写."""
        async with self._session() as session:
            stmt = select(OrganizeJournal).where(OrganizeJournal.target_path == nfc_path(target_path))
            result = await session.exec(stmt)
            return result.first()

    async def get_organize_journal_by_source_and_target(
        self, source_path: str, target_path: str
    ) -> OrganizeJournal | None:
        """UNIQUE(source_path, target_path) 的查询面: 整理同一目标前的就地恢复入口."""
        async with self._session() as session:
            stmt = select(OrganizeJournal).where(
                OrganizeJournal.source_path == nfc_path(source_path),
                OrganizeJournal.target_path == nfc_path(target_path),
            )
            result = await session.exec(stmt)
            return result.first()

    async def list_organize_journals(self) -> list[OrganizeJournal]:
        """恢复扫描的行枚举, id 升序."""
        async with self._session() as session:
            stmt = select(OrganizeJournal).order_by(col(OrganizeJournal.id).asc())
            result = await session.exec(stmt)
            return list(result.all())

    async def update_organize_journal(
        self,
        journal_id: int,
        *,
        phase: OrganizeJournalPhase | None = None,
        last_error: str | None = None,
    ) -> OrganizeJournal | None:
        """phase 与 last_error 只在显式传入时改写; None 表示保持原值."""
        async with self._session() as session:
            journal = await session.get(OrganizeJournal, journal_id)
            if journal is None:
                return None
            if phase is not None:
                journal.phase = phase.value
            if last_error is not None:
                journal.last_error = last_error
            journal.updated_at = _utcnow()
            session.add(journal)
            await session.commit()
            await session.refresh(journal)
            return journal

    async def delete_organize_journal(self, journal_id: int) -> bool:
        async with self._session() as session:
            journal = await session.get(OrganizeJournal, journal_id)
            if journal is None:
                return False
            await session.delete(journal)
            await session.commit()
            return True
