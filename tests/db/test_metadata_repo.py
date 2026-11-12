"""Metadata 仓储的原文字段写入: 显式赋值 / 长文本归一 / PATCH 派生列隔离."""

from typing import TYPE_CHECKING

import pytest

from amane.api.models.metadata import PartialMetadata
from amane.db.repo_types import WriteMode
from amane.enums import MetadataField

if TYPE_CHECKING:
    from amane.db.repository import Repository


@pytest.mark.asyncio(loop_scope="function")
async def test_update_metadata_accepts_original_fields(repo: Repository):
    """update_metadata 显式写入原文四列并读回."""
    meta = await repo.upsert_metadata("ABP-123", title="中文标题")
    assert meta.id is not None

    updated = await repo.update_metadata(
        meta.id,
        original_title="日本語タイトル",
        original_plot="日本語あらすじ",
        website="https://avsox.click/ja/movies/1",
        field_languages={"original_title": "ja", "original_plot": "ja"},
    )
    assert updated is not None
    assert updated.original_title == "日本語タイトル"
    assert updated.original_plot == "日本語あらすじ"
    assert updated.website == "https://avsox.click/ja/movies/1"
    assert updated.field_languages == {"original_title": "ja", "original_plot": "ja"}

    fetched = await repo.get_metadata(meta.id)
    assert fetched is not None
    assert fetched.original_title == "日本語タイトル"
    assert fetched.website == "https://avsox.click/ja/movies/1"
    assert fetched.field_languages == {"original_title": "ja", "original_plot": "ja"}


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("write", "html", "expected"),
    [
        pytest.param(
            "upsert",
            "<p>第一段<br>第二段</p> &amp; 续",
            "第一段\n第二段\n\n& 续",
            id="upsert_new_row",
        ),
        pytest.param(
            "upsert_existing",
            "<b>再刮</b><br>换行",
            "再刮\n换行",
            id="upsert_existing_row",
        ),
        pytest.param(
            "update",
            "<div>补刮<br>正文</div>",
            "补刮\n正文",
            id="update",
        ),
        pytest.param("upsert", None, None, id="none_kept"),
        pytest.param("update", "   ", None, id="blank_becomes_none"),
    ],
)
async def test_original_plot_normalized_as_long_text(
    repo: Repository, write: str, html: str | None, expected: str | None
):
    """original_plot 与 plot 同口径: 落库前收成纯文本 (normalize_long_text)."""
    if write.startswith("upsert"):
        meta = await repo.upsert_metadata("ABP-123", original_plot=html)
    else:
        created = await repo.upsert_metadata("ABP-123")
        assert created.id is not None
        meta = await repo.update_metadata(created.id, original_plot=html)
    assert meta is not None
    assert meta.id is not None
    assert meta.original_plot == expected

    fetched = await repo.get_metadata(meta.id)
    assert fetched is not None
    assert fetched.original_plot == expected


def test_partial_metadata_rejects_website_patch():
    """website 由聚合派生, field_languages 由刮削写入: 两者不进对外 PATCH."""
    partial_fields = PartialMetadata.model_fields
    assert "website" not in partial_fields
    assert "field_languages" not in partial_fields
    # 护栏: 排除名单失效时这里会立刻暴露.
    assert "title" in partial_fields
    assert "plot" in partial_fields


@pytest.mark.asyncio(loop_scope="function")
async def test_locked_original_title_blocks_auto_write(repo: Repository):
    """原文标题可锁定: AUTO 写入跳过锁定列, MANUAL 写入改值并续锁."""
    meta = await repo.upsert_metadata("ABP-123", title="T", original_title="锁定原文")
    assert meta.id is not None

    assert await repo.set_metadata_locks(meta.id, [MetadataField.ORIGINAL_TITLE]) is not None

    blocked = await repo.update_metadata(meta.id, original_title="自动覆盖")
    assert blocked is not None
    assert blocked.original_title == "锁定原文"

    manual = await repo.update_metadata(meta.id, mode=WriteMode.MANUAL, original_title="手动改写")
    assert manual is not None
    assert manual.original_title == "手动改写"
    assert manual.locked_fields == [str(MetadataField.ORIGINAL_TITLE)]
