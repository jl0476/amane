"""覆写协议 (规格 3.4.2): 阶段 A 备份 → 阶段 B 落位 (``os.replace`` 成功即提交点) → 阶段 C 接管与清理.

分区段的判据是磁盘事实而不是 journal 状态: 提交点之前失败回滚 (备份还原为原目标名,
journal 删除); 提交点之后任何失败都不删除目标 —— MOVE 模式下源内容的副本此时只在目标
路径上, 删目标即删数据. journal 行是磁盘操作无法进入数据库事务的唯一持久凭据, 阶段 A
独立提交, 行删除只在恢复动作全部成功后.

跨文件系统: 落位不使用 ``Path.move`` (其回退不保证原子), 按「目标目录内临时名 → 写完 →
replace」执行; EXDEV 时源保留, 交 committed 清理分支在核验通过后删除.

导入边界: 本模块只能被 organize 包之外导入, 不得被 ``amane.db`` 侧导入 ——
``db.models`` 已导入 organize 包, 反向依赖会成环. 因此 ``Repository`` / ``MediaFile``
只进 ``TYPE_CHECKING``, organize/__init__ 可安全导出本模块.
"""

from __future__ import annotations

import errno
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from ..enums import MoveMode, OrganizeJournalPhase
from ..utils.oshash import compute_oshash
from ..utils.path import is_descendant, nfc_path
from ..utils.threads import in_thread
from .file import PlaceOutcome

if TYPE_CHECKING:
    from ..db.models import MediaFile, OrganizeJournal
    from ..db.repository import Repository

logger = structlog.get_logger()

BACKUP_MARK = ".amane-overwrite-"
TMP_MARK = ".amane-tmp-"
"""备份与临时文件名标记, 后接任务标识; 跨任务不冲突, 便于事后清理."""


def backup_target_path(target: Path, token: str) -> Path:
    """同目录备份名 (同目录 → 同一文件系统 → rename 原子)."""
    return target.parent / f"{target.name}{BACKUP_MARK}{token}"


def overwrite_tmp_path(target: Path, token: str) -> Path:
    """落位临时名, 必须在目标目录内 (同文件系统) 才能原子 replace."""
    return target.parent / f"{target.name}{TMP_MARK}{token}"


def current_task_id() -> int | None:
    """worker 为每个任务绑定的 task_id; 无 worker 上下文 (测试 / 直调) 时为 None."""
    value = structlog.contextvars.get_contextvars().get("task_id")
    return value if isinstance(value, int) else None


@dataclass
class OverwriteResult:
    """覆写协议的磁盘结果. dest 恒为目标路径; FAILED 时 error 带原因."""

    outcome: PlaceOutcome
    dest: Path
    error: str | None = None


@dataclass
class _JournalPlan:
    """阶段 A 落定并写入 journal 的字段集合."""

    journal_id: int
    backup: Path
    tmp: Path
    source_oshash: str
    source_size: int


async def overwrite_target(
    *,
    source: Path,
    target: Path,
    mode: MoveMode,
    repo: Repository,
    media_file: MediaFile,
    task_id: int | None,
) -> OverwriteResult:
    """把 ``source`` 的内容覆写到已被占用的 ``target`` 上, 全程按三阶段协议执行.

    前置检查 (目标存在 / 是视频 / 在库内 / 不同于源) 由分派点完成; 本函数从 journal 写入起执行.
    返回 CONFLICT 表示目标已有进行中的覆写 (journal 在案), 调用方按冲突记账.
    """
    if media_file.id is None:
        return OverwriteResult(PlaceOutcome.FAILED, target, "覆写要求源文件已入库")
    if mode is MoveMode.INPLACE:
        return OverwriteResult(PlaceOutcome.FAILED, target, "原地整理的源即目标, 不会覆写其它文件")

    in_flight = await repo.get_organize_journal_by_target(str(target))
    if in_flight is not None:
        logger.warning("overwrite target already in flight", target=str(target), journal_id=in_flight.id)
        return OverwriteResult(PlaceOutcome.CONFLICT, target)

    plan = await _prepare(source=source, target=target, mode=mode, repo=repo, media_file=media_file, task_id=task_id)
    if isinstance(plan, OverwriteResult):
        return plan

    placed_error = await _place_overwrite_source(source, target, plan.tmp, mode)
    if placed_error is not None:
        # >>> 提交点之前 <<< 回滚: 删临时文件, 备份还原, 删 journal.
        return await _abort_before_commit(repo, plan, target, placed_error)

    return await _takeover_and_cleanup(
        repo=repo, source=source, target=target, mode=mode, media_file=media_file, plan=plan
    )


