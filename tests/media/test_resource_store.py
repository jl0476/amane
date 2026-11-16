"""测试 ResourceStore 的派生资源 (裁剪), 就地超分, 独占临时择优与获取失败的收尾."""

import asyncio
import hashlib
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlmodel import select
from structlog.testing import capture_logs

from amane.db.models import Resource
from amane.media import ResourceStore, UpscaleOutcome, derived_locator
from amane.media.resource_store import (
    RESOURCE_URL_PREFIX,
    TMP_MARK,
    _is_placeholder_url,
    internal_url_hash,
)
from amane.net.egress import EgressPolicy
from amane.net.http import RateLimiters, WebClient


def test_derived_locator_deterministic():
    a = derived_locator("https://site/t.jpg", "crop", "0.714")
    b = derived_locator("https://site/t.jpg", "crop", "0.714")
    c = derived_locator("https://site/t.jpg", "crop", "0.666")
    d = derived_locator("https://site/other.jpg", "crop", "0.714")
    assert a == b  # 同输入恒等
    assert a != c  # 参数不同
    assert a != d  # 源不同
    assert a.startswith("derived:")


class TestAcquireDerived:
    @pytest.mark.asyncio
    async def test_generate_then_hit_cache(self, resource_store: ResourceStore):
        calls = {"n": 0}

        async def producer(dest: Path) -> bool:
            calls["n"] += 1
            dest.write_bytes(b"cropped-bytes")
            return True

        r1 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert r1 is not None
        assert r1.meta == {"op": "crop", "src": "https://s/t.jpg", "args": "0.7"}
        assert r1.size == len(b"cropped-bytes")
        assert calls["n"] == 1

        # 第二次相同参数 → 命中缓存, producer 不再调用
        r2 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert r2 is not None
        assert r2.url == r1.url
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_direct_hit_requires_record_bytes(self, resource_store: ResourceStore):
        """文件存在不等于命中: 盘上字节已不是记录那份时必须重跑 producer, 相符才直接出."""

        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"cropped-bytes")
            return True

        r1 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert r1 is not None

        # 同一路径被就地改写 (超分 / 外部改写), 文件仍在而记录未变.
        resource_store.full_path(r1).write_bytes(b"stale-other-bytes")
        calls = {"n": 0}

        async def rerun(dest: Path) -> bool:
            calls["n"] += 1
            dest.write_bytes(b"cropped-bytes")
            return True

        r2 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", rerun)
        assert calls["n"] == 1  # 字节不符 → producer 重跑
        assert r2 is not None
        assert r2.url == r1.url

        r3 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", rerun)
        assert calls["n"] == 1  # 字节相符 → 直接出
        assert r3 is not None

    @pytest.mark.asyncio
    async def test_producer_failure_returns_none(self, resource_store: ResourceStore):
        async def producer(dest: Path) -> bool:
            return False  # 不生成文件

        r = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert r is None

    @pytest.mark.asyncio
    async def test_different_args_distinct_records(self, resource_store: ResourceStore):
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"x")
            return True

        r1 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        r2 = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.6", producer)
        assert r1 is not None and r2 is not None
        assert r1.url != r2.url


class TestUpscaleInPlace:
    @pytest.mark.asyncio
    async def test_overwrites_and_marks_meta(self, resource_store: ResourceStore):
        # 先造一个派生资源作为超分目标
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"small")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None
        old_hash = res.content_hash

        async def sr_producer(src: Path, out: Path) -> bool:
            out.write_bytes(b"upscaled-much-larger-bytes")
            return True

        done = await resource_store.upscale_in_place(res, {"tool": "realesrgan", "scale": 4}, sr_producer)
        assert done is UpscaleOutcome.UPSCALED

        updated = await resource_store.get_by_url(res.url)
        assert updated is not None
        assert updated.meta is not None and "sr" in updated.meta
        assert updated.meta["op"] == "crop"  # 保留原派生信息
        assert updated.content_hash != old_hash  # 内容已变
        assert updated.size == len(b"upscaled-much-larger-bytes")

    @pytest.mark.asyncio
    async def test_skips_if_already_sr(self, resource_store: ResourceStore):
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"x")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None

        async def sr_producer(src: Path, out: Path) -> bool:
            out.write_bytes(b"sr1")
            return True

        assert await resource_store.upscale_in_place(res, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED
        updated = await resource_store.get_by_url(res.url)
        assert updated is not None
        # 已有 sr 标记 → 再次调用跳过
        assert await resource_store.upscale_in_place(updated, {"scale": 4}, sr_producer) is UpscaleOutcome.SKIPPED

    @pytest.mark.asyncio
    async def test_producer_failure_keeps_original(self, resource_store: ResourceStore):
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"original")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None

        async def failing_sr(src: Path, out: Path) -> bool:
            return False

        done = await resource_store.upscale_in_place(res, {"scale": 4}, failing_sr)
        assert done is UpscaleOutcome.FAILED
        updated = await resource_store.get_by_url(res.url)
        assert updated is not None
        assert updated.meta is not None and "sr" not in updated.meta  # 未标记

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("overwrite", "expect_rerun"),
        [
            (b"xYz", True),  # 与 sr1 等长而内容不同: 大小判据无分辨力, 必须落到哈希
            (b"far-longer-than-sr1", True),  # 大小不同
            (b"sr1", False),  # 记录所指的字节仍在盘上: 跳过
        ],
    )
    async def test_marker_follows_disk_content(
        self, resource_store: ResourceStore, overwrite: bytes, expect_rerun: bool
    ):
        """sr 标记的判据是记录的那份字节仍在本文件: 磁盘已被换掉时必须重跑, 不能永久跳过."""

        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"original-bytes")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None

        async def sr_producer(src: Path, out: Path) -> bool:
            out.write_bytes(b"sr1")
            return True

        assert await resource_store.upscale_in_place(res, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED

        # 模拟并发刮削把文件覆盖回去: 记录不变, 盘上的字节变了.
        resource_store.full_path(res).write_bytes(overwrite)

        marked = await resource_store.get_by_url(res.url)
        assert marked is not None
        rerun = await resource_store.upscale_in_place(marked, {"scale": 4}, sr_producer)

        assert (rerun is UpscaleOutcome.UPSCALED) is expect_rerun
        final = await resource_store.get_by_url(res.url)
        assert final is not None
        assert resource_store.full_path(final).read_bytes() == (b"sr1" if expect_rerun else overwrite)
        assert final.content_hash == ResourceStore._hash_file(resource_store.full_path(final))
        assert final.size == resource_store.full_path(final).stat().st_size

    @pytest.mark.asyncio
    async def test_decision_reads_record_not_caller_copy(self, resource_store: ResourceStore):
        """跳过判据按锁内重读的记录判定: 调用方手上那份副本过期时不得重复超分."""

        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"original-bytes")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None
        assert res.meta is not None and "sr" not in res.meta  # 副本读在超分之前

        async def sr_producer(src: Path, out: Path) -> bool:
            out.write_bytes(b"sr-bytes")
            return True

        assert await resource_store.upscale_in_place(res, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED

        ran: list[bytes] = []

        async def second_sr(src: Path, out: Path) -> bool:
            ran.append(src.read_bytes())
            out.write_bytes(b"sr-again")
            return True

        # 仍拿着同一份过期副本: 记录已是超分结果且磁盘相符, 必须跳过.
        assert await resource_store.upscale_in_place(res, {"scale": 4}, second_sr) is UpscaleOutcome.SKIPPED
        assert ran == []
        after = await resource_store.get_by_url(res.url)
        assert after is not None
        assert resource_store.full_path(after).read_bytes() == b"sr-bytes"


class TestGetByUrlHash:
    @pytest.mark.asyncio
    async def test_serve_lookup(self, resource_store: ResourceStore):
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"img")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None

        h = ResourceStore.url_hash(res.url)
        found = await resource_store.get_by_url_hash(h)
        assert found is not None
        record, path = found
        assert record.url == res.url
        assert path.exists()

    @pytest.mark.asyncio
    async def test_missing_hash_returns_none(self, resource_store: ResourceStore):
        assert await resource_store.get_by_url_hash("deadbeefdeadbeef") is None


class TestInternalUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("/api/resources/0123456789abcdef", "0123456789abcdef"),
            ("/api/resources/0123456789abcdef?width=100", "0123456789abcdef"),
            ("/api/resources/0123456789abcdef/extra", "0123456789abcdef"),
            ("/api/resources/", None),
            ("/api/resource/0123456789abcdef", None),
            ("https://site/img.jpg", None),
            ("", None),
        ],
    )
    def test_parse(self, url: str, expected: str | None):
        assert internal_url_hash(url) == expected

    @pytest.mark.asyncio
    async def test_acquire_resolves_without_request(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"derived-bytes")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "box:0,0,10,10", producer)
        assert res is not None
        client, session = _stub_client(monkeypatch, final_url="https://s/t.jpg")
        internal = f"{RESOURCE_URL_PREFIX}/{ResourceStore.url_hash(res.url)}"

        path = await resource_store.acquire(internal, client)

        assert path == resource_store.full_path(res)
        assert session.methods == []  # 内部 URL 不发请求
        assert await resource_store.get_by_url(internal) is None  # 也不写记录

    @pytest.mark.asyncio
    async def test_acquire_internal_missing_returns_none(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        client, session = _stub_client(monkeypatch, final_url="https://s/t.jpg")
        assert await resource_store.acquire(f"{RESOURCE_URL_PREFIX}/deadbeefdeadbeef", client) is None
        assert session.methods == []


class TestResolveSource:
    @pytest.mark.asyncio
    async def test_internal_returns_locator_and_path(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        async def producer(dest: Path) -> bool:
            dest.write_bytes(b"x")
            return True

        res = await resource_store.acquire_derived("https://s/t.jpg", "crop", "0.7", producer)
        assert res is not None
        client, session = _stub_client(monkeypatch, final_url="https://s/t.jpg")
        internal = f"{RESOURCE_URL_PREFIX}/{ResourceStore.url_hash(res.url)}"

        src = await resource_store.resolve_source(internal, client)

        assert src is not None
        assert src.locator == res.url  # 底层 locator, 而非内部 URL
        assert src.path == resource_store.full_path(res)
        assert session.methods == []

    @pytest.mark.asyncio
    async def test_external_downloads(self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
        client, session = _stub_client(monkeypatch, final_url=_REQUEST, content=b"jpeg-bytes")

        src = await resource_store.resolve_source(_REQUEST, client)

        assert src is not None
        assert src.locator == _REQUEST
        assert src.path.read_bytes() == b"jpeg-bytes"
        assert session.methods[0] == "HEAD"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "ftp://host/img.jpg", "img.jpg", ""])
    async def test_rejects_other_schemes_without_request(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch, url: str
    ):
        client, session = _stub_client(monkeypatch, final_url="https://s/t.jpg")
        assert await resource_store.resolve_source(url, client) is None
        assert session.methods == []


class _StubResponse:
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, *, url: str, content: bytes = b"", status: int = 200) -> None:
        self.url = url
        self.content = content
        self.status_code = status
        self.quit_now = None

    async def aclose(self) -> None:
        return


class _StubSession:
    """替换 ``WebClient._session``: 按方法返回预设应答, 记录出站方法."""

    def __init__(self, *, final_url: str, content: bytes = b"", statuses: dict[str, int] | None = None) -> None:
        self.methods: list[str] = []
        self._final_url = final_url
        self._content = content
        self._statuses = statuses or {}

    async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
        self.methods.append(method)
        return _StubResponse(url=self._final_url, content=self._content, status=self._statuses.get(method, 200))


def _stub_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_url: str,
    content: bytes = b"",
    statuses: dict[str, int] | None = None,
) -> tuple[WebClient, _StubSession]:
    client = WebClient(limiters=RateLimiters(default_rate=100))
    session = _StubSession(final_url=final_url, content=content, statuses=statuses)
    monkeypatch.setattr(client, "_session", session)
    return client, session


