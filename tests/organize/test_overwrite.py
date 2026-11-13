"""覆写协议 (规格 3.4.2): 备份 → 落位 (提交点) → 接管与清理, 以及各失败分支的磁盘后果.

源文件必须 ≥ 128 KiB: oshash 对更小的文件返回 None, 协议会在补算后放弃覆写.
"""

import errno
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.db.models import MediaFile, MediaFileStatus
from amane.enums import MoveMode, OrganizeJournalPhase
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
    """接管事务失败 → journal 停在 placed 并写 last_error, 恢复扫描重试接管."""
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    media = await repo.create_media_file(1, path=str(source))
    assert media.id is not None

    async def failed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("row vanished")

    monkeypatch.setattr(repo, "takeover_overwrite_target", failed)

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


# --- 任务 25: 覆写索引接管 (单事务) ---


async def _journal_for(
    repo: Repository,
    *,
    source: MediaFile,
    target_path: str,
    oshash: str,
    size: int,
    target_file_id: int | None,
    source_metadata_id: int | None = None,
    source_number: str | None = None,
    phase: str = "prepared",
) -> int:
    """身份参数缺省取源行现值; 可显式传入与现值不同的归属 (恢复时复验出的另一行)."""
    assert source.id is not None
    journal = await repo.create_organize_journal(
        task_id=None,
        mode=MoveMode.MOVE.value,
        source_path=str(source.path),
        source_file_id=source.id,
        source_oshash=oshash,
        source_size=size,
        source_metadata_id=source_metadata_id if source_metadata_id is not None else source.metadata_id,
        source_number=source_number if source_number is not None else source.number,
        target_path=target_path,
        target_file_id=target_file_id,
        backup_path=f"{target_path}.amane-overwrite-1",
    )
    assert journal.id is not None
    if phase != "prepared":
        await repo.update_organize_journal(journal.id, phase=OrganizeJournalPhase(phase))
    return journal.id