async def _prepare(
    *,
    source: Path,
    target: Path,
    mode: MoveMode,
    repo: Repository,
    media_file: MediaFile,
    task_id: int | None,
) -> _JournalPlan | OverwriteResult:
    """阶段 A: 现场补算源指纹 → 写 journal (独立提交) → 备份 rename. 任一步失败不触碰目标."""
    source_file_id = media_file.id
    if source_file_id is None:
        return OverwriteResult(PlaceOutcome.FAILED, target, "覆写要求源文件已入库")
    # 指纹一律现场读取, 不使用行缓存 (联合审核 P2): 索引建立后源文件被外部修改时,
    # 缓存值描述的是旧内容 —— MOVE 会移走改过的源, 再因目标与旧指纹不符而失败,
    # 恢复也以同一旧指纹复验而持续失败. 现场值才是本次覆写要搬运并核验的内容.
    oshash = await compute_oshash(source)
    size = await _file_size(source)
    if oshash is None or size is None:
        # 视频文件不该读不出指纹, 不构成有效覆写对象.
        return OverwriteResult(PlaceOutcome.FAILED, target, f"无法读取源文件指纹, 放弃覆写: {source}")

    token = str(task_id) if task_id is not None else uuid4().hex[:12]
    backup = backup_target_path(target, token)
    tmp = overwrite_tmp_path(target, token)
    occupant = await repo.get_media_file_by_path(str(target))
    target_file_id = occupant.id if occupant is not None and occupant.id != source_file_id else None
    try:
        journal = await repo.create_organize_journal(
            task_id=task_id,
            mode=mode.value,
            source_path=nfc_path(str(source)),
            source_file_id=source_file_id,
            source_oshash=oshash,
            source_size=size,
            source_metadata_id=media_file.metadata_id,
            source_number=media_file.number,
            target_path=nfc_path(str(target)),
            target_file_id=target_file_id,
            backup_path=str(backup),
        )
    except Exception as e:
        logger.warning("overwrite journal write failed", target=str(target), error=str(e))
        return OverwriteResult(PlaceOutcome.FAILED, target, f"覆写日志写入失败: {e}")
    assert journal.id is not None
    plan = _JournalPlan(journal_id=journal.id, backup=backup, tmp=tmp, source_oshash=oshash, source_size=size)

    # 补算结果写回源行, 接管后字段填充复用; 失败不阻断 (journal 已有指纹).
    if media_file.oshash != oshash or media_file.size != size:
        with suppress(Exception):
            await repo.update_media_file(source_file_id, oshash=oshash, size=size)

    try:
        await _rename_target_to_backup(target, backup)
    except OSError as e:
        await _delete_journal(repo, plan.journal_id)
        return OverwriteResult(PlaceOutcome.FAILED, target, f"备份目标失败: {e}")
    return plan


@in_thread
def _place_overwrite_source(source: Path, target: Path, tmp: Path, mode: MoveMode) -> str | None:
    """阶段 B: 把源放到目标位置; 返回错误说明, None 即成功 (提交点已过)."""
    try:
        match mode:
            case MoveMode.MOVE:
                try:
                    source.replace(target)
                    return None
                except OSError as e:
                    if e.errno != errno.EXDEV:
                        raise
                    # 跨文件系统: 复制到目标目录内临时名再 replace; 源保留, 交 committed 分支删源.
                    _copy_content(source, tmp)
                    tmp.replace(target)
            case MoveMode.COPY:
                _copy_content(source, tmp)
                tmp.replace(target)
            case MoveMode.HARDLINK:
                tmp.hardlink_to(source)
                tmp.replace(target)
            case MoveMode.SYMLINK:
                tmp.symlink_to(source)
                tmp.replace(target)
            case MoveMode.INPLACE:
                return "原地整理的源即目标, 不会覆写其它文件"
        return None
    except OSError as e:
        return f"覆写落位失败: {e}"


