---
name: rss-monitor
description: Use when the user wants to manage RSS/Atom subscriptions — adding/removing feeds, fetching new entries, searching across stored entries, or triaging entries (mark read/important, tag).
---

# RSS monitor

Run the bundled script with `uv run`:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/rss-monitor/rss_monitor.py" <subcommand> [args...]

Add `--json` to any subcommand for machine-readable output. Use `--db PATH` to override the default reader database location.

## Config file

Pass `--config PATH` (placed before the subcommand) to drive defaults from a
JSON config in the same shape as `notion_gsheet_sync` and
`phantombuster-monitor`:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/rss-monitor/rss_monitor.py" \
        --config /path/to/config.json update

Print a worked example:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/rss-monitor/rss_monitor.py" example-config

Config keys:

- `statePath` — directory where state JSON and the reader DB live. Relative
  paths are resolved against the config file's directory; `~` is expanded.
  When set, defaults are `<statePath>/rss-monitor.sqlite` for `--db` and
  `<statePath>/rss_monitor.json` for `--state` on `update`/`poll`.
- `dbPath` — optional override for the DB filename or absolute path. Relative
  paths resolve inside `statePath`.
- `feeds` — optional list of `{url, tags}` (or bare URL strings). On
  `update`/`poll`, each entry is added to the DB if missing and any listed
  tags are applied. The list is additive — feeds not mentioned here are left
  alone.
- `updateScope` — `"all"` (default), `{"feed": "<url>"}`, or `{"tag": "<t>"}`.
  Applied to `update`/`poll` unless `--feed`/`--tag` is given explicitly.

CLI flags always win over config values.

`runner.sh` is a thin wrapper that calls `rss_monitor.py --config config.json
update` (default) or `... poll [flags...]`.

## Feed management

- `feed add <url> [--tag T ...]` — add a feed (rejects private/internal URLs).
- `feed list [--tag T] [--errors-only]` — list feeds.
- `feed remove <url>` — remove a feed.
- `feed tag <url> <tags...>` / `feed untag <url> <tags...>` — manage feed tags.

## Updating

- `update [--feed URL] [--tag T] [--state PATH]` — fetch new entries.
- `poll [--feed URL] [--tag T] [--interval SEC] [--iterations N] [--state PATH]` — run update repeatedly.

`--state PATH` writes a JSON status file after each run, aligned with the
`notion_gsheet_sync` and `phantombuster-monitor` state shape: `system`,
`lastRun`, `lastSuccess`, `lastError`, `status` (`Healthy`/`Attention`),
`materialChange`, `notes`, plus RSS-specific `scope`, `feedCount`,
`errorFeedCount`, `newEntryCount`. `lastSuccess` is preserved from the prior
state when a run fails. When `--config` provides a `statePath`, the state
file defaults to `<statePath>/rss_monitor.json`.

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