@pytest.mark.asyncio
async def test_takeover_overwrites_identity_fields(repo: Repository, tmp_path: Path) -> None:
    """接管按覆写表更新: metadata_id / number 覆盖而非补空, oshash / size 取核验值,
    duration / codec 置空待刷新, 相位字段按新路径派生, journal 同事务推进 committed.

    源行建模恢复时复验出的另一行: 绑定与 journal 记录的归属不同,
    补空或不赋值都修不回来, 只有覆盖语义能通过.
    """
    src = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB-C.mp4", CONTENT_TARGET)
    meta_old = await repo.upsert_metadata(number="OLD-001")
    meta_new = await repo.upsert_metadata(number="NSFS-039")
    assert meta_old.id is not None and meta_new.id is not None
    # 目标旧行: 已绑定别的条目且刮削字段齐全, 接管后整行删除.
    occupant = await repo.create_media_file(
        1,
        path=str(target),
        number="OLD-001",
        status=MediaFileStatus.SCRAPED,
        metadata_id=meta_old.id,
        oshash="oldoshasholdoshas",
        size=1,
        duration=12.5,
        codec="h264",
    )
    assert occupant.id is not None
    # 源行: 绑定停在旧条目, 刮削字段齐全 (路径无中字标记, 相位须按 -C 目标路径重派生);
    # journal 记录接管后应有的归属, 行现值 ≠ journal 值, 覆盖语义才可观察.
    source = await repo.create_media_file(
        1,
        path=str(src),
        number="OLD-001",
        status=MediaFileStatus.SCRAPED,
        metadata_id=meta_old.id,
        oshash="oldoshasholdoshas",
        size=1,
        duration=12.5,
        codec="h264",
    )
    assert source.id is not None
    expected_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    journal_id = await _journal_for(
        repo,
        source=source,
        target_path=str(target),
        oshash=expected_oshash,
        size=src.stat().st_size,
        target_file_id=occupant.id,
        source_metadata_id=meta_new.id,
        source_number="NSFS-039",
    )

    await repo.takeover_overwrite_target(
        journal_id, source_file_id=source.id, oshash=expected_oshash, size=src.stat().st_size
    )

    assert await repo.get_media_file(occupant.id) is None
    row = await repo.get_media_file(source.id)
    assert row is not None
    assert row.path == str(target)
    # 归属改写为 journal 记录值: 覆盖, 而非保留现值或仅补空.
    assert row.metadata_id == meta_new.id
    assert row.number == "NSFS-039"
    assert row.status is MediaFileStatus.SCRAPED
    assert row.oshash == expected_oshash
    assert row.size == src.stat().st_size
    # 目标内容已变: 原有时长 / 编码清空待刷新; 相位按新路径派生 (-C 中字标记).
    assert row.duration is None
    assert row.codec is None
    assert row.has_subtitle_in_name is True
    journal = await repo.get_organize_journal(journal_id)
    assert journal is not None and journal.phase == "committed"


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.asyncio
async def test_takeover_target_without_index(repo: Repository, tmp_path: Path, indexed: bool) -> None:
    """target_file_id=NULL: 源行 path 改目标路径; 目标路径上建 journal 后才入库的行按路径现查现删."""
    src = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    # 目标行晚于建 journal 入库 (扫描并发), id 未记入 journal.
    occupant: MediaFile | None = None
    if indexed:
        occupant = await repo.create_media_file(1, path=str(target), number="OLD-001")
        assert occupant.id is not None
    source = await repo.create_media_file(1, path=str(src), number="NSFS-039")
    assert source.id is not None
    expected_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    journal_id = await _journal_for(
        repo,
        source=source,
        target_path=str(target),
        oshash=expected_oshash,
        size=src.stat().st_size,
        target_file_id=None,
    )

    await repo.takeover_overwrite_target(
        journal_id, source_file_id=source.id, oshash=expected_oshash, size=src.stat().st_size
    )

    if occupant is not None:
        assert occupant.id is not None
        assert await repo.get_media_file(occupant.id) is None
    row = await repo.get_media_file(source.id)
    assert row is not None and row.path == str(target)
    journal = await repo.get_organize_journal(journal_id)
    assert journal is not None and journal.phase == "committed"
    assert len(await repo.list_media_files(limit=None)) == 1


