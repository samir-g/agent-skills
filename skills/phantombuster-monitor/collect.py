#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["halo"]
# ///
"""Launch or fetch a PhantomBuster LinkedIn agent run, archive the normalized
result, and dedupe new posts against state.

Run with --print-example-config to see the expected config JSON shape.

Required env:
    PHANTOMBUSTER_API_KEY    PhantomBuster API key (override var name with
                             --api-key-env).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from halo import Halo


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

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / 'phantombuster_linkedin_monitor.json'
DEFAULT_STATE_DIR = ROOT / 'state'
STATE_FILE_NAME = 'phantombuster_monitor.json'
RUNS_SUBDIR_NAME = 'phantombuster_monitor_runs'
API_BASE = 'https://api.phantombuster.com/api'
POLL_INTERVAL_SECONDS = 15
POLL_TIMEOUT_SECONDS = 600

EXAMPLE_CONFIG = {
    'agentId': 1234567890,
    'agentLabel': 'linkedin-monitor',
    'mode': 'fetch-latest',
    'launchArgument': {},
    'saveArgument': False,
    'statePath': '~/.openclaw/workspace/state',
    'envFiles': ['/path/to/phantombuster.env', '/path/to/notion.env'],
    # In `mode: launch`, reuse the latest finished container if it ended within
    # this many seconds (default 3600 = 1 hour). 0 disables; always launch.
    # An in-progress container is always attached to instead of launching a
    # duplicate, regardless of this setting.
    'recentRunWindowSeconds': 3600,
    # Drop items whose publishedAt parses to older than this many seconds.
    # 0 disables age filtering. Items with unparseable publishedAt are kept.
    # 604800 = 7 days; useful when scraping real Posts (postTimestamp).
    'maxItemAgeSeconds': 0,
}

DEFAULT_RECENT_RUN_WINDOW_SECONDS = 3600
DEFAULT_MAX_ITEM_AGE_SECONDS = 0
IN_PROGRESS_STATUSES = {'running', 'starting', 'queued', 'launched'}
TERMINAL_FAILURE_STATUSES = {'error', 'failed', 'aborted'}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec='seconds')


def info(message: str) -> None:
    """Halo-styled info line on stderr; plain prefix when not a TTY."""
    if sys.stderr.isatty():
        Halo(stream=sys.stderr).info(message)
    else:
        print(f'    {message}', file=sys.stderr, flush=True)


def succeed(message: str) -> None:
    """Halo-styled success line on stderr; plain prefix when not a TTY."""
    if sys.stderr.isatty():
        Halo(stream=sys.stderr).succeed(message)
    else:
        print(f'    {message}', file=sys.stderr, flush=True)


def load_json(path: Path, default: Any):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


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
    """Accept a single path string or a list of path strings; merge into one dict."""
    if not paths:
        return {}
    if isinstance(paths, str):
        paths = [paths]
    merged: dict[str, str] = {}
    for p in paths:
        merged.update(load_env_file(Path(p)))
    return merged


def save_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')


def pb_request(method: str, path: str, api_key: str, params: Optional[dict[str, Any]] = None, body: Optional[dict[str, Any]] = None):
    url = f'{API_BASE}{path}'
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f'{url}?{urllib.parse.urlencode(clean)}'

    headers = {
        'X-Phantombuster-Key-1': api_key,
        'Accept': 'application/json',
    }
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode('utf-8')

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        raise SystemExit(f'PhantomBuster API error {e.code}: {detail}')
    except urllib.error.URLError as e:
        raise SystemExit(f'PhantomBuster network error: {e}')


def launch_agent(api_key: str, agent_id: int, launch_argument: Any = None, save_argument: bool = False):
    body: dict[str, Any] = {'output': 'result-object'}
    if launch_argument is not None:
        body['argument'] = json.dumps(launch_argument)
        if save_argument:
            body['saveArgument'] = True
    return pb_request('POST', f'/v1/agent/{agent_id}/launch', api_key, body=body)


def fetch_latest_container(api_key: str, agent_id: int):
    response = pb_request('GET', '/v2/containers/fetch-all', api_key, params={'agentId': agent_id})
    containers = response.get('containers', []) if isinstance(response, dict) else []
    if not containers:
        raise SystemExit(f'No containers found for agent {agent_id}')
    containers.sort(key=lambda c: c.get('createdAt', 0), reverse=True)
    return containers[0]


def container_age_seconds(container: dict[str, Any]) -> Optional[float]:
    """Seconds since the container ended (or was created if no end timestamp)."""
    end_ms = container.get('endedAt') or container.get('finishedAt') or container.get('createdAt')
    if not end_ms:
        return None
    return (time.time() * 1000 - float(end_ms)) / 1000


def find_recent_or_inflight_container(api_key: str, agent_id: int, max_age_seconds: int):
    """Return (container, reason) if the latest container is reusable, else (None, None).

    - Latest is in progress: returned as-is so the caller can attach.
    - Latest finished within max_age_seconds: returned for reuse.
    - Otherwise: None.
    """
    response = pb_request('GET', '/v2/containers/fetch-all', api_key, params={'agentId': agent_id})
    containers = response.get('containers', []) if isinstance(response, dict) else []
    if not containers:
        return None, None
    containers.sort(key=lambda c: c.get('createdAt', 0), reverse=True)
    latest = containers[0]
    status = latest.get('status')
    if status in IN_PROGRESS_STATUSES:
        return latest, 'in-progress'
    if status == 'finished' and max_age_seconds > 0:
        age = container_age_seconds(latest)
        if age is not None and age <= max_age_seconds:
            return latest, 'recent-finished'
    return None, None


def fetch_container_result_object(api_key: str, container_id):
    response = pb_request('GET', '/v2/containers/fetch-result-object', api_key, params={'id': container_id})
    if not isinstance(response, dict):
        return response
    result = response.get('resultObject', response)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


def wait_for_container(api_key: str, agent_id: int, container_id = None):
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    target_id = str(container_id) if container_id is not None else None
    label = target_id or '(latest)'
    with Spinner(f'Waiting for PhantomBuster container {label}') as spinner:
        while True:
            container = fetch_latest_container(api_key, agent_id)
            current_id = str(container.get('id'))
            status = container.get('status')
            spinner.update(f'Container {current_id}: {status}')
            if (target_id is None or current_id == target_id) and status == 'finished':
                return container
            if (target_id is None or current_id == target_id) and status in {'error', 'failed', 'aborted'}:
                raise SystemExit(f'Latest container {current_id} ended with status: {status}')
            if time.monotonic() >= deadline:
                raise SystemExit(f'Timed out waiting for PhantomBuster container {target_id or current_id} to finish')
            time.sleep(POLL_INTERVAL_SECONDS)


def collect_items(payload: Any):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('posts', 'items', 'results', 'data'):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def parse_iso_to_utc(value: Any) -> Optional[dt.datetime]:
    """Best-effort parse of an ISO 8601 string to a tz-aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def is_error_record(item: dict[str, Any]) -> bool:
    """Scraper placeholder rows: explicit error key, or no postUrl + no text."""
    raw = item.get('raw') or {}
    if isinstance(raw, dict) and raw.get('error'):
        return True
    if not item.get('postUrl') and not item.get('text'):
        return True
    return False


