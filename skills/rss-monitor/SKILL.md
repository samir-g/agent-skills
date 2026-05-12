---
name: rss-monitor
description: Use when the user wants to keep a local RSS/Atom mirror in sync with a config file and pull entries by time window — config-driven feed sync, fetch new entries, list entries since last run or within the past N days.
---

# RSS monitor

Config-driven RSS mirror. Three subcommands: `update`, `entries`, `feeds`.

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/rss-monitor/rss_monitor.py" \
        --config /path/to/config.json <subcommand>

Add `--json` (before the subcommand) for machine-readable output.

## Config

Required. Same shape as `notion_gsheet_sync` and `phantombuster-monitor`.

    {
      "statePath": "~/.openclaw/workspace/state",
      "feeds": [
        {"url": "https://example.com/feed.xml", "tags": ["ai", "research"]}
      ]
    }

Keys:

- `statePath` — directory for the SQLite DB and state JSON. Relative paths resolve against the config file's directory. `~` is expanded.
- `dbPath` — optional override for the DB filename or absolute path. Relative paths resolve inside `statePath`.
- `feeds` — list of `{url, tags}` (or bare URL strings). **Authoritative**: feeds present in the DB but absent from this list are deleted on `update`. Tags are also synced — declared tags are added, undeclared tags removed.

Print a worked example:

    uv run rss_monitor.py example-config

## Subcommands

### `update`

Syncs DB feeds with config (adds missing, removes extras, syncs tags), fetches new entries from every feed, writes state JSON.

State JSON shape (aligned with sibling skills):

- `system`, `lastRun`, `lastSuccess`, `previousSuccess`, `lastError`
- `status` (`Healthy` / `Attention`)
- `feedCount`, `errorFeedCount`, `newEntryCount`, `materialChange`, `notes`

`previousSuccess` snapshots the prior `lastSuccess` on each successful run — it's the cutoff `entries` uses by default.

`runner.sh` is a thin wrapper that runs `update` against the bundled `config.json`.

### `entries`

Default: lists entries added since the previous successful run (uses `previousSuccess` from the state file, filters by `entry.added`).

    entries

Override with `--since N` to instead list entries published in the past N days (integer):

    entries --since 1     # past day
    entries --since 7     # past week

### `feeds`

Lists feeds currently in the DB, with their tags and any last-fetch error.

## Behaviour

- Uses the [`reader`](https://reader.readthedocs.io/) library for storage and feed parsing.
- Refuses feed URLs that resolve to private/internal addresses (SSRF guard) when adding from config.
- Config is authoritative — the only way to manage feeds is by editing the config file.
