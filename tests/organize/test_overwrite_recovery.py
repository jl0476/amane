"""journal 恢复扫描 (规格 3.4.2 恢复矩阵): prepared / placed / committed 各磁盘状态的处理.

恢复的身份判定是指纹三字段全等 (path, oshash, size, 无 NULL 分支); 目标核验用同一份 journal
指纹; 恢复失败一律保留 journal 行写 last_error.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from amane.config import HotSettings
from amane.db.models import MediaFile, MediaFileStatus
from amane.db.repository import Repository
from amane.enums import MoveMode, OrganizeJournalPhase
from amane.handlers.file import execute_file_operations
from amane.media import ResourceStore
from amane.organize import PlaceOutcome, ResolvedPaths
from amane.organize.overwrite import backup_target_path, recover_organize_journals
from amane.parsing import parse_file_info
from amane.utils.oshash import compute_oshash
from amane.utils.path import nfc_path

MIN_HASHABLE_SIZE = 128 * 1024
CONTENT_SOURCE = b"A" * 4096 + bytes(range(256)) * 48 + b"tail-a" + b"x" * 4096
CONTENT_TARGET = b"B" * 4096 + bytes(range(255, -1, -1)) * 48 + b"tail-b" + b"y" * 4096
"""等长不同内容, *8 后都 ≥ 128 KiB 且 oshash 互不等."""


def _video(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content * 8)
    assert path.stat().st_size >= MIN_HASHABLE_SIZE
    return path


def _fingerprint(tmp_path: Path, content: bytes) -> str:
    probe = _video(tmp_path / "probe.bin", content)
    oshash = compute_oshash.sync(probe)
    probe.unlink()
    assert oshash is not None
    return oshash


async def _media_row(repo: Repository, path: Path, **fields: object) -> MediaFile:
    media = await repo.create_media_file(1, path=str(path), **fields)  # type: ignore[arg-type]
    assert media.id is not None
    return media


async def _journal_row(
    repo: Repository,
    *,
    source: MediaFile,
    source_oshash: str,
    source_size: int,
    target_path: Path,
    backup_path: Path,
    phase: OrganizeJournalPhase,
    mode: MoveMode = MoveMode.MOVE,
    source_file_id: int | None = None,
) -> int:
    assert source.id is not None
    journal = await repo.create_organize_journal(
        task_id=None,
        mode=mode.value,
        source_path=str(source.path),
        source_file_id=source.id if source_file_id is None else source_file_id,
        source_oshash=source_oshash,
        source_size=source_size,
        source_metadata_id=source.metadata_id,
        source_number=source.number,
        target_path=str(target_path),
        target_file_id=None,
        backup_path=str(backup_path),
    )
    assert journal.id is not None
    if phase is not OrganizeJournalPhase.PREPARED:
        await repo.update_organize_journal(journal.id, phase=phase)
    return journal.id


# --- 恢复矩阵 ---

MatrixCase = Callable[[Repository, Path], Awaitable[None]]


async def _case_prepared_target_missing_restores_backup(repo: Repository, tmp_path: Path) -> None:
    """prepared + 目标缺失 + 备份存在 → 回滚: 备份恢复为目标, journal 删除."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = tmp_path / "lib" / "Studio" / "BBB.mp4"
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    media = await _media_row(repo, source)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=_fingerprint(tmp_path, CONTENT_SOURCE),
        source_size=source.stat().st_size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PREPARED,
    )

    await recover_organize_journals(repo)

    assert target.read_bytes() == CONTENT_TARGET * 8
    assert not backup.exists()
    assert await repo.list_organize_journals() == []
    assert media.id is not None
    row = await repo.get_media_file(media.id)
    assert row is not None and row.path == str(source)


async def _case_prepared_target_present_redoes_takeover(repo: Repository, tmp_path: Path) -> None:
    """prepared + 目标存在 + 备份存在 → 按新内容处理: 目标核验后接管, 再清理备份."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)  # replace 已发生
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    size = source.stat().st_size
    # 阶段 A 会把补算指纹写回源行, 恢复的指纹复验以该行为准.
    media = await _media_row(repo, source, number="NSFS-039", oshash=oshash, size=size)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=oshash,
        source_size=size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PREPARED,
    )

    await recover_organize_journals(repo)

    assert target.read_bytes() == CONTENT_SOURCE * 8
    assert not backup.exists()
    assert await repo.list_organize_journals() == []
    assert media.id is not None
    row = await repo.get_media_file(media.id)
    assert row is not None and row.path == str(target)


async def _case_prepared_backup_missing_cleans_journal(repo: Repository, tmp_path: Path) -> None:
    """prepared + 备份缺失 → 视为回滚已完成或未开始: 清理 journal, 目标原样保留."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await _media_row(repo, source)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=_fingerprint(tmp_path, CONTENT_SOURCE),
        source_size=source.stat().st_size,
        target_path=target,
        backup_path=backup_target_path(target, "1"),  # 磁盘上不存在
        phase=OrganizeJournalPhase.PREPARED,
    )

    await recover_organize_journals(repo)

    assert target.read_bytes() == CONTENT_TARGET * 8
    assert await repo.list_organize_journals() == []