def _copy_content(source: Path, tmp: Path) -> None:
    """内容复制; 元数据复制失败不算失败 (网盘挂载常拒绝 utime / chmod, 内容已完整)."""
    shutil.copyfile(source, tmp)
    try:
        shutil.copystat(source, tmp)
    except OSError as exc:
        logger.debug("overwrite copy metadata skipped", src=str(source), dst=str(tmp), error=str(exc))


async def _abort_before_commit(repo: Repository, plan: _JournalPlan, target: Path, reason: str) -> OverwriteResult:
    """提交点之前的回滚: 删临时文件, 备份还原为原目标名, 删 journal 行."""
    rollback_error = await _restore_backup(plan.tmp, plan.backup, target)
    if rollback_error is not None:
        # 回滚自身失败: 保留 journal (含 last_error), 备份残留交恢复扫描.
        logger.error("overwrite rollback failed", target=str(target), error=rollback_error)
        await _record_error(repo, plan.journal_id, f"回滚失败: {rollback_error}; 原因: {reason}")
        return OverwriteResult(PlaceOutcome.FAILED, target, f"{reason}; 回滚失败: {rollback_error}")
    await _delete_journal(repo, plan.journal_id)
    return OverwriteResult(PlaceOutcome.FAILED, target, reason)


async def _takeover_and_cleanup(
    *,
    repo: Repository,
    source: Path,
    target: Path,
    mode: MoveMode,
    media_file: MediaFile,
    plan: _JournalPlan,
) -> OverwriteResult:
    """阶段 C: 提交点之后, 任何失败都不回滚、不删除目标, 只保留 journal 交恢复扫描."""
    target_str = str(target)
    try:
        await repo.update_organize_journal(plan.journal_id, phase=OrganizeJournalPhase.PLACED)
    except Exception as e:
        # journal 更新失败不得删除目标; 保留 prepared 记录, 恢复扫描按 prepared 分支重做接管.
        logger.warning("overwrite journal advance failed", target=target_str, error=str(e))
        return OverwriteResult(
            PlaceOutcome.FAILED, target, f"覆写已落位但操作日志推进失败, 目标保留新内容, 等待恢复扫描: {e}"
        )

    error = await _verify_fingerprint(target, plan.source_oshash, plan.source_size, label="目标")
    if error is not None:
        await _record_error(repo, plan.journal_id, error)
        return OverwriteResult(PlaceOutcome.FAILED, target, error)

    try:
        assert media_file.id is not None
        # 接管 + journal committed 是同一个数据库事务, 接管成功即已 committed.
        await repo.takeover_overwrite_target(
            plan.journal_id,
            source_file_id=media_file.id,
            oshash=plan.source_oshash,
            size=plan.source_size,
        )
    except Exception as e:
        takeover_error = f"索引接管失败: {e}"
        await _record_error(repo, plan.journal_id, takeover_error)
        return OverwriteResult(PlaceOutcome.FAILED, target, takeover_error)

    try:
        await _unlink_residual(plan.backup)
    except OSError as e:
        error = f"备份删除失败, 恢复扫描下次清理: {e}"
        await _record_error(repo, plan.journal_id, error)
        return OverwriteResult(PlaceOutcome.FAILED, target, error)

    # 删源前再核验一次目标, 覆盖接管与删备份之间目标被外部改写的窗口.
    error = await _verify_fingerprint(target, plan.source_oshash, plan.source_size, label="目标")
    if error is not None:
        await _record_error(repo, plan.journal_id, error)
        return OverwriteResult(PlaceOutcome.FAILED, target, error)

    if mode is MoveMode.MOVE:
        error = await _verify_and_delete_source(source, plan.source_oshash, plan.source_size)
        if error is not None:
            await _record_error(repo, plan.journal_id, error)
            return OverwriteResult(PlaceOutcome.FAILED, target, error)

    await _delete_journal(repo, plan.journal_id)
    logger.info("overwrite completed", source=str(source), target=target_str, mode=str(mode))
    return OverwriteResult(PlaceOutcome.PLACED, target)


