"""Selector-drift recovery tests against a real headless browser."""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from argo.config import ArgoConfig  # noqa: E402
from argo.tools import computer as comp  # noqa: E402
from argo.tools.base import ToolContext  # noqa: E402

PAGE = """<!doctype html><html><body>
<h1>Console</h1>
<button data-testid="login">Sign in</button>
<a aria-label="open settings">gear</a>
<button id="submit-order">Submit order</button>
<a class="signupLink">Signup here</a>
<input name="email" placeholder="you@example.com">
<select name="priority"><option value="low">Low</option><option value="high">High</option></select>
</body></html>"""


def serve_page() -> tuple[ThreadingHTTPServer, str]:
    html = PAGE.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}/"


def test_variants_are_ordered_from_stable_attributes():
    variants = comp.selector_variants("#login-btn")
    assert variants[0] == '*[data-testid="login-btn"]'
    assert any("aria-label" in v for v in variants)
    assert comp.selector_variants(".card") and comp.selector_variants("button#save")
    assert comp.selector_variants("div.a > span") == [], "complex selectors are left alone"
    assert comp.selector_words("#submit-order") == ["submit order", "submit", "order"]
    assert any(v == '*[name*="email" i]' for v in comp.selector_variants("#email-input"))


async def test_resolve_heals_drifted_selectors_and_lists_candidates():
    from playwright.async_api import async_playwright

    srv, url = serve_page()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url)

            loc, used, note = await comp._resolve(page, "#login")  # id removed, data-testid kept
            assert "data-testid" in used and "healed" in note
            assert await loc.is_visible()

            loc, used, note = await comp._resolve(page, "a#settings")  # aria-label fallback
            assert "aria-label" in used and note

            loc, used, note = await comp._resolve(page, "button#submit")  # fuzzy role name
            assert note and await loc.count() == 1

            loc, used, note = await comp._resolve(page, "", text="Signup here")
            assert used == "text='Signup here'" and not note

            with pytest.raises(ValueError) as exc:
                await comp._resolve(page, "#does-not-exist")
            assert "Candidates on page" in str(exc.value)
            assert "submit-order" in str(exc.value)
            await browser.close()
    finally:
        srv.shutdown()


async def page_value(ctx) -> str:
    page = ctx.extra[comp.STATE_KEY]["page"]
    return await page.eval_on_selector('select[name="priority"]', "el => el.value")


async def test_computer_tool_clicks_through_drift_and_records_heal(tmp_path: Path):
    cfg = ArgoConfig(workspace=tmp_path, data_dir=tmp_path / "d", session_dir=tmp_path / "s",
                     index_dir=tmp_path / "i", workflow_dir=tmp_path / "w", skill_dir=tmp_path / "k",
                     spool_dir=tmp_path / "p")
    cfg.ensure_dirs()
    ctx = ToolContext(cfg=cfg)
    srv, url = serve_page()
    try:
        out = await comp._computer(ctx, "launch", url=url)
        assert not out.is_error, out.content
        out = await comp._computer(ctx, "click", selector="#login")
        assert not out.is_error, out.content
        assert "healed selector" in out.content
        assert any(e.type == "selector_heal" for e in ctx.events.history)
        # typing into a renamed field heals too
        out = await comp._computer(ctx, "type", selector="#email-input", text="a@b.c")
        assert not out.is_error, out.content
        state = ctx.extra[comp.STATE_KEY]
        assert state["heals"]["click:#login"].startswith("*[data-testid")
        # selecting by value and by visible label
        out = await comp._computer(ctx, "select", selector='select[name="priority"]', text="high")
        assert not out.is_error, out.content
        assert await page_value(ctx) == "high"
        out = await comp._computer(ctx, "select", selector="#priority-field", text="Low")
        assert not out.is_error and "healed selector" in out.content
        assert await page_value(ctx) == "low"
    finally:
        await comp.shutdown(ctx)
        srv.shutdown()
