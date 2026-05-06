---
name: web-fetch
description: Use when the user wants to fetch and read a webpage's content as markdown. Includes a 24h cache, optional JSON output, an SPA fallback via Playwright, and an SSRF guard that refuses non-public addresses.
---

# Web fetch

Run the bundled script with `uv run` so dependencies install on first use:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/web-fetch/web_fetch.py" <url>

## Common flags

- `--json` / `-j` — emit a JSON object instead of markdown.
- `--no-cache` — bypass the 24h cache.
- `--render` — force Playwright rendering (skip the SPA heuristic).
- `--timeout N` — request timeout in seconds (default 30).

## Behaviour

- Default output: markdown with title, status, and the page's main content.
- Cache lives at `~/.cache/web_fetch/` and expires after 24 hours.
- Responses larger than 10 MiB are rejected.
- URLs that resolve to private/internal addresses (loopback, RFC1918, link-local, cloud metadata) are refused — including across redirects.
