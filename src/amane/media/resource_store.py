"""URL 级下载缓存. 未被 Metadata 引用的条目由 CLEANUP 回收, 不做 LRU.

派生资源用合成 locator ``derived:{sha256(src)}:{op}:{args}``.
就地超分覆盖原文件, URL 不变, meta 打 ``sr`` 标记.
内部 URL ``/api/resources/{hash}`` 是对既有 Resource 的引用, 解析为本地文件而不发请求.
"""

import asyncio
import hashlib
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.sql.expression import SelectOfScalar

from ..db.models import Resource
from .file_type import detect_media_type
from .images import probe_size

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from ..net.egress import EgressPolicy
    from ..net.http import WebClient

logger = structlog.get_logger()

RESOURCE_URL_PREFIX = "/api/resources"

TMP_MARK = ".amane-tmp-"
"""独占临时名标记, 后接任务标识; 与 organize/overwrite.py 的落位临时同一纪律, 便于事后清理."""

# 上游把已下架资源重定向到 200 的占位图 (DMM 的 now_printing, up-timely 的 noimage) 时不能算获取成功:
# 否则该 URL 会被判为可用并前置, 前端最终显示占位图本身.
_PLACEHOLDER_PATH_MARKERS: tuple[str, ...] = ("/now_printing/", "/noimage.")

# 覆盖缓存时先在记录上挂待提交标记, 文件换新后再落记录; 中途取消或进程退出会留下"标记在、字节新"
# 的中间态, 读路径据此按盘上字节自愈 (见 ``_heal_record``).
PENDING_KEY = "pending"
"""``meta`` 内的待提交标记: 值为待写入记录的 content_hash.

普通资源用字符串形态; 派生资源用 ``{'hash': 待写入的 content_hash, 'meta': 待写入的 meta}``,
自愈时按该 meta 写回, 派生记录的源版本标记因此与盘上字节同步.
"""

SRC_VERSION_KEY = "srcv"
"""``meta`` 内的源内容版本: 派生记录记源内容 hash, 命中时核对源是否已变."""

_CONTENT_BOUND_META_KEYS: tuple[str, ...] = (PENDING_KEY, "sr")
"""与记录自身字节绑定的标记: 字节被替换后一律失效 (超分标记等)."""


def _is_placeholder_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(marker in path for marker in _PLACEHOLDER_PATH_MARKERS)


def _without_pending(meta: dict | None) -> dict:
    """去掉待提交标记, 其余 meta 原样保留."""
    if not meta:
        return {}
    return {k: v for k, v in meta.items() if k != PENDING_KEY}


@dataclass(frozen=True)
class _Pending:
    """待提交标记的内容: 待写入的 content_hash 与待写入的 meta (None 表示 meta 不变)."""

    content_hash: str
    meta: dict | None
    path: str | None
    """待写入的相对路径; None 表示路径不变."""


def _pending(meta: dict | None) -> _Pending | None:
    """解析待提交标记: 字符串形态是普通资源, 字典形态是派生资源 (带待写入的 meta 与路径).

    标记不是这两种形态时按不可信处理, 返回 None (无从判定中间态). 调用方据此撤除该标记 ——
    不可信形态无从自愈, 留着只会让标记永久滞留.
    """
    if not meta:
        return None
    value = meta.get(PENDING_KEY)
    if isinstance(value, str):
        return _Pending(content_hash=value, meta=None, path=None)
    if isinstance(value, dict):
        content_hash = value.get("hash")
        if not isinstance(content_hash, str):
            return None
        pending_meta = value.get("meta")
        pending_path = value.get("path")
        return _Pending(
            content_hash=content_hash,
            meta=pending_meta if isinstance(pending_meta, dict) else None,
            path=pending_path if isinstance(pending_path, str) else None,
        )
    return None


def _without_content_bound(meta: dict | None) -> dict:
    """去掉与字节绑定的标记 (超分标记等): 字节已换新, 旧标记不再成立."""
    if not meta:
        return {}
    return {k: v for k, v in meta.items() if k not in _CONTENT_BOUND_META_KEYS}


def internal_url_hash(url: str) -> str | None:
    """内部 URL 的 hash; 非内部 URL 返回 None. 查询串与额外路径段一律剥离."""
    prefix = f"{RESOURCE_URL_PREFIX}/"
    if not url.startswith(prefix):
        return None
    token = url[len(prefix) :].split("?", 1)[0].split("/", 1)[0]
    return token or None


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def derived_locator(src_url: str, op: str, args: str) -> str:
    # 同一 (src_url, op, args) 恒得同一 locator, 无需反推 url.
    inner = hashlib.sha256(src_url.encode()).hexdigest()
    return f"derived:{inner}:{op}:{args}"


def _guess_ext(url: str) -> str:
    path = url.split("?")[0]
    ext = Path(path).suffix.lower()
    if ext and len(ext) <= 5:
        return ext
    return ".bin"


