---
name: rss-monitor
description: Use when the user wants to manage RSS/Atom subscriptions — adding/removing feeds, fetching new entries, searching across stored entries, or triaging entries (mark read/important, tag).
---

# RSS monitor

Run the bundled script with `uv run`:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/rss-monitor/rss_monitor.py" <subcommand> [args...]

Add `--json` to any subcommand for machine-readable output. Use `--db PATH` to override the default reader database location.

## Feed management

- `feed add <url> [--tag T ...]` — add a feed (rejects private/internal URLs).
- `feed list [--tag T] [--errors-only]` — list feeds.
- `feed remove <url>` — remove a feed.
- `feed tag <url> <tags...>` / `feed untag <url> <tags...>` — manage feed tags.

## Updating

- `update [--feed URL] [--tag T]` — fetch new entries.
- `poll [--interval SEC] [--iterations N]` — run update repeatedly.

## Reading

- `new [--tag T] [--feed URL] [--limit N] [--since 24h|3d|1w]` — list unread entries.
- `list [--read|--unread] [--important] [--tag T] [--feed URL] [--limit N] [--since ...]`
- `show <hash>` — full entry content.
- `search <query> [--tag T]` — full-text search across entries.

## Mutation

Hashes are the 16-char prefix shown by `new`, `list`, and `search`.

- `read <hash...>` / `unread <hash...>`
- `important <hash...>` / `unimportant <hash...>`
- `tag <hash> <tags...>` / `untag <hash> <tags...>`

## Bulk triage

`triage` reads decisions from stdin as JSON:

    [
      {"hash": "abc...", "read": true, "important": false,
       "tags_add": ["ai"], "tags_remove": []}
    ]

## Behaviour

- Uses the [`reader`](https://reader.readthedocs.io/) library for storage and feed parsing.
- SQLite database at reader's default location unless `--db` is set.
- Refuses feed URLs that resolve to private/internal addresses (SSRF guard).
- Entry hashes are 16 hex chars (64 bits) of SHA-256 over `feed_url + entry_id`.