async def _case_placed_redoes_takeover(repo: Repository, tmp_path: Path) -> None:
    """placed + 目标核验通过 → 重做索引接管, 随后进清理 (备份删除, journal 删除)."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    size = source.stat().st_size
    media = await _media_row(repo, source, number="NSFS-039", oshash=oshash, size=size)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=oshash,
        source_size=size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PLACED,
    )

    await recover_organize_journals(repo)

    assert media.id is not None
    row = await repo.get_media_file(media.id)
    assert row is not None and row.path == str(target)
    assert not backup.exists()
    assert await repo.list_organize_journals() == []


async def _case_placed_target_missing_restores_backup(repo: Repository, tmp_path: Path) -> None:
    """placed + 目标缺失 → 无法接管: 恢复备份为目标, 删除 journal."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = tmp_path / "lib" / "Studio" / "BBB.mp4"  # 目标被外部删除
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_SOURCE)
    media = await _media_row(repo, source)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=_fingerprint(tmp_path, CONTENT_SOURCE),
        source_size=source.stat().st_size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PLACED,
    )

    await recover_organize_journals(repo)

    assert target.read_bytes() == CONTENT_SOURCE * 8
    assert not backup.exists()
    assert await repo.list_organize_journals() == []


async def _case_committed_cleans_up(repo: Repository, tmp_path: Path) -> None:
    """committed → 清理分支: 目标核验通过后删备份; move 且源仍在 → 核验后删源; journal 删除."""
    source = _video(tmp_path / "other-fs" / "AAA.mp4", CONTENT_SOURCE)  # 跨文件系统 move: 源仍在
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    size = source.stat().st_size
    media = await _media_row(repo, source, number="NSFS-039", oshash=oshash, size=size)
    journal_id = await _journal_row(
        repo,
        source=media,
        source_oshash=oshash,
        source_size=size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.COMMITTED,
    )
    # phase=committed 的前提是接管已提交: 先完成接管 (源行 path 已是目标路径).
    assert media.id is not None
    await repo.takeover_overwrite_target(journal_id, source_file_id=media.id, oshash=oshash, size=size)

    await recover_organize_journals(repo)

    assert not backup.exists()
    assert not source.exists()
    assert target.read_bytes() == CONTENT_SOURCE * 8
    assert await repo.list_organize_journals() == []
    assert media.id is not None
    row = await repo.get_media_file(media.id)
    assert row is not None and row.path == str(target)


MATRIX_CASES: dict[str, MatrixCase] = {
    "prepared_target_missing": _case_prepared_target_missing_restores_backup,
    "prepared_target_present": _case_prepared_target_present_redoes_takeover,
    "prepared_backup_missing": _case_prepared_backup_missing_cleans_journal,
    "placed_takeover": _case_placed_redoes_takeover,
    "placed_target_missing": _case_placed_target_missing_restores_backup,
    "committed_cleanup": _case_committed_cleans_up,
}


@pytest.mark.parametrize("case", sorted(MATRIX_CASES))
@pytest.mark.asyncio
async def test_recovery_matrix(repo: Repository, tmp_path: Path, case: str) -> None:
    await MATRIX_CASES[case](repo, tmp_path)


# --- 指纹复验 ---