async def _verify_fingerprint(path: Path, expected_oshash: str, expected_size: int, *, label: str) -> str | None:
    """对磁盘文件现场重算 oshash + size, 与 journal 指纹全等才算有效."""
    live_oshash = await compute_oshash(path)
    live_size = await _file_size(path)
    if live_oshash is None or live_size is None:
        return f"{label}核验失败: 文件不可读取 ({path})"
    if live_oshash != expected_oshash or live_size != expected_size:
        return (
            f"{label}核验失败: 现场 oshash={live_oshash} size={live_size}, "
            f"期望 oshash={expected_oshash} size={expected_size}"
        )
    return None


async def _verify_and_delete_source(source: Path, expected_oshash: str, expected_size: int) -> str | None:
    """mode=move 的源删除: 现场重算指纹全等才删除, 拦下源路径被其它文件占用时误删."""
    if not await _is_file(source):
        # 源已不在磁盘 (同文件系统 move 在提交点已消失): 无可删除.
        return None
    error = await _verify_fingerprint(source, expected_oshash, expected_size, label="源")
    if error is not None:
        return error
    try:
        await _unlink_residual(source)
    except OSError as e:
        return f"源删除失败: {e}"
    return None


@in_thread
def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


@in_thread
def _rename_target_to_backup(target: Path, backup: Path) -> None:
    target.rename(backup)


@in_thread
def _restore_backup(tmp: Path, backup: Path, target: Path) -> str | None:
    with suppress(FileNotFoundError):
        tmp.unlink()
    if backup.exists(follow_symlinks=False):
        try:
            backup.rename(target)
        except OSError as e:
            return str(e)
    return None


@in_thread
def _unlink_residual(path: Path) -> None:
    path.unlink(missing_ok=True)


@in_thread
def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


async def _delete_journal(repo: Repository, journal_id: int) -> None:
    try:
        await repo.delete_organize_journal(journal_id)
    except Exception as e:
        logger.warning("journal delete failed", journal_id=journal_id, error=str(e))


async def _record_error(repo: Repository, journal_id: int, error: str) -> None:
    logger.warning("overwrite step failed", journal_id=journal_id, error=error)
    await _record_error_quietly(repo, journal_id, error)


async def _record_error_quietly(repo: Repository, journal_id: int, error: str) -> None:
    with suppress(Exception):
        await repo.update_organize_journal(journal_id, last_error=error)


# --- 中断恢复 (规格 3.4.2 恢复矩阵) ---


async def recover_organize_journals(
    repo: Repository, *, target_path: str | None = None, library_root: Path | None = None
) -> int:
    """恢复扫描入口: 逐行按恢复矩阵处理未完成的覆写, 返回处理的行数.

    三处调用: 应用启动时的全量扫描 (bootstrap 在 worker 认领前顺序 await), 以及整理同一目标
    前的就地恢复 (``target_path`` 限定该目标), 整理任务清理缺失索引前 (``library_root``
    限定本库, 避免触碰其它库任务正在写入的 journal 行). 每行独立处理, 单行失败保留
    journal 行写 ``last_error``, 不影响其它行; 各步幂等, 下次扫描重试.
    """
    rows = await repo.list_organize_journals()
    if target_path is not None:
        wanted = nfc_path(target_path)
        rows = [row for row in rows if row.target_path == wanted]
    elif library_root is not None:
        root = nfc_path(str(library_root))
        rows = [row for row in rows if is_descendant(row.target_path, root)]
    for row in rows:
        try:
            await _recover_one(repo, row)
        except Exception as e:
            logger.warning("journal recovery failed", journal_id=row.id, target=row.target_path, error=str(e))
            await _record_error(repo, row.id if row.id is not None else 0, f"恢复失败: {e}")
    return len(rows)


async def _recover_one(repo: Repository, journal: OrganizeJournal) -> None:
    journal_id = journal.id
    assert journal_id is not None
    target = Path(journal.target_path)
    backup = Path(journal.backup_path)
    mode = MoveMode(journal.mode)
    oshash = journal.source_oshash
    size = journal.source_size
    if oshash is None or size is None:
        # 没有指纹无法核验任何磁盘事实, 保留记录交人工处理.
        await _record_error(repo, journal_id, "恢复失败: journal 缺少源指纹, 无法复验")
        return
    phase = OrganizeJournalPhase(journal.phase)
    if phase is OrganizeJournalPhase.PREPARED:
        await _recover_prepared(repo, journal, target=target, backup=backup, mode=mode, oshash=oshash, size=size)
    elif phase is OrganizeJournalPhase.PLACED:
        await _recover_placed(repo, journal, target=target, backup=backup, mode=mode, oshash=oshash, size=size)
    else:
        await _recover_cleanup(
            repo,
            journal_id,
            source=Path(journal.source_path),
            target=target,
            backup=backup,
            mode=mode,
            oshash=oshash,
            size=size,
        )


