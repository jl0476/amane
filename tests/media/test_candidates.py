"""整理期海报候选重建与用户产物分类的测试 (纯函数, 数据注入)."""

from collections.abc import Mapping

import pytest

from amane.enums import MetadataField
from amane.media import format_crop_box_args
from amane.media.candidates import rebuild_poster_candidates

# 自动裁剪旧产物的 locator args 数值形态 (右侧比).
_AUTO_CROP_ARGS = "0.7"


def test_rebuild_routes_through_current_field_chain() -> None:
    """重建序跟随当前字段链而非 raw 键序; lang 限定快照键按站点归组; poster 黑名单剔除与链外站点不参与."""
    raw = {
        "beta": {"poster_urls": ["https://beta.example/b1", "https://beta.example/b2"]},
        "gamma": {"poster_urls": ["https://gamma.example/g1"]},
        "retired": {"poster_urls": ["https://retired.example/r1"]},
        "alpha": {"poster_urls": ["https://alpha.example/a1"]},
        "alpha:ja": {"poster_urls": ["https://alpha.example/a1-ja"]},
        # 旧形态的单值字符串与聚合拼接同样宽容处理.
        "delta": {"poster_urls": "https://delta.example/s1"},
    }
    result = rebuild_poster_candidates(
        raw=raw,
        poster_urls=[],
        locator_args={},
        locked_fields=[],
        route=["beta", "alpha", "gamma", "delta"],
        field_priority={MetadataField.POSTER_URLS: ["alpha"]},
        field_blacklist={MetadataField.POSTER_URLS: ["gamma"]},
    )
    assert result.candidates == [
        "https://alpha.example/a1",
        "https://alpha.example/a1-ja",
        "https://beta.example/b1",
        "https://beta.example/b2",
        "https://delta.example/s1",
    ]
    assert result.user_product is False
    assert result.fallback is False


@pytest.mark.parametrize(
    ("raw", "expected_candidates"),
    [
        pytest.param(
            {"alpha": {"poster_urls": ["https://alpha.example/a1"]}},
            ["https://alpha.example/a1", "/api/resources/original0"],
            id="body_first_tail_after",
        ),
        pytest.param({}, ["/api/resources/original0"], id="raw_empty_local_original_only"),
    ],
)
def test_auto_derived_urls_excluded(raw: Mapping[str, Mapping[str, object]], expected_candidates: list[str]) -> None:
    """数值 args 的自动裁剪旧产物从候选排除; 未派生的既有条目保留在链尾, 不构成用户产物."""
    result = rebuild_poster_candidates(
        raw=raw,
        poster_urls=["/api/resources/auto0", "/api/resources/original0"],
        locator_args={"/api/resources/auto0": _AUTO_CROP_ARGS},
        locked_fields=[],
        route=["alpha"],
        field_priority={},
    )
    assert result.candidates == expected_candidates
    assert result.user_product is False
    assert result.fallback is False


@pytest.mark.parametrize(
    "args",
    [
        format_crop_box_args(10, 20, 30, 40),
        format_crop_box_args(10, 20, 30, 40, rotation=90),
    ],
    ids=["plain", "rotated"],
)
def test_box_manual_url_means_user_product(args: str) -> None:
    """poster_urls 任一内部 URL 的 locator args 以 box: 开头即用户产物; 该判定不依赖锁定集合."""
    result = rebuild_poster_candidates(
        raw={"alpha": {"poster_urls": ["https://alpha.example/a1"]}},
        poster_urls=["/api/resources/manual0"],
        locator_args={"/api/resources/manual0": args},
        locked_fields=[],
        route=["alpha"],
        field_priority={},
    )
    assert result.user_product is True
    assert result.candidates == []
    assert result.fallback is False


@pytest.mark.parametrize(
    "poster_urls",
    [["https://user.example/picked.jpg"], []],
    ids=["external", "empty"],
)
def test_locked_poster_urls_means_user_product(poster_urls: list[str]) -> None:
    """poster_urls ∈ locked_fields 即用户产物 (PATCH 外部 URL 场景); 列表为空时仍短路, 空产物由调用方处理."""
    result = rebuild_poster_candidates(
        raw={"alpha": {"poster_urls": ["https://alpha.example/a1"]}},
        poster_urls=poster_urls,
        locator_args={},
        locked_fields=[MetadataField.POSTER_URLS.value, MetadataField.TITLE.value],
        route=["alpha"],
        field_priority={},
    )
    assert result.user_product is True
    assert result.candidates == []
    assert result.fallback is False


@pytest.mark.parametrize(
    ("raw", "poster_urls", "locator_args"),
    [
        pytest.param({}, [], {}, id="no_raw_no_products"),
        pytest.param({}, ["/api/resources/auto0"], {"/api/resources/auto0": _AUTO_CROP_ARGS}, id="only_auto_derived"),
        pytest.param({"alpha": {"title": "t"}}, [], {}, id="snapshot_without_poster"),
        pytest.param({"alpha": {"poster_urls": None}}, [], {}, id="malformed_value"),
        pytest.param({"retired": {"poster_urls": ["https://retired.example/r1"]}}, [], {}, id="out_of_chain_source"),
    ],
)
def test_empty_rebuild_falls_back(
    raw: Mapping[str, Mapping[str, object]], poster_urls: list[str], locator_args: Mapping[str, str]
) -> None:
    """重建候选为空时给出 fallback 信号, 供调用方回退物化图复制路径."""
    result = rebuild_poster_candidates(
        raw=raw,
        poster_urls=poster_urls,
        locator_args=locator_args,
        locked_fields=[],
        route=["alpha"],
        field_priority={},
    )
    assert result.candidates == []
    assert result.user_product is False
    assert result.fallback is True