def has_no_date(item: dict[str, Any]) -> bool:
    """True if publishedAt is missing or doesn't parse to a real timestamp."""
    return parse_iso_to_utc(item.get('publishedAt')) is None


def is_too_old(item: dict[str, Any], max_age_seconds: int, now: Optional[dt.datetime] = None) -> bool:
    """True if publishedAt parses to older than max_age_seconds."""
    if max_age_seconds <= 0:
        return False
    parsed = parse_iso_to_utc(item.get('publishedAt'))
    if parsed is None:
        return False
    reference = now or dt.datetime.now(dt.timezone.utc)
    return (reference - parsed).total_seconds() > max_age_seconds


def filter_items(items: list[dict[str, Any]], max_age_seconds: int):
    kept: list[dict[str, Any]] = []
    dropped_error: list[dict[str, Any]] = []
    dropped_no_date: list[dict[str, Any]] = []
    dropped_old: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.timezone.utc)
    for item in items:
        if is_error_record(item):
            dropped_error.append(item)
        elif has_no_date(item):
            dropped_no_date.append(item)
        elif is_too_old(item, max_age_seconds, now=now):
            dropped_old.append(item)
        else:
            kept.append(item)
    return kept, dropped_error, dropped_no_date, dropped_old


def archive_run(
    runs_dir: Path,
    agent_label: str,
    container_id: Any,
    payload: Any,
    normalized_items: list[dict[str, Any]],
    dropped_error: Optional[list[dict[str, Any]]] = None,
    dropped_no_date: Optional[list[dict[str, Any]]] = None,
    dropped_old: Optional[list[dict[str, Any]]] = None,
):
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).astimezone().strftime('%Y-%m-%d-%H-%M-%S')
    run_dir = runs_dir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    raw_path = run_dir / 'raw_result.json'
    normalized_path = run_dir / 'normalized_items.json'
    content_path = run_dir / 'content_items.json'
    dropped_path = run_dir / 'dropped_items.json'
    summary_path = run_dir / 'summary.json'

    dropped_error = dropped_error or []
    dropped_no_date = dropped_no_date or []
    dropped_old = dropped_old or []

    raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    normalized_path.write_text(json.dumps(normalized_items, indent=2, ensure_ascii=False), encoding='utf-8')
    dropped_path.write_text(json.dumps({'error': dropped_error, 'noDate': dropped_no_date, 'tooOld': dropped_old}, indent=2, ensure_ascii=False), encoding='utf-8')

    content_items = []
    post_count = 0
    article_count = 0
    other_count = 0
    for item in normalized_items:
        action = str((item.get('action') or '')).strip().lower()
        item_type = str((item.get('type') or '')).strip().lower()
        if action == 'post':
            content_items.append(item)
            post_count += 1
        elif item_type == 'article':
            content_items.append(item)
            article_count += 1
        else:
            other_count += 1
    content_path.write_text(json.dumps(content_items, indent=2, ensure_ascii=False), encoding='utf-8')

    summary = {
        'archivedAt': now_iso(),
        'agentLabel': agent_label,
        'containerId': str(container_id),
        'totalItems': len(normalized_items),
        'contentItems': len(content_items),
        'postItems': post_count,
        'articleItems': article_count,
        'otherItems': other_count,
        'droppedErrorRecords': len(dropped_error),
        'droppedNoDateRecords': len(dropped_no_date),
        'droppedOldRecords': len(dropped_old),
        'paths': {
            'raw': str(raw_path),
            'normalized': str(normalized_path),
            'content': str(content_path),
            'dropped': str(dropped_path),
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    return summary


def pick_first(item: dict[str, Any], *keys: str):
    for key in keys:
        value = item.get(key)
        if value not in (None, ''):
            return value
    return None


def normalize_item(item: Any):
    if not isinstance(item, dict):
        item = {'value': item}
    author = pick_first(item, 'authorName', 'profileName', 'ownerName', 'name', 'author')
    profile_url = pick_first(item, 'profileUrl', 'ownerUrl', 'authorUrl', 'linkedinProfileUrl')
    post_url = pick_first(item, 'postUrl', 'activityUrl', 'url', 'linkedinPostUrl')
    text = pick_first(item, 'text', 'postText', 'postContent', 'content', 'message', 'description', 'articleTitle')
    # Only post-specific fields. Generic ones like `timestamp` are the
    # scrape/run time on PhantomBuster's LinkedIn Activity Extractor.
    published = pick_first(item, 'postTimestamp', 'postedAt', 'publishedAt', 'postDate')
    uid = pick_first(item, 'postUrl', 'postId', 'activityId', 'id', 'urn', 'entityUrn', 'url')
    if uid is None:
        uid = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode('utf-8')).hexdigest()
    return {
        'id': str(uid),
        'author': author,
        'profileUrl': profile_url,
        'postUrl': post_url,
        'text': text,
        'publishedAt': published,
        'action': pick_first(item, 'action'),
        'type': pick_first(item, 'type'),
        'raw': item,
    }


def render_summary(new_items: list[dict[str, Any]], label: str):
    if not new_items:
        return f'[{now_iso()}] No new LinkedIn posts for {label}.'

    lines = [f'[{now_iso()}] {len(new_items)} new LinkedIn post(s) for {label}:']
    for idx, item in enumerate(new_items[:20], start=1):
        author = item.get('author') or 'Unknown author'
        post_url = item.get('postUrl') or item.get('profileUrl') or '(no URL)'
        published = item.get('publishedAt') or 'unknown time'
        text = (item.get('text') or '').strip().replace('\n', ' ')
        if len(text) > 180:
            text = text[:177].rstrip() + '...'
        lines.append(f'{idx}. {author} | {published}')
        lines.append(f'   {post_url}')
        if text:
            lines.append(f'   {text}')
    if len(new_items) > 20:
        lines.append(f'...and {len(new_items) - 20} more.')
    return '\n'.join(lines)


def write_attention_state(state_path: Path, message: str):
    existing = load_json(state_path, {})
    if not isinstance(existing, dict):
        existing = {}
    existing['status'] = 'Attention'
    existing['lastRun'] = now_iso()
    existing['lastError'] = message
    existing['notes'] = 'Latest PhantomBuster monitor run failed. See lastError.'
    save_json(state_path, existing)


def main():
    parser = argparse.ArgumentParser(description='Launch a PhantomBuster LinkedIn agent and report newly seen posts.')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--api-key-env', default='PHANTOMBUSTER_API_KEY')
    parser.add_argument('--env-file', action='append', help='KEY=VALUE file with API credentials. May be passed multiple times. Overrides envFiles/envFile in config.')
    parser.add_argument('--container-id', help='Fetch and archive a specific existing container instead of the latest or a new launch.')
    parser.add_argument('--dry-run', action='store_true', help='Print the normalized current payload without updating state.')
    parser.add_argument('--recent-window', type=int, help='Override config recentRunWindowSeconds. In mode=launch, reuse the latest finished container if it ended within this many seconds. 0 disables.')
    parser.add_argument('--force-launch', action='store_true', help='In mode=launch, skip the recent-run check and always launch a new container.')
    parser.add_argument('--max-item-age', type=int, help='Override config maxItemAgeSeconds. Drop items whose publishedAt parses to older than this many seconds. 0 disables.')
    parser.add_argument('--print-example-config', action='store_true', help='Print an example config JSON to stdout and exit.')
    args = parser.parse_args()

    if args.print_example_config:
        print(json.dumps(EXAMPLE_CONFIG, indent=2))
        return

    config_path = Path(args.config)

    config = load_json(config_path, None)
    if not config:
        raise SystemExit(f'Missing config file: {config_path}')

    config_dir = config_path.resolve().parent
    raw_state = config.get('statePath')
    if raw_state:
        sp = Path(raw_state).expanduser()
        state_dir = sp if sp.is_absolute() else (config_dir / sp)
    else:
        state_dir = DEFAULT_STATE_DIR
    state_path = state_dir / STATE_FILE_NAME
    runs_dir = state_dir / RUNS_SUBDIR_NAME

    env_paths = args.env_file if args.env_file else (config.get('envFiles') or config.get('envFile'))
    env = load_env_files(env_paths)
    api_key = env.get(args.api_key_env) or os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f'Missing environment variable: {args.api_key_env}')

    agent_id = config.get('agentId')
    if not agent_id:
        raise SystemExit('Config is missing agentId')

    try:
        _run(args, config, state_path, runs_dir, api_key, agent_id)
    except SystemExit as e:
        if e.code not in (None, 0):
            write_attention_state(state_path, str(e))
        raise


