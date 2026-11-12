"""NFO 默认输出的逐字节基准; write_nfo 默认配置下的任何输出变化都必须同步更新 GOLDEN.

基准串由当前实现生成, 行尾为 LF (仓库 .gitattributes 保证源码 LF 检出);
比对是同一平台的逐字节比较, Windows 落盘 CRLF 造成的差异不属于文本内容差异.
"""

from typing import TYPE_CHECKING

import pytest

from amane.db.models import Metadata
from amane.media import write_nfo

if TYPE_CHECKING:
    from pathlib import Path


GOLDEN = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <plot>剧情内容第一行

剧情内容第二行</plot>
  <outline>剧情内容第一行

剧情内容第二行</outline>
  <premiered>2026-01-15</premiered>
  <releasedate>2026-01-15</releasedate>
  <year>2026</year>
  <num>MIDV-123</num>
  <title>MIDV-123 Test Title</title>
  <originaltitle>MIDV-123 Test Title</originaltitle>
  <sorttitle>MIDV-123 Test Title</sorttitle>
  <mpaa>JP-18+</mpaa>
  <actor>
    <name>Actor A</name>
    <type>Actor</type>
  </actor>
  <actor>
    <name>Actor B</name>
    <type>Actor</type>
  </actor>
  <rating>8.5</rating>
  <criticrating>85</criticrating>
  <runtime>120</runtime>
  <series>Series Y</series>
  <set>
    <name>Series Y</name>
  </set>
  <studio>Studio X</studio>
  <maker>Studio X</maker>
  <publisher>Publisher P</publisher>
  <label>Publisher P</label>
  <tag>Drama</tag>
  <genre>Drama</genre>
  <tag>Romance</tag>
  <genre>Romance</genre>
  <poster>https://example.com/poster.jpg</poster>
  <cover>https://example.com/thumb.jpg</cover>
  <trailer>https://example.com/trailer.mp4</trailer>
  <director>Director Z</director>
  <javdbid>ABC123</javdbid>
  <r18id>R001</r18id>
</movie>
"""


def full_metadata() -> Metadata:
    """全部可输出字段均非空的条目, 覆盖 write_nfo 的每个元素分支."""
    return Metadata(
        number="MIDV-123",
        title="Test Title",
        plot="剧情内容第一行\n\n剧情内容第二行",
        release="2026-01-15",
        runtime=120,
        scores={"javdb": 8.5},
        actors=["Actor A", "Actor B"],
        directors=["Director Z"],
        series="Series Y",
        studio="Studio X",
        publisher="Publisher P",
        tags=["Drama", "Romance"],
        poster_urls=["https://example.com/poster.jpg"],
        thumb_urls=["https://example.com/thumb.jpg"],
        trailer_urls=["https://example.com/trailer.mp4"],
        external_ids={"javdb": "ABC123", "r18": "R001"},
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_default_nfo_matches_golden(tmp_path: Path) -> None:
    nfo_path = tmp_path / "MIDV-123.nfo"

    assert await write_nfo(full_metadata(), nfo_path) is True

    assert nfo_path.read_bytes() == GOLDEN.encode("utf-8")