@pytest.mark.asyncio
async def test_takeover_is_single_transaction(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事务中途失败 → 四步全部回滚: 目标旧行仍在, 源行 path 不变, journal 未推进."""
    src = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    occupant = await repo.create_media_file(1, path=str(target), number="OLD-001")
    assert occupant.id is not None
    source = await repo.create_media_file(1, path=str(src), number="NSFS-039")
    assert source.id is not None
    journal_id = await _journal_for(
        repo,
        source=source,
        target_path=str(target),
        oshash="deadbeefdeadbeef",
        size=src.stat().st_size,
        target_file_id=occupant.id,
    )

    def boom(media: MediaFile) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("amane.db.repos.organize_journal._apply_path_phase", boom)

    with pytest.raises(RuntimeError):
        await repo.takeover_overwrite_target(
            journal_id, source_file_id=source.id, oshash="deadbeefdeadbeef", size=src.stat().st_size
        )

    assert await repo.get_media_file(occupant.id) is not None
    row = await repo.get_media_file(source.id)
    assert row is not None and row.path == str(src)
    journal = await repo.get_organize_journal(journal_id)
    assert journal is not None and journal.phase == "prepared"


@pytest.mark.asyncio
async def test_takeover_missing_source_row_raises(repo: Repository, tmp_path: Path) -> None:
    """指纹复验后的源行 id 取不到行 → 抛错, journal 不推进, 由调用方写 last_error."""
    src = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    source = await repo.create_media_file(1, path=str(src))
    assert source.id is not None
    journal_id = await _journal_for(
        repo,
        source=source,
        target_path=str(target),
        oshash="deadbeefdeadbeef",
        size=src.stat().st_size,
        target_file_id=None,
    )
    await repo.delete_media_file(source.id)

    with pytest.raises(LookupError):
        await repo.takeover_overwrite_target(
            journal_id, source_file_id=int(source.id), oshash="deadbeefdeadbeef", size=src.stat().st_size
        )

    journal = await repo.get_organize_journal(journal_id)
    assert journal is not None and journal.phase == "prepared"


@pytest.mark.asyncio
async def test_takeover_missing_journal_row_raises(repo: Repository, tmp_path: Path) -> None:
    """journal 行不存在 → 抛错, 源行不被触碰 (接管的前置校验先于一切写)."""
    src = tmp_path / "lib" / "incoming" / "AAA.mp4"
    source = await repo.create_media_file(1, path=str(src), number="NSFS-039")
    assert source.id is not None
    assert await repo.list_organize_journals() == []

    with pytest.raises(LookupError):
        await repo.takeover_overwrite_target(1, source_file_id=source.id, oshash="deadbeefdeadbeef", size=4096)

    row = await repo.get_media_file(source.id)
    assert row is not None and row.path == str(src)


@pytest.mark.asyncio
async def test_takeover_reentry_with_source_at_target(repo: Repository, tmp_path: Path) -> None:
    """源行已在目标路径时重入接管: 路径查到的占用行即源行自身, 按 id 跳过删除, 幂等收敛."""
    src = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    source = await repo.create_media_file(1, path=str(src), number="NSFS-039")
    assert source.id is not None
    expected_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    journal_id = await _journal_for(
        repo,
        source=source,
        target_path=str(target),
        oshash=expected_oshash,
        size=src.stat().st_size,
        target_file_id=None,
    )

    await repo.takeover_overwrite_target(
        journal_id, source_file_id=source.id, oshash=expected_oshash, size=src.stat().st_size
    )
    # 二次接管: 不按 id 跳过自身的话, 源行会被当作目标旧行删掉.
    await repo.takeover_overwrite_target(
        journal_id, source_file_id=source.id, oshash=expected_oshash, size=src.stat().st_size
    )

    assert [row.id for row in await repo.list_media_files(limit=None)] == [source.id]
    row = await repo.get_media_file(source.id)
    assert row is not None and row.path == str(target) and row.number == "NSFS-039"
    journal = await repo.get_organize_journal(journal_id)
    assert journal is not None and journal.phase == "committed"


@pytest.mark.asyncio
async def test_fingerprint_uses_live_content_not_cached(
    repo: Repository, tmp_path: Path
) -> None:
    """行缓存的 oshash/size 过期 (源被外部改写) 时, 阶段 A 按现场内容覆写与核验 (联合审核 P2).

    源与目标等长不同内容: 行里的旧指纹与目标相等, 若按缓存核验会在目标核验处失败;
    现场读取后按新内容覆写, 接管行的 oshash/size 必须是新指纹.
    """
    source = _video(tmp_path / "lib" / "incoming" / "AAA.mp4", CONTENT_SOURCE)
    target = _video(tmp_path / "lib" / "Studio" / "BBB.mp4", CONTENT_TARGET)
    stale_oshash = _fingerprint(tmp_path, CONTENT_TARGET)
    stale_size = target.stat().st_size
    media = await repo.create_media_file(1, path=str(source), oshash=stale_oshash, size=stale_size)
    assert media.id is not None
    live_oshash = _fingerprint(tmp_path, CONTENT_SOURCE)
    live_size = source.stat().st_size
    assert (stale_oshash, stale_size) != (live_oshash, live_size)

    result = await overwrite_target(
        source=source, target=target, mode=MoveMode.MOVE, repo=repo, media_file=media, task_id=11
    )

    assert result.outcome is PlaceOutcome.PLACED
    assert target.read_bytes() == CONTENT_SOURCE * 8
    updated = await repo.get_media_file(media.id)
    assert updated is not None and updated.path == str(target)
    assert updated.oshash == live_oshash
    assert updated.size == live_size
    assert await repo.list_organize_journals() == []
