"""Web tools: pluggable search (DuckDuckGo/Tavily/Brave/SearxNG) and content fetching."""
from __future__ import annotations

import html as htmllib
import io
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from ..core.security import url_contains_secret, wrap_untrusted
from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry, truncate_output

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _client(timeout: float = 25.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def _check_url(url: str, guard_secrets: bool = True) -> str:
    if guard_secrets and url_contains_secret(url):
        raise ValueError("URL appears to contain a credential")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if host in ("169.254.169.254", "metadata.google.internal"):
        raise ValueError("blocked metadata endpoint")
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_link_local or ip.is_multicast:
                raise ValueError(f"blocked address: {ip}")
    except socket.gaierror:
        raise ValueError(f"cannot resolve host: {host}")
    return url


async def _ddg(query: str, n: int) -> list[dict]:
    async with _client() as c:
        resp = await c.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "wt-wt"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        out = []
        for res in soup.select(".result")[:n]:
            a = res.select_one("a.result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            snippet_el = res.select_one(".result__snippet") or res.select_one(".result__body")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            out.append({"title": a.get_text(" ", strip=True), "url": href, "snippet": snippet})
        return out


def _decode_bing(href: str) -> str:
    from base64 import urlsafe_b64decode
    m = re.search(r"[?&]u=a1([^&]+)", href)
    if not m:
        return href
    raw = m.group(1)
    raw += "=" * (-len(raw) % 4)
    try:
        return urlsafe_b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return href


async def _bing(query: str, n: int) -> list[dict]:
    async with _client() as c:
        resp = await c.get("https://www.bing.com/search",
                           params={"q": query, "count": n, "setlang": "en"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        out = []
        for li in soup.select("li.b_algo")[:n]:
            a = li.select_one("h2 a")
            if not a:
                continue
            p_el = li.select_one("p") or li.select_one(".b_caption")
            out.append({"title": a.get_text(" ", strip=True),
                        "url": _decode_bing(a.get("href", "")),
                        "snippet": p_el.get_text(" ", strip=True) if p_el else ""})
        return out


async def _tavily(query: str, n: int, key: str) -> list[dict]:
    async with _client(30) as c:
        resp = await c.post("https://api.tavily.com/search",
                            json={"api_key": key, "query": query, "max_results": n,
                                  "include_answer": False, "search_depth": "advanced"})
        resp.raise_for_status()
        data = resp.json()
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("content", "")} for r in data.get("results", [])]


async def _brave(query: str, n: int, key: str) -> list[dict]:
    async with _client() as c:
        resp = await c.get("https://api.search.brave.com/res/v1/web/search",
                           params={"q": query, "count": n},
                           headers={"X-Subscription-Token": key, "Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("description", "")}
                for r in data.get("web", {}).get("results", [])]


async def _searxng(query: str, n: int, base: str) -> list[dict]:
    async with _client() as c:
        resp = await c.get(f"{base.rstrip('/')}/search",
                           params={"q": query, "format": "json"}, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("content", "")} for r in data.get("results", [])[:n]]


def _untrusted(ctx: ToolContext, text: str, source: str, kind: str = "external") -> str:
    enabled = getattr(getattr(ctx.cfg, "security", None), "wrap_untrusted", True)
    return wrap_untrusted(text, source, kind) if enabled else text


async def _web_search(ctx: ToolContext, query: str, max_results: int = 8,
                      provider: str | None = None) -> ToolResult:
    provider = (provider or ctx.cfg.search.provider).lower()
    n = max(1, min(int(max_results), 20))
    errors = []
    chain = [provider]
    for fb in (["duckduckgo", "bing"] if provider == "duckduckgo" else ["duckduckgo", "bing"]):
        if fb not in chain:
            chain.append(fb)
    for attempt in chain:
        try:
            if attempt == "tavily" and ctx.cfg.search.tavily_api_key:
                results = await _tavily(query, n, ctx.cfg.search.tavily_api_key)
            elif attempt == "brave" and ctx.cfg.search.brave_api_key:
                results = await _brave(query, n, ctx.cfg.search.brave_api_key)
            elif attempt == "searxng" and ctx.cfg.search.searxng_url:
                results = await _searxng(query, n, ctx.cfg.search.searxng_url)
            elif attempt == "bing":
                results = await _bing(query, n)
            else:
                results = await _ddg(query, n)
            results = [r for r in results if r.get("url")]
            if results:
                lines = [f"Search results for: {query}  (provider: {attempt})"]
                for i, r in enumerate(results, 1):
                    lines.append(f"\n[{i}] {r['title']}\n    {r['url']}\n    {r['snippet'][:500]}")
                lines.append("\nNext: call fetch_url on the most relevant URLs.")
                return ToolResult(_untrusted(ctx, "\n".join(lines), f"search provider: {attempt}", "search results"),
                                  metadata={"results": results, "provider": attempt, "trusted": False})
            errors.append(f"{attempt}: no results")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{attempt}: {type(exc).__name__}: {exc}")
    return ToolResult("Web search failed. " + "; ".join(errors), is_error=True)


def _dom_to_text(node: Tag | NavigableString | None, depth: int = 0) -> str:
    if node is None or depth > 40:
        return ""
    if isinstance(node, NavigableString):
        text = str(node)
        return re.sub(r"\s+", " ", text)
    if not isinstance(node, Tag):
        return ""
    name = node.name
    if name in {"script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside"}:
        return ""
    if name == "img":
        alt = node.get("alt", "")
        return f"![{alt}]({node.get('src', '')})" if alt else ""
    if name == "a":
        label = _dom_to_text_children(node, depth + 1).strip()
        href = node.get("href", "")
        if label and href and not href.startswith("javascript:"):
            return f"[{label}]({href})"
        return label
    inner = _dom_to_text_children(node, depth + 1)
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"\n\n{'#' * int(name[1])} {inner.strip()}\n"
    if name == "p":
        return f"\n\n{inner.strip()}\n"
    if name == "li":
        return f"\n- {inner.strip()}"
    if name == "blockquote":
        return f"\n> {inner.strip()}\n"
    if name == "pre":
        return f"\n```\n{node.get_text()}\n```\n"
    if name in {"br"}:
        return "\n"
    if name in {"tr"}:
        return f"\n{inner.strip()}"
    if name in {"td", "th"}:
        return f" {inner.strip()} |"
    if name in {"article", "main", "section", "div"}:
        return f"\n{inner.strip()}\n" if inner.strip() else ""
    return inner


def _dom_to_text_children(node: Tag, depth: int) -> str:
    return "".join(_dom_to_text(child, depth) for child in node.children)


def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = _dom_to_text(root) or ""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if base_url:
        text = re.sub(r"\]\((/[^)]*)\)", lambda m: f"]({urljoin(base_url, m.group(1))})", text)
    return title, text


async def _fetch_url(ctx: ToolContext, url: str, max_chars: int = 30_000,
                     extract: str = "markdown") -> ToolResult:
    try:
        guard = getattr(getattr(ctx.cfg, "security", None), "block_secret_egress", True)
        url = _check_url(url, guard_secrets=guard)
    except ValueError as exc:
        return ToolResult(f"Refused URL: {exc}", is_error=True)
    try:
        async with _client(45) as c:
            resp = await c.get(url, headers={"Accept": "text/html,application/xhtml+xml,application/pdf,application/json;q=0.9,*/*;q=0.8"})
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Fetch failed: {type(exc).__name__}: {exc}", is_error=True)
    ct = resp.headers.get("content-type", "").lower()
    status = resp.status_code
    if resp.status_code >= 400:
        return ToolResult(f"HTTP {status} fetching {url}", is_error=True)
    if "application/pdf" in ct or url.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(resp.content))
            pages = [p.extract_text() or "" for p in reader.pages[:80]]
            text = "\n\n".join(pages)
            title = (reader.metadata.title if reader.metadata else "") or url
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"PDF parse failed: {exc}", is_error=True)
    elif "application/json" in ct:
        try:
            text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
        except Exception:
            text = resp.text
        title = url
    elif "text/" in ct or "xml" in ct or not ct:
        if extract == "raw":
            text, title = resp.text, url
        else:
            title, text = html_to_markdown(resp.text, str(resp.url))
    else:
        return ToolResult(f"Unsupported content-type {ct} ({len(resp.content)} bytes) at {url}", is_error=True)

    full_len = len(text)
    if full_len > max_chars:
        text, meta = truncate_output(ctx, text, max_chars, label="fetch")
    else:
        meta = None
    header = f"# {title or 'Fetched page'}\nSource: {resp.url}\nContent-Type: {ct} | HTTP {status}\n\n"
    body = _untrusted(ctx, text, str(resp.url), "web page")
    return ToolResult(header + body, metadata={**(meta or {}), "url": str(resp.url), "chars": full_len,
                                               "trusted": False})


