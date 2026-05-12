---
name: phantombuster-monitor
description: Use when the user wants to launch or fetch a PhantomBuster LinkedIn agent run and archive normalized posts. A co-located Notion sync is run by the shared `runner.sh`.
---

# PhantomBuster monitor

Two stdlib-only scripts:

- `phantombuster_linkedin_monitor.py` — primary entrypoint. Launches or fetches a PhantomBuster agent container, normalizes the result, archives a per-run directory, and dedupes new posts against state. Writes `lastArchiveDir` into state so the runner can hand the archive dir to the Notion sync.
- `phantombuster_notion_sync.py` — destination-side script. Reads an archived run directory and writes new pages to a Notion database. Also runs standalone against any archived run dir.

## Required env

- `PHANTOMBUSTER_API_KEY` — required by the monitor (override the var name with `--api-key-env`).
- `NOTION_TOKEN` — required by the Notion sync.
- `NOTION_VERSION` — optional, defaults to `2025-09-03`.

Both scripts read these from any `KEY=VALUE` files listed in the config under `envFiles` (string or list) or `envFile` (string), and fall back to the process env. Repeatable `--env-file PATH` flags override the config. Values from later env files override earlier ones.

## Monitor

Print a worked example config:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/phantombuster-monitor/phantombuster_linkedin_monitor.py" --print-example-config

Run:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/phantombuster-monitor/phantombuster_linkedin_monitor.py" \
        --config /path/to/config.json

Useful flags:

- `--dry-run` — print normalized items, skip state writes.
- `--container-id ID` — archive a specific historical container instead of the latest.
- `--recent-window SECONDS` — overrides config `recentRunWindowSeconds`. Only meaningful in `mode: launch`.
- `--force-launch` — bypass the recent-run check in `mode: launch`; always launch a new container.
- `--max-item-age SECONDS` — overrides config `maxItemAgeSeconds`. Drop items whose `publishedAt` parses to older than this. 0 disables.
- `--print-example-config` — print example monitor config JSON.

State layout (everything is created inside the directory named by `statePath`):

