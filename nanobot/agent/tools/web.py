"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web using Brave Search API."""
    
    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }
    
    def __init__(self, api_key: str | None = None, max_results: int = 5):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = max_results
    
    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        if not self.api_key:
            return "Error: BRAVE_API_KEY not configured"
        
        try:
            n = min(max(count or self.max_results, 1), 10)
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0
                )
                r.raise_for_status()
            
            results = r.json().get("web", {}).get("results", [])
            if not results:
                return f"No results for: {query}"
            
            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


def _extract_tweet_id(url: str) -> str | None:
    """Extract tweet/status ID from x.com or twitter.com URLs."""
    p = urlparse(url)
    if p.netloc.replace("www.", "") not in ("x.com", "twitter.com"):
        return None
    # Match /USER/status/ID or /i/status/ID
    m = re.search(r'/status/(\d+)', p.path)
    return m.group(1) if m else None


async def _fetch_tweet(tweet_id: str) -> dict | None:
    """Fetch tweet content via fxtwitter API."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"https://api.fxtwitter.com/status/{tweet_id}",
                                 headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            return None
        t = data["tweet"]
        author = t.get("author", {})
        parts = [
            f"**@{author.get('screen_name', '?')}** ({author.get('name', '')})",
            f"Followers: {author.get('followers', 0):,}",
            "",
            t.get("text", ""),
            "",
            f"Likes: {t.get('likes', 0):,} | Retweets: {t.get('retweets', 0):,} | "
            f"Replies: {t.get('replies', 0):,} | Views: {t.get('views', 0):,}",
            f"Posted: {t.get('created_at', 'unknown')}",
        ]
        # Include media if present
        if media := t.get("media"):
            if photos := media.get("photos"):
                parts.append(f"\nImages: {', '.join(p.get('url', '') for p in photos)}")
            if videos := media.get("videos"):
                parts.append(f"\nVideos: {', '.join(v.get('url', '') for v in videos)}")
        # Include quote tweet if present
        if quote := t.get("quote"):
            q_author = quote.get("author", {})
            parts.extend([
                "",
                f"> Quoted @{q_author.get('screen_name', '?')}:",
                f"> {quote.get('text', '')}",
            ])
        return {"url": t.get("url", ""), "text": "\n".join(parts)}
    except Exception:
        return None


def _extract_youtube_url(url: str) -> str | None:
    """Extract canonical YouTube video URL, or None if not a YouTube link."""
    p = urlparse(url)
    host = p.netloc.replace("www.", "")
    if host in ("youtube.com", "m.youtube.com"):
        from urllib.parse import parse_qs
        vid = parse_qs(p.query).get("v", [None])[0]
        return f"https://www.youtube.com/watch?v={vid}" if vid else None
    if host == "youtu.be":
        vid = p.path.lstrip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={vid}" if vid else None
    return None


async def _fetch_youtube(canonical_url: str) -> dict | None:
    """Fetch YouTube video metadata: title, channel, description via oembed + page scrape."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # oembed for title + channel
            oembed = await client.get(
                "https://www.youtube.com/oembed",
                params={"url": canonical_url, "format": "json"},
                headers={"User-Agent": USER_AGENT},
            )
            oembed.raise_for_status()
            meta = oembed.json()

            # Page scrape for description
            page = await client.get(canonical_url, headers={"User-Agent": USER_AGENT})
            desc = ""
            m = re.search(r'"shortDescription":"(.*?)"(?:,|})', page.text)
            if m:
                desc = m.group(1).replace("\\n", "\n")[:2000]

        parts = [
            f"**{meta.get('title', '')}**",
            f"Channel: [{meta.get('author_name', '')}]({meta.get('author_url', '')})",
        ]
        if desc:
            parts.extend(["", desc])
        parts.append(f"\nURL: {canonical_url}")
        return {"url": canonical_url, "text": "\n".join(parts)}
    except Exception:
        return None


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        from readability import Document

        max_chars = maxChars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        # Handle x.com / twitter.com URLs via fxtwitter API
        tweet_id = _extract_tweet_id(url)
        if tweet_id:
            result = await _fetch_tweet(tweet_id)
            if result:
                return json.dumps({"url": url, "finalUrl": result["url"], "status": 200,
                                  "extractor": "fxtwitter", "truncated": False,
                                  "length": len(result["text"]), "text": result["text"]}, ensure_ascii=False)
            return json.dumps({"error": "Failed to fetch tweet via fxtwitter API", "url": url}, ensure_ascii=False)

        # Handle YouTube URLs via oembed + page scrape
        yt_url = _extract_youtube_url(url)
        if yt_url:
            result = await _fetch_youtube(yt_url)
            if result:
                return json.dumps({"url": url, "finalUrl": result["url"], "status": 200,
                                  "extractor": "youtube", "truncated": False,
                                  "length": len(result["text"]), "text": result["text"]}, ensure_ascii=False)
            return json.dumps({"error": "Failed to fetch YouTube video metadata", "url": url}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
            
            ctype = r.headers.get("content-type", "")
            
            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"
            
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            
            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
    
    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