async def _recover_prepared(
    repo: Repository,
    journal: OrganizeJournal,
    *,
    target: Path,
    backup: Path,
    mode: MoveMode,
    oshash: str,
    size: int,
) -> None:
    journal_id = journal.id
    assert journal_id is not None
    target_present = await _is_file(target)
    backup_present = await _is_file(backup)
    if not target_present and backup_present:
        # 回滚: 备份恢复为目标, 清理临时文件, 删 journal.
        try:
            await _rename_target_to_backup(backup, target)
        except OSError as e:
            await _record_error(repo, journal_id, f"恢复回滚失败: {e}")
            return
        logger.info("journal recovery rollback restored", journal_id=journal_id, backup=str(backup), target=str(target))
        await _cleanup_tmp(target, backup)
        await _delete_journal(repo, journal_id)
        return
    if target_present and backup_present:
        # 目标与备份并存, 磁盘无法区分新旧: 按新内容处理, 目标核验通过后接管.
        error = await _verify_fingerprint(target, oshash, size, label="目标")
        if error is not None:
            await _record_error(repo, journal_id, error)
            return
        await _recover_takeover(repo, journal, target=target, backup=backup, mode=mode, oshash=oshash, size=size)
        return
    # 备份缺失: 视为回滚已完成或未开始, 清理临时文件与 journal 行.
    logger.info("journal recovery cleared", journal_id=journal_id, target=str(target))
    await _cleanup_tmp(target, backup)
    await _delete_journal(repo, journal_id)


async def _recover_placed(
    repo: Repository,
    journal: OrganizeJournal,
    *,
    target: Path,
    backup: Path,
    mode: MoveMode,
    oshash: str,
    size: int,
) -> None:
    journal_id = journal.id
    assert journal_id is not None
    if not await _is_file(target):
        # 目标被外部删除, 无法接管: 恢复备份为目标, 删除 journal.
        if await _is_file(backup):
            try:
                await _rename_target_to_backup(backup, target)
            except OSError as e:
                await _record_error(repo, journal_id, f"恢复备份失败: {e}")
                return
            logger.info(
                "journal recovery rollback restored", journal_id=journal_id, backup=str(backup), target=str(target)
            )
        else:
            # 目标与备份双缺: mode=move 的源可能已在提交点移走, 内容去向无从追溯, journal 是唯一痕迹.
            logger.warning(
                "journal recovery target and backup both missing",
                journal_id=journal_id,
                source=str(journal.source_path),
                target=str(target),
                oshash=oshash,
                size=size,
            )
        await _delete_journal(repo, journal_id)
        return
    error = await _verify_fingerprint(target, oshash, size, label="目标")
    if error is not None:
        # 目标内容已与覆写时不一致: 禁止接管与删源, 交人工处理.
        await _record_error(repo, journal_id, error)
        return
    await _recover_takeover(repo, journal, target=target, backup=backup, mode=mode, oshash=oshash, size=size)


async def _recover_takeover(
    repo: Repository,
    journal: OrganizeJournal,
    *,
    target: Path,
    backup: Path,
    mode: MoveMode,
    oshash: str,
    size: int,
) -> None:
    """指纹复验找源行并重做接管; 已完成接管的行 (后置复验) 直接进清理分支."""
    journal_id = journal.id
    assert journal_id is not None
    row, done, mismatch = await _resolve_source_row(repo, journal)
    if row is None or row.id is None:
        await _record_error(repo, journal_id, f"恢复失败: 指纹复验全部失败 ({mismatch})")
        return
    if not done:
        try:
            await repo.takeover_overwrite_target(journal_id, source_file_id=row.id, oshash=oshash, size=size)
        except Exception as e:
            await _record_error(repo, journal_id, f"恢复接管失败: {e}")
            return
        logger.info(
            "journal recovery takeover redone",
            journal_id=journal_id,
            source_row=row.id,
            target=str(target),
        )
    await _recover_cleanup(
        repo,
        journal_id,
        source=Path(journal.source_path),
        target=target,
        backup=backup,
        mode=mode,
        oshash=oshash,
        size=size,
    )


