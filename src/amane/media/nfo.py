"""Kodi ``<movie>`` NFO, 供 Emby / Jellyfin / Kodi 读取."""

import re
from io import StringIO
from typing import TYPE_CHECKING, Literal

import aiofiles
import structlog
from pydantic import BaseModel

from ..utils.text import strip_illegal_xml_chars

if TYPE_CHECKING:
    from pathlib import Path

    from ..db.models import Metadata


logger = structlog.get_logger()


_XML_ESCAPE_MAP: dict[str, str] = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&apos;",
    '"': "&quot;",
}


def _escape_xml(text: str) -> str:
    """写进 XML 文本节点: 先剥非法字符 (存量脏数据兜底), 再转义; 换行原样保留."""
    escaped = strip_illegal_xml_chars(text)
    for char, entity in _XML_ESCAPE_MAP.items():
        escaped = escaped.replace(char, entity)
    return escaped


def _title_with_number(number: str, title: str) -> str:
    """NFO 显示标题: 「番号 + 空格 + 标题」; 标题已以番号开头时不再重复拼接.

    番号后须为非字母数字, 否则 ``ABF-06`` 会把 ``ABF-062`` 当作自己的前缀.
    """
    text = title.strip()
    if number:
        match = re.match(re.escape(number), text, re.IGNORECASE)
        if match is not None:
            rest = text[match.end() :]
            if not rest or not rest[0].isalnum():
                text = rest.strip()
    return f"{number} {text}".strip()


class NfoOptions(BaseModel):
    """write_nfo 的元素开关, 字段名即对应的 XML 元素名.

    默认值构成 NFO 的固定模板口径; votes / country / customrating 当前无数据来源, 开启也不输出元素.
    """

    model_config = {"extra": "forbid"}

    # 标题类
    title: bool = True
    originaltitle: bool = True
    originaltitle_source: Literal["title", "original"] = "title"
    sorttitle: bool = True
    # 剧情类
    plot: bool = True
    outline: bool = True
    outline_summary: bool = False
    originalplot: bool = False
    # 人物类
    actor: bool = True
    director: bool = True
    # 日期类
    premiered: bool = True
    releasedate: bool = True
    year: bool = True
    # 其它
    num: bool = True
    runtime: bool = True
    votes: bool = False
    score: bool = True
    criticrating: bool = True
    country: bool = False
    mpaa: bool = True
    mpaa_value: str = "JP-18+"
    customrating: bool = False
    series: bool = True
    set: bool = True
    tag: bool = True
    genre: bool = True
    studio: bool = True
    maker: bool = True
    publisher: bool = True
    label: bool = True
    poster: bool = True
    cover: bool = True
    trailer: bool = True
    website: bool = False
    external_ids: bool = True
    add_part_to_title: bool = False
    add_translated_by: bool = False
    tagline_format: str | None = None
    set_by_actress: bool = False
    set_by_serial_name: bool = False
    set_by_series: bool = False
    additional_tags: list[str] = []


