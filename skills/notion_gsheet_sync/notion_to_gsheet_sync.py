#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["halo"]
# ///
"""Sync rows from a Notion database into a Google Sheet tab.

The source database, optional relation lookups, destination sheet, and
column mapping are all defined in a JSON config file. Google Sheet writes
are performed via the `gog` CLI (configurable).

Usage:
    python3 notion_to_gsheet_sync.py --config path/to/config.json
    python3 notion_to_gsheet_sync.py --print-example-config

Required env (or via --env-file KEY=VALUE file):
    NOTION_TOKEN      Notion integration token.
    NOTION_VERSION    Optional, defaults to 2022-06-28.
"""
import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from halo import Halo

NOTION_API = 'https://api.notion.com/v1'
DEFAULT_NOTION_VERSION = '2022-06-28'
DEFAULT_GOG_BIN = 'gog'
DEFAULT_CLEAR_RANGE = 'A:Z'
DEFAULT_WRITE_START = 'A1'

EXAMPLE_CONFIG: dict[str, Any] = {
    'notion': {
        'databaseId': '<contacts-database-id>',
        'filter': {
            'and': [
                {'property': 'LinkedIn Status', 'status': {'equals': 'Connected'}},
                {'property': 'LinkedIn', 'url': {'is_not_empty': True}},
            ]
        },
        'lookups': {
            'companies': {
                'databaseId': '<companies-database-id>',
                'fields': {
                    'name': {'property': 'Name', 'from': 'title'},
                    'linkedin': {'property': 'LinkedIn', 'from': 'url'},
                },
            }
        },
    },
    'gsheet': {
        'sheetId': '<google-sheet-id>',
        'tab': 'Sheet1',
        'writeStart': 'A1',
        'clearRange': 'A:Z',
        'account': 'you@example.com',
    },
    'columns': [
        {'header': 'Company', 'from': 'relation', 'property': 'Company',
         'lookup': 'companies', 'field': 'name', 'join': ' | '},
        {'header': 'Name', 'from': 'title', 'property': 'Name'},
        {'header': 'Role', 'from': 'multi_select', 'property': 'Position'},
        {'header': 'LinkedIn URL', 'from': 'url', 'property': 'LinkedIn'},
        {'header': 'Status', 'from': 'status', 'property': 'Status'},
        {'header': 'Tier', 'from': 'select', 'property': 'Prospect Tier'},
        {'header': 'Company LinkedIn', 'from': 'relation', 'property': 'Company',
         'lookup': 'companies', 'field': 'linkedin', 'join': ' | '},
    ],
    'dedupBy': 'LinkedIn URL',
    'sortBy': ['Company', 'Name', 'LinkedIn URL'],
    'statePath': '/path/to/state.json',
}