async def _resolve_source_row(repo: Repository, journal: OrganizeJournal) -> tuple[MediaFile | None, bool, str]:
    """按 id → path 次序找指纹全等的源行; 返回 (行, 是否已完成接管, 复验明细).

    指纹 = (path, oshash, size), 三项全等才命中, 没有 NULL 跳过分支 (journal 指纹恒非空,
    缺失在 _recover_one 已拦截). 后置复验: 候选行指纹全等且 path 已等于 target_path,
    视为接管已完成, 交清理分支的目标核验. 复验全部失败返回 (None, False, 明细), journal 保留.
    """
    source_path = nfc_path(journal.source_path)
    target_path = nfc_path(journal.target_path)
    expected_oshash = journal.source_oshash
    expected_size = journal.source_size
    assert expected_oshash is not None and expected_size is not None
    mismatches: list[str] = []
    candidates: list[tuple[str, MediaFile | None]] = [
        (
            f"id={journal.source_file_id}",
            await repo.get_media_file(journal.source_file_id) if journal.source_file_id is not None else None,
        ),
        (f"path={journal.source_path}", await repo.get_media_file_by_path(source_path)),
    ]
    for label, candidate in candidates:
        if candidate is None or candidate.id is None:
            mismatches.append(f"{label}: 行不存在")
            continue
        if candidate.oshash != expected_oshash or candidate.size != expected_size:
            mismatches.append(f"{label}: oshash={candidate.oshash!r} size={candidate.size!r} 与期望不等")
            continue
        if candidate.path == target_path:
            return candidate, True, ""
        if candidate.path == source_path:
            return candidate, False, ""
        mismatches.append(f"{label}: path={candidate.path!r} 既非源路径也非目标路径")
    detail = "; ".join(mismatches)
    logger.warning("journal fingerprint mismatch", journal_id=journal.id, detail=detail)
    return None, False, detail


async def _recover_cleanup(
    repo: Repository,
    journal_id: int,
    *,
    source: Path,
    target: Path,
    backup: Path,
    mode: MoveMode,
    oshash: str,
    size: int,
) -> None:
    """committed 的清理分支: 目标核验 → 删备份 → 再核验目标 → (move) 核验并删源 → 删 journal."""
    error = await _verify_fingerprint(target, oshash, size, label="目标")
    if error is not None:
        # 目标缺失 / 不等 / 不可读: 源与备份可能就是最后的有效副本, 一律保留.
        await _record_error(repo, journal_id, error)
        return
    if await _is_file(backup):
        try:
            await _unlink_residual(backup)
        except OSError as e:
            await _record_error(repo, journal_id, f"备份删除失败: {e}")
            return
    await _cleanup_tmp(target, backup)
    # 删源前再次核验目标, 覆盖上一步前后目标被外部改写的窗口.
    error = await _verify_fingerprint(target, oshash, size, label="目标")
    if error is not None:
        await _record_error(repo, journal_id, error)
        return
    if mode is MoveMode.MOVE:
        error = await _verify_and_delete_source(source, oshash, size)
        if error is not None:
            await _record_error(repo, journal_id, error)
            return
    await _delete_journal(repo, journal_id)
    logger.info(
        "journal recovery cleanup done",
        journal_id=journal_id,
        source=str(source),
        target=str(target),
        mode=str(mode),
    )


def _tmp_from_backup(target: Path, backup: Path) -> Path | None:
    prefix = f"{target.name}{BACKUP_MARK}"
    if not backup.name.startswith(prefix):
        return None
    return overwrite_tmp_path(target, backup.name[len(prefix) :])


async def _cleanup_tmp(target: Path, backup: Path) -> None:
    """清理残留的 .amane-tmp-<任务id> 临时文件; 名字对不上时无从推算, 忽略."""
    tmp = _tmp_from_backup(target, backup)
    if tmp is not None:
        with suppress(OSError):
            await _unlink_residual(tmp)