async def write_nfo(metadata: Metadata, nfo_path: Path, options: NfoOptions | None = None) -> bool:
    if options is None:
        options = NfoOptions()
    try:
        nfo_path.parent.mkdir(parents=True, exist_ok=True)

        code = StringIO()
        code.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
        code.write("<movie>\n")

        if metadata.plot:
            # 长文本按纯文本写出: 转义后 "]]>" 不可能出现, CDATA 只带来非法 XML 的风险.
            if options.plot:
                code.write(f"  <plot>{_escape_xml(metadata.plot)}</plot>\n")
            if options.outline:
                code.write(f"  <outline>{_escape_xml(metadata.plot)}</outline>\n")

        if metadata.release:
            if options.premiered:
                code.write(f"  <premiered>{metadata.release}</premiered>\n")
            if options.releasedate:
                code.write(f"  <releasedate>{metadata.release}</releasedate>\n")
            if options.year:
                year_match = re.match(r"(\d{4})", metadata.release)
                if year_match:
                    code.write(f"  <year>{year_match.group(1)}</year>\n")

        if options.num and metadata.number:
            code.write(f"  <num>{_escape_xml(metadata.number)}</num>\n")

        if metadata.title:
            # originaltitle_source 的 "original" 取值需要条目原文标题数据, 当前与 "title" 同值输出.
            title = _escape_xml(_title_with_number(metadata.number, metadata.title))
            if options.title:
                code.write(f"  <title>{title}</title>\n")
            if options.originaltitle:
                code.write(f"  <originaltitle>{title}</originaltitle>\n")
            if options.sorttitle:
                code.write(f"  <sorttitle>{title}</sorttitle>\n")

        if options.mpaa and options.mpaa_value:
            code.write(f"  <mpaa>{_escape_xml(options.mpaa_value)}</mpaa>\n")

        if options.actor and metadata.actors:
            for actor in metadata.actors:
                code.write("  <actor>\n")
                code.write(f"    <name>{_escape_xml(actor)}</name>\n")
                code.write("    <type>Actor</type>\n")
                code.write("  </actor>\n")

        if metadata.score is not None:
            if options.score:
                code.write(f"  <rating>{metadata.score}</rating>\n")
            if options.criticrating:
                code.write(f"  <criticrating>{int(metadata.score * 10)}</criticrating>\n")

        if options.runtime and metadata.runtime is not None:
            code.write(f"  <runtime>{metadata.runtime}</runtime>\n")

        if metadata.series:
            if options.series:
                code.write(f"  <series>{_escape_xml(metadata.series)}</series>\n")
            if options.set:
                code.write("  <set>\n")
                code.write(f"    <name>{_escape_xml(metadata.series)}</name>\n")
                code.write("  </set>\n")

        if metadata.studio:
            if options.studio:
                code.write(f"  <studio>{_escape_xml(metadata.studio)}</studio>\n")
            if options.maker:
                code.write(f"  <maker>{_escape_xml(metadata.studio)}</maker>\n")

        if metadata.publisher:
            if options.publisher:
                code.write(f"  <publisher>{_escape_xml(metadata.publisher)}</publisher>\n")
            if options.label:
                code.write(f"  <label>{_escape_xml(metadata.publisher)}</label>\n")

        if metadata.tags:
            for tag in metadata.tags:
                if not tag:
                    continue
                if options.tag:
                    code.write(f"  <tag>{_escape_xml(tag)}</tag>\n")
                if options.genre:
                    code.write(f"  <genre>{_escape_xml(tag)}</genre>\n")
        if options.tag:
            for tag in options.additional_tags:
                if tag:
                    code.write(f"  <tag>{_escape_xml(tag)}</tag>\n")

        if options.poster and metadata.poster_url:
            code.write(f"  <poster>{_escape_xml(metadata.poster_url)}</poster>\n")

        if options.cover and metadata.thumb_url:
            code.write(f"  <cover>{_escape_xml(metadata.thumb_url)}</cover>\n")

        if options.trailer and metadata.trailer_url:
            code.write(f"  <trailer>{_escape_xml(metadata.trailer_url)}</trailer>\n")

        if options.director and metadata.directors:
            for director in metadata.directors:
                code.write(f"  <director>{_escape_xml(director)}</director>\n")

        if options.external_ids and metadata.external_ids:
            for site, ext_id in metadata.external_ids.items():
                if ext_id:
                    code.write(f"  <{site}id>{_escape_xml(str(ext_id))}</{site}id>\n")

        code.write("</movie>\n")

        async with aiofiles.open(nfo_path, "w", encoding="UTF-8") as f:
            await f.write(code.getvalue())

        logger.debug("nfo written", path=str(nfo_path))
        return True

    except Exception:
        logger.exception("nfo write failed", path=str(nfo_path))
        return False
