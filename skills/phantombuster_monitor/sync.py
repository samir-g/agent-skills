#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["halo"]
# ///
"""Sync a PhantomBuster monitor archive run into Notion.

Driven entirely by the config JSON. The script reads:

    statePath                       (or notionImport.statePath) -> directory
                                    holding the state JSON written by
                                    collect.py; sync reads `lastArchiveDir`
                                    from it.
    envFiles / envFile              -> NOTION_TOKEN / NOTION_VERSION.
    notionImport.enabled            Must be true; otherwise no-op.
    notionImport.databaseId         Target Notion database.
    notionImport.itemsFile          File within run dir to read
                                    (default: normalized_items.json).
    notionImport.lookups            {name: {databaseId, matchProperty}} for
                                    relation columns.
    notionImport.dedupBy            Notion property name (or list) used to
                                    skip items whose value already exists
                                    in the destination DB.
    notionImport.properties         Ordered list of property specs:
                                    {name, type, field|value, default?,
                                     maxLength?, lookup?}.

Usage:
    python3 sync.py --config /path/to/config.json
"""
import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from halo import Halo

NOTION_API_BASE = 'https://api.notion.com/v1'
DEFAULT_NOTION_VERSION = '2025-09-03'
STATE_FILE_NAME = 'phantombuster_monitor.json'


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


def progress(current: int, total: int, suffix: str = ''):
    if not sys.stderr.isatty() or total <= 0:
        return
    width = 30
    pct = min(1.0, current / total)
    filled = int(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    end = '\n' if current >= total else ''
    sys.stderr.write(f'\r{bar} {current}/{total} {suffix}\033[K{end}')
    sys.stderr.flush()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec='seconds')


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip()
    return env


def load_env_files(paths: Any) -> dict[str, str]:
    if not paths:
        return {}
    if isinstance(paths, str):
        paths = [paths]
    merged: dict[str, str] = {}
    for p in paths:
        merged.update(load_env_file(Path(p)))
    return merged


def notion_headers(token: str, version: str) -> dict[str, str]:
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': version,
        'Content-Type': 'application/json',
    }


def notion_request(method: str, path: str, headers: dict[str, str], body: Optional[dict[str, Any]] = None):
    url = f'{NOTION_API_BASE}{path}'
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        raise SystemExit(f'Notion API error {e.code}: {detail}')
    except urllib.error.URLError as e:
        raise SystemExit(f'Notion network error: {e}')


def notion_rich_text(text: Optional[str]):
    if not text:
        return []
    text = str(text)
    if len(text) > 1900:
        text = text[:1897] + '...'
    return [{'type': 'text', 'text': {'content': text}}]


def query_all_pages(headers: dict[str, str], db_id: str, label: str) -> list[dict]:
    pages: list[dict] = []
    cursor: Optional[str] = None
    page = 0
    with Spinner(label) as spinner:
        while True:
            page += 1
            spinner.update(f'{label} (page {page}, {len(pages)} loaded)')
            body: dict[str, Any] = {'page_size': 100}
            if cursor:
                body['start_cursor'] = cursor
            resp = notion_request('POST', f'/databases/{db_id}/query', headers, body)
            pages.extend(resp.get('results', []))
            if not resp.get('has_more'):
                return pages
            cursor = resp.get('next_cursor')
            if not cursor:
                return pages


def extract_property_scalar(prop: Optional[dict]) -> str:
    """Pull a string out of a Notion property payload (read or write shape)."""
    if not prop:
        return ''
    if prop.get('url'):
        return str(prop['url'])
    if prop.get('email'):
        return str(prop['email'])
    if prop.get('phone_number'):
        return str(prop['phone_number'])
    if prop.get('title'):
        return ''.join((t.get('plain_text') or t.get('text', {}).get('content', '')) for t in prop['title'])
    if prop.get('rich_text'):
        return ''.join((t.get('plain_text') or t.get('text', {}).get('content', '')) for t in prop['rich_text'])
    if prop.get('select'):
        return prop['select'].get('name', '')
    if prop.get('status'):
        return prop['status'].get('name', '')
    if prop.get('date'):
        return prop['date'].get('start', '') or ''
    return ''