- `phantombuster_monitor.json` — running dedup map, last-run metadata. Includes `lastArchiveDir`, which `sync.py` reads to find the run dir to import.
- `phantombuster_monitor_runs/<YYYY-MM-DD-HH-MM-SS>/` — per-run archive: `raw_result.json` (untouched payload from PhantomBuster), `normalized_items.json` (everything kept after the bad-record / age filter), `content_items.json` (subset that's actually content: items with `action == "Post"` OR `type == "Article"`), `dropped_items.json` (`{"error": [...], "tooOld": [...]}` for forensics), `summary.json` (counts: `contentItems`, `postItems`, `articleItems`, `otherItems`, `droppedErrorRecords`, `droppedOldRecords`).

Monitor config keys:

- `agentId` (required) — PhantomBuster agent ID.
- `agentLabel` — human-readable label used in archive directory names.
- `mode` — `fetch-latest` (default) reads the most recent finished container; `launch` triggers a new run and waits for completion (subject to recent-run reuse, see below).
- `launchArgument` — passed to PhantomBuster on `launch`.
- `saveArgument` — boolean, optional, also `launch` only.
- `recentRunWindowSeconds` — only meaningful in `mode: launch`. If the latest finished container ended within this many seconds, it is reused instead of launching a new one. If the latest container is still in progress, the monitor attaches to it (waits for completion) instead of launching a duplicate. Default `3600` (1 hour). `0` disables — always launch. The chosen path is recorded in `state.lastContainerSource` (`launched` / `reused-recent` / `attached-running` / `fetch-latest` / `container-id`).
- `maxItemAgeSeconds` — drop items whose normalized `publishedAt` parses to older than this many seconds, before archiving. Default `0` (disabled). `604800` (7 days) is a sensible value when the agent returns real `postTimestamp` values; for scrapers where `publishedAt` is the scrape time (e.g. LinkedIn Activity Extractor articles), the filter is effectively a no-op. Items with unparseable `publishedAt` are kept. Scraper error placeholders (rows with `raw.error` set, or no `postUrl` and no `text`) are always dropped regardless of this setting. Dropped items are written to `dropped_items.json` in the run dir, and counts surface as `state.lastDroppedErrorCount` and `state.lastDroppedOldCount`.
- `statePath` — directory that holds all generated state. The script writes `phantombuster_monitor.json` and a `phantombuster_monitor_runs/` subdir inside it. Defaults to `state/` next to the script. Relative paths are resolved against the config file's directory; `~` is expanded. The monitor writes `status: "Healthy"` (or `"Attention"` on failure), `lastRun`, `lastError`, `notes`, etc. on every run.
- `envFiles` / `envFile` — optional path or list of paths to `KEY=VALUE` env files providing `PHANTOMBUSTER_API_KEY` (and, for the Notion sync, `NOTION_TOKEN`). Overridden by `--env-file`.

## Notion sync

The sync is fully driven by the config; the only CLI flag is `--config`:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/phantombuster-monitor/sync.py" \
        --config /path/to/config.json

It reads the run directory from `state.lastArchiveDir`, the items file (within that run dir) from `notionImport.itemsFile`, the property mapping from `notionImport.properties`, dedup property names from `notionImport.dedupBy`, and any relation lookups from `notionImport.lookups`. Credentials come from `envFiles` / `envFile` (or the process env).

Stdout (always JSON):

    {"created": N, "skipped": N, "notionImport": {...metadata...} | null}

Re-running is safe: any item whose dedup-property values match an existing page is skipped.

### Notion config keys

- `notionImport.enabled` — `true` to enable; `false` makes the script no-op.
- `notionImport.databaseId` *(required)* — target Notion database ID.
- `notionImport.itemsFile` — file within the archive run dir to read. Default `normalized_items.json`. Set to `content_items.json` to sync only posts and articles.
- `notionImport.dedupBy` — Notion property name (string) or list of names. Existing pages in the destination DB are queried, and any incoming item whose values for these properties match an existing page is skipped. Items with all-empty dedup values are also skipped. Omit to always create.
- `notionImport.lookups` — `{name: {databaseId, matchProperty}}`. Each lookup pages through the named DB once and indexes pages by the value of `matchProperty` (URL stripped of trailing `/`). Referenced by `relation` properties via `lookup: <name>`.
- `notionImport.properties` *(required)* — ordered list of property specs (see below).
- `notionImport.statePath` — fallback state directory used only if no top-level `statePath` is set. Resolution order: top-level `statePath` > `notionImport.statePath` > no state file written. Same semantics as the top-level key (directory containing `phantombuster_monitor.json`). When set, the sync writes `notionImport.status`, `lastRun`, `lastError`, `notes`, `lastRunDir`, plus a metadata block (created/skipped/databaseId/runId).

### Property spec

Each entry in `notionImport.properties` is a dict with:

- `name` *(required)* — Notion property name on the destination DB.
- `type` *(required)* — one of: `title`, `rich_text`, `url`, `email`, `date`, `select`, `status`, `multi_select`, `checkbox`, `number`, `relation`.
- `field` — name of an item field to read (string), or a list of field names tried in order; the first non-empty value wins.
- `value` — literal string. Mutually exclusive with `field`. Supports `{runId}` substitution.
- `default` — used when `field` resolves to nothing. Special value `"now"` returns the current ISO-8601 timestamp.
- `maxLength` — truncate the rendered string before writing (useful for `title` / `rich_text`).
- `lookup` — required for `type: relation`. Names a key in `notionImport.lookups`; the rendered value is matched against the lookup's `matchProperty` index to produce the relation page ID.

For `multi_select`, a string `field`/`value` is split on commas; a list is used as-is. For `checkbox`, the value is coerced via `bool(...)`.

Unknown property types raise an error.

## Chaining the two

The two scripts are orchestrated by `runner.sh`, which takes an optional step argument:

    ./runner.sh           # collect then sync (default; same as `all`)
    ./runner.sh collect   # collect.py only
    ./runner.sh sync      # sync.py only — uses lastArchiveDir from state
    ./runner.sh all       # explicit form of the default

Both scripts read env files from the config (`envFiles`) and resolve `statePath` from the config — the directory in which the shared state JSON and per-run archive subdirs live. `sync.py` reads `lastArchiveDir` from that state file, so no path glue is needed in the runner.

## Behavior notes

- Stdlib only. PEP 723 inline metadata pins `requires-python = ">=3.9"` and declares zero dependencies, so `uv run` resolves an interpreter and runs the script with no setup. `python3 script.py` also works directly.
- Both scripts emit progress feedback to **stderr**: a spinner during long PhantomBuster waits and Notion paginated queries, plus a progress bar during the per-item Notion page creation loop. When stderr is not a TTY (e.g. piped to a log file or under `2>&1` redirect from the runner), animation is suppressed and replaced with plain `... start` / `done in Xs` lines so the log stays readable. Stdout is reserved for the sync's machine-readable JSON result.
- LinkedIn item fields are matched on common aliases (`postUrl`/`activityUrl`/`url`, `authorName`/`profileName`/..., etc.) — see `normalize_item` in the monitor. The sync reads these flat field names via `field:` in the property spec.
- The sync script has no domain-specific logic — every Notion property and dedup rule is declared in the config. To populate a static field (e.g. a `Status` of `New`), use `value:`. To compute one from the item, use `field:` with optional `default:`. To populate a relation, define a `lookup` and reference it.