def _run(args, config, state_path: Path, runs_dir: Path, api_key: str, agent_id):
    info(f'PhantomBuster monitor: agent {agent_id} ({config.get("agentLabel") or "unlabeled"})')

    launch_mode = config.get('mode', 'fetch-latest')
    container_source = launch_mode

    if args.container_id:
        container = {'id': str(args.container_id), 'status': 'finished'}
        container_source = 'container-id'
    elif launch_mode == 'launch':
        recent_window = args.recent_window if args.recent_window is not None else int(config.get('recentRunWindowSeconds', DEFAULT_RECENT_RUN_WINDOW_SECONDS))
        recent, reason = (None, None)
        if not args.force_launch and recent_window > 0:
            with Spinner(f'Checking for recent runs (within {recent_window}s)'):
                recent, reason = find_recent_or_inflight_container(api_key, int(agent_id), recent_window)
        if reason == 'recent-finished':
            age = container_age_seconds(recent) or 0
            info(f'Reusing recent finished container {recent.get("id")} (ended {age:.0f}s ago, within {recent_window}s window)')
            container = recent
            container_source = 'reused-recent'
        elif reason == 'in-progress':
            info(f'Attaching to in-progress container {recent.get("id")} (status: {recent.get("status")})')
            container = wait_for_container(api_key, int(agent_id), recent.get('id'))
            container_source = 'attached-running'
        else:
            with Spinner(f'Launching PhantomBuster agent {agent_id}'):
                response = launch_agent(
                    api_key=api_key,
                    agent_id=int(agent_id),
                    launch_argument=config.get('launchArgument'),
                    save_argument=bool(config.get('saveArgument', False)),
                )
            launched_container_id = response.get('containerId') if isinstance(response, dict) else None
            container = wait_for_container(api_key, int(agent_id), launched_container_id)
            container_source = 'launched'
    else:
        with Spinner(f'Fetching latest container for agent {agent_id}'):
            container = fetch_latest_container(api_key, int(agent_id))
        if container.get('status') != 'finished':
            raise SystemExit(f"Latest container {container.get('id')} is not finished yet: {container.get('status')}")

    with Spinner(f'Fetching container {container.get("id")} result'):
        payload = fetch_container_result_object(api_key, container.get('id'))
    raw_items = [normalize_item(item) for item in collect_items(payload)]

    max_age = args.max_item_age if args.max_item_age is not None else int(config.get('maxItemAgeSeconds', DEFAULT_MAX_ITEM_AGE_SECONDS))
    items, dropped_error, dropped_no_date, dropped_old = filter_items(raw_items, max_age)
    if dropped_error or dropped_no_date or dropped_old:
        info(f'Filtered {len(raw_items)} -> {len(items)} (dropped {len(dropped_error)} error, {len(dropped_no_date)} no date, {len(dropped_old)} too old)')

    archive = archive_run(
        runs_dir=runs_dir,
        agent_label=config.get('agentLabel') or f'agent {agent_id}',
        container_id=container.get('id'),
        payload=payload,
        normalized_items=items,
        dropped_error=dropped_error,
        dropped_no_date=dropped_no_date,
        dropped_old=dropped_old,
    )
    succeed(f'Archived {len(items)} items ({archive["postItems"]} posts) to {Path(archive["paths"]["raw"]).parent}')

    if args.dry_run:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return

    state = load_json(state_path, {'seen': {}, 'lastRun': None, 'lastCount': 0})
    seen = state.setdefault('seen', {})

    seen_urls = {
        str(v.get('postUrl'))
        for v in seen.values()
        if isinstance(v, dict) and v.get('postUrl')
    }

    new_items = []
    for item in items:
        item_url = item.get('postUrl')
        if item['id'] in seen or (item_url and item_url in seen_urls):
            continue
        seen[item['id']] = {
            'firstSeenAt': now_iso(),
            'author': item.get('author'),
            'postUrl': item.get('postUrl'),
            'publishedAt': item.get('publishedAt'),
            'action': item.get('action'),
            'type': item.get('type'),
        }
        if item_url:
            seen_urls.add(item_url)
        new_items.append(item)

    state['status'] = 'Healthy'
    state['notes'] = (
        f"Container {container.get('id')} finished ({container_source}). "
        f"Raw items: {len(raw_items)}. Kept after filter: {len(items)} "
        f"(dropped {len(dropped_error)} error, {len(dropped_no_date)} no date, {len(dropped_old)} too old). "
        f"Content: {archive['contentItems']} ({archive['postItems']} posts, {archive['articleItems']} articles, {archive['otherItems']} other). "
        f"New unique items: {len(new_items)}."
    )
    state['lastError'] = ''
    state['lastSuccess'] = now_iso()
    state['lastRun'] = now_iso()
    state['lastCount'] = len(items)
    state['lastNewCount'] = len(new_items)
    state['lastContainerId'] = str(container.get('id'))
    state['lastContainerSource'] = container_source
    state['lastArchiveDir'] = str(Path(archive['paths']['raw']).parent)
    state['lastContentPath'] = archive['paths']['content']
    state['lastRawResultPath'] = archive['paths']['raw']
    state['lastNormalizedPath'] = archive['paths']['normalized']
    state['lastDroppedPath'] = archive['paths']['dropped']
    state['lastContentCount'] = archive['contentItems']
    state['lastPostCount'] = archive['postItems']
    state['lastArticleCount'] = archive['articleItems']
    state['lastOtherCount'] = archive['otherItems']
    state['lastDroppedErrorCount'] = archive['droppedErrorRecords']
    state['lastDroppedNoDateCount'] = archive['droppedNoDateRecords']
    state['lastDroppedOldCount'] = archive['droppedOldRecords']
    save_json(state_path, state)

    summary = render_summary(new_items, config.get('agentLabel') or f'agent {agent_id}')
    content_line = f"Content items: {archive['contentItems']} ({archive['postItems']} posts, {archive['articleItems']} articles, {archive['otherItems']} other)"
    dropped_line = f"Dropped: {archive['droppedErrorRecords']} error, {archive['droppedNoDateRecords']} no date, {archive['droppedOldRecords']} too old"
    print(summary)
    print(f"Archived raw result: {archive['paths']['raw']}")
    print(f"Content copy: {archive['paths']['content']}")
    print(content_line)
    print(dropped_line)
    print(f"Archive run dir: {Path(archive['paths']['raw']).parent}")


if __name__ == '__main__':
    main()
