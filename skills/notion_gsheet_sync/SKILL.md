---
name: notion-gsheet-sync
description: Use when the user wants to mirror a Notion database into a Google Sheet tab — define the source database, optional relation lookups, destination sheet, and column mapping in a JSON config.
---

# Notion → Google Sheet sync

One script (`notion_to_gsheet_sync.py`, stdlib + `halo` for the spinner) that:

1. Queries a Notion database (with an optional Notion API filter passed through verbatim).
2. Resolves named relation lookups against other Notion databases.
3. Maps Notion property values into named sheet columns.
4. Optionally dedupes / sorts the resulting rows.
5. Clears and rewrites the destination sheet tab via the `gog` CLI.
6. Writes a state JSON capturing row counts, added/removed samples, and last error.

## Required env

- `NOTION_TOKEN` — Notion integration token. Provide via env or via `--env-file PATH` (a `KEY=VALUE` file).
- `NOTION_VERSION` — optional, defaults to `2022-06-28`.

The integration must be invited to every database referenced (source + each lookup).

## Required external tool

- [`gog`](https://github.com/jaytaylor/gog) — Google APIs CLI used to read/clear/update the sheet. Path is auto-discovered on `PATH`; override via `gsheet.gogBin` in the config.

## Run

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/notion_gsheet_sync/notion_to_gsheet_sync.py" \
        --config /path/to/config.json

Useful flags:

- `--print-example-config` — print a worked example config JSON and exit.
- `--dry-run` — query Notion, build rows, print result, **do not** clear/write the sheet.
- `--env-file PATH` — KEY=VALUE file providing `NOTION_TOKEN` / `NOTION_VERSION` (overrides `envFile` in config).
- `--state PATH` — override `statePath` from the config.

`notion_to_gsheet_sync_runner.sh` is a thin wrapper that defaults `--config` to `config.json` next to the script and forwards remaining flags.

## Config schema

Top-level keys:

- `notion.databaseId` *(required)* — source database ID.
- `notion.filter` — raw Notion API [filter object](https://developers.notion.com/reference/post-database-query-filter) passed through unchanged. Omit to fetch the whole database.
- `notion.lookups` — map of `{lookup_name: {databaseId, fields, filter?}}`. Each lookup is queried once and indexed by page ID. `fields` is `{field_name: column_spec}` using the same `from`/`property` shape as top-level columns. Used by `relation` columns.
- `gsheet.sheetId` *(required)* — Google Sheet ID.
- `gsheet.tab` *(required)* — tab name within the sheet.
- `gsheet.writeStart` — A1 cell to start writing at (default `A1`).
- `gsheet.clearRange` — column range to clear before writing (default `A:Z`).
- `gsheet.readRange` — column range to read existing rows from (default: derived from column count, e.g. 8 columns → `A:H`).
- `gsheet.account` — Google account email passed to `gog -a`. Optional.
- `gsheet.gogBin` — path to the `gog` binary. Optional; defaults to `gog` on `PATH`.
- `columns` *(required)* — ordered list of `{header, property, from, ...}`. The `header` becomes the sheet column header. Each row is built by extracting `property` from the Notion page using extractor `from`.
- `dedupBy` — optional column header. Rows with empty or duplicate values in this column are dropped.
- `sortBy` — optional list of column headers. Rows are sorted case-insensitively by these in order.
- `statePath` — optional state JSON path. Overridden by `--state`.
- `envFile` — optional default for `--env-file`.

### Column extractors (`from`)

| `from`             | Reads from Notion property of type | Extra spec keys |
|--------------------|------------------------------------|-----------------|
| `title`            | title                              | —               |
| `rich_text`        | rich_text                          | —               |
| `url`              | url                                | —               |
| `email`            | email                              | —               |
| `phone_number`     | phone_number                       | —               |
| `number`           | number                             | —               |
| `checkbox`         | checkbox                           | — (emits `TRUE`/`FALSE`) |
| `select`           | select                             | —               |
| `status`           | status                             | —               |
| `multi_select`     | multi_select                       | `join` (default `, `) |
| `people`           | people                             | `join` (default `, `) |
| `files`            | files                              | `join` (default `, `) |
| `date`             | date                               | `dateField`: `start` (default) or `end` |
| `created_time`     | created_time                       | —               |
| `last_edited_time` | last_edited_time                   | —               |
| `created_by`       | created_by                         | —               |
| `last_edited_by`   | last_edited_by                     | —               |
| `formula`          | formula (string/number/boolean/date) | —             |
| `relation`         | relation                           | `lookup` (name from `notion.lookups`), `field` (key in that lookup's `fields`), `join` (default `, `) — duplicates within a row are dropped, order preserved |

Unknown `from` values raise an error.

### Worked example config

Run `--print-example-config` to print this:

```json
{
  "notion": {
    "databaseId": "<contacts-database-id>",
    "filter": {
      "and": [
        {"property": "LinkedIn Status", "status": {"equals": "Connected"}},
        {"property": "LinkedIn", "url": {"is_not_empty": true}}
      ]
    },
    "lookups": {
      "companies": {
        "databaseId": "<companies-database-id>",
        "fields": {
          "name":     {"property": "Name",     "from": "title"},
          "linkedin": {"property": "LinkedIn", "from": "url"}
        }
      }
    }
  },
  "gsheet": {
    "sheetId": "<google-sheet-id>",
    "tab": "Sheet1",
    "writeStart": "A1",
    "clearRange": "A:Z",
    "account": "you@example.com"
  },
  "columns": [
    {"header": "Company",          "property": "Company",       "from": "relation",     "lookup": "companies", "field": "name",     "join": " | "},
    {"header": "Name",             "property": "Name",          "from": "title"},
    {"header": "Role",             "property": "Position",      "from": "multi_select"},
    {"header": "LinkedIn URL",     "property": "LinkedIn",      "from": "url"},
    {"header": "Status",           "property": "Status",        "from": "status"},
    {"header": "Tier",             "property": "Prospect Tier", "from": "select"},
    {"header": "Company LinkedIn", "property": "Company",       "from": "relation",     "lookup": "companies", "field": "linkedin", "join": " | "}
  ],
  "dedupBy": "LinkedIn URL",
  "sortBy": ["Company", "Name", "LinkedIn URL"],
  "statePath": "/path/to/state.json"
}
```

## Behaviour notes

- The sheet is **cleared then rewritten** on every successful run — there is no incremental upsert. If `clear` succeeds but `update` fails, the sheet is left empty; the state JSON records the failure.
- Existing sheet rows are read **before** writing, only to compute added/removed samples for the state JSON. The sync is otherwise stateless from the sheet's perspective.
- PEP 723 inline metadata pins `requires-python = ">=3.9"` and declares `halo` as the only dependency, so `uv run` resolves the env on first use. `python3 notion_to_gsheet_sync.py` also works if `halo` is already installed.
- Progress feedback (Notion paginated query, sheet read/clear/write) is written to **stderr** as a halo spinner when stderr is a TTY, and as plain `... start` / `done in Xs` lines when stderr is piped or redirected. Stdout stays clean for the JSON state.
- Stdout is the state JSON on success; stderr is the state JSON on failure (with non-zero exit).