def build_lookup(headers: dict[str, str], lookup_cfg: dict) -> dict[str, str]:
    """Return {match_value: page_id} for a Notion lookup database."""
    db_id = lookup_cfg['databaseId']
    match_prop = lookup_cfg['matchProperty']
    pages = query_all_pages(headers, db_id, f'Loading lookup {match_prop} ({db_id[:8]}…)')
    out: dict[str, str] = {}
    for p in pages:
        val = extract_property_scalar(p.get('properties', {}).get(match_prop))
        if val:
            out[val.rstrip('/')] = p['id']
    return out


def render_value(spec: dict, item: dict, context: dict) -> Any:
    if 'value' in spec:
        return str(spec['value']).format(**context)
    fields = spec.get('field')
    if fields:
        if isinstance(fields, str):
            fields = [fields]
        for f in fields:
            v = item.get(f)
            if v not in (None, ''):
                return v
    if 'default' in spec:
        d = spec['default']
        return now_iso() if d == 'now' else d
    return None


def build_property(spec: dict, item: dict, context: dict, lookups: dict[str, dict[str, str]]) -> dict:
    ptype = spec['type']
    val = render_value(spec, item, context)
    max_len = spec.get('maxLength')
    if isinstance(val, str) and max_len:
        val = val[:max_len]

    if ptype == 'title':
        return {'title': notion_rich_text(val or '')}
    if ptype == 'rich_text':
        return {'rich_text': notion_rich_text(val or '')}
    if ptype == 'url':
        return {'url': val or None}
    if ptype == 'email':
        return {'email': val or None}
    if ptype == 'date':
        return {'date': {'start': val} if val else None}
    if ptype == 'select':
        return {'select': {'name': val}} if val else {'select': None}
    if ptype == 'status':
        return {'status': {'name': val}} if val else {'status': None}
    if ptype == 'multi_select':
        if val is None:
            return {'multi_select': []}
        if isinstance(val, str):
            val = [v.strip() for v in val.split(',') if v.strip()]
        return {'multi_select': [{'name': v} for v in val]}
    if ptype == 'checkbox':
        return {'checkbox': bool(val)}
    if ptype == 'number':
        return {'number': float(val) if val not in (None, '') else None}
    if ptype == 'relation':
        lookup_name = spec.get('lookup')
        if not lookup_name:
            raise SystemExit(f"property '{spec.get('name')}' (relation) requires 'lookup'")
        mapping = lookups.get(lookup_name) or {}
        key = str(val or '').rstrip('/')
        rid = mapping.get(key)
        return {'relation': [{'id': rid}] if rid else []}
    raise SystemExit(f"Unknown property type: {ptype} (property '{spec.get('name')}')")


