#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "reader>=3.15",
#     "halo>=0.0.31",
# ]
# ///
"""Config-driven RSS monitor: sync feeds, fetch entries, query by time window."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from halo import Halo
from reader import (
    make_reader,
    Reader,
    FeedExistsError,
    FeedNotFoundError,
    TagNotFoundError,
)


DEFAULT_STATE_FILE_NAME = "rss_monitor.json"
DEFAULT_DB_FILE_NAME = "rss-monitor.sqlite"

EXAMPLE_CONFIG = {
    "statePath": "~/.openclaw/workspace/state",
    "feeds": [
        {"url": "https://example.com/feed.xml", "tags": ["ai", "research"]},
    ],
}


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    """Reject malformed URLs and hosts resolving to non-public addresses (SSRF guard)."""
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


class _NullSpinner:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def start(self, *a, **kw): return self
    def stop(self): pass


def _spinner(text: str, *, enabled: bool):
    if not enabled or not sys.stderr.isatty():
        return _NullSpinner()
    return Halo(text=text, spinner="dots", stream=sys.stderr)


@contextmanager
def _open_reader(db: Path):
    r = make_reader(str(db))
    try:
        yield r
    finally:
        r.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _load_config_file(path: Path) -> dict:
    if not path.exists():
        sys.stderr.write(f"error: config not found: {path}\n")
        sys.exit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.stderr.write(f"error: invalid config JSON {path}: {e}\n")
        sys.exit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"error: config must be a JSON object: {path}\n")
        sys.exit(2)
    return data


def _resolve_paths(config: dict, config_path: Path) -> tuple[Path, Path]:
    state_dir_raw = config.get("statePath")
    if not state_dir_raw:
        sys.stderr.write("error: config must set statePath\n")
        sys.exit(2)
    state_dir = Path(state_dir_raw).expanduser()
    if not state_dir.is_absolute():
        state_dir = (config_path.resolve().parent / state_dir).resolve()

    db_override = config.get("dbPath")
    if db_override:
        db = Path(db_override).expanduser()
        if not db.is_absolute():
            db = state_dir / db
    else:
        db = state_dir / DEFAULT_DB_FILE_NAME

    return db, state_dir / DEFAULT_STATE_FILE_NAME


def _normalize_feeds(config: dict) -> list[tuple[str, list[str]]]:
    result = []
    for feed_cfg in config.get("feeds") or []:
        if isinstance(feed_cfg, str):
            url, tags = feed_cfg, []
        elif isinstance(feed_cfg, dict):
            url = feed_cfg.get("url")
            tags = feed_cfg.get("tags") or []
        else:
            continue
        if not url:
            continue
        result.append((url, list(tags)))
    return result


def _sync_feeds(reader: Reader, config_feeds: list[tuple[str, list[str]]]) -> dict:
    """Make DB feeds authoritatively match config: add missing, remove extras, sync tags."""
    config_urls = {url for url, _ in config_feeds}
    existing_urls = {f.url for f in reader.get_feeds()}

    added, skipped = [], []
    for url, _ in config_feeds:
        if url in existing_urls:
            continue
        try:
            check_url(url)
        except UnsafeURL as e:
            sys.stderr.write(f"warning: skipping unsafe feed: {e}\n")
            skipped.append(url)
            continue
        try:
            reader.add_feed(url)
            added.append(url)
        except FeedExistsError:
            pass

    removed = []
    for url in existing_urls - config_urls:
        try:
            reader.delete_feed(url)
            removed.append(url)
        except FeedNotFoundError:
            pass

    live_urls = {f.url for f in reader.get_feeds()}
    for url, tags in config_feeds:
        if url not in live_urls:
            continue
        desired = set(tags)
        current = set(reader.get_tag_keys(url))
        for tag in desired - current:
            reader.set_tag(url, tag)
        for tag in current - desired:
            try:
                reader.delete_tag(url, tag)
            except TagNotFoundError:
                pass

    return {"added": added, "removed": removed, "skipped": skipped}


def _fetch_all(reader: Reader, *, show_progress: bool) -> tuple[list[dict], bool]:
    feeds = list(reader.get_feeds())
    results, any_error = [], False
    total = len(feeds)
    for idx, feed in enumerate(feeds, start=1):
        spinner = _spinner(f"[{idx}/{total}] fetching {feed.url}", enabled=show_progress)
        spinner.start()
        try:
            before = reader.get_entry_counts(feed=feed.url).total
            list(reader.update_feeds_iter(feed=feed.url))
            after = reader.get_entry_counts(feed=feed.url).total
            updated = reader.get_feed(feed.url)
            err = str(updated.last_exception) if updated.last_exception else None
            if err:
                any_error = True
            results.append({
                "url": updated.url,
                "title": updated.title or "",
                "new": max(0, after - before),
                "error": err,
            })
        finally:
            spinner.stop()
    return results, any_error


def _build_state(results: list[dict], prev: dict, any_error: bool) -> dict:
    total_new = sum(r["new"] for r in results)
    error_feeds = [f for f in results if f["error"]]
    now = _now_iso()
    state = {
        "system": "RSS Monitor",
        "lastRun": now,
        "lastSuccess": prev.get("lastSuccess"),
        "previousSuccess": prev.get("previousSuccess"),
        "lastError": "",
        "status": "Attention",
        "feedCount": len(results),
        "errorFeedCount": len(error_feeds),
        "newEntryCount": total_new,
        "materialChange": total_new > 0,
        "notes": "",
    }
    if not any_error:
        state["previousSuccess"] = prev.get("lastSuccess")
        state["lastSuccess"] = now
        state["status"] = "Healthy"
        state["notes"] = f"Updated {len(results)} feed(s); {total_new} new entries."
    else:
        first_err = next(
            (f"{f['url']}: {f['error']}" for f in error_feeds), "unknown error"
        )
        state["lastError"] = first_err
        state["notes"] = (
            f"Updated {len(results)} feed(s); {total_new} new entries; "
            f"{len(error_feeds)} feed(s) failed."
        )
    return state


def cmd_update(args) -> int:
    config_feeds = _normalize_feeds(args.config)
    show_progress = not args.json

    with _open_reader(args.db) as reader:
        sync = _sync_feeds(reader, config_feeds)
        results, any_error = _fetch_all(reader, show_progress=show_progress)

    prev = _load_state(args.state)
    state = _build_state(results, prev, any_error)
    _save_state(args.state, state)

    payload = {
        "ok": not any_error,
        "added": sync["added"],
        "removed": sync["removed"],
        "skipped": sync["skipped"],
        "feedCount": len(results),
        "newEntryCount": state["newEntryCount"],
        "feeds": results,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
        return 2 if any_error else 0

    lines = []
    if sync["added"]:
        lines.append(f"+ added {len(sync['added'])} feed(s): {', '.join(sync['added'])}")
    if sync["removed"]:
        lines.append(f"- removed {len(sync['removed'])} feed(s): {', '.join(sync['removed'])}")
    for r in results:
        if r["error"]:
            lines.append(f"✗ {r['title'] or r['url']} — {r['error']}")
        else:
            lines.append(f"✓ {r['title'] or r['url']} — {r['new']} new")
    lines.append("")
    lines.append(f"Total: {state['newEntryCount']} new across {len(results)} feed(s)")
    sys.stdout.write("\n".join(lines) + "\n")
    return 2 if any_error else 0


def _entry_to_dict(reader: Reader, entry) -> dict:
    return {
        "title": entry.title or "(untitled)",
        "feed_url": entry.feed_url,
        "feed_title": entry.feed.title or "",
        "feed_tags": sorted(reader.get_tag_keys(entry.feed_url)),
        "link": entry.link or "",
        "published": entry.published.isoformat() if entry.published else None,
        "added": entry.added.isoformat() if entry.added else None,
        "summary": (entry.summary or "")[:500],
    }


def _render_entries_md(entries: list[dict], header: str) -> str:
    lines = [f"# {len(entries)} {header}", ""]
    for e in entries:
        tag_str = " ".join(e["feed_tags"]) if e["feed_tags"] else "(no tags)"
        published = (e["published"] or e["added"] or "")[:16].replace("T", " ")
        summary = e["summary"].split("\n")[0][:200]
        lines.append(f"## {e['title']}")
        lines.append(f"{tag_str} · {published} · {e['link']}")
        if summary:
            lines.append(f"> {summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cmd_entries(args) -> int:
    state = _load_state(args.state)

    if args.since is not None:
        if args.since <= 0:
            sys.stderr.write("error: --since must be a positive integer (days)\n")
            return 2
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        filter_field = "published"
        header = f"entries · past {args.since} day(s)"
    else:
        prev_success = state.get("previousSuccess")
        filter_field = "added"
        if prev_success:
            cutoff = _parse_iso(prev_success)
            header = f"entries · since last run ({prev_success})"
        else:
            cutoff = None
            header = "entries · since last run (no prior run, showing all)"

    with _open_reader(args.db) as reader:
        entries = []
        for entry in reader.get_entries():
            if cutoff is not None:
                ts = getattr(entry, filter_field, None)
                if not ts or ts < cutoff:
                    continue
            entries.append(_entry_to_dict(reader, entry))

    entries.sort(key=lambda e: e["published"] or e["added"] or "", reverse=True)

    if args.json:
        sys.stdout.write(json.dumps(entries, default=str) + "\n")
    elif not entries:
        sys.stdout.write(f"(no {header})\n")
    else:
        sys.stdout.write(_render_entries_md(entries, header))
    return 0


def cmd_feeds(args) -> int:
    with _open_reader(args.db) as reader:
        feeds = []
        for feed in reader.get_feeds():
            feeds.append({
                "url": feed.url,
                "title": feed.title or "",
                "tags": sorted(reader.get_tag_keys(feed.url)),
                "last_exception": str(feed.last_exception) if feed.last_exception else None,
            })

    if args.json:
        sys.stdout.write(json.dumps(feeds, default=str) + "\n")
        return 0

    if not feeds:
        sys.stdout.write("(no feeds)\n")
        return 0

    lines = []
    for f in feeds:
        marker = " ✗" if f["last_exception"] else ""
        tag_str = f" [{', '.join(f['tags'])}]" if f["tags"] else ""
        title = f" — {f['title']}" if f["title"] else ""
        lines.append(f"- {f['url']}{tag_str}{title}{marker}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_example_config(args) -> int:
    sys.stdout.write(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rss_monitor", description=__doc__)
    p.add_argument("--config", default=None,
                   help="Path to config JSON (required for update/entries/feeds)")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of markdown")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "update",
        help="Sync DB feeds with config (authoritative) and fetch new entries",
    ).set_defaults(func=cmd_update)

    e = sub.add_parser(
        "entries",
        help="List entries since last successful update; "
             "use --since N to query the past N days instead",
    )
    e.add_argument("--since", type=int, metavar="DAYS",
                   help="Show entries from the past N days (overrides since-last-run)")
    e.set_defaults(func=cmd_entries)

    sub.add_parser("feeds", help="List feeds currently in the DB") \
        .set_defaults(func=cmd_feeds)

    sub.add_parser("example-config", help="Print example config JSON") \
        .set_defaults(func=cmd_example_config)

    return p


def _resolve_config_args(args) -> None:
    if args.cmd == "example-config":
        return
    if not args.config:
        sys.stderr.write("error: --config is required\n")
        sys.exit(2)
    cfg_path = Path(args.config)
    config = _load_config_file(cfg_path)
    db, state = _resolve_paths(config, cfg_path)
    args.config = config
    args.db = db
    args.state = state


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _resolve_config_args(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