class Spinner:
    """Stderr spinner backed by halo; no-ops cleanly when stderr is not a TTY."""

    def __init__(self, message: str):
        self.message = message
        self._enabled = sys.stderr.isatty()
        self._halo: Optional[Halo] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        if self._enabled:
            self._halo = Halo(text=self.message, spinner='dots', stream=sys.stderr)
            self._halo.start()
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
        else:
            print(f'... {self.message}', file=sys.stderr, flush=True)
        return self

    def _tick(self):
        while not self._stop.wait(0.1):
            elapsed = time.monotonic() - self._start
            if self._halo is not None:
                self._halo.text = f'{self.message} ({elapsed:.0f}s)'

    def update(self, message: str):
        self.message = message

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.monotonic() - self._start
        if self._enabled and self._halo is not None:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=1)
            final = f'{self.message}: {"done" if exc_type is None else "failed"} in {elapsed:.1f}s'
            if exc_type is None:
                self._halo.succeed(final)
            else:
                self._halo.fail(final)
        else:
            marker = 'done' if exc_type is None else 'failed'
            print(f'    {self.message}: {marker} in {elapsed:.1f}s', file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip()
    return env


def notion_request(method: str, path: str, token: str, version: str, body: Optional[dict] = None) -> dict:
    headers = {
        'Authorization': f'Bearer {token}',
        'Notion-Version': version,
    }
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(NOTION_API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Notion API HTTP {e.code} on {method} {path}: {raw}') from e


def notion_query_all(
    database_id: str,
    token: str,
    version: str,
    filter_body: Optional[dict] = None,
    label: Optional[str] = None,
) -> list[dict]:
    rows: list[dict] = []
    cursor: Optional[str] = None
    page = 0
    base_label = label or f'Querying Notion database {database_id[:8]}…'
    with Spinner(base_label) as spinner:
        while True:
            page += 1
            spinner.update(f'{base_label} (page {page}, {len(rows)} rows)')
            body: dict[str, Any] = {'page_size': 100}
            if filter_body:
                body['filter'] = filter_body
            if cursor:
                body['start_cursor'] = cursor
            data = notion_request('POST', f'/databases/{database_id}/query', token, version, body)
            rows.extend(data.get('results', []))
            if not data.get('has_more'):
                return rows
            cursor = data.get('next_cursor')
            if not cursor:
                return rows


# --- Property extraction ---------------------------------------------------

def _rich_text_plain(items: Optional[list]) -> str:
    return ''.join(item.get('plain_text', '') for item in items or [])


def _join_names(items: Optional[list], join: str) -> str:
    return join.join(v.get('name', '') for v in (items or []) if v.get('name'))


def extract_property(prop: Optional[dict], spec: dict, lookups: dict[str, dict[str, dict]]) -> str:
    """Extract a value from a Notion property based on the column spec.

    `spec['from']` selects the extractor; `spec` may include extractor-specific
    fields (e.g. `join`, `field` for relations, `dateField` for dates).
    Returns a string suitable for a sheet cell.
    """
    if prop is None:
        return ''
    kind = spec.get('from')
    join = spec.get('join', ', ')

    if kind == 'title':
        return _rich_text_plain(prop.get('title'))
    if kind == 'rich_text':
        return _rich_text_plain(prop.get('rich_text'))
    if kind == 'url':
        return prop.get('url') or ''
    if kind == 'email':
        return prop.get('email') or ''
    if kind == 'phone_number':
        return prop.get('phone_number') or ''
    if kind == 'number':
        v = prop.get('number')
        return '' if v is None else str(v)
    if kind == 'checkbox':
        return 'TRUE' if prop.get('checkbox') else 'FALSE'
    if kind == 'select':
        return ((prop.get('select') or {}).get('name')) or ''
    if kind == 'status':
        return ((prop.get('status') or {}).get('name')) or ''
    if kind == 'multi_select':
        return _join_names(prop.get('multi_select'), join)
    if kind == 'people':
        return join.join(p.get('name', '') for p in (prop.get('people') or []) if p.get('name'))
    if kind == 'date':
        date = prop.get('date') or {}
        return date.get(spec.get('dateField', 'start')) or ''
    if kind == 'created_time':
        return prop.get('created_time') or ''
    if kind == 'last_edited_time':
        return prop.get('last_edited_time') or ''
    if kind == 'created_by':
        return ((prop.get('created_by') or {}).get('name')) or ''
    if kind == 'last_edited_by':
        return ((prop.get('last_edited_by') or {}).get('name')) or ''
    if kind == 'files':
        urls = []
        for f in prop.get('files') or []:
            u = (f.get('external') or {}).get('url') or (f.get('file') or {}).get('url')
            if u:
                urls.append(u)
        return join.join(urls)
    if kind == 'formula':
        f = prop.get('formula') or {}
        ftype = f.get('type')
        if ftype == 'string':
            return f.get('string') or ''
        if ftype == 'number':
            n = f.get('number')
            return '' if n is None else str(n)
        if ftype == 'boolean':
            return 'TRUE' if f.get('boolean') else 'FALSE'
        if ftype == 'date':
            return ((f.get('date') or {}).get('start')) or ''
        return ''
    if kind == 'relation':
        lookup_name = spec.get('lookup')
        field = spec.get('field')
        if not lookup_name or not field:
            raise RuntimeError(f"relation column '{spec.get('header')}' requires 'lookup' and 'field'")
        lookup = lookups.get(lookup_name)
        if lookup is None:
            raise RuntimeError(f"relation column '{spec.get('header')}' references unknown lookup '{lookup_name}'")
        ids = [r.get('id') for r in (prop.get('relation') or []) if r.get('id')]
        values = []
        for rid in ids:
            entry = lookup.get(rid) or {}
            v = entry.get(field)
            if v:
                values.append(str(v))
        # preserve order, drop duplicates
        return join.join(dict.fromkeys(values))
    raise RuntimeError(f"Unknown column 'from': {kind!r} (column: {spec.get('header')!r})")


def build_lookups(notion_cfg: dict, token: str, version: str) -> dict[str, dict[str, dict]]:
    """Run each configured lookup database query and build {page_id: {field: value}}."""
    lookups_cfg = (notion_cfg.get('lookups') or {})
    out: dict[str, dict[str, dict]] = {}
    for name, cfg in lookups_cfg.items():
        db_id = cfg.get('databaseId')
        if not db_id:
            raise RuntimeError(f"lookup '{name}' missing databaseId")
        fields_cfg = cfg.get('fields') or {}
        rows = notion_query_all(db_id, token, version, cfg.get('filter'), label=f"Loading lookup '{name}' ({db_id[:8]}…)")
        mapped: dict[str, dict] = {}
        for row in rows:
            props = row.get('properties', {})
            entry: dict[str, str] = {}
            for field_name, field_spec in fields_cfg.items():
                prop = props.get(field_spec.get('property'))
                entry[field_name] = extract_property(prop, field_spec, {})
            mapped[row['id']] = entry
        out[name] = mapped
    return out


# --- Row construction ------------------------------------------------------

def build_rows(items: list[dict], columns: list[dict], lookups: dict[str, dict[str, dict]]) -> list[dict]:
    rows: list[dict] = []
    for item in items:
        props = item.get('properties', {})
        row: dict[str, str] = {}
        for col in columns:
            prop = props.get(col.get('property'))
            row[col['header']] = extract_property(prop, col, lookups)
        rows.append(row)
    return rows


def sort_rows(rows: list[dict], sort_by: Optional[list]) -> list[dict]:
    if not sort_by:
        return rows
    return sorted(rows, key=lambda r: tuple((r.get(h) or '').lower() for h in sort_by))


def dedupe_rows(rows: list[dict], dedup_by: Optional[str]) -> list[dict]:
    if not dedup_by:
        return rows
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        key = (r.get(dedup_by) or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# --- Sheet I/O via gog -----------------------------------------------------

def gog_cmd(gsheet_cfg: dict) -> list[str]:
    bin_path = gsheet_cfg.get('gogBin') or shutil.which(DEFAULT_GOG_BIN) or DEFAULT_GOG_BIN
    return [bin_path, 'sheets']


def gog_account_args(gsheet_cfg: dict) -> list[str]:
    account = gsheet_cfg.get('account')
    return ['-a', account] if account else []


def run_gog(args: list[str], gsheet_cfg: dict, timeout: int = 120) -> str:
    cmd = gog_cmd(gsheet_cfg) + list(args) + gog_account_args(gsheet_cfg)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or 'gog sheets command failed').strip())
    return proc.stdout.strip()


def read_sheet_rows(gsheet_cfg: dict, headers: list[str]) -> list[dict]:
    sheet_id = gsheet_cfg['sheetId']
    tab = gsheet_cfg['tab']
    cols = gsheet_cfg.get('readRange') or _columns_for_count(len(headers))
    rng = f'{tab}!{cols}'
    with Spinner(f'Reading sheet {sheet_id[:8]}… {rng}'):
        raw = run_gog(['get', sheet_id, rng, '-j', '--results-only'], gsheet_cfg)
    data = json.loads(raw) if raw else []
    rows: list[dict] = []
    for row in data:
        if not row:
            continue
        padded = list(row) + [''] * max(0, len(headers) - len(row))
        record = dict(zip(headers, [str(v) if v is not None else '' for v in padded[:len(headers)]]))
        if record == {h: h for h in headers}:
            continue
        if not any(record.values()):
            continue
        rows.append(record)
    return rows


def clear_sheet(gsheet_cfg: dict) -> None:
    sheet_id = gsheet_cfg['sheetId']
    tab = gsheet_cfg['tab']
    cols = gsheet_cfg.get('clearRange', DEFAULT_CLEAR_RANGE)
    with Spinner(f'Clearing sheet {sheet_id[:8]}… {tab}!{cols}'):
        run_gog(['clear', sheet_id, f'{tab}!{cols}'], gsheet_cfg, timeout=60)


def write_sheet_rows(gsheet_cfg: dict, headers: list[str], rows: list[dict]) -> None:
    sheet_id = gsheet_cfg['sheetId']
    tab = gsheet_cfg['tab']
    start = gsheet_cfg.get('writeStart', DEFAULT_WRITE_START)
    values = [headers] + [[r.get(h, '') for h in headers] for r in rows]
    with Spinner(f'Writing {len(rows)} rows to {sheet_id[:8]}… {tab}!{start}'):
        run_gog(['update', sheet_id, f'{tab}!{start}', '--values-json', json.dumps(values)], gsheet_cfg, timeout=120)


def _columns_for_count(n: int) -> str:
    """A1-style column range covering the first n columns (e.g. 8 -> 'A:H')."""
    if n <= 0:
        return 'A:A'
    last = ''
    idx = n
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        last = chr(ord('A') + rem) + last
    return f'A:{last}'


# --- State -----------------------------------------------------------------

def load_state(path: Optional[Path]) -> dict:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(path: Optional[Path], state: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding='utf-8')


def row_key(row: dict, headers: list[str]) -> str:
    return ' | '.join((row.get(h, '') or '').strip() for h in headers)


# --- Entry point -----------------------------------------------------------

def run(config: dict, token: str, version: str, dry_run: bool, state_path_override: Optional[Path]) -> dict:
    notion_cfg = config['notion']
    gsheet_cfg = config['gsheet']
    columns = config['columns']
    headers = [c['header'] for c in columns]

    state_path = state_path_override
    if state_path is None and config.get('statePath'):
        state_path = Path(config['statePath'])
    prev = load_state(state_path)

    state: dict[str, Any] = {
        'system': 'Notion to Google Sheet Sync',
        'lastRun': now_iso(),
        'lastSuccess': prev.get('lastSuccess'),
        'lastError': '',
        'status': 'Attention',
        'notionDatabaseId': notion_cfg.get('databaseId'),
        'sheetId': gsheet_cfg.get('sheetId'),
        'sheetTab': gsheet_cfg.get('tab'),
        'dryRun': dry_run,
    }

    try:
        old_rows = [] if dry_run else read_sheet_rows(gsheet_cfg, headers)
        lookups = build_lookups(notion_cfg, token, version)
        items = notion_query_all(
            notion_cfg['databaseId'], token, version, notion_cfg.get('filter'),
            label=f"Loading source database ({notion_cfg['databaseId'][:8]}…)",
        )
        rows = build_rows(items, columns, lookups)
        rows = dedupe_rows(rows, config.get('dedupBy'))
        rows = sort_rows(rows, config.get('sortBy'))

        if not dry_run:
            clear_sheet(gsheet_cfg)
            write_sheet_rows(gsheet_cfg, headers, rows)

        old_keys = {row_key(r, headers) for r in old_rows}
        new_keys = {row_key(r, headers) for r in rows}
        added = [r for r in rows if row_key(r, headers) not in old_keys]
        removed = [r for r in old_rows if row_key(r, headers) not in new_keys]
        changed = len(added) + len(removed)

        state.update({
            'lastSuccess': now_iso(),
            'status': 'Healthy',
            'sourcePageCount': len(items),
            'rowCountBefore': len(old_rows),
            'rowCountAfter': len(rows),
            'addedCount': len(added),
            'removedCount': len(removed),
            'changedRowCount': changed,
            'materialChange': changed > 0,
            'notes': (
                f"{'[dry-run] ' if dry_run else ''}"
                f'Synced {len(rows)} rows from Notion DB {notion_cfg["databaseId"]} to '
                f'{gsheet_cfg["sheetId"]} ({gsheet_cfg["tab"]}). '
                f'Added {len(added)}, removed {len(removed)}.'
            ),
        })
        save_state(state_path, state)
        return state
    except Exception as e:
        state.update({
            'status': 'Attention',
            'lastError': str(e),
            'notes': 'Notion to Google Sheet sync failed.',
        })
        save_state(state_path, state)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description='Sync a Notion database into a Google Sheet tab.')
    parser.add_argument('--config', help='Path to the JSON config file.')
    parser.add_argument('--env-file', help='KEY=VALUE file providing NOTION_TOKEN / NOTION_VERSION.')
    parser.add_argument('--state', help='Override state JSON path (defaults to config.statePath).')
    parser.add_argument('--dry-run', action='store_true', help='Skip sheet writes; print computed rows.')
    parser.add_argument('--print-example-config', action='store_true', help='Print example config JSON and exit.')
    args = parser.parse_args()

    if args.print_example_config:
        print(json.dumps(EXAMPLE_CONFIG, indent=2))
        return 0

    if not args.config:
        parser.error('--config is required (or use --print-example-config)')

    config = json.loads(Path(args.config).read_text(encoding='utf-8'))

    env: dict[str, str] = {}
    if args.env_file:
        env = load_env_file(Path(args.env_file))
    elif config.get('envFile'):
        env = load_env_file(Path(config['envFile']))

    import os
    token = env.get('NOTION_TOKEN') or os.environ.get('NOTION_TOKEN', '')
    version = env.get('NOTION_VERSION') or os.environ.get('NOTION_VERSION', DEFAULT_NOTION_VERSION)
    if not token:
        print('Missing NOTION_TOKEN (set env var or provide --env-file).', file=sys.stderr)
        return 2

    state_path = Path(args.state) if args.state else None

    try:
        result = run(config, token, version, args.dry_run, state_path)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({'status': 'Attention', 'lastError': str(e)}, indent=2), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