def sync_to_notion(config: dict, items: list[dict], env: dict[str, str], run_id: str) -> dict:
    notion_cfg = config.get('notionImport') or {}
    if not notion_cfg.get('enabled'):
        return {'created': 0, 'skipped': 0, 'notionImport': None}

    db_id = notion_cfg.get('databaseId')
    if not db_id:
        raise SystemExit('notionImport.databaseId is required')

    properties_cfg = notion_cfg.get('properties') or []
    if not properties_cfg:
        raise SystemExit('notionImport.properties is required (list of property specs)')

    token = env.get('NOTION_TOKEN') or os.environ.get('NOTION_TOKEN')
    version = env.get('NOTION_VERSION') or os.environ.get('NOTION_VERSION') or DEFAULT_NOTION_VERSION
    if not token:
        raise SystemExit('NOTION_TOKEN is unavailable (set env var or add to envFiles)')

    headers = notion_headers(token, version)

    lookups: dict[str, dict[str, str]] = {}
    for name, lookup_cfg in (notion_cfg.get('lookups') or {}).items():
        lookups[name] = build_lookup(headers, lookup_cfg)

    dedup_props = notion_cfg.get('dedupBy') or []
    if isinstance(dedup_props, str):
        dedup_props = [dedup_props]

    existing_keys: set[tuple[str, ...]] = set()
    if dedup_props:
        existing_pages = query_all_pages(headers, db_id, f'Querying existing pages in {db_id[:8]}…')
        for p in existing_pages:
            key = tuple(extract_property_scalar(p.get('properties', {}).get(n)).rstrip('/') for n in dedup_props)
            if any(key):
                existing_keys.add(key)

    context = {'runId': run_id}
    created = 0
    skipped = 0
    total = len(items)
    print(f'Syncing {total} item(s) to Notion (existing pages indexed: {len(existing_keys)})', file=sys.stderr, flush=True)

    for idx, item in enumerate(items, start=1):
        progress(idx, total, f'(created {created}, skipped {skipped})')
        properties = {spec['name']: build_property(spec, item, context, lookups) for spec in properties_cfg}

        if dedup_props:
            key = tuple(extract_property_scalar(properties.get(n)).rstrip('/') for n in dedup_props)
            if not any(key) or key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)

        notion_request('POST', '/pages', headers, {'parent': {'database_id': db_id}, 'properties': properties})
        created += 1

    return {
        'created': created,
        'skipped': skipped,
        'notionImport': {
            'created': created,
            'skipped': skipped,
            'databaseId': db_id,
            'runId': run_id,
        },
    }


def resolve_state_dir(config: dict, config_dir: Path) -> Optional[Path]:
    notion_cfg = config.get('notionImport') or {}
    raw = config.get('statePath') or notion_cfg.get('statePath')
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (config_dir / p)


def load_state(path: Optional[Path]) -> dict:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_state(path: Optional[Path], state: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Sync a PhantomBuster monitor archive run into Notion.')
    parser.add_argument('--config', required=True, help='Path to the config JSON.')
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config_dir = config_path.resolve().parent

    state_dir = resolve_state_dir(config, config_dir)
    state_path = (state_dir / STATE_FILE_NAME) if state_dir else None
    state = load_state(state_path)
    env = load_env_files(config.get('envFiles') or config.get('envFile'))

    run_dir_raw = state.get('lastArchiveDir')
    if not run_dir_raw:
        raise SystemExit('No lastArchiveDir in state. Run collect.py first.')
    run_dir = Path(run_dir_raw)
    if not run_dir.exists():
        raise SystemExit(f'Archive run dir not found: {run_dir}')

    notion_cfg = config.get('notionImport') or {}
    items_file = notion_cfg.get('itemsFile', 'normalized_items.json')
    items_path = run_dir / items_file
    if not items_path.exists():
        raise SystemExit(f'Items file not found: {items_path}')
    items = json.loads(items_path.read_text(encoding='utf-8'))

    run_id = run_dir.name

    try:
        result = sync_to_notion(config, items, env, run_id)
    except SystemExit as e:
        if state_path is not None:
            existing = load_state(state_path)
            ni = existing.setdefault('notionImport', {})
            ni.update({
                'status': 'Attention',
                'lastRun': now_iso(),
                'lastError': str(e),
                'notes': 'Notion sync failed. See lastError.',
            })
            save_state(state_path, existing)
        raise

    if state_path is not None and result.get('notionImport'):
        ni = state.setdefault('notionImport', {})
        ni.update(result['notionImport'])
        ni.update({
            'status': 'Healthy',
            'lastRun': now_iso(),
            'lastSuccess': now_iso(),
            'lastError': '',
            'notes': f"Synced {run_dir.name}: {result['created']} created, {result['skipped']} skipped.",
            'lastRunDir': str(run_dir),
        })
        save_state(state_path, state)

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
