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
from ..utils.path import nfc_path
from ..utils.threads import in_thread
from .file import PlaceOutcome

if TYPE_CHECKING:
    from ..db.models import MediaFile
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
    oshash = media_file.oshash
    size = media_file.size
    if oshash is None:
        oshash = await compute_oshash(source)
    if size is None:
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

    takeover_error = await _takeover_index(repo, media_file=media_file, target=target)
    if takeover_error is not None:
        await _record_error(repo, plan.journal_id, takeover_error)
        return OverwriteResult(PlaceOutcome.FAILED, target, takeover_error)

    try:
        await repo.update_organize_journal(plan.journal_id, phase=OrganizeJournalPhase.COMMITTED)
    except Exception as e:
        await _record_error_quietly(repo, plan.journal_id, f"committed 推进失败: {e}")
        return OverwriteResult(PlaceOutcome.FAILED, target, f"覆写日志推进 committed 失败: {e}")

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
        error = await _verify_and_delete_source(source, plan)
        if error is not None:
            await _record_error(repo, plan.journal_id, error)
            return OverwriteResult(PlaceOutcome.FAILED, target, error)

    await _delete_journal(repo, plan.journal_id)
    logger.info("overwrite completed", source=str(source), target=target_str, mode=str(mode))
    return OverwriteResult(PlaceOutcome.PLACED, target)


async def _takeover_index(repo: Repository, *, media_file: MediaFile, target: Path) -> str | None:
    """索引接管: 删目标旧行 → 源行 path 改目标路径. journal 推进紧随其后, 由恢复扫描兜底中断."""
    assert media_file.id is not None
    try:
        occupant = await repo.get_media_file_by_path(str(target))
        if occupant is not None and occupant.id is not None and occupant.id != media_file.id:
            await repo.delete_media_file(occupant.id)
        await repo.update_media_file(media_file.id, path=str(target))
        return None
    except Exception as e:
        return f"索引接管失败: {e}"


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


async def _verify_and_delete_source(source: Path, plan: _JournalPlan) -> str | None:
    """mode=move 的源删除: 现场重算指纹全等才删除, 拦下源路径被其它文件占用时误删."""
    if await _file_size(source) is None:
        # 源已不在磁盘 (同文件系统 move 在提交点已消失): 无可删除.
        return None
    error = await _verify_fingerprint(source, plan.source_oshash, plan.source_size, label="源")
    if error is not None:
        return error
    try:
        await _unlink_residual(source)
    except OSError as e:
        return f"源删除失败: {e}"
    return None


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