def register(reg: ToolRegistry) -> None:
    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add("web_search",
            "Search the web. Returns titles, URLs and snippets. Use fetch_url to read a result. "
            "Providers: duckduckgo (keyless, default), tavily, brave, searxng.",
            params({"query": {"type": "string"}, "max_results": {"type": "integer", "default": 8},
                    "provider": {"type": "string"}}, ["query"]),
            _web_search, read_only=True, parallel_safe=True, timeout=60)
    reg.add("fetch_url",
            "Fetch a URL and extract readable markdown (HTML), text (PDF), or JSON. "
            "Long content is truncated to a spool file.",
            params({"url": {"type": "string"}, "max_chars": {"type": "integer", "default": 30000},
                    "extract": {"type": "string", "enum": ["markdown", "raw"], "default": "markdown"}}, ["url"]),
            _fetch_url, read_only=True, parallel_safe=True, timeout=90, max_output=40_000)
    register_research(reg)


async def _wikipedia_search(ctx: ToolContext, query: str, max_results: int = 5) -> ToolResult:
    async with _client() as c:
        resp = await c.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": max(1, min(int(max_results), 10))})
        resp.raise_for_status()
        rows = resp.json().get("query", {}).get("search", [])
    if not rows:
        return ToolResult(f"No Wikipedia results for {query!r}", is_error=True)
    lines = [f"Wikipedia results for: {query}"]
    for i, r in enumerate(rows, 1):
        slug = r["title"].replace(" ", "_")
        snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
        lines.append(f"\n[{i}] {r['title']}\n    https://en.wikipedia.org/wiki/{slug}\n    {htmllib.unescape(snippet)}")
    return ToolResult(_untrusted(ctx, "\n".join(lines), "wikipedia", "search results"),
                      metadata={"results": rows, "trusted": False})