@dataclass
class AcquireResult:
    success: bool
    path: Path | None = None
    used_url: str | None = None
    failed: list[str] | None = None


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    waiters: int = 0


@dataclass(frozen=True)
class ResolvedSource:
    """处理入口的源解析结果: 底层 locator 与本地文件."""

    locator: str
    path: Path


@dataclass(frozen=True)
class SourceSnapshot:
    """源内容快照: 独占副本的路径与其字节版本.

    检测 / 裁剪 / 版本标记都基于同一份快照, 三者因此对应同一份源内容.
    """

    path: Path
    version: str


class UpscaleOutcome(StrEnum):
    """``SKIPPED`` 表示盘上已是记录所指的那份字节, 调用方不应计作失败."""

    UPSCALED = "upscaled"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class _BestCandidate:
    """择优轮的一个候选: 独占临时上的那份字节与其来源 URL."""

    url: str
    temp: Path


def _content_rank(path: Path) -> tuple[int, int]:
    """比较键: 分辨率面积最大者胜, 同面积取字节数大者. 尺寸读不出按 0 面积, 劣于任何可读图."""
    size = probe_size(path)
    area = size[0] * size[1] if size is not None else 0
    return area, path.stat().st_size


class ResourceStore:
    def __init__(self, engine: AsyncEngine, base_dir: Path):
        self._engine = engine
        self._base_dir = base_dir
        self._guards: dict[str, _LockEntry] = {}
        self._guards_lock = asyncio.Lock()
        self._heal_lock = asyncio.Lock()
        """自愈与落库 (覆盖提交 / 就地超分) 互斥: 覆盖的"挂标记 → 换文件 → 落记录"整段持锁,
        自愈整段持锁.

        读路径的自愈因此撞不上在飞提交: 标记在库而盘上仍是旧字节这一窗口, 提交正持锁,
        自愈等提交走完再看盘上字节 —— 标记要么已被提交撤掉, 要么提交确实夭折, 两种都不是
        "在飞的标记". 锁序固定为「locator 锁 → 自愈锁」, 自愈锁内不再取别的锁.
        """
        base_dir.mkdir(parents=True, exist_ok=True)

    def _session(self) -> AsyncSession:
        return AsyncSession(self._engine, expire_on_commit=False)

    @asynccontextmanager
    async def _serialized(self, locator: str) -> AsyncGenerator[None]:
        """按 locator 串行内容写入; 无人在用时回收条目, 长跑的进程不积累锁."""
        async with self._guards_lock:
            entry = self._guards.get(locator)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._guards[locator] = entry
            entry.waiters += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._guards_lock:
                entry.waiters -= 1
                if entry.waiters == 0:
                    del self._guards[locator]

    def _compute_path(self, url: str, ext: str | None = None) -> Path:
        h = _url_hash(url)
        suffix = ext if ext is not None else _guess_ext(url)
        if suffix and not suffix.startswith("."):
            suffix = f".{suffix}"
        return self._base_dir / h[:2] / f"{h}{suffix}"

    def _relative_path(self, full_path: Path) -> str:
        # 必须用 POSIX 分隔符; 否则 Windows 反斜杠路径与 LIKE '{h[:2]}/{h}.%' 匹配不到.
        return full_path.relative_to(self._base_dir).as_posix()

    def full_path(self, resource: Resource) -> Path:
        return self._base_dir / resource.file_path

    @property
    def data_dir(self) -> Path:
        return self._base_dir.parent

    async def resolve(self, url: str) -> Path | None:
        stmt = select(Resource).where(Resource.url == url)
        # 自愈须在锁内的独立会话做: 会话内开着读事务时, 另开会话写库会被 SQLite 锁住.
        record = await self._heal_record(stmt)
        if record is None:
            return None

        # 自愈可能改写路径 (派生资源待提交的路径与记录当前路径不同): 按自愈后的路径核对盘上字节.
        full_path = self._base_dir / record.file_path
        if not full_path.exists():
            # 文件丢失则删除记录.
            logger.warning("resource file missing, invalidating record", url=url, path=str(full_path))
            await self._invalidate_record(record.id)
            return None
        return full_path

    async def _invalidate_record(self, row_id: int | None) -> None:
        """删除一行: 行不在时无须处理."""
        if row_id is None:
            return
        async with self._session() as session:
            row = await session.get(Resource, row_id)
            if row is None:
                return
            await session.delete(row)
            await session.commit()

    async def _heal_record(self, stmt: SelectOfScalar[Resource]) -> Resource | None:
        """读一行并按盘上字节自愈: 取行与自愈在自愈锁内一次完成.

        覆盖流程中途夭折的自愈: 记录带待提交标记时, 按盘上字节把记录修正过来.

        盘上已是待提交的那份字节 (文件换新后夭折): 记录补齐 hash/size/类型. 与旧字节绑定的标记
        (超分标记) 是否失效, 取决于这份字节自身是否带有效超分: 记录记的 hash 就是它则保留,
        否则失效 —— 旧字节的超分结果不适用于新字节.
        盘上仍是旧字节 (文件尚未换新): 只撤待提交标记, 记录原样仍指盘上那份.

        自愈与落库 (覆盖提交 / 就地超分) 持同一把自愈锁, 整段互斥: 覆盖"挂标记 → 换文件 →
        落记录"期间读路径的自愈排队等待, 等到时标记已被覆盖撤除 (或在覆盖夭折后由自愈接手) ——
        在飞提交的标记不会被读路径撤掉. 锁序固定为「locator 锁 → 自愈锁」, 自愈锁内不再取别的锁.

        取行放在锁内: 取行与自愈之间不会插入一整次提交落地 (提交持同一把锁), 调用方拿到的就是
        库里的当前行 —— 用旧 hash 发 ETag 的一次性陈旧 304 窗口因此关闭. 取行也在锁内, 读路径
        不必为自愈另开一次会话重读一行, 少一次 DB 往返.
        """
        async with self._heal_lock:
            return await self._heal_locked(stmt)

    async def _heal_locked(self, stmt: SelectOfScalar[Resource]) -> Resource | None:
        """自愈临界区: 持自愈锁, 按语句取行并自愈.

        判定依据是锁内读到的当前库态: 取行到自愈之间若有新的提交挂上标记, 按先前的快照判定会
        拿旧标记去撤新提交的标记.

        返回锁内读到的行 (有标记则已自愈). 无标记时也返回该行: 它就是库里的当前行, 不会引入
        失配, 却让调用方拿到最新库态.
        """
        async with self._session() as session:
            result = await session.exec(stmt)
            row = result.first()
            if row is None:
                return None
            meta = dict(row.meta) if row.meta else {}
            pending = _pending(meta)
            if pending is None:
                if PENDING_KEY in meta:
                    # 不可信形态的标记 (外部写坏库): 无从判定中间态, 撤掉免得永久滞留.
                    row.meta = _without_pending(meta)
                    session.add(row)
                    await session.commit()
                return row
            # 派生资源的待提交路径可能与记录当前路径不同 (扩展名变更): 按待提交路径核对盘上字节.
            rel_path = pending.path or row.file_path
            full_path = self._base_dir / rel_path
            try:
                disk_hash = self._hash_file(full_path)
            except OSError:
                # 待提交路径读不出 (文件尚未移入): 按记录当前路径判定, 那份仍是盘上字节.
                full_path = self._base_dir / row.file_path
                try:
                    disk_hash = self._hash_file(full_path)
                except OSError:
                    return None
                rel_path = row.file_path

            if disk_hash == pending.content_hash:
                size = full_path.stat().st_size
                mime = detect_media_type(full_path)
                if pending.meta is not None:
                    # 派生资源: 待写入的 meta (含源版本标记) 一并写回 ——
                    # 盘上字节与其源版本须同批落库, 否则版本标记描述的是另一份源.
                    row.meta = {k: v for k, v in pending.meta.items() if k != PENDING_KEY}
                else:
                    # 超分标记属不属于这份字节: 记录自己记的 hash 就是它时才保留, 否则失效 ——
                    # 旧字节的超分结果不适用于新字节, 而这份字节自带的超分结果仍然有效.
                    keep_sr = row.content_hash == disk_hash
                    row.meta = {k: v for k, v in meta.items() if k != PENDING_KEY and not (k == "sr" and not keep_sr)}
                row.file_path = rel_path
                row.content_hash = disk_hash
                row.size = size
                row.mime_type = mime
                session.add(row)
                await session.commit()
                logger.warning(
                    "resource record healed after interrupted overwrite", url=row.url, content_hash=disk_hash
                )
                return row

            # 文件尚未换新: 撤标记, 记录仍指盘上那份字节. 只清 pending 一个键, 不动其余列 ——
            # 整列写回会用陈旧快照覆盖别的列.
            row.meta = _without_pending(meta)
            session.add(row)
            await session.commit()
            return row

    async def acquire(
        self,
        url: str,
        client: WebClient,
        *,
        egress: EgressPolicy | None = None,
        max_bytes: int | None = None,
    ) -> Path | None:
        """``egress`` / ``max_bytes`` 原样交给下载通道: URL 来自第三方数据时由调用方给出策略."""
        # 内部 URL 是对既有 Resource 的引用: 直接解析本地文件, 不下载也不写记录.
        internal = internal_url_hash(url)
        if internal is not None:
            found = await self.get_by_url_hash(internal)
            return found[1] if found else None

        # 同一 URL 的内容写入串行: 一起到达的调用都判缓存未命中, 若各自下载并改名到同一路径,
        # 后写会覆盖先写 (含先写方刚做完的就地超分), 而先写方的记录仍然作数.
        async with self._serialized(url):
            return await self._acquire_locked(url, client, egress=egress, max_bytes=max_bytes)

    async def _acquire_locked(
        self,
        url: str,
        client: WebClient,
        *,
        egress: EgressPolicy | None,
        max_bytes: int | None,
    ) -> Path | None:
        # 缓存命中且文件存在则直出. 判据是记录 + 文件存在, 不看哈希: 超分是就地覆盖, 哈希每次都会变.
        cached = await self.resolve(url)
        if cached:
            return cached

        # 上游改派占位图时跳过, 不占用 dest 也不写记录.
        final_url = await self._placeholder_final_url(url, client, egress=egress)
        if final_url is not None:
            logger.warning("resource is upstream placeholder", url=url, final_url=final_url)
            return None

        dest = self._compute_path(url)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # 下载. 失败可能是站点拦下指纹 (403/406, 见 net/http.py 的轮换), 也可能是上游本来就没有图;
        # 两种失败都不写资源记录, 这里给出资源层的告警, 与上游的请求级日志区分开.
        #
        # 临时文件按调用命名, 分块下载失败留下的残片只落在其中; 成功后原子改名到 dest, 失败只清理
        # 临时文件, 不触碰 dest (dest 只由 URL 决定, 而这里已按 URL 串行, 改名不会被并发写覆盖).
        # 临时文件保留原扩展名: download 的实现可能按扩展名决定写出的格式.
        tmp = dest.with_name(f"{dest.stem}.{uuid4().hex}.part{dest.suffix}")
        try:
            if not await client.download(url, tmp, egress=egress, max_bytes=max_bytes):
                logger.warning("resource download failed", url=url, host=urlsplit(url).hostname)
                return None
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

        size = dest.stat().st_size
        mime = detect_media_type(dest)
        content_hash = self._hash_file(dest)

        # 写入记录.
        async with self._session() as session:
            record = Resource(
                url=url,
                file_path=self._relative_path(dest),
                content_hash=content_hash,
                size=size,
                mime_type=mime,
            )
            session.add(record)
            await session.commit()

        return dest

    async def _placeholder_final_url(
        self, url: str, client: WebClient, *, egress: EgressPolicy | None = None
    ) -> str | None:
        """上游改派占位图时返回重定向终址; 否则返回 None.

        两条下载路径 (``acquire`` 与 quality 的独占临时) 共用此判定, 口径一致.
        """
        final_url = await client.resolve_final_url(url, egress=egress)
        return final_url if _is_placeholder_url(final_url) else None

    async def resolve_source(self, url: str, client: WebClient) -> ResolvedSource | None:
        """处理入口的源解析: 内部 URL 定位既有 Resource, 外部仅接受 http(s), 其余返回 None."""
        internal = internal_url_hash(url)
        if internal is not None:
            found = await self.get_by_url_hash(internal)
            if found is None:
                return None
            return ResolvedSource(locator=found[0].url, path=found[1])

        if not url.startswith(("http://", "https://")):
            return None
        path = await self.acquire(url, client)
        if path is None:
            return None
        return ResolvedSource(locator=url, path=path)

    async def get_by_url(self, url: str) -> Resource | None:
        stmt = select(Resource).where(Resource.url == url)
        return await self._heal_record(stmt)

    async def list_all(self) -> list[Resource]:
        async with self._session() as session:
            result = await session.exec(select(Resource))
            return list(result.all())

    async def get_by_url_hash(self, url_hash: str) -> tuple[Resource, Path] | None:
        # 文件名即 _url_hash(url); 按前缀匹配定位, 无需在 DB 存 hash 列.
        pattern = f"{url_hash[:2]}/{url_hash}.%"
        stmt = select(Resource).where(col(Resource.file_path).like(pattern))
        record = await self._heal_record(stmt)
        if record is None:
            return None
        # 自愈可能改写路径 (派生资源待提交的路径与记录当前路径不同): 按自愈后的路径返回.
        full_path = self._base_dir / record.file_path
        if not full_path.exists():
            return None
        return record, full_path

    @staticmethod
    def url_hash(url: str) -> str:
        return _url_hash(url)

    @asynccontextmanager
    async def source_snapshot(self, src_path: Path) -> AsyncGenerator[SourceSnapshot | None]:
        """源内容快照: 源的独占副本, 供检测 / 裁剪 / 版本标记共用同一份字节.

        副本与源同目录, 复制是原子的读: 源被改名换新时, 副本要么是换新前的那份, 要么是换新后的
        那份, 不会取到半截内容. 副本在离开上下文时删除; 派生记录只记版本 hash, 不引用副本路径,
        因此不存在「副本已删而记录仍指向它」的悬空引用.

        复制失败时给出 None: 调用方据此放弃本轮派生. 降级为「直接用源文件」会让检测与裁剪之间
        源被换新时混用两个版本, 产出「源 A 的检测框套在源 B 上」的错配产物, 而源版本标记记的是
        当时盘上的那份 —— 错配产物配一个看似自洽的版本标记. 磁盘瞬时错误时宁可这一轮不出派生图.
        """
        tmp = src_path.with_name(f"{src_path.stem}{TMP_MARK}snap-{uuid4().hex[:8]}{src_path.suffix}")
        try:
            shutil.copyfile(src_path, tmp)
        except OSError:
            tmp.unlink(missing_ok=True)
            logger.warning("source snapshot failed", path=str(src_path))
            yield None
            return
        try:
            yield SourceSnapshot(path=tmp, version=self._hash_file(tmp))
        finally:
            tmp.unlink(missing_ok=True)

    async def acquire_derived(
        self,
        src_url: str,
        op: str,
        args: str,
        producer: Callable[[Path], Awaitable[bool]],
        *,
        ext: str = ".jpg",
        src_snapshot: SourceSnapshot | None = None,
    ) -> Resource | None:
        """同 (src_url, op, args) 命中已有记录且磁盘仍是记录的那份字节则直出. producer 失败返回 None.

        命中还须核对源内容版本: 源图内容更新后派生产物须重跑, 否则派生缓存复用的是旧源的裁剪结果.

        ``src_snapshot`` 给定时源版本取快照字节: 命中核对与版本标记都描述同一份源内容, 派生产物
        与其版本标记因此不会混用两个版本的源.
        """
        locator = derived_locator(src_url, op, args)
        dest = self._compute_path(locator, ext=ext)
        async with self._serialized(locator):
            existing = await self.get_by_url(locator)
            src_version = await self._source_version(src_url, snapshot=src_snapshot)
            if (
                existing is not None
                and self._matches_record(self.full_path(existing), existing)
                and self._source_version_matches(existing, src_version)
            ):
                return existing

            dest.parent.mkdir(parents=True, exist_ok=True)

            # producer 写到独占临时, 不直接写最终路径: 最终路径的字节变更与记录落库之间有窗口,
            # 直接写入时窗口内夭折会留下「盘上是新字节、记录仍是旧 hash」的失配, 读路径无从自愈.
            tmp = dest.with_name(f"{dest.stem}{TMP_MARK}derived-{uuid4().hex[:8]}{dest.suffix}")
            try:
                ok = await producer(tmp)
                if not ok or not tmp.exists():
                    logger.warning("derived producer failed", src=src_url, op=op, args=args)
                    return None

                size = tmp.stat().st_size
                mime = detect_media_type(tmp)
                content_hash = self._hash_file(tmp)
                meta = {"op": op, "src": src_url, "args": args}
                if src_version is not None:
                    meta[SRC_VERSION_KEY] = src_version

                return await self._commit_derived(locator, dest, tmp, existing, content_hash, size, mime, meta)
            finally:
                tmp.unlink(missing_ok=True)

    async def _commit_derived(
        self,
        locator: str,
        dest: Path,
        tmp: Path,
        existing: Resource | None,
        content_hash: str,
        size: int,
        mime: str | None,
        meta: dict,
    ) -> Resource | None:
        """派生产物的可恢复提交: 挂待提交标记 → 原子移入 → 落库撤标记. 磁盘错误返回 None.

        派生 locator 由 ``(src_url, op, args)`` 决定, 必然存在: 既有记录要覆盖, 首次入库则无记录可标记,
        记录缺失即无失配可言.

        「挂标记 → 换文件 → 落记录」整段持自愈锁: 读路径的自愈此时排队, 等到时标记已被撤除
        (或在提交夭折后由自愈接手). 少了这段互斥, 读路径会在「标记在库、盘上仍是旧字节」的窗口撤掉
        尚未提交的标记, 提交随后夭折就留下永久失配.
        """
        async with self._heal_lock:
            if existing is not None:
                await self._mark_pending_derived(existing, content_hash, meta, self._relative_path(dest))

            try:
                tmp.replace(dest)
            except OSError:
                if existing is not None:
                    await self._clear_pending(locator)
                logger.warning("derived commit failed", locator=locator)
                return None

            async with self._session() as session:
                if existing is not None:
                    # 重新读行再改: ``existing`` 是落库前的旧副本, 直接改它会把待提交标记一并覆盖回去
                    # (meta 整列写), 留下「标记还在」的假中间态.
                    row = await session.get(Resource, existing.id)
                    if row is None:
                        row = existing
                        session.add(row)
                    row.file_path = self._relative_path(dest)
                    row.content_hash = content_hash
                    row.size = size
                    row.mime_type = mime
                    row.meta = meta
                else:
                    row = Resource(
                        url=locator,
                        file_path=self._relative_path(dest),
                        content_hash=content_hash,
                        size=size,
                        mime_type=mime,
                        meta=meta,
                    )
                    session.add(row)
                await session.commit()
                return row

    async def _source_version(self, src_url: str, *, snapshot: SourceSnapshot | None = None) -> str | None:
        """源内容版本: 源文件盘上字节的 hash; 源无记录或文件缺失时返回 None (无从核对).

        按盘上字节算, 不信记录列: 源字节被换掉而记录未更新时 (覆盖中途夭折等),
        盘上那份才是派生实际面对的输入.

        源以内部 URL (``/api/resources/{hash}``) 形式给出时先解析回底层 locator ——
        Resource 行的 ``url`` 存的是底层 locator, 按内部 URL 直查查不到行, 源版本就无从核对.

        ``snapshot`` 给定时版本取快照字节: 快照是派生实际面对的那份源内容, 版本须描述它.
        重读盘上字节会取到另一版本 (检测与裁剪之间的源换新), 那样版本标记与产物就混用了两个版本.
        """
        if not src_url:
            return None
        source = await self._source_record(src_url)
        if source is None:
            return None
        if snapshot is not None:
            return snapshot.version
        path = self.full_path(source)
        if not path.exists():
            return None
        try:
            return self._hash_file(path)
        except OSError:
            return None

    async def _source_record(self, src_url: str) -> Resource | None:
        """源的 Resource 行: 内部 URL 解析为底层 locator 再查, 外部 URL 按 locator 直查."""
        internal = internal_url_hash(src_url)
        if internal is not None:
            found = await self.get_by_url_hash(internal)
            return found[0] if found is not None else None
        return await self.get_by_url(src_url)

    @staticmethod
    def _source_version_matches(record: Resource, src_version: str | None) -> bool:
        """源版本核对: 记了版本就必须与当前源版本相同.

        源无版本 (无源记录, 无从核对) 时不阻命中 —— 不据猜测判定源已变.
        存量派生记录没有版本标记而源有版本时按不符处理: 重跑一次即补上标记,
        此后源不变即直出.
        """
        if src_version is None:
            return True
        recorded = record.meta.get(SRC_VERSION_KEY) if record.meta else None
        return recorded == src_version

    async def upscale_in_place(
        self,
        resource: Resource,
        sr_args: dict,
        producer: Callable[[Path, Path], Awaitable[bool]],
    ) -> UpscaleOutcome:
        """``sr`` 标记的判据是该标记所指的那份字节仍在盘上; 磁盘已被换掉时按未超分处理, 重跑后重新
        记录 — 标记与内容因此不会永久不一致.
        """
        async with self._serialized(resource.url):
            # 调用方的 Resource 可能已过期 (并发调用刚改过), 标记与哈希一律按锁内重读的行判定.
            async with self._session() as session:
                result = await session.exec(select(Resource).where(Resource.url == resource.url))
                record = result.first()
            if record is None:
                return UpscaleOutcome.SKIPPED
            src = self._base_dir / record.file_path
            if not src.exists():
                logger.warning("upscale source missing", url=record.url, path=str(src))
                return UpscaleOutcome.SKIPPED

            # 临时文件按调用命名: 同一路径的并发超分不共用半截文件.
            tmp = src.with_name(f"{src.stem}.{uuid4().hex}.sr_tmp{src.suffix}")
            try:
                if record.meta and "sr" in record.meta and self._matches_record(src, record):
                    return UpscaleOutcome.SKIPPED
                ok = await producer(src, tmp)
                if not ok or not tmp.exists():
                    logger.warning("upscale producer failed", url=record.url)
                    return UpscaleOutcome.FAILED
                tmp.replace(src)
            except Exception as exc:
                logger.warning("upscale in place error", url=record.url, error=str(exc))
                return UpscaleOutcome.FAILED
            finally:
                tmp.unlink(missing_ok=True)

            # 原地替换后重新量取, 记入的就是盘上那份. 输出格式由超分配置决定, 媒体类型须一并重算.
            size = src.stat().st_size
            content_hash = self._hash_file(src)
            mime = detect_media_type(src)
            new_meta = dict(record.meta) if record.meta else {}
            new_meta["sr"] = sr_args

            # 落库持自愈锁: 与读路径的自愈互斥, 免得自愈用陈旧快照覆盖这次落库.
            async with self._heal_lock, self._session() as session:
                result = await session.exec(select(Resource).where(Resource.url == record.url))
                row = result.first()
                if row is None:
                    return UpscaleOutcome.SKIPPED
                row.size = size
                row.content_hash = content_hash
                row.mime_type = mime
                row.meta = new_meta
                session.add(row)
                await session.commit()
            return UpscaleOutcome.UPSCALED

    async def acquire_first(self, urls: list[str], client: WebClient) -> AcquireResult:
        if not urls:
            return AcquireResult(success=False, failed=[])

        # 逐 URL 尝试, 成功即停.
        failed: list[str] = []
        for url in urls:
            path = await self.acquire(url, client)
            if path:
                return AcquireResult(success=True, path=path, used_url=url, failed=failed)
            failed.append(url)
            logger.debug("download source failed, trying next", url=url)

        logger.warning("all download sources exhausted", urls_tried=len(urls))
        return AcquireResult(success=False, path=None, used_url=None, failed=failed)

    async def acquire_best(self, urls: list[str], client: WebClient, *, task_token: str) -> AcquireResult:
        """quality 策略: 各候选落到本轮独占临时 (命中共享缓存则复制参与, 共享文件只读), 面积→字节
        择优, 胜者经 ``commit_temp`` 绑定已比较的字节提交.

        清理责任全在本轮: 落选临时与取消/异常路径的全部临时由本轮删除; 共享 Resource 绝不删除.
        """
        if not urls:
            return AcquireResult(success=False, failed=[])

        temps: list[Path] = []
        failed: list[str] = []
        try:
            candidates: list[_BestCandidate] = []
            # 去重保序: 同一 URL 的两份临时会互相覆盖.
            for url in dict.fromkeys(urls):
                temp = await self._exclusive_download(url, client, task_token, temps)
                if temp is None:
                    failed.append(url)
                    continue
                candidates.append(_BestCandidate(url=url, temp=temp))
            if not candidates:
                logger.warning("all download sources exhausted", urls_tried=len(urls))
                return AcquireResult(success=False, failed=failed)

            winner = max(candidates, key=lambda c: _content_rank(c.temp))
            result = await self.commit_temp(winner.url, winner.temp)
            if result.success:
                return AcquireResult(success=True, path=result.path, used_url=winner.url, failed=failed)
            return AcquireResult(success=False, failed=[*failed, winner.url])
        finally:
            for temp in temps:
                temp.unlink(missing_ok=True)

    async def _exclusive_download(self, url: str, client: WebClient, task_token: str, temps: list[Path]) -> Path | None:
        """单候选落到本轮独占临时: 命中共享缓存复制参与 (共享文件只读), 未命中直接下载.

        独占临时不写记录也不进缓存. 单候选异常按该候选失败处理, 择优轮继续; ``CancelledError``
        是 ``BaseException``, 不在此拦, 由调用方的 finally 清理后原样上抛.
        """
        temp = self._exclusive_tmp(url, task_token)
        temps.append(temp)
        try:
            cached = await self.resolve(url)
            if cached is not None:
                shutil.copyfile(cached, temp)
                return temp
            # 上游改派占位图时按该候选失败处理: 不下载, 临时不落字节.
            final_url = await self._placeholder_final_url(url, client)
            if final_url is not None:
                logger.warning("candidate is upstream placeholder", url=url, final_url=final_url)
                return None
            temp.parent.mkdir(parents=True, exist_ok=True)
            if not await client.download(url, temp):
                logger.warning("candidate download failed", url=url, host=urlsplit(url).hostname)
                return None
            return temp
        except Exception as exc:
            logger.warning("candidate acquire failed", url=url, error=str(exc))
            return None

    def _exclusive_tmp(self, url: str, task_token: str) -> Path:
        """本轮独占临时名: 与缓存目标同目录 (同文件系统 → 提交改名原子), 保留扩展名
        (download 的实现可能按扩展名决定写出的格式); 加 uuid 段保证同任务并发轮不共用文件.
        """
        dest = self._compute_path(url)
        return dest.with_name(f"{dest.stem}{TMP_MARK}{task_token}-{uuid4().hex[:8]}{dest.suffix}")

    async def commit_temp(self, url: str, temp: Path) -> AcquireResult:
        """绑定**已比较的字节**原子提交: 按 url_hash 定位缓存路径, 原子改名并写记录;
        禁止重新下载替代提交.

        并发内容冲突 (提交时该 URL 已有内容 hash 不同的缓存) 把既有内容纳入比较重选:
        既有胜 → 删本轮临时返回既有路径; 本轮胜 → 覆盖缓存 (记录与文件同步更新) 并记 warning;
        同内容 hash → 幂等成功, 异内容冲突双方同面积同字节时同样保既有. 提交失败 (磁盘错误,
        含既有缓存文件读失败) 删本轮临时返回 FAILED.

        记录在而文件缺失视作无既有内容: 直接覆盖并更新既有记录, 不落重复行.
        """
        async with self._serialized(url):
            return await self._commit_temp_locked(url, temp)

    async def _commit_temp_locked(self, url: str, temp: Path) -> AcquireResult:
        dest = self._compute_path(url)
        overwrite = False
        try:
            temp_hash = self._hash_file(temp)
            existing = await self.get_by_url(url)
            existing_path = self.full_path(existing) if existing is not None else None
            if existing_path is not None and existing_path.exists():
                # 内容判定按盘上字节, 不信记录列: 磁盘被外部换掉时记录可能已过期.
                existing_hash = self._hash_file(existing_path)
                if existing_hash == temp_hash:
                    temp.unlink(missing_ok=True)
                    return AcquireResult(success=True, path=existing_path, used_url=url)
                if _content_rank(existing_path) >= _content_rank(temp):
                    # 既有不劣于本轮 (含同面积同字节的并列): 本轮落选, 只删本轮临时.
                    temp.unlink(missing_ok=True)
                    return AcquireResult(success=True, path=existing_path, used_url=url)
                overwrite = True

            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            temp.unlink(missing_ok=True)
            logger.warning("commit temp failed", url=url)
            return AcquireResult(success=False, failed=[url])

        # 覆盖既有缓存时先挂待提交标记: 文件换新与记录落库之间若被取消或进程退出,
        # 读路径据此按盘上字节自愈, 不会留下"记录 hash ≠ 盘上字节"的持久失配.
        # 首次入库无既有记录可标记, 记录缺失即无失配可言.
        #
        # 挂标记 → 换文件 → 落记录整段持自愈锁: 读路径的自愈此时排队, 等到时标记已被撤除.
        # 少了这段互斥, 读路径会在"标记在库、盘上仍是旧字节"的窗口撤掉在飞提交的标记, 提交随后
        # 夭折就留下永久失配.
        async with self._heal_lock:
            if existing is not None:
                await self._mark_pending(existing, temp_hash)

            try:
                temp.replace(dest)
            except OSError:
                await self._clear_pending(url)
                temp.unlink(missing_ok=True)
                logger.warning("commit temp failed", url=url)
                return AcquireResult(success=False, failed=[url])

            # 文件已就位, 记录写盘上那份.
            size = dest.stat().st_size
            mime = detect_media_type(dest)
            async with self._session() as session:
                if existing is not None:
                    # 重新读行再改: ``existing`` 是落库前的旧副本, 直接改它会把待提交标记
                    # 一并覆盖回去 (meta 整列写), 留下"标记还在"的假中间态.
                    row = await session.get(Resource, existing.id)
                    if row is None:
                        row = existing
                        session.add(row)
                    row.content_hash = temp_hash
                    row.size = size
                    row.mime_type = mime
                    # 字节已换新: 与旧字节绑定的标记 (超分标记) 一并失效 —— 旧字节的
                    # 超分结果不适用于新字节, 留着会让超分被误判为"已超分"而跳过.
                    row.meta = _without_content_bound(row.meta)
                else:
                    session.add(
                        Resource(
                            url=url,
                            file_path=self._relative_path(dest),
                            content_hash=temp_hash,
                            size=size,
                            mime_type=mime,
                        )
                    )
                await session.commit()

        if overwrite:
            logger.warning("resource cache overwritten", url=url, content_hash=temp_hash)
        return AcquireResult(success=True, path=dest, used_url=url)

    async def _mark_pending(self, record: Resource, content_hash: str) -> None:
        """在记录上挂待提交标记 (待写入的 content_hash), 供读路径识别中间态."""
        meta = dict(record.meta) if record.meta else {}
        meta[PENDING_KEY] = content_hash
        record.meta = meta
        async with self._session() as session:
            row = await session.get(Resource, record.id)
            if row is not None:
                row.meta = meta
                session.add(row)
                await session.commit()

    async def _mark_pending_derived(self, record: Resource, content_hash: str, meta: dict, path: str) -> None:
        """派生资源挂待提交标记: 待写入的 content_hash、待写入的 meta 与待写入的路径一并挂上.

        自愈时按标记里的 meta 与路径写回: 源版本标记与盘上字节同批落库, 不会出现「字节已换新、
        源版本仍是旧值」的失配.
        """
        pending_meta = dict(record.meta) if record.meta else {}
        pending_meta[PENDING_KEY] = {"hash": content_hash, "meta": meta, "path": path}
        record.meta = pending_meta
        async with self._session() as session:
            row = await session.get(Resource, record.id)
            if row is not None:
                row.meta = pending_meta
                session.add(row)
                await session.commit()

    async def _clear_pending(self, url: str) -> None:
        """撤掉待提交标记: 文件未换新时记录仍指旧字节, 不必自愈."""
        async with self._session() as session:
            result = await session.exec(select(Resource).where(Resource.url == url))
            row = result.first()
            if row is None:
                return
            row.meta = _without_pending(row.meta)
            session.add(row)
            await session.commit()

    async def acquire_extrafanart(
        self,
        urls_by_site: dict[str, list[str]],
        priority: list[str],
        client: WebClient,
    ) -> list[Path]:
        for site in priority:
            if site not in urls_by_site:
                continue
            results = await asyncio.gather(*[self.acquire(u, client) for u in urls_by_site[site]])
            paths = [p for p in results if p is not None]
            if paths:
                return paths
        return []

    async def invalidate(self, url: str) -> None:
        async with self._session() as session:
            stmt = select(Resource).where(Resource.url == url)
            result = await session.exec(stmt)
            record = result.first()
            if record is None:
                return

            full_path = self._base_dir / record.file_path
            if full_path.exists():
                full_path.unlink()

            await session.delete(record)
            await session.commit()

    async def purge_unreferenced(self, live_urls: set[str], live_hashes: set[str]) -> int:
        # 存活: url 在 live_urls, 或 url_hash 在 live_hashes (内部相对 URL /api/resources/{hash}).
        removed = 0
        for resource in await self.list_all():
            if resource.url in live_urls or _url_hash(resource.url) in live_hashes:
                continue
            await self.invalidate(resource.url)
            removed += 1
        return removed

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _matches_record(self, path: Path, record: Resource) -> bool:
        """路径上的字节是否仍是 ``record`` 记的那份."""
        if record.content_hash is None or record.size is None or not path.exists():
            return False
        if path.stat().st_size != record.size:
            return False
        return self._hash_file(path) == record.content_hash
