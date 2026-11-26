"""Browser / computer-use tool (optional Playwright backend).

The agent gets both a screenshot (for vision models) and a compact DOM text view,
then acts with selectors, text, or coordinates. Persistent storage_state lets saved
workflows resume authenticated sessions without storing credentials in the trace.

Install the extra with:  pip install 'argo-harness[computer]' && playwright install chromium
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry

STATE_KEY = "__browser__"


def _browser_dir(ctx: ToolContext) -> Path:
    d = Path(ctx.cfg.workspace) / ".argo" / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _ensure(ctx: ToolContext, headless: bool = True, viewport: tuple[int, int] = (1440, 900)):
    from playwright.async_api import async_playwright  # type: ignore
    state = ctx.extra.get(STATE_KEY)
    if state and state.get("page"):
        return state
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    storage = _browser_dir(ctx) / "state.json"
    context = await browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        storage_state=str(storage) if storage.exists() else None,
        user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"),
    )
    page = await context.new_page()
    page.set_default_timeout(15000)
    state = {"pw": pw, "browser": browser, "context": context, "page": page, "history": []}
    ctx.extra[STATE_KEY] = state
    return state


async def _save_state(ctx: ToolContext, state) -> None:
    try:
        await state["context"].storage_state(path=str(_browser_dir(ctx) / "state.json"))
    except Exception:
        pass


def _record_heal(ctx: ToolContext, state: dict, action: str, selector: str, used: str) -> None:
    """Remember the working locator so later steps and replays skip the drift."""
    heals = state.setdefault("heals", {})
    heals[f"{action}:{selector}"] = used
    ctx.emit("selector_heal", action=action, selector=selector, used=used)


async def _shot(ctx: ToolContext, page, label: str = "shot", full_page: bool = False) -> tuple[str, str]:
    path = _browser_dir(ctx) / f"{label}_{int(time.time() * 1000)}.png"
    await page.screenshot(path=str(path), full_page=full_page)
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return str(path), f"data:image/png;base64,{data}"


async def _dom_summary(page, max_chars: int = 6000) -> str:
    try:
        data = await page.evaluate("""() => {
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
            const heads = [...document.querySelectorAll('h1,h2,h3')].slice(0, 30)
                .map(h => h.tagName + ': ' + clean(h.innerText));
            const links = [...document.querySelectorAll('a[href]')].slice(0, 120)
                .map(a => clean(a.innerText) + ' -> ' + a.href)
                .filter(x => x.length > 4);
            const forms = [...document.querySelectorAll('input,select,textarea,button')].slice(0, 80)
                .map(e => e.tagName.toLowerCase() + (e.type ? '[' + e.type + ']' : '') +
                          (e.id ? '#' + e.id : '') + (e.name ? '[name=' + e.name + ']' : '') +
                          ' placeholder=' + clean(e.placeholder || e.innerText));
            return {title: document.title, url: location.href,
                    text: clean(document.body ? document.body.innerText : ''),
                    heads, links, forms};
        }""")
    except Exception as exc:  # noqa: BLE001
        return f"(DOM extraction failed: {exc})"
    parts = [f"URL: {data.get('url')}", f"TITLE: {data.get('title')}",
             "HEADINGS:\n" + "\n".join(data.get("heads", [])[:20]),
             "INTERACTIVE:\n" + "\n".join(data.get("forms", [])[:40]),
             "LINKS:\n" + "\n".join(data.get("links", [])[:60]),
             "TEXT:\n" + data.get("text", "")]
    out = "\n\n".join(parts)
    return out[:max_chars]



_SIMPLE = re.compile(r"^(?P<tag>[a-zA-Z][\w-]*)?(?:(?P<hash>#)(?P<id>[\w-]+)|\.(?P<cls>[\w-]+))?$")


def selector_variants(selector: str) -> list[str]:
    """Alternative CSS selectors for a drifted simple selector.

    Selector drift is the dominant failure mode of scripted UI automation: an id or class is renamed while the
    element keeps its role, test id, accessible name or placeholder. We therefore retry the *same identity* under
    progressively more stable attributes, most stable first (data-testid > aria-label/name > placeholder > text).
    """
    sel = (selector or "").strip()
    m = _SIMPLE.match(sel)
    if not m or not (m.group("id") or m.group("cls")):
        return []
    tag = (m.group("tag") or "").lower()
    token = m.group("id") or m.group("cls")
    prefix = tag if tag else "*"
    if tag == "a":
        prefix = "a"
    variants = [
        f'{prefix}[data-testid="{token}"]',
        f'{prefix}[data-testid="{token}" i]',
        f'{prefix}[aria-label="{token}" i]',
        f'{prefix}[name="{token}" i]',
        f'{prefix}[placeholder="{token}" i]',
        f'{prefix}[title="{token}" i]',
    ]
    if m.group("id"):
        variants.append(f'{prefix}[id="{m.group("id")}" i]')
    if m.group("cls"):
        variants.append(f'{prefix}.{m.group("cls")}')
    # last resort: the element kept a *related* attribute (e.g. #email-input -> input[name="email"])
    for word in selector_words(token):
        variants.extend([
            f'{prefix}[data-testid*="{word}" i]',
            f'{prefix}[aria-label*="{word}" i]',
            f'{prefix}[name*="{word}" i]',
            f'{prefix}[placeholder*="{word}" i]',
        ])
    return variants


def selector_words(selector: str) -> list[str]:
    """Human words inside a selector, e.g. '#login-btn' -> ['login btn', 'login', 'btn']."""
    sel = (selector or "").strip()
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", re.sub(r"^[.#]", "", sel))
    words: list[str] = []
    for tok in tokens:
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tok).replace("-", " ").replace("_", " ").strip()
        if len(spaced) >= 3 and spaced not in words:
            words.append(spaced)
        if " " in spaced:
            for part in spaced.split():
                if len(part) >= 4 and part not in words:
                    words.append(part)
    return words


async def _interactive_summary(page, limit: int = 25) -> str:
    try:
        rows = await page.evaluate("""(limit) => {
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
            const sel = e => {
                let s = e.tagName.toLowerCase();
                if (e.id) s += '#' + e.id;
                if (e.getAttribute('data-testid')) s += '[data-testid="' + e.getAttribute('data-testid') + '"]';
                if (e.getAttribute('name')) s += '[name="' + e.getAttribute('name') + '"]';
                return s;
            };
            return [...document.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=link]')]
                .filter(e => e.offsetParent !== null || e.tagName === 'INPUT')
                .slice(0, limit)
                .map(e => sel(e) + ' | ' + (e.getAttribute('aria-label') || clean(e.innerText) ||
                                             clean(e.placeholder) || clean(e.value) || ''));
        }""", limit)
    except Exception:  # noqa: BLE001
        return ""
    return "Candidates on page:\n" + "\n".join(rows) if rows else ""


async def _resolve(page, selector: str, text: str = "", timeout: int = 1500):
    """Resolve a click/type target, healing a drifted selector.

    Returns (locator, used_selector, heal_note). Raises ValueError if nothing matches, with the interactive
    elements listed so the model can correct itself in one step instead of guessing repeatedly.
    """
    if selector:
        loc = page.locator(selector)
        try:
            if await loc.count() and await loc.first.is_visible():
                return loc.first, selector, ""
        except Exception:  # noqa: BLE001
            pass
        for variant in selector_variants(selector):
            try:
                cand = page.locator(variant)
                if await cand.count() and await cand.first.is_visible():
                    return cand.first, variant, f"healed selector {selector!r} -> {variant!r}"
            except Exception:  # noqa: BLE001
                continue
        for word in selector_words(selector):
            for role in ("button", "link", "textbox", "searchbox", "combobox"):
                try:
                    cand = page.get_by_role(role, name=re.compile(re.escape(word), re.I)).first
                    if await cand.count():
                        used = f'role={role}, name~{word!r}'
                        return cand, used, f"healed selector {selector!r} -> {used}"
                except Exception:  # noqa: BLE001
                    continue
            try:
                cand = page.get_by_text(re.compile(re.escape(word), re.I)).first
                if await cand.count():
                    used = f"text~{word!r}"
                    return cand, used, f"healed selector {selector!r} -> {used}"
            except Exception:  # noqa: BLE001
                continue
    if text:
        cand = page.get_by_text(text, exact=False).first
        if await cand.count():
            return cand, f"text={text!r}", ""
    listing = await _interactive_summary(page)
    raise ValueError(f"no element matched selector {selector!r}" +
                     (f" or text {text!r}" if text else "") + (f"\n{listing}" if listing else ""))


def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    return url


async def shutdown(ctx: ToolContext) -> None:
    state = ctx.extra.pop(STATE_KEY, None)
    if not state:
        return
    try:
        await _save_state(ctx, state)
    except Exception:
        pass
    try:
        await state["browser"].close()
    except Exception:
        pass
    try:
        await state["pw"].stop()
    except Exception:
        pass


async def _computer(ctx: ToolContext, action: str, url: str = "", selector: str = "", text: str = "",
                    key: str = "", x: int | None = None, y: int | None = None, dy: int = 0,
                    ms: int = 800, full_page: bool = False, script: str = "",
                    headless: bool = True, enter: bool = False, save_state: bool = True) -> ToolResult:
    action = (action or "").lower()
    try:
        import playwright  # noqa: F401
    except ImportError:
        return ToolResult("Playwright is not installed. Run: pip install 'argo-harness[computer]' && "
                          "playwright install chromium", is_error=True)
    if action in ("close", "quit"):
        await shutdown(ctx)
        return ToolResult("Browser closed.")
    try:
        state = await _ensure(ctx, headless=headless)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Failed to launch browser: {type(exc).__name__}: {exc}", is_error=True)
    page = state["page"]
    try:
        if action in ("launch", "goto", "navigate", "open"):
            url = _check_url(url)
            resp = await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(ms)
            status = resp.status if resp else "?"
            shot_path, data_url = await _shot(ctx, page, "goto")
            dom = await _dom_summary(page)
            state["history"].append({"action": action, "url": url})
            return ToolResult(f"Opened {url} (HTTP {status})\nScreenshot: {shot_path}\n\n{dom}",
                              metadata={"image": data_url, "url": url, "screenshot": shot_path})
        if action == "back":
            await page.go_back(); await page.wait_for_timeout(ms)
        heal_note = ""
        if action == "click":
            if selector or text:
                locator, used, heal_note = await _resolve(page, selector, text)
                if heal_note:
                    _record_heal(ctx, state, action, selector, used)
                await locator.click()
            elif x is not None and y is not None:
                await page.mouse.click(x, y)
            else:
                return ToolResult("click needs selector, text, or x/y", is_error=True)
            state["history"].append({"action": "click", "selector": selector, "text": text, "x": x, "y": y})
            await page.wait_for_timeout(ms)
        elif action == "type":
            if not selector:
                return ToolResult("type needs a selector", is_error=True)
            locator, used, heal_note = await _resolve(page, selector)
            if heal_note:
                _record_heal(ctx, state, action, selector, used)
            await locator.fill(text)
            if enter:
                await locator.press("Enter")
                await page.wait_for_timeout(ms)
            state["history"].append({"action": "type", "selector": selector, "text": text})
        elif action == "select":
            if not selector:
                return ToolResult("select needs a selector for the <select> element", is_error=True)
            locator, used, heal_note = await _resolve(page, selector)
            if heal_note:
                _record_heal(ctx, state, action, selector, used)
            try:
                await locator.select_option(value=text)
            except Exception:  # noqa: BLE001 - fall back to the visible label
                await locator.select_option(label=text)
            state["history"].append({"action": "select", "selector": selector, "text": text})
            await page.wait_for_timeout(ms)
        elif action == "press":
            await page.keyboard.press(key or "Enter")
            await page.wait_for_timeout(ms)
        elif action == "scroll":
            await page.mouse.wheel(0, dy or 800)
            await page.wait_for_timeout(ms)
        elif action == "wait":
            if selector:
                try:
                    await page.wait_for_selector(selector, timeout=max(1000, ms * 5))
                except Exception:
                    locator, used, heal_note = await _resolve(page, selector)
                    _record_heal(ctx, state, action, selector, used)
                    await locator.wait_for(state="visible", timeout=max(1000, ms * 5))
            else:
                await page.wait_for_timeout(ms)
        elif action == "eval":
            if not script:
                return ToolResult("eval needs script", is_error=True)
            result = await page.evaluate(script)
            return ToolResult(f"eval result:\n{json.dumps(result, default=str)[:8000]}")
        elif action == "extract":
            dom = await _dom_summary(page, max_chars=12000)
            return ToolResult(dom, metadata={"url": page.url})
        elif action == "screenshot":
            shot_path, data_url = await _shot(ctx, page, "shot", full_page=full_page)
            dom = await _dom_summary(page, max_chars=3000)
            return ToolResult(f"Screenshot: {shot_path}\n\n{dom}",
                              metadata={"image": data_url, "screenshot": shot_path, "url": page.url})
        else:
            return ToolResult(f"Unknown action '{action}'. Use launch/goto/click/type/select/press/scroll/wait/"
                              f"screenshot/extract/eval/back/close.", is_error=True)
        if save_state:
            await _save_state(ctx, state)
        shot_path, data_url = await _shot(ctx, page, action)
        dom = await _dom_summary(page)
        prefix = f"{heal_note}\n" if heal_note else ""
        return ToolResult(f"{prefix}{action} ok. URL: {page.url}\nScreenshot: {shot_path}\n\n{dom}",
                          metadata={"image": data_url, "screenshot": shot_path, "url": page.url})
    except Exception as exc:  # noqa: BLE001
        try:
            shot_path, data_url = await _shot(ctx, page, "error")
        except Exception:
            shot_path, data_url = "", ""
        return ToolResult(f"{action} failed: {type(exc).__name__}: {exc}\nScreenshot: {shot_path}",
                          is_error=True, metadata={"image": data_url} if data_url else {})


def register(reg: ToolRegistry) -> None:
    reg.add(
        "computer",
        "Control a real browser (Playwright). Returns a screenshot for vision models plus compact DOM text "
        "(headings, links, form fields). Actions: launch/goto, click (selector|text|x,y), type, select (choose an "
        "<option> by value or label), press, scroll, wait, screenshot, extract, eval, back, close. Use for "
        "UI-only flows; prefer fetch_url/APIs when possible.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["launch", "goto", "click", "type", "select", "press", "scroll",
                                                  "wait", "screenshot", "extract", "eval", "back", "close"]},
            "url": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"},
            "key": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"},
            "dy": {"type": "integer", "default": 800}, "ms": {"type": "integer", "default": 800},
            "full_page": {"type": "boolean", "default": False}, "script": {"type": "string"},
            "headless": {"type": "boolean", "default": True}, "enter": {"type": "boolean", "default": False},
            "save_state": {"type": "boolean", "default": True}},
         "required": ["action"]},
        _computer, read_only=False, timeout=180, max_output=30_000, category="computer",
    )