@pytest.mark.asyncio
async def test_fingerprint_mismatch_blocks_takeover(repo: Repository, tmp_path: Path) -> None:
    """id 被新文件复用 (指纹不等) → 拒绝按 id 接管; 按 source_path + 指纹兜底; 全失败保留 journal."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    media = await _media_row(repo, source, number="NSFS-039")
    assert media.id is not None
    source_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    source_size = source.stat().st_size
    await _journal_row(
        repo,
        source=media,
        source_oshash=source_oshash,
        source_size=source_size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PLACED,
    )

    # 模拟 id 复用: 删除源行后新登记的行拿到同一 id, 但路径与内容都不同.
    await repo.delete_media_file(media.id)
    reused = await _media_row(repo, tmp_path / "lib" / "reused.mp4", oshash="reusedoshashreus")
    assert reused.id == media.id

    await recover_organize_journals(repo)

    # id 候选指纹不等被拒绝, 按 path 兜底也无线索 → journal 保留, 不接管.
    rows = await repo.list_organize_journals()
    assert len(rows) == 1
    assert rows[0].phase == "placed"
    assert rows[0].last_error is not None
    assert reused.id is not None
    row = await repo.get_media_file(reused.id)
    assert row is not None and row.path != str(target)

    # 兜底: source_path 上出现指纹全等的行 → 接管该行.
    stand_in = await _media_row(repo, source, number="NSFS-039", oshash=source_oshash, size=source_size)
    await recover_organize_journals(repo)

    assert await repo.list_organize_journals() == []
    assert stand_in.id is not None
    row = await repo.get_media_file(stand_in.id)
    assert row is not None and row.path == str(target)


# --- 目标核验拦截 ---


@pytest.mark.asyncio
async def test_target_verification_blocks_cleanup(repo: Repository, tmp_path: Path) -> None:
    """placed: 目标被等大小替换 → 不接管; committed: 目标被删 → 不删源."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    source_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    source_size = source.stat().st_size
    media = await _media_row(repo, source, number="NSFS-039", oshash=source_oshash, size=source_size)
    placed_id = await _journal_row(
        repo,
        source=media,
        source_oshash=source_oshash,
        source_size=source_size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PLACED,
    )

    # 目标在提交点之后被等大小内容替换.
    target.write_bytes(CONTENT_TARGET * 8)
    await recover_organize_journals(repo)

    rows = await repo.list_organize_journals()
    assert len(rows) == 1 and rows[0].id == placed_id
    assert rows[0].phase == "placed"
    assert rows[0].last_error is not None and "oshash" in rows[0].last_error
    assert media.id is not None
    row = await repo.get_media_file(media.id)
    assert row is not None and row.path == str(source)  # 未接管

    # committed + 目标被删: 清理终止, 源与备份绝不删除 (另一目标: UNIQUE(source, target)).
    target2 = _video(tmp_path / "lib" / "Studio" / "CCC.mp4", CONTENT_SOURCE)
    backup2 = backup_target_path(target2, "2")
    _video(backup2, CONTENT_TARGET)
    committed_id = await _journal_row(
        repo,
        source=media,
        source_oshash=source_oshash,
        source_size=source_size,
        target_path=target2,
        backup_path=backup2,
        phase=OrganizeJournalPhase.COMMITTED,
    )
    target2.unlink()
    await recover_organize_journals(repo)

    rows = await repo.list_organize_journals()
    # placed 行按设计保留 (核验失败转人工), committed 行同样保留 (目标被删, 清理终止).
    assert {row.id for row in rows} == {placed_id, committed_id}
    assert all(row.last_error is not None for row in rows)
    assert source.exists()  # 目标核验不通过, 删源被拦截
    assert backup.exists() and backup2.exists()  # 备份同样保留


# --- 后置复验 (接管已完成) ---