@pytest.mark.asyncio
async def test_acquire_forwards_egress_policy(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """策略与上限必须原样到下载通道: 漏传等于端点上的限制静默失效."""
    policy = EgressPolicy()
    client = AsyncMock()
    client.resolve_final_url = AsyncMock(return_value=_REQUEST)
    client.download = AsyncMock(return_value=False)
    monkeypatch.setattr(resource_store, "_compute_path", lambda url: tmp_path / "a.jpg")

    assert await resource_store.acquire(_REQUEST, client, egress=policy, max_bytes=1024) is None

    client.resolve_final_url.assert_awaited_once_with(_REQUEST, egress=policy)
    kwargs = client.download.await_args.kwargs
    assert kwargs["egress"] is policy
    assert kwargs["max_bytes"] == 1024


@pytest.mark.asyncio
async def test_acquire_records_content_type(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """入库按内容写 MIME: 无后缀 URL 的图片不再记成通用二进制."""
    url = "https://img.example.com/actor/12345"
    client = AsyncMock()
    client.resolve_final_url = AsyncMock(return_value=url)

    async def download(_url: str, dest: Path, **_kwargs: object) -> bool:
        dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        return True

    client.download = AsyncMock(side_effect=download)
    monkeypatch.setattr(resource_store, "_compute_path", lambda url: tmp_path / "resources" / "resource.bin")

    assert await resource_store.acquire(url, client) is not None
    record = await resource_store.get_by_url(url)
    assert record is not None
    assert record.mime_type == "image/png"


@pytest.mark.asyncio
async def test_derived_mime_follows_content(resource_store: ResourceStore):
    """派生资源同样按内容写类型; 盘上字节被换掉后重跑并覆盖记录."""
    url = "https://s/derived.jpg"

    async def png(dest: Path) -> bool:
        dest.write_bytes(b"\x89PNG\r\n\x1a\n")
        return True

    first = await resource_store.acquire_derived(url, "crop", "0.7", png)
    assert first is not None
    assert first.mime_type == "image/png"

    # 盘上字节不再是记录所指的那份, 下一次调用必须重跑 producer.
    resource_store.full_path(first).write_bytes(b"stale")

    async def jpeg(dest: Path) -> bool:
        dest.write_bytes(b"\xff\xd8\xff\xe0")
        return True

    second = await resource_store.acquire_derived(url, "crop", "0.7", jpeg)
    assert second is not None
    saved = await resource_store.get_by_url(first.url)
    assert saved is not None
    assert saved.mime_type == "image/jpeg"


_REQUEST = "https://pics.dmm.co.jp/digital/video/mide00030/mide00030pl.jpg"
_PLACEHOLDER = "https://pics.dmm.com/mono/movie/n/now_printing/now_printing.jpg"


class TestPlaceholderRedirect:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (_PLACEHOLDER, True),
            ("https://imgsrc.dmm.com/pics/mono/movie/n/now_printing/now_printing.jpg?h=800&w=800", True),
            ("https://cdn.up-timely.com/noimage.png", True),
            (_REQUEST, False),
            # 只匹配路径, 查询串里的同名字样不算
            ("https://pics.dmm.com/redirect?to=/now_printing/now_printing.jpg", False),
        ],
    )
    def test_detects_placeholder_path(self, url: str, expected: bool):
        assert _is_placeholder_url(url) is expected

    @pytest.mark.asyncio
    async def test_acquire_rejects_placeholder_redirect(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        client, session = _stub_client(monkeypatch, final_url=_PLACEHOLDER, content=b"placeholder-bytes")

        assert await resource_store.acquire(_REQUEST, client) is None
        assert session.methods == ["HEAD"]  # 只探测, 不下载
        assert await resource_store.get_by_url(_REQUEST) is None

    @pytest.mark.asyncio
    async def test_acquire_records_downloaded_size(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        """普通重定向仍下载; 记录写回的 size 就是盘上这份字节的字节数."""
        client, session = _stub_client(monkeypatch, final_url=_REQUEST, content=b"jpeg-bytes")

        path = await resource_store.acquire(_REQUEST, client)

        assert path is not None and path.read_bytes() == b"jpeg-bytes"
        assert session.methods[0] == "HEAD" and "GET" in session.methods
        record = await resource_store.get_by_url(_REQUEST)
        assert record is not None
        assert record.size == resource_store.full_path(record).stat().st_size


def _siblings(dest: Path) -> list[Path]:
    """dest 所在目录里除 dest 之外的文件; 用于断言下载的临时文件已清理."""
    if not dest.parent.exists():
        return []
    return sorted(p for p in dest.parent.iterdir() if p != dest)


class TestAcquireFailure:
    """获取失败既可能是站点拦下指纹, 也可能是上游没有该图; 两种失败都不写资源记录, 也不留残缺文件."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [403, 404, 500])
    async def test_failed_download_keeps_no_trace(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch, status: int
    ):
        client, session = _stub_client(monkeypatch, final_url=_REQUEST, statuses={"GET": status})
        dest = resource_store._compute_path(_REQUEST)

        assert await resource_store.acquire(_REQUEST, client) is None

        assert not dest.exists()
        assert _siblings(dest) == []  # 下载用的临时文件也已清理
        assert await resource_store.get_by_url(_REQUEST) is None
        assert session.methods[0] == "HEAD" and "GET" in session.methods

    @pytest.mark.asyncio
    async def test_partial_download_is_removed(self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
        """分块下载先按 Content-Length 建文件, 失败会留下只写入一部分的文件: 残片必须落在临时文件上并被清理."""
        client, _ = _stub_client(monkeypatch, final_url=_REQUEST)
        dest = resource_store._compute_path(_REQUEST)
        written: list[Path] = []

        async def partial_download(url: str, target: Path, **kwargs: Any) -> bool:
            written.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"half")  # 只写入一部分
            return False

        monkeypatch.setattr(client, "download", partial_download)

        assert await resource_store.acquire(_REQUEST, client) is None

        assert written and written[0] != dest  # 不就地写 dest
        assert written[0].parent == dest.parent  # 同目录, 改名才是原子的
        assert written[0].suffix == dest.suffix  # 保留扩展名: download 的实现可能按扩展名推断格式
        assert not written[0].exists()  # 残片已清理
        assert not dest.exists()
        assert _siblings(dest) == []

    @pytest.mark.asyncio
    async def test_failure_keeps_existing_file(self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
        """dest 可能已被同一 URL 的另一个调用写好, 失败方不得删掉它."""
        dest = resource_store._compute_path(_REQUEST)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"complete-bytes")
        client, _ = _stub_client(monkeypatch, final_url=_REQUEST, statuses={"GET": 403})

        assert await resource_store.acquire(_REQUEST, client) is None

        assert dest.read_bytes() == b"complete-bytes"


class TestAcquireConcurrency:
    @pytest.mark.asyncio
    async def test_failed_call_leaves_successful_call_intact(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        """并发获取同一 URL: 失败方既不留残缺文件, 也不动成功方写好的文件与记录.

        同一 URL 的写入按 locator 串行, 因此两次调用只有先后两种次序, 都要求失败方无痕.
        """
        released = asyncio.Event()

        class GatedSession(_StubSession):
            """成功方的 GET 停在闸门上, 让失败方先走完. 停住时缓存里还没有这条记录."""

            async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
                if method == "GET":
                    await released.wait()
                return await super().request(method, url, **kwargs)

        ok_client = WebClient(limiters=RateLimiters(default_rate=100))
        monkeypatch.setattr(ok_client, "_session", GatedSession(final_url=_REQUEST, content=b"jpeg-bytes"))
        fail_client, _ = _stub_client(monkeypatch, final_url=_REQUEST, statuses={"GET": 403})

        failing = asyncio.create_task(resource_store.acquire(_REQUEST, fail_client))
        succeeding = asyncio.create_task(resource_store.acquire(_REQUEST, ok_client))
        try:
            released.set()
            path = await succeeding
            assert path is not None and path.read_bytes() == b"jpeg-bytes"
            assert await failing is None  # 成功方写完后, 失败方直接拿到该文件
            record = await resource_store.get_by_url(_REQUEST)
            assert record is not None and resource_store.full_path(record) == path
            assert record.content_hash == ResourceStore._hash_file(path)
        finally:
            released.set()
            await asyncio.gather(failing, succeeding, return_exceptions=True)
            await resource_store.invalidate(_REQUEST)

    @pytest.mark.asyncio
    async def test_second_caller_rechecks_cache_after_first(
        self, resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
    ):
        """两方同时越过缓存判据: 先进入者落盘落库后, 后来者必须重判并命中, 不得再下一次."""
        released = asyncio.Event()
        started = asyncio.Event()
        downloads: list[str] = []

        class GatedSession(_StubSession):
            async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
                if method == "GET":
                    downloads.append(url)
                    started.set()
                    await released.wait()
                return await super().request(method, url, **kwargs)

        client = WebClient(limiters=RateLimiters(default_rate=100))
        monkeypatch.setattr(client, "_session", GatedSession(final_url=_REQUEST, content=b"jpeg-bytes"))

        first = asyncio.create_task(resource_store.acquire(_REQUEST, client))
        second = asyncio.create_task(resource_store.acquire(_REQUEST, client))
        try:
            # 先进入者已判过缓存并开始下载, 后来者此刻也判过缓存: 记录还没写.
            await started.wait()
            released.set()
            first_path = await first
            second_path = await second
            assert first_path is not None and first_path.read_bytes() == b"jpeg-bytes"
            assert second_path == first_path
            assert len(downloads) == 1
        finally:
            released.set()
            await asyncio.gather(first, second, return_exceptions=True)


# --- 独占临时择优 (acquire_best) 与提交 (commit_temp) ---


def _png(width: int, height: int, *, variant: int = 0) -> bytes:
    """生成确定尺寸的 PNG; variant != 0 时逐像素伪噪声, 同尺寸得到显著更大的字节量 (难压缩)."""
    img = Image.new("RGB", (width, height))
    if variant:
        img.putdata([((x * 37 + y * 91 + x * y * 13) % 256,) * 3 for y in range(height) for x in range(width)])
    else:
        img.paste((200, 30, 30), (0, 0, width, height))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _download_client(
    monkeypatch: pytest.MonkeyPatch,
    contents: Mapping[str, bytes | None],
    *,
    final_urls: Mapping[str, str] | None = None,
) -> tuple[WebClient, list[str]]:
    """download 按 url 写入预设字节 (None 表示下载失败); 记录每次远端下载请求.

    ``final_urls`` 给定重定向终址 (缺省即 url 本身), 用于上游改派占位图的场景.
    """
    client = WebClient(limiters=RateLimiters(default_rate=100))
    requested: list[str] = []

    async def resolve_final_url(url: str, **_kwargs: Any) -> str:
        return (final_urls or {}).get(url, url)

    async def download(url: str, dest: Path, **kwargs: Any) -> bool:
        requested.append(url)
        content = contents[url]
        if content is None:
            return False
        dest.write_bytes(content)
        return True

    monkeypatch.setattr(client, "resolve_final_url", resolve_final_url)
    monkeypatch.setattr(client, "download", download)
    return client, requested


async def _seed_cache(store: ResourceStore, url: str, content: bytes, monkeypatch: pytest.MonkeyPatch) -> Path:
    """经既有 acquire 路径预置一条共享缓存 (记录 + 文件)."""
    client = AsyncMock()
    client.resolve_final_url = AsyncMock(return_value=url)

    async def download(_url: str, dest: Path, **kwargs: object) -> bool:
        dest.write_bytes(content)
        return True

    client.download = AsyncMock(side_effect=download)
    path = await store.acquire(url, client)
    assert path is not None and path.read_bytes() == content
    return path


def _tmp_leftovers(store: ResourceStore) -> list[Path]:
    """base_dir 下残留的独占临时文件; 择优轮结束后必须为空."""
    return sorted(store._base_dir.rglob(f"*{TMP_MARK}*"))


@pytest.mark.asyncio
async def test_acquire_best_picks_largest_area(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    urls = [f"https://s/{name}.jpg" for name in ("a", "b", "c")]
    contents = {urls[0]: _png(100, 100), urls[1]: _png(50, 150), urls[2]: _png(200, 150)}
    client, requested = _download_client(monkeypatch, contents)

    result = await resource_store.acquire_best(urls, client, task_token="t9")

    assert result.success is True
    assert result.used_url == urls[2]  # 面积 30000 最大
    assert result.path is not None and result.path.read_bytes() == contents[urls[2]]
    assert result.failed == []
    assert requested == urls  # 每候选一次, 无额外请求
    for loser in urls[:2]:
        assert await resource_store.get_by_url(loser) is None  # 落选者不落库
    record = await resource_store.get_by_url(urls[2])
    assert record is not None and record.size == len(contents[urls[2]])
    assert _tmp_leftovers(resource_store) == []  # 落选临时已清理


@pytest.mark.asyncio
async def test_acquire_best_tie_breaks_by_bytes(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    urls = ["https://s/tie-a.jpg", "https://s/tie-b.jpg"]
    small = _png(100, 100)
    big = _png(100, 100, variant=1)
    assert len(big) > len(small)  # 前置: 同尺寸不同字节
    client, _ = _download_client(monkeypatch, {urls[0]: small, urls[1]: big})

    result = await resource_store.acquire_best(urls, client, task_token="t9")

    assert result.success is True
    assert result.used_url == urls[1]  # 同面积 → 字节数大者
    assert result.path is not None and result.path.read_bytes() == big


@pytest.mark.asyncio
async def test_acquire_best_partial_and_all_failure(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    # 部分失败: 在成功者中择优, 失败者计入 failed
    urls = [f"https://s/p{name}.jpg" for name in ("a", "b", "c")]
    contents = {urls[0]: _png(80, 80), urls[1]: _png(200, 100), urls[2]: None}
    client, _ = _download_client(monkeypatch, contents)

    result = await resource_store.acquire_best(urls, client, task_token="t9")

    assert result.success is True
    assert result.used_url == urls[1]
    assert result.failed == [urls[2]]
    assert await resource_store.get_by_url(urls[2]) is None

    # 全失败: success=False, failed 清单完整
    dead_urls = ["https://s/dead1.jpg", "https://s/dead2.jpg"]
    dead_client, _ = _download_client(monkeypatch, dict.fromkeys(dead_urls))

    failed_result = await resource_store.acquire_best(dead_urls, dead_client, task_token="t9")

    assert failed_result.success is False
    assert failed_result.path is None and failed_result.used_url is None
    assert failed_result.failed == dead_urls
    for url in dead_urls:
        assert await resource_store.get_by_url(url) is None
    assert _tmp_leftovers(resource_store) == []


@pytest.mark.asyncio
async def test_cached_candidate_readonly_in_comparison(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """命中共享缓存的候选复制到独占临时参与比较, 共享文件与记录只读不动."""
    cached_url = "https://s/cached-big.jpg"
    cached_bytes = _png(200, 150)
    cached_path = await _seed_cache(resource_store, cached_url, cached_bytes, monkeypatch)
    record_before = await resource_store.get_by_url(cached_url)
    assert record_before is not None
    new_url = "https://s/cached-new.jpg"
    client, requested = _download_client(monkeypatch, {new_url: _png(100, 100)})

    result = await resource_store.acquire_best([new_url, cached_url], client, task_token="t9")

    # 缓存内容参与比较并获胜
    assert result.success is True
    assert result.used_url == cached_url
    assert result.path == cached_path
    assert requested == [new_url]  # 缓存命中的候选不发请求
    # 共享文件与记录原样
    assert cached_path.read_bytes() == cached_bytes
    record_after = await resource_store.get_by_url(cached_url)
    assert record_after is not None
    assert record_after.content_hash == record_before.content_hash
    assert record_after.size == record_before.size
    # 新候选落选: 不落库, 独占临时 (含缓存复制的那份) 已清理
    assert await resource_store.get_by_url(new_url) is None
    assert _tmp_leftovers(resource_store) == []


@pytest.mark.asyncio
async def test_commit_temp_binds_compared_bytes(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """提交的就是参与比较的那份字节; 除每候选一次下载外不得有第二次远端请求."""
    urls = ["https://s/bind-a.jpg", "https://s/bind-b.jpg"]
    contents = {urls[0]: _png(100, 100), urls[1]: _png(200, 150)}
    client, requested = _download_client(monkeypatch, contents)

    result = await resource_store.acquire_best(urls, client, task_token="t9")

    assert result.success is True and result.used_url == urls[1]
    dest = resource_store._compute_path(urls[1])
    assert result.path == dest
    assert dest.read_bytes() == contents[urls[1]]
    record = await resource_store.get_by_url(urls[1])
    assert record is not None
    assert record.content_hash == ResourceStore._hash_file(dest)
    assert record.size == len(contents[urls[1]])
    assert requested == urls  # 无第二次远端请求


@pytest.mark.asyncio
async def test_commit_temp_same_hash_idempotent(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """并发同 URL 同内容 hash: 删本轮临时, 幂等返回既有持久路径."""
    url = "https://s/idem.jpg"
    content = _png(150, 150)
    cached_path = await _seed_cache(resource_store, url, content, monkeypatch)
    record_before = await resource_store.get_by_url(url)
    assert record_before is not None
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(content)

    result = await resource_store.commit_temp(url, temp)

    assert result.success is True
    assert result.path == cached_path and result.used_url == url
    assert not temp.exists()  # 本轮临时已删
    assert cached_path.read_bytes() == content  # 既有缓存原样
    record_after = await resource_store.get_by_url(url)
    assert record_after is not None
    assert record_after.content_hash == record_before.content_hash


@pytest.mark.asyncio
async def test_commit_temp_different_hash_lower_res_reselects(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """并发同 URL 更低分辨率缓存: 既有纳入比较重选, 本轮高分辨率胜 → 覆盖缓存 (记录+文件) + warning."""
    url = "https://s/conflict.jpg"
    cached_path = await _seed_cache(resource_store, url, _png(100, 100), monkeypatch)
    high = _png(200, 150)
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(high)

    with capture_logs() as logs:
        result = await resource_store.commit_temp(url, temp)

    assert result.success is True
    assert result.path == cached_path  # 路径不变, 字节已覆盖
    assert cached_path.read_bytes() == high
    record = await resource_store.get_by_url(url)
    assert record is not None
    assert record.content_hash == ResourceStore._hash_file(cached_path)
    assert record.size == len(high)
    assert not temp.exists()
    assert any(log["log_level"] == "warning" and log["event"] == "resource cache overwritten" for log in logs)


@pytest.mark.asyncio
async def test_commit_temp_different_hash_existing_better_returns_existing(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """既有内容更优: 返回既有持久路径, 本轮临时删除, 既有缓存与记录原样."""
    url = "https://s/better.jpg"
    high = _png(200, 150)
    cached_path = await _seed_cache(resource_store, url, high, monkeypatch)
    record_before = await resource_store.get_by_url(url)
    assert record_before is not None
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(100, 100))

    result = await resource_store.commit_temp(url, temp)

    assert result.success is True
    assert result.path == cached_path and result.used_url == url
    assert not temp.exists()  # 本轮临时已删
    assert cached_path.read_bytes() == high  # 既有缓存未被覆盖
    record_after = await resource_store.get_by_url(url)
    assert record_after is not None
    assert record_after.content_hash == record_before.content_hash


@pytest.mark.asyncio
async def test_commit_failure_cleans_temp(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """提交磁盘错误: 删本轮临时, 返回 FAILED, 不写记录."""
    url = "https://s/disk-error.jpg"
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"plain file")  # dest.parent.mkdir 在普通文件下抛 OSError
    monkeypatch.setattr(resource_store, "_compute_path", lambda target: blocker / "sub" / "x.jpg")
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(100, 100))

    result = await resource_store.commit_temp(url, temp)

    assert result.success is False
    assert result.failed == [url]
    assert not temp.exists()  # 本轮临时已清理
    assert await resource_store.get_by_url(url) is None  # 未写记录


@pytest.mark.asyncio
async def test_cancel_cleans_temp(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """本轮取消/异常: 独占临时 (含缓存复制的那份) 全部清理; 共享缓存与记录不动."""
    cached_url = "https://s/keep.jpg"
    cached_bytes = _png(200, 150)
    cached_path = await _seed_cache(resource_store, cached_url, cached_bytes, monkeypatch)
    record_before = await resource_store.get_by_url(cached_url)
    assert record_before is not None
    slow_url = "https://s/cancelled.jpg"
    client = AsyncMock()
    client.resolve_final_url = AsyncMock(return_value=slow_url)

    async def download(url: str, dest: Path, **kwargs: object) -> bool:
        raise asyncio.CancelledError

    client.download = AsyncMock(side_effect=download)

    with pytest.raises(asyncio.CancelledError):
        await resource_store.acquire_best([cached_url, slow_url], client, task_token="t9")

    assert _tmp_leftovers(resource_store) == []
    assert cached_path.read_bytes() == cached_bytes
    record_after = await resource_store.get_by_url(cached_url)
    assert record_after is not None
    assert record_after.content_hash == record_before.content_hash
    assert await resource_store.get_by_url(slow_url) is None


@pytest.mark.asyncio
async def test_acquire_best_rejects_placeholder_redirect(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """上游改派占位图的候选按失败处理: 不下载, 不落缓存, 择优在成功者中进行."""
    placeholder_url = "https://s/placeholder.jpg"
    good_url = "https://s/good.jpg"
    good_bytes = _png(200, 150)
    client, requested = _download_client(
        monkeypatch,
        {placeholder_url: _png(300, 300), good_url: good_bytes},
        final_urls={placeholder_url: _PLACEHOLDER},
    )

    result = await resource_store.acquire_best([placeholder_url, good_url], client, task_token="t9")

    assert result.success is True
    assert result.used_url == good_url
    assert result.failed == [placeholder_url]
    assert result.path is not None and result.path.read_bytes() == good_bytes
    assert requested == [good_url]  # 占位图候选不下载
    assert await resource_store.get_by_url(placeholder_url) is None  # 占位图不落缓存
    assert _tmp_leftovers(resource_store) == []


@pytest.mark.asyncio
async def test_acquire_best_all_placeholder_fails(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """全部候选都被改派到占位图: 整轮失败, 不落任何缓存记录."""
    placeholder_url = "https://s/only-placeholder.jpg"
    client, requested = _download_client(
        monkeypatch,
        {placeholder_url: _png(300, 300)},
        final_urls={placeholder_url: _PLACEHOLDER},
    )

    result = await resource_store.acquire_best([placeholder_url], client, task_token="t9")

    assert result.success is False
    assert result.path is None and result.used_url is None
    assert result.failed == [placeholder_url]
    assert requested == []
    assert await resource_store.get_by_url(placeholder_url) is None
    assert _tmp_leftovers(resource_store) == []


@pytest.mark.asyncio
async def test_overwrite_cancel_heals_record(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """覆盖流程在文件换新后被取消: 再次读取时记录与盘上字节一致, 缓存仍可用.

    文件已换新而记录未落库时, 记录按盘上字节自愈; 不得留下"记录 hash ≠ 盘上字节"的失配.
    """
    url = "https://s/heal.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)
    new_bytes = _png(200, 150)
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(new_bytes)

    # 在文件换新之后、记录落库之前取消.
    original_replace = Path.replace

    def replace_then_cancel(self: Path, target: Path) -> Path:
        original_replace(self, target)
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "replace", replace_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await resource_store.commit_temp(url, temp)

    monkeypatch.undo()

    # 盘上是新字节, 记录尚未更新 —— 失配此刻存在.
    assert cached_path.read_bytes() == new_bytes

    # 再次读取: 记录按盘上字节自愈, 失配消失.
    healed = await resource_store.get_by_url(url)
    assert healed is not None
    assert healed.content_hash == ResourceStore._hash_file(cached_path)
    assert healed.size == len(new_bytes)
    assert healed.meta is not None and "pending" not in healed.meta

    # 缓存可用: resolve 直出, 且与记录一致.
    resolved = await resource_store.resolve(url)
    assert resolved == cached_path
    assert resolved is not None and resolved.read_bytes() == new_bytes


@pytest.mark.asyncio
async def test_overwrite_cancel_before_replace_keeps_old_bytes(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """覆盖流程在文件换新之前被取消: 盘上仍是旧字节, 记录仍指旧字节, 标记撤除."""
    url = "https://s/heal2.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(200, 150))

    def cancel_before_replace(self: Path, target: Path) -> Path:
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "replace", cancel_before_replace)

    with pytest.raises(asyncio.CancelledError):
        await resource_store.commit_temp(url, temp)

    monkeypatch.undo()

    assert cached_path.read_bytes() == old_bytes  # 盘上仍是旧字节
    record = await resource_store.get_by_url(url)
    assert record is not None
    assert record.content_hash == ResourceStore._hash_file(cached_path)
    assert record.meta is not None and "pending" not in record.meta


@pytest.mark.asyncio
async def test_commit_replace_failure_leaves_no_pending(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """覆盖时文件替换失败: 待提交标记撤除, 记录仍指旧字节, 缓存仍可用."""
    url = "https://s/replace-fail.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(200, 150))

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    result = await resource_store.commit_temp(url, temp)

    monkeypatch.undo()

    assert result.success is False
    assert result.failed == [url]
    assert cached_path.read_bytes() == old_bytes  # 盘上仍是旧字节
    record = await resource_store.get_by_url(url)
    assert record is not None
    assert record.content_hash == ResourceStore._hash_file(cached_path)
    assert record.meta is not None and "pending" not in record.meta


@pytest.mark.asyncio
async def test_overwrite_clears_stale_sr_marker(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """覆盖为新字节后, 旧字节的超分标记不再成立: upscale_in_place 须实际执行.

    旧标记留着时, 记录已指新字节且与盘上相符, 超分被误判为"已超分"而跳过 ——
    新原图实际未超分却被当作已超分.
    """
    url = "https://s/sr-overwrite.jpg"
    await _seed_cache(resource_store, url, _png(100, 100), monkeypatch)
    record = await resource_store.get_by_url(url)
    assert record is not None

    async def sr_producer(src: Path, out: Path) -> bool:
        out.write_bytes(b"sr-bytes")
        return True

    assert await resource_store.upscale_in_place(record, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED
    marked = await resource_store.get_by_url(url)
    assert marked is not None and marked.meta is not None and "sr" in marked.meta

    # quality 覆盖为更高分辨率的新字节 (本轮胜 → 覆盖缓存).
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(200, 150))

    result = await resource_store.commit_temp(url, temp)

    assert result.success is True
    overwritten = await resource_store.get_by_url(url)
    assert overwritten is not None
    assert overwritten.meta is not None and "sr" not in overwritten.meta  # 旧字节的标记失效
    assert "pending" not in overwritten.meta  # 待提交标记不得留在库里的正常路径

    # 新字节实际未超分: 不得跳过.
    outcome = await resource_store.upscale_in_place(overwritten, {"scale": 4}, sr_producer)
    assert outcome is UpscaleOutcome.UPSCALED


@pytest.mark.asyncio
async def test_same_hash_overwrite_keeps_sr_marker(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """同 hash 幂等覆盖: 字节未变, 超分结果仍有效, 标记保留."""
    url = "https://s/sr-idem.jpg"
    await _seed_cache(resource_store, url, _png(100, 100), monkeypatch)
    record = await resource_store.get_by_url(url)
    assert record is not None

    async def sr_producer(src: Path, out: Path) -> bool:
        out.write_bytes(b"sr-bytes")
        return True

    assert await resource_store.upscale_in_place(record, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED
    marked = await resource_store.get_by_url(url)
    assert marked is not None
    sr_bytes = resource_store.full_path(marked).read_bytes()

    # 同 hash 幂等提交: 字节与盘上那份相同.
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(sr_bytes)

    result = await resource_store.commit_temp(url, temp)

    assert result.success is True
    after = await resource_store.get_by_url(url)
    assert after is not None
    assert after.meta is not None and "sr" in after.meta  # 字节未变, 标记保留
    assert await resource_store.upscale_in_place(after, {"scale": 4}, sr_producer) is UpscaleOutcome.SKIPPED


@pytest.mark.asyncio
async def test_existing_wins_keeps_sr_marker(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """既有胜 (本轮落选): 缓存字节未变, 超分标记保留."""
    url = "https://s/sr-existing-wins.jpg"
    await _seed_cache(resource_store, url, _png(200, 150), monkeypatch)
    record = await resource_store.get_by_url(url)
    assert record is not None

    async def sr_producer(src: Path, out: Path) -> bool:
        out.write_bytes(_png(200, 150))  # 字节不变, 只挂标记
        return True

    assert await resource_store.upscale_in_place(record, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED
    marked = await resource_store.get_by_url(url)
    assert marked is not None and marked.meta is not None and "sr" in marked.meta

    # 本轮分辨率更低: 既有胜, 缓存字节不变.
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(50, 50))

    result = await resource_store.commit_temp(url, temp)

    assert result.success is True
    after = await resource_store.get_by_url(url)
    assert after is not None
    assert after.meta is not None and "sr" in after.meta  # 字节未变, 标记保留


@pytest.mark.asyncio
async def test_derived_hit_requires_same_source_version(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """源内容未变 → 命中直出, producer 不重跑."""
    src_url = "https://s/src-stable.jpg"
    await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    calls = {"n": 0}

    async def producer(dest: Path) -> bool:
        calls["n"] += 1
        dest.write_bytes(b"cropped")
        return True

    first = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)
    assert first is not None
    assert calls["n"] == 1

    second = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)
    assert second is not None
    assert calls["n"] == 1  # 源未变 → 命中, producer 不重跑


@pytest.mark.asyncio
async def test_derived_reruns_when_source_changed(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """源内容变化 (同 src_url, 缓存源字节被替换) → 派生重跑, 产物反映新源."""
    src_url = "https://s/src-changed.jpg"
    src_path = await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    calls = {"n": 0}

    async def producer(dest: Path) -> bool:
        calls["n"] += 1
        # 产物取源文件的字节: 源换了则产物随之换.
        dest.write_bytes(src_path.read_bytes())
        return True

    first = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)
    assert first is not None
    assert calls["n"] == 1
    first_bytes = resource_store.full_path(first).read_bytes()

    # 源缓存字节被替换 (同 src_url).
    new_src = _png(200, 150)
    src_path.write_bytes(new_src)

    second = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)
    assert second is not None
    assert calls["n"] == 2  # 源已变 → producer 重跑
    assert resource_store.full_path(second).read_bytes() == new_src
    assert resource_store.full_path(second).read_bytes() != first_bytes


@pytest.mark.asyncio
async def test_derived_legacy_record_picks_up_source_version(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """存量派生记录没有版本标记而源有版本: 重跑一次补上标记, 此后源不变即直出."""
    src_url = "https://s/src-legacy.jpg"
    await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    calls = {"n": 0}

    async def producer(dest: Path) -> bool:
        calls["n"] += 1
        dest.write_bytes(b"cropped")
        return True

    # 造一条没有版本标记的存量派生记录.
    locator = derived_locator(src_url, "crop", "0.7")
    legacy_path = resource_store._compute_path(locator, ext=".jpg")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b"legacy-cropped")

    async with resource_store._session() as session:
        session.add(
            Resource(
                url=locator,
                file_path=resource_store._relative_path(legacy_path),
                content_hash=ResourceStore._hash_file(legacy_path),
                size=legacy_path.stat().st_size,
                mime_type="image/jpeg",
                meta={"op": "crop", "src": src_url, "args": "0.7"},
            )
        )
        await session.commit()

    result = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)

    assert result is not None
    assert calls["n"] == 1  # 存量记录补标记, 重跑一次
    assert result.meta is not None and "srcv" in result.meta

    # 版本已记下: 源不变即直出.
    again = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)
    assert again is not None
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_derived_without_source_record_still_hits(resource_store: ResourceStore):
    """源无 Resource 记录 (无从核对版本): 同配置幂等直出, producer 不重跑."""
    calls = {"n": 0}

    async def producer(dest: Path) -> bool:
        calls["n"] += 1
        dest.write_bytes(b"cropped")
        return True

    first = await resource_store.acquire_derived("https://s/no-src-row.jpg", "crop", "0.7", producer)
    assert first is not None
    assert calls["n"] == 1
    assert first.meta is not None and "srcv" not in first.meta  # 无源记录, 不记版本

    second = await resource_store.acquire_derived("https://s/no-src-row.jpg", "crop", "0.7", producer)
    assert second is not None
    assert calls["n"] == 1


# --- 自愈路径的并发安全: 在飞提交的待提交标记不得被读路径撤掉 ---


async def _row_meta(store: ResourceStore, url: str) -> dict:
    """读库里的 meta 列: 断言用库态而非调用方手里的副本."""
    async with store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        return dict(row.meta) if row is not None and row.meta else {}


@pytest.mark.asyncio
async def test_concurrent_read_keeps_inflight_pending_marker(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """提交已挂待提交标记、尚未换文件时的并发读: 读不得撤掉在飞提交的标记.

    撤掉后提交再夭折, 记录 hash 与盘上字节就永久失配, 且此后再无自愈触发条件.
    """
    url = "https://s/inflight-pending.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    new_bytes = _png(200, 150)
    temp.write_bytes(new_bytes)
    new_hash = hashlib.sha256(new_bytes).hexdigest()

    marked = asyncio.Event()
    proceed = asyncio.Event()

    original_mark = ResourceStore._mark_pending

    async def mark_then_pause(self: ResourceStore, record: Resource, content_hash: str) -> None:
        await original_mark(self, record, content_hash)
        marked.set()
        await proceed.wait()

    monkeypatch.setattr(ResourceStore, "_mark_pending", mark_then_pause)

    commit = asyncio.create_task(resource_store.commit_temp(url, temp))
    try:
        await marked.wait()  # 标记已在库, 文件尚未换
        assert (await _row_meta(resource_store, url)).get("pending") == new_hash

        # 并发读: 盘上仍是旧字节, 库里有待提交标记.
        read = asyncio.create_task(resource_store.get_by_url(url))
        await asyncio.wait({read}, timeout=1.0)

        # 读不得撤掉在飞提交的标记.
        assert (await _row_meta(resource_store, url)).get("pending") == new_hash
    finally:
        proceed.set()
        await commit
        monkeypatch.undo()

    # 提交走完: 记录与盘上一致, 标记撤除.
    assert cached_path.read_bytes() == new_bytes
    final = await resource_store.get_by_url(url)
    assert final is not None
    assert final.content_hash == ResourceStore._hash_file(cached_path)
    assert final.meta is not None and "pending" not in final.meta


@pytest.mark.asyncio
async def test_concurrent_read_then_cancel_keeps_record_consistent(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """并发读发生在"标记已挂、文件未换"窗口, 随后提交在换文件后被取消: 记录须与盘上一致.

    审查发现 1 的可观察后果: 读撤掉在飞标记后提交夭折, 记录 hash 与盘上字节永久失配.
    """
    url = "https://s/inflight-cancel.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)
    temp = resource_store._base_dir / "cand.amane-tmp-t9.jpg"
    temp.write_bytes(_png(200, 150))

    marked = asyncio.Event()
    proceed = asyncio.Event()

    original_mark = ResourceStore._mark_pending

    async def mark_then_pause(self: ResourceStore, record: Resource, content_hash: str) -> None:
        await original_mark(self, record, content_hash)
        marked.set()
        await proceed.wait()

    monkeypatch.setattr(ResourceStore, "_mark_pending", mark_then_pause)

    original_replace = Path.replace

    def replace_then_cancel(self: Path, target: Path) -> Path:
        original_replace(self, target)
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "replace", replace_then_cancel)

    commit = asyncio.create_task(resource_store.commit_temp(url, temp))
    try:
        await marked.wait()
        read = asyncio.create_task(resource_store.get_by_url(url))
        await asyncio.wait({read}, timeout=1.0)
    finally:
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await commit
        monkeypatch.undo()

    assert cached_path.read_bytes() != old_bytes  # 文件已换新

    # 记录须按盘上字节自愈, 不得留下失配.
    healed = await resource_store.get_by_url(url)
    assert healed is not None
    assert healed.content_hash == ResourceStore._hash_file(cached_path)
    assert healed.meta is not None and "pending" not in healed.meta


@pytest.mark.asyncio
async def test_stale_pending_marker_still_stripped(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """崩溃遗留的待提交标记 (无在飞提交) 仍能被正确撤除: 与在飞标记的处置须可区分."""
    url = "https://s/stale-pending.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)

    # 造一条崩溃遗留的标记: 库里有 pending, 盘上仍是旧字节, 且无在飞提交.
    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        assert row is not None
        row.meta = {**(row.meta or {}), "pending": "f" * 64}
        session.add(row)
        await session.commit()

    record = await resource_store.get_by_url(url)
    assert record is not None
    assert record.meta is not None and "pending" not in record.meta  # 陈旧标记已撤
    assert record.content_hash == ResourceStore._hash_file(cached_path)  # 记录仍指盘上那份


@pytest.mark.asyncio
async def test_heal_checks_db_state_not_caller_snapshot(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """读路径的快照早于提交挂标记时: 自愈仍须按库态修复记录与盘上字节的失配.

    自愈若按调用方手里的快照判有无标记, 取快照到自愈之间刚挂上的标记会被漏掉, 记录留在旧 hash,
    盘上却是新字节 —— 旧 ETag 于是按旧记录返回新内容.
    """
    url = "https://s/heal-late-pending.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)
    new_bytes = _png(200, 150)

    # 读路径先取快照: 此时库中还没有待提交标记.
    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        assert row is not None
        snapshot = Resource.model_validate(row.model_dump())
    assert snapshot.meta is not None and "pending" not in snapshot.meta

    # 提交挂上标记并换文件, 随后夭折: 盘上是新字节, 记录仍是旧 hash 且带标记.
    new_hash = hashlib.sha256(new_bytes).hexdigest()
    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        assert row is not None
        await resource_store._mark_pending(row, new_hash)
    cached_path.write_bytes(new_bytes)

    # 读路径拿着陈旧快照自愈: 须按库态修复, 不按快照.
    await resource_store._heal_record(snapshot, cached_path)

    assert snapshot.content_hash == ResourceStore._hash_file(cached_path)
    assert snapshot.meta is not None and "pending" not in snapshot.meta


@pytest.mark.asyncio
async def test_heal_write_does_not_clobber_concurrent_commit(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """自愈落库不得覆盖提交刚写入的新 hash: 最终记录 hash 须等于盘上字节.

    审查发现 2 的确定性复现: 读路径拿着陈旧快照 (旧 hash + pending) → 提交换文件并落库
    (新 hash) → 自愈落库; 自愈不得用陈旧快照把新 hash 盖回去.
    """
    url = "https://s/heal-clobber.jpg"
    old_bytes = _png(100, 100)
    cached_path = await _seed_cache(resource_store, url, old_bytes, monkeypatch)

    # 第一次提交在换文件后被取消: 盘上是 A, 记录仍指旧字节且带待提交标记.
    bytes_a = _png(200, 150)
    temp_a = resource_store._base_dir / "cand.amane-tmp-t1.jpg"
    temp_a.write_bytes(bytes_a)
    original_replace = Path.replace

    def replace_then_cancel(self: Path, target: Path) -> Path:
        original_replace(self, target)
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "replace", replace_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await resource_store.commit_temp(url, temp_a)
    monkeypatch.undo()

    assert cached_path.read_bytes() == bytes_a

    # 读路径: 快照在此刻取到 (旧 hash + pending), 自愈被拦在快照之后.
    paused = asyncio.Event()
    proceed = asyncio.Event()
    armed = False
    original_heal = ResourceStore._heal_record

    async def heal_pause_first(self: ResourceStore, record: Resource, full_path: Path) -> None:
        # 只拦这次读的自愈: 自愈锁还没取, 提交得以在快照之后落库.
        if armed and not paused.is_set():
            paused.set()
            await proceed.wait()
        await original_heal(self, record, full_path)

    monkeypatch.setattr(ResourceStore, "_heal_record", heal_pause_first)

    armed = True
    read = asyncio.create_task(resource_store.get_by_url(url))
    await paused.wait()

    # 第二次提交: 更大的字节胜出, 换文件并落库 (新 hash).
    bytes_b = _png(300, 200)
    temp_b = resource_store._base_dir / "cand.amane-tmp-t2.jpg"
    temp_b.write_bytes(bytes_b)
    result = await resource_store.commit_temp(url, temp_b)
    assert result.success is True
    assert cached_path.read_bytes() == bytes_b

    proceed.set()
    await read
    monkeypatch.undo()

    # 最终记录 hash 必须等于盘上字节, 不得被自愈的陈旧快照盖回旧 hash.
    final = await resource_store.get_by_url(url)
    assert final is not None
    assert final.content_hash == ResourceStore._hash_file(cached_path)
    assert final.meta is not None and "pending" not in final.meta


@pytest.mark.asyncio
async def test_upscale_write_excluded_from_heal(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """就地超分的落库须与自愈互斥: 自愈持锁期间超分不得落库.

    两处都改记录 (hash/size/类型/sr 标记), 不互斥时自愈可用陈旧快照把超分的结果盖回去 ——
    记录指回超分前的字节, 超分被当成没做过.
    """
    url = "https://s/heal-upscale.jpg"
    cached_path = await _seed_cache(resource_store, url, _png(100, 100), monkeypatch)

    # 崩溃遗留的标记: pending 指向盘上那份字节 —— 自愈走"认下盘上字节"分支, 正是改写
    # content_hash/size/类型的那几列.
    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        assert row is not None
        row.meta = {**(row.meta or {}), "pending": ResourceStore._hash_file(cached_path)}
        session.add(row)
        await session.commit()

    # 自愈持锁期间把落库卡住.
    paused = asyncio.Event()
    proceed = asyncio.Event()
    original_locked = ResourceStore._heal_locked
    seen = {"n": 0}

    async def heal_hold(self: ResourceStore, row_id: int | None) -> Resource | None:
        seen["n"] += 1
        if seen["n"] == 1:
            paused.set()
            await proceed.wait()
        return await original_locked(self, row_id)

    monkeypatch.setattr(ResourceStore, "_heal_locked", heal_hold)

    read = asyncio.create_task(resource_store.get_by_url(url))
    await paused.wait()  # 自愈正持锁

    sr_bytes = b"upscaled-bytes"

    async def sr_producer(src: Path, out: Path) -> bool:
        out.write_bytes(sr_bytes)
        return True

    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        record = result.first()
    assert record is not None
    upscale = asyncio.create_task(resource_store.upscale_in_place(record, {"scale": 2}, sr_producer))
    await asyncio.sleep(0.2)
    # 自愈持锁时超分不得落库.
    assert not upscale.done()

    proceed.set()
    await read
    outcome = await upscale
    monkeypatch.undo()

    assert outcome is UpscaleOutcome.UPSCALED
    final = await resource_store.get_by_url(url)
    assert final is not None
    assert final.content_hash == ResourceStore._hash_file(cached_path)
    assert final.meta is not None and "sr" in final.meta


@pytest.mark.asyncio
async def test_heal_keeps_valid_sr_marker(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """待提交的那份字节自带有效超分标记时, 自愈不得撤掉它 (否则二次超分).

    崩溃的提交提交的正是已超分那份字节: 盘上已是超分后的字节, 记录上的 sr 标记仍有效.
    """
    url = "https://s/heal-sr.jpg"
    await _seed_cache(resource_store, url, _png(100, 100), monkeypatch)
    record = await resource_store.get_by_url(url)
    assert record is not None

    async def sr_producer(src: Path, out: Path) -> bool:
        out.write_bytes(b"sr-bytes")
        return True

    assert await resource_store.upscale_in_place(record, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED
    marked = await resource_store.get_by_url(url)
    assert marked is not None and marked.meta is not None and "sr" in marked.meta
    sr_hash = marked.content_hash
    assert sr_hash is not None

    # 崩溃的提交提交的正是这份已超分的字节: 记录挂上指向它的待提交标记, 盘上已是那份字节.
    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        assert row is not None
        row.meta = {**(row.meta or {}), "pending": sr_hash}
        session.add(row)
        await session.commit()

    healed = await resource_store.get_by_url(url)
    assert healed is not None
    assert healed.content_hash == ResourceStore._hash_file(resource_store.full_path(healed))
    assert healed.meta is not None and "sr" in healed.meta  # 有效超分标记保留
    # 不得二次超分.
    assert await resource_store.upscale_in_place(healed, {"scale": 4}, sr_producer) is UpscaleOutcome.SKIPPED


@pytest.mark.asyncio
async def test_heal_drops_sr_marker_of_replaced_bytes(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """待提交的是新字节 (旧字节的超分标记不适用): 自愈须撤掉旧超分标记."""
    url = "https://s/heal-sr-stale.jpg"
    cached_path = await _seed_cache(resource_store, url, _png(100, 100), monkeypatch)
    record = await resource_store.get_by_url(url)
    assert record is not None

    async def sr_producer(src: Path, out: Path) -> bool:
        out.write_bytes(b"sr-bytes")
        return True

    assert await resource_store.upscale_in_place(record, {"scale": 2}, sr_producer) is UpscaleOutcome.UPSCALED

    new_bytes = _png(200, 150)
    async with resource_store._session() as session:
        result = await session.exec(select(Resource).where(Resource.url == url))
        row = result.first()
        assert row is not None
        # 待提交的是新字节 (与盘上那份不同): 旧字节的超分标记随之失效.
        row.meta = {**(row.meta or {}), "pending": hashlib.sha256(new_bytes).hexdigest()}
        session.add(row)
        await session.commit()

    cached_path.write_bytes(new_bytes)

    healed = await resource_store.get_by_url(url)
    assert healed is not None
    assert healed.meta is not None and "sr" not in healed.meta
    assert healed.content_hash == ResourceStore._hash_file(cached_path)


@pytest.mark.asyncio
async def test_derived_internal_url_source_reruns_when_source_changed(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """源以内部 URL 形式给出时, 源字节被换后派生须重跑 (版本核对不得静默失效)."""
    src_url = "https://s/src-internal.jpg"
    src_path = await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    internal = f"{RESOURCE_URL_PREFIX}/{resource_store.url_hash(src_url)}"
    calls = {"n": 0}

    async def producer(dest: Path) -> bool:
        calls["n"] += 1
        dest.write_bytes(src_path.read_bytes())
        return True

    first = await resource_store.acquire_derived(internal, "crop", "0.7", producer)
    assert first is not None
    assert calls["n"] == 1
    assert first.meta is not None and "srcv" in first.meta  # 内部 URL 也记源版本

    new_src = _png(200, 150)
    src_path.write_bytes(new_src)

    second = await resource_store.acquire_derived(internal, "crop", "0.7", producer)
    assert second is not None
    assert calls["n"] == 2  # 源已变 → 派生重跑
    assert resource_store.full_path(second).read_bytes() == new_src


# --- 派生重生成的中断保护 (IMPL-10) ---


@pytest.mark.asyncio
async def test_derived_regenerate_cancel_heals_record(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """派生重生成在文件移入后、落库前被取消: 再次读取时记录与盘上字节一致 (IMPL-10).

    派生路径原先没有待提交标记: 中途夭折会留下「盘上是新字节、记录仍是旧 hash」的失配,
    资源接口按旧 ETag 返回 304, 新字节永远取不到.
    """
    src_url = "https://s/src-cancel.jpg"
    src_path = await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    old_bytes = b"old-derived-bytes"

    async def old_producer(dest: Path) -> bool:
        dest.write_bytes(old_bytes)
        return True

    first = await resource_store.acquire_derived(src_url, "crop", "0.7", old_producer)
    assert first is not None
    old_hash = first.content_hash
    derived_path = resource_store.full_path(first)

    # 源换新 → 派生须重生成; producer 写出新字节.
    src_path.write_bytes(_png(200, 150))
    new_bytes = b"new-derived-bytes"

    async def new_producer(dest: Path) -> bool:
        dest.write_bytes(new_bytes)
        return True

    # 在文件移入之后、记录落库之前取消.
    original_replace = Path.replace

    def replace_then_cancel(self: Path, target: Path) -> Path:
        original_replace(self, target)
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "replace", replace_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await resource_store.acquire_derived(src_url, "crop", "0.7", new_producer)

    monkeypatch.undo()

    # 盘上是新字节, 记录尚未更新 —— 失配此刻存在.
    assert derived_path.read_bytes() == new_bytes
    assert derived_path.read_bytes() != old_bytes

    # 再次读取: 记录按盘上字节自愈, 失配消失.
    healed = await resource_store.get_by_url(first.url)
    assert healed is not None
    assert healed.content_hash == ResourceStore._hash_file(derived_path)
    assert healed.content_hash != old_hash
    assert healed.size == len(new_bytes)
    assert healed.meta is not None and "pending" not in healed.meta

    # 读路径自愈: 源版本标记随盘上字节一并写回, 不留旧值.
    assert healed.meta.get("srcv") == ResourceStore._hash_file(src_path)


@pytest.mark.asyncio
async def test_derived_regenerate_cancel_keeps_old_bytes_before_move(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """派生重生成在文件移入前被取消: 盘上仍是旧字节, 记录仍指旧字节, 待提交标记撤除."""
    src_url = "https://s/src-cancel-early.jpg"
    src_path = await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    old_bytes = b"old-derived-bytes"

    async def old_producer(dest: Path) -> bool:
        dest.write_bytes(old_bytes)
        return True

    first = await resource_store.acquire_derived(src_url, "crop", "0.7", old_producer)
    assert first is not None
    derived_path = resource_store.full_path(first)

    src_path.write_bytes(_png(200, 150))

    async def new_producer(dest: Path) -> bool:
        dest.write_bytes(b"new-derived-bytes")
        return True

    def cancel_before_replace(self: Path, target: Path) -> Path:
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "replace", cancel_before_replace)

    with pytest.raises(asyncio.CancelledError):
        await resource_store.acquire_derived(src_url, "crop", "0.7", new_producer)

    monkeypatch.undo()

    assert derived_path.read_bytes() == old_bytes  # 盘上仍是旧字节
    record = await resource_store.get_by_url(first.url)
    assert record is not None
    assert record.content_hash == ResourceStore._hash_file(derived_path)
    assert record.meta is not None and "pending" not in record.meta


@pytest.mark.asyncio
async def test_derived_producer_writes_exclusive_temp(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch):
    """producer 写到独占临时而非最终路径: 最终路径的字节变更与落库之间不出现窗口.

    派生重生成直接写最终路径时, 「字节已换新、记录未落库」的窗口内夭折会留下永久失配.
    """
    src_url = "https://s/src-temp.jpg"
    await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    seen: list[Path] = []

    async def producer(dest: Path) -> bool:
        seen.append(dest)
        dest.write_bytes(b"derived-bytes")
        return True

    result = await resource_store.acquire_derived(src_url, "crop", "0.7", producer)
    assert result is not None
    assert len(seen) == 1
    final_path = resource_store.full_path(result)
    assert seen[0] != final_path  # producer 写的是临时
    assert TMP_MARK in seen[0].name
    assert final_path.read_bytes() == b"derived-bytes"
    # 临时在提交后清理.
    assert not seen[0].exists()
    assert _tmp_leftovers(resource_store) == []


@pytest.mark.asyncio
async def test_derived_replace_failure_leaves_no_pending(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch
):
    """派生提交时文件移入失败: 待提交标记撤除, 记录仍指旧字节, 盘上仍是旧字节."""
    src_url = "https://s/derived-replace-fail.jpg"
    src_path = await _seed_cache(resource_store, src_url, _png(100, 100), monkeypatch)
    old_bytes = b"old-derived-bytes"

    async def old_producer(dest: Path) -> bool:
        dest.write_bytes(old_bytes)
        return True

    first = await resource_store.acquire_derived(src_url, "crop", "0.7", old_producer)
    assert first is not None
    derived_path = resource_store.full_path(first)

    src_path.write_bytes(_png(200, 150))

    async def new_producer(dest: Path) -> bool:
        dest.write_bytes(b"new-derived-bytes")
        return True

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    result = await resource_store.acquire_derived(src_url, "crop", "0.7", new_producer)

    monkeypatch.undo()

    assert result is None  # 提交失败按失败处理, 不返回半成品记录
    assert derived_path.read_bytes() == old_bytes  # 盘上仍是旧字节
    record = await resource_store.get_by_url(first.url)
    assert record is not None
    assert record.content_hash == ResourceStore._hash_file(derived_path)
    assert record.meta is not None and "pending" not in record.meta
    assert _tmp_leftovers(resource_store) == []
