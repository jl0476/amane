"""测试 NFO 附属文件生成"""

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from amane.db.models import Metadata
from amane.media import NfoOptions, write_nfo
from tests.media.test_nfo_baseline import GOLDEN, full_metadata

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def metadata() -> Metadata:
    return Metadata(
        number="MIDV-123",
        title="Test Title",
        actors=["Actor A", "Actor B"],
        studio="Studio X",
        release="2026-01-15",
        runtime=120,
        tags=["Drama", "Romance"],
        series="Series Y",
        scores={"javdb": 85.0},
        directors=["Director Z"],
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_has_required_fields(tmp_path: Path, metadata: Metadata):
    nfo_path = tmp_path / "MIDV-123.nfo"
    ok = await write_nfo(metadata, nfo_path)
    assert ok is True
    assert nfo_path.exists()
    content = nfo_path.read_text(encoding="utf-8")
    assert "<title>MIDV-123 Test Title</title>" in content
    assert "<num>MIDV-123</num>" in content
    assert "<actor>" in content
    assert "<name>Actor A</name>" in content
    assert "<studio>Studio X</studio>" in content
    assert "<genre>Drama</genre>" in content
    assert "<director>Director Z</director>" in content
    assert "<rating>85.0</rating>" in content
    # 系列使用规范嵌套: <set><name>…</name></set>
    assert "<series>Series Y</series>" in content
    assert "<set>" in content
    assert "<name>Series Y</name>" in content


@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_plot_is_plain_text(tmp_path: Path):
    """长文本按纯文本写出: 无 CDATA, HTML 转义, 非法字符剥离, 换行原样保留."""
    plot = "结尾 ]]> 之后 & <标签>\n\n第二段\x0b"
    nfo_path = tmp_path / "PLOT-1.nfo"

    assert await write_nfo(Metadata(number="PLOT-1", title="T", plot=plot), nfo_path) is True

    content = nfo_path.read_text(encoding="utf-8")
    assert "<![CDATA[" not in content
    assert "]]>" not in content
    assert "\x0b" not in content

    root = ET.fromstring(content)
    assert root.findtext("plot") == "结尾 ]]> 之后 & <标签>\n\n第二段"
    assert root.findtext("outline") == root.findtext("plot")


TITLE_CASES: list[tuple[str, str, str]] = [
    ("MIDV-123", "Test Title", "MIDV-123 Test Title"),
    # 来源标题自带番号前缀: 不再重复拼接, 番号统一为条目写法
    ("MIDV-123", "MIDV-123 Test Title", "MIDV-123 Test Title"),
    ("MIDV-123", "midv-123 Test Title", "MIDV-123 Test Title"),
    ("MIDV-123", "MIDV-123【限定】Test", "MIDV-123 【限定】Test"),
    ("MIDV-123", "MIDV-123", "MIDV-123"),
    ("MIDV-123", " MIDV-123   Test Title ", "MIDV-123 Test Title"),
    # 番号后紧跟字母数字: 不是该番号的前缀
    ("ABF-06", "ABF-062 別作品", "ABF-06 ABF-062 別作品"),
    # 清理后的标题仍要转义
    ("MIDV-123", "MIDV-123 A & B", "MIDV-123 A & B"),
]


@pytest.mark.parametrize(("number", "title", "expected"), TITLE_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_title_keeps_number_once(tmp_path: Path, number: str, title: str, expected: str) -> None:
    nfo_path = tmp_path / f"{number}.nfo"

    assert await write_nfo(Metadata(number=number, title=title), nfo_path) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert [root.findtext(tag) for tag in ("title", "originaltitle", "sorttitle")] == [expected] * 3


# (开关名, 关闭该开关的 NfoOptions, 该开关对应元素的完整标记)
SWITCH_CASES: list[tuple[str, NfoOptions, list[str]]] = [
    ("title", NfoOptions(title=False), ["<title>MIDV-123 Test Title</title>"]),
    ("originaltitle", NfoOptions(originaltitle=False), ["<originaltitle>MIDV-123 Test Title</originaltitle>"]),
    ("sorttitle", NfoOptions(sorttitle=False), ["<sorttitle>MIDV-123 Test Title</sorttitle>"]),
    ("plot", NfoOptions(plot=False), ["<plot>剧情内容第一行\n\n剧情内容第二行</plot>"]),
    ("outline", NfoOptions(outline=False), ["<outline>剧情内容第一行\n\n剧情内容第二行</outline>"]),
    ("actor", NfoOptions(actor=False), ["<actor>", "<name>Actor A</name>", "</actor>"]),
    ("director", NfoOptions(director=False), ["<director>Director Z</director>"]),
    ("premiered", NfoOptions(premiered=False), ["<premiered>2026-01-15</premiered>"]),
    ("releasedate", NfoOptions(releasedate=False), ["<releasedate>2026-01-15</releasedate>"]),
    ("year", NfoOptions(year=False), ["<year>2026</year>"]),
    ("num", NfoOptions(num=False), ["<num>MIDV-123</num>"]),
    ("runtime", NfoOptions(runtime=False), ["<runtime>120</runtime>"]),
    ("score", NfoOptions(score=False), ["<rating>8.5</rating>"]),
    ("criticrating", NfoOptions(criticrating=False), ["<criticrating>85</criticrating>"]),
    ("mpaa", NfoOptions(mpaa=False), ["<mpaa>JP-18+</mpaa>"]),
    ("series", NfoOptions(series=False), ["<series>Series Y</series>"]),
    ("set", NfoOptions(set=False), ["<set>", "<name>Series Y</name>", "</set>"]),
    ("tag", NfoOptions(tag=False), ["<tag>Drama</tag>", "<tag>Romance</tag>"]),
    ("genre", NfoOptions(genre=False), ["<genre>Drama</genre>", "<genre>Romance</genre>"]),
    ("studio", NfoOptions(studio=False), ["<studio>Studio X</studio>"]),
    ("maker", NfoOptions(maker=False), ["<maker>Studio X</maker>"]),
    ("publisher", NfoOptions(publisher=False), ["<publisher>Publisher P</publisher>"]),
    ("label", NfoOptions(label=False), ["<label>Publisher P</label>"]),
    ("poster", NfoOptions(poster=False), ["<poster>https://example.com/poster.jpg</poster>"]),
    ("cover", NfoOptions(cover=False), ["<cover>https://example.com/thumb.jpg</cover>"]),
    ("trailer", NfoOptions(trailer=False), ["<trailer>https://example.com/trailer.mp4</trailer>"]),
    (
        "external_ids",
        NfoOptions(external_ids=False),
        ["<javdbid>ABC123</javdbid>", "<r18id>R001</r18id>"],
    ),
]


@pytest.mark.parametrize(("switch", "off_options", "markers"), SWITCH_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_every_switch_off_removes_element(
    tmp_path: Path, switch: str, off_options: NfoOptions, markers: list[str]
) -> None:
    """开关开启 (默认) 时元素出现, 关闭后对应元素整段消失."""
    on_path = tmp_path / f"{switch}-on.nfo"
    off_path = tmp_path / f"{switch}-off.nfo"

    assert await write_nfo(full_metadata(), on_path, NfoOptions()) is True
    assert await write_nfo(full_metadata(), off_path, off_options) is True

    on_content = on_path.read_text(encoding="utf-8")
    off_content = off_path.read_text(encoding="utf-8")
    for marker in markers:
        assert marker in on_content
        assert marker not in off_content


# 当前无数据来源的开关: 开启也不产生元素 (输出规则表: 为空时不写)
SOURCELESS_SWITCHES: list[tuple[str, NfoOptions]] = [
    ("votes", NfoOptions(votes=True)),
    ("country", NfoOptions(country=True)),
    ("customrating", NfoOptions(customrating=True)),
]


@pytest.mark.parametrize(("switch", "on_options"), SOURCELESS_SWITCHES, ids=[name for name, _ in SOURCELESS_SWITCHES])
@pytest.mark.asyncio(loop_scope="function")
async def test_sourceless_switch_never_writes_element(tmp_path: Path, switch: str, on_options: NfoOptions) -> None:
    nfo_path = tmp_path / f"{switch}.nfo"

    assert await write_nfo(full_metadata(), nfo_path, on_options) is True

    assert f"<{switch}>" not in nfo_path.read_text(encoding="utf-8")


@pytest.mark.asyncio(loop_scope="function")
async def test_default_options_match_golden(tmp_path: Path) -> None:
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(full_metadata(), nfo_path, NfoOptions()) is True

    assert nfo_path.read_text(encoding="utf-8") == GOLDEN


@pytest.mark.asyncio(loop_scope="function")
async def test_all_element_switches_off_writes_skeleton(tmp_path: Path) -> None:
    options = NfoOptions(
        title=False,
        originaltitle=False,
        sorttitle=False,
        plot=False,
        outline=False,
        actor=False,
        director=False,
        premiered=False,
        releasedate=False,
        year=False,
        num=False,
        runtime=False,
        score=False,
        criticrating=False,
        mpaa=False,
        series=False,
        set=False,
        tag=False,
        genre=False,
        studio=False,
        maker=False,
        publisher=False,
        label=False,
        poster=False,
        cover=False,
        trailer=False,
        external_ids=False,
    )
    nfo_path = tmp_path / "skeleton.nfo"

    assert await write_nfo(full_metadata(), nfo_path, options) is True

    assert nfo_path.read_text(encoding="utf-8") == '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<movie>\n</movie>\n'


@pytest.mark.asyncio(loop_scope="function")
async def test_mpaa_value_renders_configured_text(tmp_path: Path) -> None:
    nfo_path = tmp_path / "mpaa.nfo"

    assert await write_nfo(full_metadata(), nfo_path, NfoOptions(mpaa_value="R-15")) is True

    assert "<mpaa>R-15</mpaa>" in nfo_path.read_text(encoding="utf-8")


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        NfoOptions.model_validate({"unrelated": True})


def test_invalid_originaltitle_source_rejected() -> None:
    with pytest.raises(ValidationError):
        NfoOptions.model_validate({"originaltitle_source": "both"})


# (NfoOptions, 预期的 <tag> 元素文本序列)
ADDITIONAL_TAG_CASES: list[tuple[NfoOptions, list[str]]] = [
    (NfoOptions(), ["Drama", "Romance"]),
    (NfoOptions(additional_tags=["中文字幕", "1080p"]), ["Drama", "Romance", "中文字幕", "1080p"]),
    # 空串附加标签跳过, 与元数据标签同口径 (边界)
    (NfoOptions(additional_tags=["", "中文字幕"]), ["Drama", "Romance", "中文字幕"]),
    # 关 tag 时元数据标签与附加标签都不出现 (边界)
    (NfoOptions(tag=False, additional_tags=["中文字幕"]), []),
]


@pytest.mark.parametrize(
    ("options", "expected_tags"),
    ADDITIONAL_TAG_CASES,
    ids=["default", "appended", "skip-empty", "switch-off"],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_additional_tags_appended_as_tag_elements(
    tmp_path: Path, options: NfoOptions, expected_tags: list[str]
) -> None:
    nfo_path = tmp_path / "tags.nfo"

    assert await write_nfo(full_metadata(), nfo_path, options) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert [element.text for element in root.findall("tag")] == expected_tags


@pytest.mark.asyncio(loop_scope="function")
async def test_additional_tags_do_not_extend_genre(tmp_path: Path) -> None:
    nfo_path = tmp_path / "genre.nfo"

    assert await write_nfo(full_metadata(), nfo_path, NfoOptions(additional_tags=["中文字幕"])) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert [element.text for element in root.findall("genre")] == ["Drama", "Romance"]


@pytest.mark.asyncio(loop_scope="function")
async def test_mpaa_value_escapes_xml_special_chars(tmp_path: Path) -> None:
    nfo_path = tmp_path / "mpaa.nfo"

    assert await write_nfo(full_metadata(), nfo_path, NfoOptions(mpaa_value="R-15 & <未定>")) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert root.findtext("mpaa") == "R-15 & <未定>"


# (NfoOptions, 元数据字段覆盖, 预期 <originaltitle> 文本; None 表示不写)
ORIGINAL_TITLE_CASES: list[tuple[NfoOptions, dict[str, str | None], str | None]] = [
    # 默认 source="title": 原文非空也不读, 仍取 <title> 的值
    (NfoOptions(), {"original_title": "オリジナルタイトル"}, "MIDV-123 Test Title"),
    (NfoOptions(originaltitle_source="original"), {"original_title": "オリジナルタイトル"}, "オリジナルタイトル"),
    (NfoOptions(originaltitle_source="original"), {"original_title": None}, "MIDV-123 Test Title"),
    (NfoOptions(originaltitle_source="original"), {"original_title": ""}, "MIDV-123 Test Title"),
    (NfoOptions(originaltitle_source="original"), {"original_title": "A & B"}, "A & B"),
    # 原文标题不拼番号, 与 <title> 的拼接口径不同
    (NfoOptions(originaltitle_source="original"), {"original_title": "オリジナルタイトル"}, "オリジナルタイトル"),
    # 无标题时原文独立写出; 标题与原文皆空则整段不写
    (
        NfoOptions(originaltitle_source="original"),
        {"title": None, "original_title": "オリジナルタイトル"},
        "オリジナルタイトル",
    ),
    (NfoOptions(originaltitle_source="original"), {"title": None, "original_title": None}, None),
]

ORIGINAL_TITLE_IDS = [
    "default-ignores-original",
    "original",
    "original-none-falls-back",
    "original-empty-falls-back",
    "original-escaped",
    "original-keeps-raw",
    "original-without-title",
    "original-and-title-empty",
]


@pytest.mark.parametrize(("options", "overrides", "expected"), ORIGINAL_TITLE_CASES, ids=ORIGINAL_TITLE_IDS)
@pytest.mark.asyncio(loop_scope="function")
async def test_originaltitle_source_selects_content_track(
    tmp_path: Path, options: NfoOptions, overrides: dict[str, str | None], expected: str | None
) -> None:
    """originaltitle_source 决定取值轨: "original" 取原文标题, 为空回退 <title> 的值."""
    entry = Metadata(number="MIDV-123", title="Test Title").model_copy(update=overrides)
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(entry, nfo_path, options) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert root.findtext("originaltitle") == expected


# (元素名, NfoOptions, 元数据字段覆盖, 预期元素文本; None 表示不写)
OPT_IN_FIELD_CASES: list[tuple[str, NfoOptions, dict[str, str | None], str | None]] = [
    ("originalplot", NfoOptions(), {"original_plot": "原文剧情"}, None),
    ("originalplot", NfoOptions(originalplot=True), {"original_plot": "原文剧情"}, "原文剧情"),
    ("originalplot", NfoOptions(originalplot=True), {"original_plot": None}, None),
    ("originalplot", NfoOptions(originalplot=True), {"original_plot": ""}, None),
    ("originalplot", NfoOptions(originalplot=True), {"original_plot": "A & <B>"}, "A & <B>"),
    ("website", NfoOptions(), {"website": "https://example.com/page"}, None),
    ("website", NfoOptions(website=True), {"website": "https://example.com/page"}, "https://example.com/page"),
    ("website", NfoOptions(website=True), {"website": None}, None),
    ("website", NfoOptions(website=True), {"website": ""}, None),
]

OPT_IN_FIELD_IDS = [
    "originalplot-off-by-default",
    "originalplot-on",
    "originalplot-none",
    "originalplot-empty",
    "originalplot-escaped",
    "website-off-by-default",
    "website-on",
    "website-none",
    "website-empty",
]


@pytest.mark.parametrize(("element", "options", "overrides", "expected"), OPT_IN_FIELD_CASES, ids=OPT_IN_FIELD_IDS)
@pytest.mark.asyncio(loop_scope="function")
async def test_opt_in_field_written_only_when_enabled_and_non_empty(
    tmp_path: Path, element: str, options: NfoOptions, overrides: dict[str, str | None], expected: str | None
) -> None:
    """originalplot / website 默认关闭, 开启且有值才写."""
    entry = Metadata(number="MIDV-123", title="Test Title").model_copy(update=overrides)
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(entry, nfo_path, options) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert root.findtext(element) == expected


# (external_ids, 预期元素名, 预期文本; None 表示不写)
SITE_ID_CASES: list[tuple[dict[str, str], str, str | None]] = [
    # 多语言 cache_key 的 ``:`` 替换为 ``_``, 否则 <r18dev:jpid> 是未绑定前缀的非法 XML
    ({"r18dev:jp": "x"}, "r18dev_jp_id", "x"),
    ({"javdb": "ABC123"}, "javdbid", "ABC123"),
    ({"a b:c": "x"}, "a_b_c_id", "x"),
    # 键整体非法: 两个 ``_`` 来自键, 一个来自与 ``id`` 的分段
    ({"日本": "x"}, "___id", "x"),
    ({"r18dev:jp": ""}, "r18dev_jp_id", None),
]

SITE_ID_IDS = ["multilingual-colon", "plain-key-unchanged", "space-and-colon", "non-ascii", "empty-value"]


@pytest.mark.parametrize(("external_ids", "element", "expected"), SITE_ID_CASES, ids=SITE_ID_IDS)
@pytest.mark.asyncio(loop_scope="function")
async def test_site_id_element_name_sanitized(
    tmp_path: Path, external_ids: dict[str, str], element: str, expected: str | None
) -> None:
    """<{site}id> 元素名里的非 XML 名称字符替换为 _; ElementTree 能解析即名称合法."""
    entry = Metadata(number="MIDV-123", title="Test Title", external_ids=external_ids)
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(entry, nfo_path) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert root.findtext(element) == expected


# (plot, 预期 <outline> 文本; None 表示不写)
SUMMARY_CASES: list[tuple[str, str | None]] = [
    ("第一句。第二句。第三句", "第一句"),
    ("第一句！第二句。第三句", "第一句"),
    ("第一句？第二句", "第一句"),
    ("第一行\n第二行", "第一行"),
    # 半角句点不是分隔符: ``vol.1`` 这类写法不截断
    ("vol.1 的剧情", "vol.1 的剧情"),
    ("没有分隔符", "没有分隔符"),
    # 123 字节: 截断点落在第 41 个字符内部, 回退到完整字符
    ("あ" * 41, "あ" * 40),
    ("a" * 130, "a" * 120),
    ("あ" * 40, "あ" * 40),
    # 分隔符前无内容: 摘要为空不写
    ("\n第二行", None),
    ("。第一句", None),
    # 分隔符前的空白原样保留
    ("第一句 。第二句", "第一句 "),
]

SUMMARY_IDS = [
    "period",
    "exclamation-first",
    "question",
    "newline",
    "ascii-dot-kept",
    "short-no-separator",
    "multibyte-cut-inside",
    "ascii-cut",
    "exact-120-bytes",
    "empty-after-leading-newline",
    "empty-after-leading-period",
    "trailing-space-kept",
]


@pytest.mark.parametrize(("plot", "expected"), SUMMARY_CASES, ids=SUMMARY_IDS)
@pytest.mark.asyncio(loop_scope="function")
async def test_outline_summary_takes_first_sentence(tmp_path: Path, plot: str, expected: str | None) -> None:
    """outline_summary=True 时 outline 取摘要; plot 全文不受影响."""
    entry = Metadata(number="MIDV-123", title="Test Title", plot=plot)
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(entry, nfo_path, NfoOptions(outline_summary=True)) is True

    root = ET.fromstring(nfo_path.read_text(encoding="utf-8"))
    assert root.findtext("outline") == expected
    assert root.findtext("plot") == plot


# (元素名, 元数据字段覆盖, NfoOptions): 取值为空串时整段不写 (输出规则表「为空时: 不写」)
EMPTY_VALUE_CASES: list[tuple[str, dict[str, str | None], NfoOptions]] = [
    ("num", {"number": ""}, NfoOptions()),
    ("mpaa", {}, NfoOptions(mpaa_value="")),
]


@pytest.mark.parametrize(
    ("element", "overrides", "options"), EMPTY_VALUE_CASES, ids=["empty-number", "empty-mpaa-value"]
)
@pytest.mark.asyncio(loop_scope="function")
async def test_empty_value_writes_no_element(
    tmp_path: Path, element: str, overrides: dict[str, str | None], options: NfoOptions
) -> None:
    entry = Metadata(number="MIDV-123", title="Test Title").model_copy(update=overrides)
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(entry, nfo_path, options) is True

    assert f"<{element}>" not in nfo_path.read_text(encoding="utf-8")


@pytest.mark.asyncio(loop_scope="function")
async def test_default_options_ignore_original_fields(tmp_path: Path) -> None:
    """含原文字段的条目在默认开关下输出与基准逐字节一致: 默认值不读 original_title / original_plot / website."""
    entry = full_metadata().model_copy(
        update={
            "original_title": "オリジナルタイトル",
            "original_plot": "オリジナル剧情第一行",
            "website": "https://example.com/page",
        }
    )
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(entry, nfo_path, NfoOptions()) is True

    assert nfo_path.read_text(encoding="utf-8") == GOLDEN