async def _arxiv_search(ctx: ToolContext, query: str, max_results: int = 5,
                        sort_by: str = "relevance") -> ToolResult:
    from xml.etree import ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    async with _client(40) as c:
        resp = await c.get("https://export.arxiv.org/api/query", params={
            "search_query": f"all:{query}", "start": 0,
            "max_results": max(1, min(int(max_results), 20)),
            "sortBy": sort_by, "sortOrder": "descending"})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    entries = root.findall("a:entry", ns)
    if not entries:
        return ToolResult(f"No arXiv results for {query!r}", is_error=True)
    lines = [f"arXiv results for: {query}"]
    for i, e in enumerate(entries, 1):
        title = " ".join((e.findtext("a:title", "", ns) or "").split())
        link = e.findtext("a:id", "", ns)
        authors = ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:5])
        summary = " ".join((e.findtext("a:summary", "", ns) or "").split())[:600]
        published = e.findtext("a:published", "", ns)[:10]
        lines.append(f"\n[{i}] {title}\n    {link}\n    {authors} ({published})\n    {summary}")
    return ToolResult(_untrusted(ctx, "\n".join(lines), "arxiv", "search results"),
                      metadata={"count": len(entries), "trusted": False})


def register_research(reg: ToolRegistry) -> None:
    def params(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    reg.add("wikipedia_search", "Search Wikipedia (reliable, keyless). Then fetch_url an article for depth.",
            params({"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}}, ["query"]),
            _wikipedia_search, read_only=True, timeout=40)
    reg.add("arxiv_search", "Search arXiv papers (titles, authors, abstracts, links).",
            params({"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5},
                    "sort_by": {"type": "string", "enum": ["relevance", "lastUpdatedDate", "submittedDate"],
                                "default": "relevance"}}, ["query"]),
            _arxiv_search, read_only=True, timeout=60)
