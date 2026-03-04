"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web with a three-tier fallback: Brave → Tavily → DuckDuckGo.

    The engine is selected at call time based on which API keys are available:
    - Brave Search is used when ``BRAVE_API_KEY`` (or ``api_key``) is set.
    - Tavily is used when ``TAVILY_API_KEY`` (or ``tavily_api_key``) is set.
    - DuckDuckGo (``ddgs``) is used as a keyless final fallback.
    """

    def __init__(
        self,
        api_key: str | None = None,
        tavily_api_key: str | None = None,
        max_results: int = 5,
        proxy: str | None = None,
    ):
        self._init_api_key = api_key
        self._init_tavily_api_key = tavily_api_key
        self.max_results = max_results
        self.proxy = proxy

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Search the web. Returns titles, URLs, and snippets."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {
                    "type": "integer",
                    "description": "Results (1-10)",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        }

    @property
    def brave_api_key(self) -> str:
        """Resolve Brave API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("BRAVE_API_KEY", "")

    @property
    def tavily_api_key(self) -> str:
        """Resolve Tavily API key at call time so env/config changes are picked up."""
        return self._init_tavily_api_key or os.environ.get("TAVILY_API_KEY", "")

    async def execute(self, **kwargs: Any) -> str:
        query: str = kwargs["query"]
        n = min(max(int(kwargs.get("count") or self.max_results), 1), 10)

        if self.brave_api_key:
            logger.debug("WebSearch: using Brave")
            return await self._search_brave(query, n)
        if self.tavily_api_key:
            logger.debug("WebSearch: Brave key absent, falling back to Tavily")
            return await self._search_tavily(query, n)
        logger.debug("WebSearch: no API keys configured, falling back to DuckDuckGo")
        return await self._search_ddg(query, n)

    async def _search_brave(self, query: str, n: int) -> str:
        """Search via Brave Search API."""
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self.brave_api_key,
                    },
                    timeout=10.0,
                )
                r.raise_for_status()

            results = r.json().get("web", {}).get("results", [])[:n]
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query} [via Brave]\n"]
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except httpx.ProxyError as e:
            logger.error("WebSearch (Brave) proxy error: {}", e)
            return f"Proxy error: {e}"
        except Exception as e:
            logger.error("WebSearch (Brave) error: {}", e)
            return f"Error: {e}"

    async def _search_tavily(self, query: str, n: int) -> str:
        """Search via Tavily Search API (sync SDK wrapped with asyncio.to_thread)."""
        import asyncio

        try:
            from tavily import TavilyClient

            def _call() -> list[dict[str, Any]]:
                client = TavilyClient(api_key=self.tavily_api_key)
                response = client.search(query=query, max_results=n, search_depth="basic")
                return response.get("results", [])

            results: list[dict[str, Any]] = await asyncio.to_thread(_call)
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query} [via Tavily]\n"]
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if snippet := item.get("content"):
                    lines.append(f"   {snippet}")
            return "\n".join(lines)
        except ImportError:
            logger.warning("WebSearch: tavily-python not installed, falling back to DuckDuckGo")
            return await self._search_ddg(query, n)
        except Exception as e:
            logger.error("WebSearch (Tavily) error: {}", e)
            return f"Error: {e}"

    async def _search_ddg(self, query: str, n: int) -> str:
        """Search via DuckDuckGo (ddgs package, sync wrapped with asyncio.to_thread)."""
        import asyncio

        try:
            from ddgs import DDGS

            def _call() -> list[dict[str, Any]]:
                return list(DDGS().text(query, max_results=n))  # type: ignore[arg-type]

            results: list[dict[str, Any]] = await asyncio.to_thread(_call)
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query} [via DuckDuckGo]\n"]
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('href', '')}")
                if snippet := item.get("body"):
                    lines.append(f"   {snippet}")
            return "\n".join(lines)
        except ImportError:
            logger.error("WebSearch: ddgs not installed. Run: uv add ddgs")
            return "Error: No search backend available. Install ddgs: uv add ddgs"
        except Exception as e:
            logger.error("WebSearch (DuckDuckGo) error: {}", e)
            return f"Error: {e}"


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    def __init__(self, max_chars: int = 50000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "Fetch URL and extract readable content (HTML → markdown/text)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "extractMode": {
                    "type": "string",
                    "enum": ["markdown", "text"],
                    "default": "markdown",
                },
                "maxChars": {"type": "integer", "minimum": 100},
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> str:
        from readability import Document

        url: str = kwargs["url"]
        extract_mode: str = kwargs.get("extractMode", "markdown")
        max_chars_param: int | None = kwargs.get("maxChars")
        max_chars = max_chars_param or self.max_chars
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps(
                {"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False
            )

        try:
            logger.debug("WebFetch: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
                proxy=self.proxy,
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = (
                    self._to_markdown(doc.summary())
                    if extract_mode == "markdown"
                    else _strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": str(r.url),
                    "status": r.status_code,
                    "extractor": extractor,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                },
                ensure_ascii=False,
            )
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f"[{_strip_tags(m[2])}]({m[1]})",
            html,
            flags=re.I,
        )
        text = re.sub(
            r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
            lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I
        )
        text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
        text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
        return _normalize(_strip_tags(text))
