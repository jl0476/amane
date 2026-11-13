"""覆写协议 (规格 3.4.2): 备份 → 落位 (提交点) → 接管与清理, 以及各失败分支的磁盘后果.

源文件必须 ≥ 128 KiB: oshash 对更小的文件返回 None, 协议会在补算后放弃覆写.
"""

import errno
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.enums import MoveMode
from amane.organize import PlaceOutcome
from amane.organize.overwrite import backup_target_path, overwrite_target, overwrite_tmp_path
from amane.utils.oshash import compute_oshash

if TYPE_CHECKING:
    from amane.db.repository import Repository

MIN_HASHABLE_SIZE = 128 * 1024
"""oshash 可计算的最小文件大小 (首尾各 64 KiB)."""

CONTENT_SOURCE = b"A" * 4096 + bytes(range(256)) * 48 + b"tail-a" + b"x" * 4096
CONTENT_TARGET = b"B" * 4096 + bytes(range(255, -1, -1)) * 48 + b"tail-b" + b"y" * 4096
"""等长不同内容: 两者任意一方 *8 都 ≥ 128 KiB 且 oshash 互不等."""


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


@pytest.mark.parametrize("mode", [MoveMode.MOVE, MoveMode.COPY])
@pytest.mark.asyncio
async def test_overwrite_move_backup_replace_and_source_delete(
    repo: Repository, tmp_path: Path, mode: MoveMode
) -> None:
    """完整协议: 备份 → replace → 接管 → 删备份 → 删 journal; move 删源, copy 留源."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB" / "BBB.mp4", CONTENT_TARGET)
    source_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    source_size = source.stat().st_size

    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None
    # 目标上已索引的旧行: 接管后必须删除, 路径让给源行.
    occupant = await repo.create_media_file(1, path=str(target))
    assert occupant.id is not None

    result = await overwrite_target(source=source, target=target, mode=mode, repo=repo, media_file=media, task_id=7)

    assert result.outcome is PlaceOutcome.PLACED
    assert result.dest == target
    assert target.read_bytes() == CONTENT_SOURCE * 8
    assert not backup_target_path(target, "7").exists()
    assert not overwrite_tmp_path(target, "7").exists()
    assert await repo.list_organize_journals() == []

    updated = await repo.get_media_file(media.id)
    assert updated is not None
    assert updated.path == str(target)
    # 现场补算的指纹写回源行, 接管后字段填充复用.
    assert updated.oshash == source_oshash
    assert updated.size == source_size
    assert await repo.get_media_file(occupant.id) is None

    if mode is MoveMode.MOVE:
        assert not source.exists()
    else:
        assert source.read_bytes() == CONTENT_SOURCE * 8


@pytest.mark.asyncio
async def test_pre_commit_failure_restores_backup(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交点之前失败 (copy 写临时文件抛错) → 备份还原, 目标保持旧内容, journal 删除."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    def no_space(src: object, dst: object) -> str:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("amane.organize.overwrite.shutil.copyfile", no_space)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.COPY, repo=repo, media_file=media, task_id=3
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    assert target.read_bytes() == CONTENT_TARGET * 8
    assert not overwrite_tmp_path(target, "3").exists()
    assert not backup_target_path(target, "3").exists()
    assert await repo.list_organize_journals() == []
    assert source.read_bytes() == CONTENT_SOURCE * 8


@pytest.mark.asyncio
async def test_post_commit_journal_failure_keeps_target(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """replace 成功后 journal 推进失败 → 不回滚: 目标保留新内容, journal 仍 prepared, 备份保留."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    async def locked(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "update_organize_journal", locked)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.MOVE, repo=repo, media_file=media, task_id=5
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    # 提交点之后不删目标: 新内容留在目标路径上.
    assert target.read_bytes() == CONTENT_SOURCE * 8
    rows = await repo.list_organize_journals()
    assert [row.phase for row in rows] == ["prepared"]
    assert backup_target_path(target, "5").exists()


