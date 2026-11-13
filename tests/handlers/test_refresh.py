"""识别词生产入口的集成测试 (规格 §3.1.6 注入表): 真实扫描流程两库同番号不同类型,
调度发现与清理清单解析注入对应库 ParseRuleSet.
"""

from pathlib import Path

import pytest

from amane.db.models import PARSE_RULE_SCOPE_LIBRARY, TaskType
from amane.db.parse_rules import ParseRuleProvider
from amane.db.repository import Repository
from amane.enums import LibraryAutomation
from amane.events import EventBus
from amane.handlers import RefreshHandler, RefreshPayload, ScanMode
from amane.library import build_footprint
from amane.library.cleanup import footprint as footprint_module
from amane.parsing import ContentType, ParseRuleSet, RuleKind, parse_file_info
from amane.scheduler.service import WatcherService


@pytest.mark.asyncio(loop_scope="function")
async def test_two_libraries_different_rules_via_real_scan(repo: Repository, tmp_path: Path) -> None:
    """两个库对同一番号文件采用不同规则: 扫描扇出的 SCRAPE 载荷 content_type 各随其库;
    类型在全局唯一的 Metadata 上、首建时定, 库规则不改写既有条目 (upsert 契约)."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "MIDV-123.mp4").write_bytes(b"\x00" * 100)
    (root_b / "MIDV-123.mp4").write_bytes(b"\x00" * 100)
    lib_a = await repo.create_library(name="a", path=str(root_a))
    lib_b = await repo.create_library(name="b", path=str(root_b))
    assert lib_a.id is not None and lib_b.id is not None
    await repo.create_parse_rule(
        scope_type=PARSE_RULE_SCOPE_LIBRARY,
        scope_id=lib_a.id,
        kind=RuleKind.NUMBER_KEYWORD,
        content_type=ContentType.CHINESE,
        pattern="MIDV",
    )
    await repo.create_parse_rule(
        scope_type=PARSE_RULE_SCOPE_LIBRARY,
        scope_id=lib_b.id,
        kind=RuleKind.NUMBER_KEYWORD,
        content_type=ContentType.WESTERN,
        pattern="MIDV",
    )

    provider = ParseRuleProvider(repo)
    result_a = await RefreshHandler(repo, parse_rules=provider).handle(
        RefreshPayload(library_id=lib_a.id, path=str(root_a), scan={ScanMode.add})
    )
    result_b = await RefreshHandler(repo, parse_rules=provider).handle(
        RefreshPayload(library_id=lib_b.id, path=str(root_b), scan={ScanMode.add})
    )
    assert result_a.success is True and result_b.success is True

    assert result_a.followups and result_b.followups
    payload_a = result_a.followups[0].payload
    payload_b = result_b.followups[0].payload
    assert payload_a["number"] == payload_b["number"] == "MIDV-123"
    assert payload_a["content_type"] == "chinese"
    assert payload_b["content_type"] == "western"

    # 库 A 的载荷先建条目; 库 B 的载荷再来不改写既有类型.
    meta_first = await repo.upsert_metadata("MIDV-123", content_type=ContentType.CHINESE)
    meta_again = await repo.upsert_metadata("MIDV-123", content_type=ContentType.WESTERN)
    assert meta_again.id == meta_first.id
    assert meta_again.content_type is ContentType.CHINESE


@pytest.mark.asyncio(loop_scope="function")
async def test_scheduler_and_cleanup_use_rules(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """调度发现解析与清理清单解析注入对应库 ParseRuleSet."""
    root = tmp_path / "lib"
    root.mkdir()
    video = root / "ABC-123.mp4"
    video.write_bytes(b"\x00" * 100)
    lib = await repo.create_library(name="lib", path=str(root), automation=LibraryAutomation.SCRAPE)
    assert lib.id is not None
    await repo.create_parse_rule(
        scope_type=PARSE_RULE_SCOPE_LIBRARY,
        scope_id=lib.id,
        kind=RuleKind.NUMBER_KEYWORD,
        content_type=ContentType.CHINESE,
        pattern="ABC",
    )

    # --- 调度: 文件发现解析按库规则定类型并随 SCRAPE 载荷下发 ---
    service = WatcherService(repo, EventBus(), use_polling=True, parse_rules=ParseRuleProvider(repo))
    await service._on_file_found(video, lib.id)

    media_files = await repo.list_media_files(library_id=lib.id, limit=None)
    assert [f.number for f in media_files] == ["ABC-123"]
    scrape_tasks = await repo.list_tasks(task_types=[TaskType.SCRAPE], limit=10)
    assert len(scrape_tasks) == 1
    assert scrape_tasks[0].payload["content_type"] == "chinese"

    # --- 清理清单: 足迹解析注入对应库 ParseRuleSet (content_type 不影响清单产物, 在注入点断言) ---
    meta = await repo.upsert_metadata("ABC-123", content_type=ContentType.CHINESE)
    assert meta.id is not None
    assert media_files[0].id is not None
    media = await repo.update_media_file(media_files[0].id, metadata_id=meta.id)
    assert media is not None

    captured: list[ParseRuleSet | None] = []
    real_parse = footprint_module.parse_file_info

    def spy(path: str | Path, **kwargs: object) -> object:
        captured.append(kwargs.get("rules"))  # pyright: ignore[reportArgumentType]
        return real_parse(path, has_external_subtitle=bool(kwargs.get("has_external_subtitle", False)))  # pyright: ignore[reportArgumentType,reportCallIssue]

    monkeypatch.setattr(footprint_module, "parse_file_info", spy)
    rules = await ParseRuleProvider(repo).for_library(lib.id)
    outcome = build_footprint.sync(
        library=lib,
        items=[media],
        indexed=[media],
        metas={meta.id: meta},
        include_work_dir=False,
        rules=rules,
    )
    assert outcome.inventory.entries
    assert captured and captured[0] is rules
    assert parse_file_info("/data/ABC-123.mp4", rules=captured[0]).content_type is ContentType.CHINESE
    assert parse_file_info("/data/other.mp4", rules=captured[0]).content_type is ContentType.WESTERN
