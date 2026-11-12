"""MediaMetadata 原文双轨字段与逐字段语言声明."""

import pytest
from pydantic import ValidationError

from amane.crawlers.models import MediaMetadata


def test_media_metadata_original_fields_default_none():
    m = MediaMetadata(number="ABP-123")
    assert m.original_title is None
    assert m.original_plot is None
    assert m.field_languages is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"number": "ABP-123"},
            (None, None, None),
            id="stale_snapshot_without_fields",
        ),
        pytest.param(
            {
                "number": "ABP-123",
                "original_title": "日本語タイトル",
                "original_plot": "日本語あらすじ",
                "field_languages": {"original_title": "ja", "original_plot": "ja"},
            },
            ("日本語タイトル", "日本語あらすじ", {"original_title": "ja", "original_plot": "ja"}),
            id="with_original_fields",
        ),
        pytest.param(
            {"number": "ABP-123", "field_languages": {}},
            (None, None, {}),
            id="empty_language_map",
        ),
    ],
)
def test_deserialize_raw_snapshot(payload: dict, expected: tuple):
    """存量 raw 快照缺新字段时反序列化不得抛错; 有新字段时原样保留."""
    m = MediaMetadata.model_validate(payload)
    assert (m.original_title, m.original_plot, m.field_languages) == expected


def test_stale_snapshot_constructs_from_kwargs():
    """聚合引擎 _fetch_one 用 MediaMetadata(**cached) 反序列化, 缺字段的存量快照同样不得抛 TypeError."""
    # dict 模拟 DB JSON 快照的松散形态, 与 _fetch_one 的实参类型一致.
    cached: dict = {"number": "ABP-123"}
    m = MediaMetadata(**cached)
    assert m.original_title is None
    assert m.original_plot is None
    assert m.field_languages is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"number": "ABP-123", "field_languages": "ja"}, id="languages_as_str"),
        pytest.param({"number": "ABP-123", "field_languages": ["ja"]}, id="languages_as_list"),
        pytest.param({"number": "ABP-123", "original_title": {"nested": True}}, id="title_as_dict"),
        pytest.param({"number": "ABP-123", "original_plot": []}, id="plot_as_list"),
    ],
)
def test_invalid_original_field_types_rejected(payload: dict):
    with pytest.raises(ValidationError):
        MediaMetadata.model_validate(payload)
