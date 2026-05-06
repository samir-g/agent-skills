#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31",
#     "beautifulsoup4>=4.12",
#     "markdownify>=0.11",
#     "playwright>=1.40",
# ]
# ///
"""Fetch and analyse website content. See design doc for full spec."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

CACHE_DIR = Path.home() / ".cache" / "web_fetch"
CACHE_TTL = timedelta(hours=24)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
SPA_MIN_TEXT = 500
STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form")
MAX_REDIRECTS = 10
MAX_BYTES = 10 * 1024 * 1024


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    """Reject malformed URLs and any host that resolves to a non-public address.

    Guards against SSRF: localhost, RFC1918, link-local (incl. cloud metadata
    169.254.169.254), multicast, and reserved ranges. Re-run on every redirect
    hop, since the original URL passing does not imply the redirect target does.
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UnsafeURL(f"invalid url: {url}") from e
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURL(f"missing host in url: {url}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURL(f"could not resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or not ip.is_global):
            raise UnsafeURL(f"refusing non-public address: {host} ({ip})")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    title: str = ""
    description: str = ""
    markdown: str = ""
    text: str = ""
    headings: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    fetched_at: str = ""
    rendered: bool = False
    cached: bool = False


def cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def load_cache(url: str) -> FetchResult | None:
    path = cache_path(url)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        fetched = datetime.fromisoformat(data["fetched_at"])
        if datetime.now(timezone.utc) - fetched > CACHE_TTL:
            return None
        result = FetchResult(**data)
        result.cached = True
        return result
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def save_cache(result: FetchResult) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(result)
    data["cached"] = False
    cache_path(result.url).write_text(json.dumps(data, indent=2))


def looks_like_spa(html: str, text: str) -> bool:
    if len(text) >= SPA_MIN_TEXT:
        return False
    markers = (
        '<div id="root"',
        "<div id='root'",
        '<div id="app"',
        "<div id='app'",
        "enable javascript",
        "please enable js",
    )
    lower = html.lower()
    return any(m in lower for m in markers)


def fetch_requests(url: str, timeout: int) -> tuple[str, str, int]:
    session = requests.Session()
    current = url
    for _ in range(MAX_REDIRECTS):
        check_url(current)
        resp = session.get(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                break
            current = urljoin(current, location)
            continue
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > MAX_BYTES:
            resp.close()
            raise ValueError(f"response too large: {cl} bytes (cap {MAX_BYTES})")
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_content(8192):
            size += len(chunk)
            if size > MAX_BYTES:
                resp.close()
                raise ValueError(f"response exceeded cap of {MAX_BYTES} bytes")
            chunks.append(chunk)
        # Hand the bounded body back to requests so .text/.apparent_encoding work.
        resp._content = b"".join(chunks)
        resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text, resp.url, resp.status_code
    raise requests.TooManyRedirects(f"too many redirects starting at {url}")


def fetch_playwright(url: str, timeout: int) -> tuple[str, str, int]:
    check_url(url)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed; skipping render", file=sys.stderr)
        raise

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                print("installing chromium (one-time)...", file=sys.stderr)
                import subprocess
                subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    check=True,
                )
                browser = p.chromium.launch(headless=True)
            else:
                raise
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()

            def _route_guard(route):
                try:
                    check_url(route.request.url)
                except UnsafeURL:
                    route.abort()
                    return
                route.continue_()

            page.route("**/*", _route_guard)
            resp = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            html = page.content()
            final_url = page.url
            status = resp.status if resp else 0
            return html, final_url, status
        finally:
            browser.close()


def pick_main(soup: BeautifulSoup) -> Tag:
    for selector in ("main", "article", '[role="main"]', "#main", "#content"):
        node = soup.select_one(selector)
        if node:
            return node
    body = soup.body or soup
    for tag_name in STRIP_TAGS:
        for tag in body.find_all(tag_name):
            tag.decompose()
    return body


def extract(html: str, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property")
        content = tag.get("content")
        if name and content:
            meta[name.lower()] = content.strip()
    description = meta.get("description") or meta.get("og:description", "")

    main = pick_main(BeautifulSoup(html, "html.parser"))

    headings = [
        {"level": int(h.name[1]), "text": h.get_text(strip=True)}
        for h in main.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        if h.get_text(strip=True)
    ]

    links = []
    for a in main.find_all("a", href=True):
        href = urljoin(final_url, a["href"])
        text = a.get_text(strip=True)
        if href.startswith(("http://", "https://")):
            links.append({"href": href, "text": text})

    md = markdownify(str(main), heading_style="ATX", strip=["script", "style"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))

    return {
        "title": title,
        "description": description,
        "markdown": md,
        "text": text,
        "headings": headings,
        "links": links,
        "meta": meta,
    }


def fetch(url: str, timeout: int, force_render: bool) -> FetchResult:
    rendered = False
    if force_render:
        html, final_url, status = fetch_playwright(url, timeout)
        rendered = True
    else:
        html, final_url, status = fetch_requests(url, timeout)
        probe = BeautifulSoup(html, "html.parser")
        for tag_name in STRIP_TAGS:
            for t in probe.find_all(tag_name):
                t.decompose()
        probe_text = probe.get_text(" ", strip=True) if probe else ""
        if looks_like_spa(html, probe_text):
            print("page looks like SPA, retrying with playwright...", file=sys.stderr)
            try:
                html, final_url, status = fetch_playwright(url, timeout)
                rendered = True
            except Exception as e:
                print(f"playwright fallback failed: {e}", file=sys.stderr)

    data = extract(html, final_url)
    return FetchResult(
        url=url,
        final_url=final_url,
        status=status,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        rendered=rendered,
        cached=False,
        **data,
    )


def render_markdown(r: FetchResult) -> str:
    header = (
        f"# {r.title or '(no title)'}\n"
        f"**URL:** {r.final_url}   **Status:** {r.status}   "
        f"**Rendered:** {str(r.rendered).lower()}   "
        f"**Cached:** {str(r.cached).lower()}\n"
        f"\n---\n\n"
    )
    return header + r.markdown


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and analyse website content."
    )
    parser.add_argument("url")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--no-cache", action="store_true", help="Bypass cache")
    parser.add_argument("--render", action="store_true", help="Force Playwright")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout seconds")
    args = parser.parse_args()

    try:
        check_url(args.url)
    except UnsafeURL as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    result: FetchResult | None = None
    if not args.no_cache and not args.render:
        result = load_cache(args.url)

    if result is None:
        try:
            result = fetch(args.url, args.timeout, args.render)
        except UnsafeURL as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except requests.RequestException as e:
            print(f"fetch failed: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        save_cache(result)

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