@pytest.mark.asyncio
async def test_cross_filesystem_move_uses_tmp_then_replace(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXDEV: 复制到目标目录内临时名再 replace; 源在接管与核验完成后才删除."""
    source = _video(tmp_path / "other-fs" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    real_replace = Path.replace
    rejected = False
    calls: list[tuple[Path, Path]] = []

    def exdev_once(self: Path, dst: Path) -> Path:
        nonlocal rejected
        calls.append((self, dst))
        if self == source and dst == target and not rejected:
            rejected = True
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", exdev_once)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.MOVE, repo=repo, media_file=media, task_id=9
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert rejected
    # 源 → 目标被 EXDEV 拒绝后, 走临时名落位.
    assert (source, target) in calls
    assert (overwrite_tmp_path(target, "9"), target) in calls
    assert target.read_bytes() == CONTENT_SOURCE * 8
    assert not overwrite_tmp_path(target, "9").exists()
    # 跨文件系统 move: 源在核验通过后由 committed 清理分支删除.
    assert not source.exists()
    assert await repo.list_organize_journals() == []


@pytest.mark.asyncio
async def test_concurrent_overwrite_of_same_target_conflicts(repo: Repository, tmp_path: Path) -> None:
    """同一目标已有进行中的覆写 → 第二次请求 CONFLICT: 不双写, 不动对方的备份与 journal."""
    source_a = _video(tmp_path / "lib" / "a" / "AAA.mp4", CONTENT_SOURCE)
    source_b = _video(tmp_path / "lib" / "b" / "CCC.mp4", CONTENT_TARGET)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media_a = await repo.create_media_file(1, path=str(source_a))
    media_b = await repo.create_media_file(1, path=str(source_b))
    assert media_a.id is not None and media_b.id is not None

    # 任务 1 已完成阶段 A: journal 行 + 备份文件都在.
    in_flight_backup = backup_target_path(target, "1")
    in_flight_backup.write_bytes(CONTENT_TARGET * 8)
    await repo.create_organize_journal(
        task_id=1,
        mode=MoveMode.MOVE.value,
        source_path=str(source_a),
        source_file_id=media_a.id,
        source_oshash=_fingerprint(tmp_path, CONTENT_SOURCE),
        source_size=source_a.stat().st_size,
        source_metadata_id=None,
        source_number=None,
        target_path=str(target),
        target_file_id=None,
        backup_path=str(in_flight_backup),
    )

    result = await overwrite_target(
        source=source_b, target=target, mode=MoveMode.COPY, repo=repo, media_file=media_b, task_id=2
    )

    assert result.outcome is PlaceOutcome.CONFLICT
    # 目标与任务 1 的备份都原样保留.
    assert target.read_bytes() == CONTENT_TARGET * 8
    assert in_flight_backup.read_bytes() == CONTENT_TARGET * 8
    assert len(await repo.list_organize_journals()) == 1
    assert source_b.read_bytes() == CONTENT_TARGET * 8


@pytest.mark.asyncio
async def test_unhashable_source_aborts_before_touching_disk(repo: Repository, tmp_path: Path) -> None:
    """源指纹补算后仍为空 (文件过小) → 放弃覆写: 不写 journal, 不动目标."""
    source = tmp_path / "lib" / "incoming" / "AAA.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tiny")
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.MOVE, repo=repo, media_file=media, task_id=4
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    assert target.read_bytes() == CONTENT_TARGET * 8
    assert await repo.list_organize_journals() == []


@pytest.mark.asyncio
async def test_target_verification_failure_stops_takeover(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交点后目标核验不过 (目标被等大小替换) → 不接管不删源, 保留备份与 journal."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    # 提交点之后目标被等大小换走: 核验读到另一个文件的指纹.
    impostor = _video(tmp_path / "impostor.bin", CONTENT_TARGET)

    async def swapped(path: Path) -> str | None:
        return compute_oshash.sync(impostor) if path == target else compute_oshash.sync(path)

    monkeypatch.setattr("amane.organize.overwrite.compute_oshash", swapped)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.MOVE, repo=repo, media_file=media, task_id=6
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    assert "oshash" in result.error
    rows = await repo.list_organize_journals()
    assert len(rows) == 1
    assert rows[0].phase == "placed"
    assert rows[0].last_error is not None and "oshash" in rows[0].last_error
    assert backup_target_path(target, "6").exists()


@pytest.mark.asyncio
async def test_source_verification_failure_keeps_source(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨文件系统 move: 删源前源核验不过 (源路径被另一个文件占用) → 绝不删除该文件."""
    source = _video(tmp_path / "other-fs" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    impostor = _video(tmp_path / "impostor.bin", CONTENT_TARGET)
    real_replace = Path.replace

    def exdev(self: Path, dst: Path) -> Path:
        if self == source and dst == target:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", exdev)

    # 阶段 A 对源补算指纹时仍读真源 (第 1 次); 删源前核验 (第 2 次起) 读到的是占用源路径的新文件.
    source_reads = 0

    async def swapped(path: Path) -> str | None:
        nonlocal source_reads
        if path == source:
            source_reads += 1
            if source_reads > 1:
                return compute_oshash.sync(impostor)
        return compute_oshash.sync(path)

    monkeypatch.setattr("amane.organize.overwrite.compute_oshash", swapped)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.MOVE, repo=repo, media_file=media, task_id=8
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    assert source_reads >= 2
    # 被别人占用的新文件不删除, journal 保留等待人工核查.
    assert source.read_bytes() == CONTENT_SOURCE * 8
    assert len(await repo.list_organize_journals()) == 1


@pytest.mark.asyncio
async def test_takeover_failure_keeps_journal(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接管写库失败 → journal 停在 placed 并写 last_error, 恢复扫描重试接管."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    async def vanished(media_id: int, **updates: object) -> None:
        raise RuntimeError("row vanished")

    monkeypatch.setattr(repo, "update_media_file", vanished)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.COPY, repo=repo, media_file=media, task_id=11
    )

    assert result.outcome is PlaceOutcome.FAILED
    assert result.error is not None
    rows = await repo.list_organize_journals()
    assert len(rows) == 1
    assert rows[0].phase == "placed"
    assert rows[0].last_error is not None
    # 接管未完成: 源保留 (copy), 备份保留.
    assert source.read_bytes() == CONTENT_SOURCE * 8
    assert backup_target_path(target, "11").exists()