@pytest.mark.asyncio
async def test_takeover_already_done_recovery(repo: Repository, tmp_path: Path) -> None:
    """源行 path 已等于 target_path 且指纹全等 → 按已完成接管进清理, 不按身份不匹配处理."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    size = source.stat().st_size
    media = await _media_row(repo, source, number="NSFS-039", oshash=oshash, size=size)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=oshash,
        source_size=size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PLACED,
    )
    # 接管已完成但 journal 未推进 (旧版本数据): 源行 path 已是目标路径.
    await repo.update_media_file(media.id, path=str(target))

    await recover_organize_journals(repo)

    assert not backup.exists()
    assert await repo.list_organize_journals() == []
    assert media.id is not None
    row = await repo.get_media_file(media.id)
    assert row is not None and row.path == str(target)


# --- 跨文件系统删源 ---


@pytest.mark.parametrize("mode", [MoveMode.MOVE, MoveMode.COPY])
@pytest.mark.asyncio
async def test_cross_fs_source_delete_needs_target_valid(repo: Repository, tmp_path: Path, mode: MoveMode) -> None:
    """committed 清理: 目标核验通过才处理源; move 删源, copy 保留源."""
    source = _video(tmp_path / "other-fs" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_SOURCE)
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    size = source.stat().st_size
    media = await _media_row(repo, source, number="NSFS-039", oshash=oshash, size=size)
    journal_id = await _journal_row(
        repo,
        source=media,
        source_oshash=oshash,
        source_size=size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.COMMITTED,
        mode=mode,
    )
    assert media.id is not None
    await repo.takeover_overwrite_target(journal_id, source_file_id=media.id, oshash=oshash, size=size)

    await recover_organize_journals(repo)

    assert await repo.list_organize_journals() == []
    assert not backup.exists()
    if mode is MoveMode.MOVE:
        assert not source.exists()
    else:
        assert source.read_bytes() == CONTENT_SOURCE * 8


# --- 整理同一目标前的就地恢复 ---


@pytest.mark.asyncio
async def test_in_place_recovery_runs_before_organize(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """整理命中未完成覆写的目标时先就地恢复: journal 处理完毕后继续常规占用判定."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = tmp_path / "lib" / "Studio" / "BBB.mp4"  # 目标缺失 + 备份存在: prepared 待回滚
    backup = backup_target_path(target, "1")
    _video(backup, CONTENT_TARGET)
    meta = await repo.upsert_metadata(number="NSFS-039")
    assert meta.id is not None
    media = await _media_row(repo, source, number="NSFS-039", status=MediaFileStatus.SCRAPED, metadata_id=meta.id)
    assert media.id is not None
    await _journal_row(
        repo,
        source=media,
        source_oshash=_fingerprint(tmp_path, CONTENT_SOURCE),
        source_size=source.stat().st_size,
        target_path=target,
        backup_path=backup,
        phase=OrganizeJournalPhase.PREPARED,
    )
    paths = ResolvedPaths(
        video=target,
        thumb=target.parent / "thumb.jpg",
        poster=target.parent / "poster.jpg",
        fanart=target.parent / "fanart.jpg",
        extrafanart_dir=target.parent / "extrafanart",
        nfo=target.parent / "BBB.nfo",
        trailer=target.parent / "trailer.mp4",
    )

    result = await execute_file_operations(
        media_file=media,
        metadata=meta,
        paths=paths,
        move_mode=MoveMode.MOVE,
        resource_store=resource_store,
        file_info=parse_file_info(source),
        web_client=None,
        config=HotSettings(),
        overwrite_video=False,
        repo=repo,
    )

    # 就地恢复已回滚: 目标恢复为旧内容, journal 删除; 随后常规预检判定目标被占 → 冲突.
    assert target.read_bytes() == CONTENT_TARGET * 8
    assert not backup.exists()
    assert await repo.list_organize_journals() == []
    assert result.outcome is PlaceOutcome.CONFLICT
    assert result.conflict_target == target


def test_bootstrap_recovers_before_worker_claims() -> None:
    """启动顺序契约 (联合审核 P1): start_app 内恢复必须先于 worker 认领任务.

    后台并发会把正在落盘的新 journal 误当作中断遗留, 令回滚与正常落盘交错.
    """
    import ast
    import inspect

    from amane.app import bootstrap

    tree = ast.parse(inspect.getsource(bootstrap.start_app))
    recover_line: int | None = None
    worker_start_line: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name == "recover_organize_journals" and recover_line is None:
                recover_line = node.lineno
            if (
                name == "start"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "worker"
                and worker_start_line is None
            ):
                worker_start_line = node.lineno
    assert recover_line is not None, "start_app 未调用启动恢复扫描"
    assert worker_start_line is not None, "start_app 未启动 worker"
    assert recover_line < worker_start_line, "恢复扫描必须先于 worker.start()"


@pytest.mark.asyncio(loop_scope="function")
async def test_recovery_library_root_filter(repo: Repository, tmp_path: Path) -> None:
    """library_root 限定只处理本库目标; 其它库的 journal 行原样保留 (跨库锁外不触碰)."""
    other_dir = _video(tmp_path / "other" / "T.mp4", CONTENT_TARGET)
    own_target = tmp_path / "lib" / "own.mp4"
    own_dir = _video(tmp_path / "lib" / "own-src.mp4", CONTENT_SOURCE)
    await repo.create_media_file(1, path=str(own_dir))
    other_media = await repo.create_media_file(2, path=str(other_dir))
    assert other_media.id is not None
    await repo.create_organize_journal(
        task_id=None,
        mode="move",
        source_path=str(own_dir),
        source_file_id=None,
        source_oshash="0" * 16,
        source_size=own_dir.stat().st_size,
        source_metadata_id=None,
        source_number=None,
        target_path=str(own_target),
        target_file_id=None,
        backup_path=str(own_target.parent / "own.mp4.amane-overwrite-x"),
    )
    await repo.create_organize_journal(
        task_id=None,
        mode="copy",
        source_path=str(other_dir),
        source_file_id=other_media.id,
        source_oshash="1" * 16,
        source_size=other_dir.stat().st_size,
        source_metadata_id=None,
        source_number=None,
        target_path=str(tmp_path / "other" / "T2.mp4"),
        target_file_id=None,
        backup_path=str(tmp_path / "other" / "T2.mp4.amane-overwrite-x"),
    )

    recovered = await recover_organize_journals(repo, library_root=tmp_path / "lib")

    assert recovered == 1
    rows = await repo.list_organize_journals()
    assert [row.target_path for row in rows] == [nfc_path(str(tmp_path / "other" / "T2.mp4"))]
