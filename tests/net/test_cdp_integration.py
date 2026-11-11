"""CDP 后端集成测试: 连真实浏览器, 验证借用登录态与不覆盖远程 cookie.

需要本机有 Chrome; 无 Chrome 时整组跳过. 用本地 HTTP 服务做站点, 不依赖外网.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from amane.net.browser import CdpBackend

pytest.importorskip("patchright", reason="CDP 后端需要 patchright")

_COOKIE = "session_token"
_LOGGED_IN = "<html><body>LOGGED-IN-CONTENT</body></html>"
_ANONYMOUS = "<html><body>ANONYMOUS-CONTENT</body></html>"

_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def _find_chrome() -> str | None:
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = (_LOGGED_IN if _COOKIE in self.headers.get("Cookie", "") else _ANONYMOUS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def site_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def chrome_endpoint() -> Iterator[str]:
    chrome = _find_chrome()
    if chrome is None:
        pytest.skip("本机没有 Chrome, 跳过 CDP 集成测试")
    port = _free_port()
    proc = subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir=/tmp/amane-test-cdp-{port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--headless=new",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            probe = subprocess.run(["curl", "-s", f"{endpoint}/json/version"], capture_output=True, text=True)
            if probe.stdout.strip():
                break
            time.sleep(0.2)
        else:
            pytest.skip("Chrome 未就绪, 跳过 CDP 集成测试")
        yield endpoint
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.asyncio
async def test_cdp_backend_borrows_remote_login_state(site_url: str, chrome_endpoint: str) -> None:
    """远程浏览器已登录时, 后端取到的是登录后的内容."""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome_endpoint)
        context = browser.contexts[0]
        await context.add_cookies([{"name": _COOKIE, "value": "abc123", "url": site_url}])
        await browser.close()

    backend = CdpBackend(endpoint=chrome_endpoint, default_timeout=20000)
    try:
        html, failure = await backend.get_page(site_url, scope="login-test")
    finally:
        await backend.close()

    assert failure is None
    assert html is not None
    assert "LOGGED-IN-CONTENT" in html


@pytest.mark.asyncio
async def test_cdp_backend_does_not_overwrite_remote_cookies(site_url: str, chrome_endpoint: str) -> None:
    """后端不注入 cookie: 调用方给的 cookie 不写进远程浏览器的 profile."""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome_endpoint)
        context = browser.contexts[0]
        await context.add_cookies([{"name": _COOKIE, "value": "abc123", "url": site_url}])
        await browser.close()

    backend = CdpBackend(endpoint=chrome_endpoint, default_timeout=20000)
    try:
        await backend.get_page(site_url, scope="login-test", cookies={_COOKIE: "INJECTED"})
    finally:
        await backend.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome_endpoint)
        context = browser.contexts[0]
        cookies = {c.get("name"): c.get("value") for c in await context.cookies([site_url])}
        await browser.close()

    assert cookies.get(_COOKIE) == "abc123"


@pytest.mark.asyncio
async def test_cdp_backend_close_keeps_browser_running(chrome_endpoint: str) -> None:
    """关闭只断开连接, 远程浏览器进程不受影响."""
    from patchright.async_api import async_playwright

    backend = CdpBackend(endpoint=chrome_endpoint, default_timeout=20000)
    await backend.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome_endpoint)
        assert browser.is_connected()
        await browser.close()
