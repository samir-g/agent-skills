#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "reader>=3.15",
#     "markdownify>=0.11",
# ]
# ///
"""RSS/Atom monitor CLI for AI-agent intel triage. See design doc for full spec."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from markdownify import markdownify
from reader import (
    make_reader,
    Reader,
    FeedExistsError,
    FeedNotFoundError,
    TagNotFoundError,
)


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    """Reject malformed URLs and any host that resolves to a non-public address.

    Guards against SSRF: localhost, RFC1918, link-local (incl. cloud metadata
    169.254.169.254), multicast, and reserved ranges.
    """
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


@contextmanager
def _open_reader(args):
    r: Reader = make_reader(str(args.db)) if args.db else make_reader()
    try:
        yield r
    finally:
        r.close()


def _emit(args, text: str, data: dict | list) -> None:
    if args.json:
        sys.stdout.write(json.dumps(data, default=str))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")


def cmd_feed_add(args) -> int:
    url = args.url
    try:
        check_url(url)
    except UnsafeURL as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    with _open_reader(args) as reader:
        try:
            reader.add_feed(url)
            existed = False
        except FeedExistsError:
            existed = True
        for tag in args.tag or []:
            reader.set_tag(url, tag)
        tags = sorted(reader.get_tag_keys(url))
    msg = ("feed exists, tags merged" if existed else "added feed")
    text = f"✓ {msg}: {url} [{', '.join(tags)}]"
    _emit(args, text, {"ok": True, "url": url, "tags": tags, "existed": existed})
    return 0


def _feed_to_dict(feed, tags: list[str]) -> dict:
    return {
        "url": feed.url,
        "title": feed.title or "",
        "tags": tags,
        "last_exception": str(feed.last_exception) if feed.last_exception else None,
    }


def cmd_feed_list(args) -> int:
    with _open_reader(args) as reader:
        feeds = []
        for feed in reader.get_feeds():
            tags = sorted(reader.get_tag_keys(feed.url))
            if args.tag and args.tag not in tags:
                continue
            if args.errors_only and not feed.last_exception:
                continue
            feeds.append(_feed_to_dict(feed, tags))

    if args.json:
        _emit(args, "", feeds)
        return 0

    if not feeds:
        _emit(args, "(no feeds)", feeds)
        return 0

    lines = []
    for f in feeds:
        marker = " ✗" if f["last_exception"] else ""
        tag_str = f" [{', '.join(f['tags'])}]" if f["tags"] else ""
        lines.append(f"- {f['url']}{tag_str}{marker}")
    _emit(args, "\n".join(lines), feeds)
    return 0


def cmd_feed_remove(args) -> int:
    with _open_reader(args) as reader:
        try:
            reader.delete_feed(args.url)
            removed = True
        except FeedNotFoundError:
            removed = False
    if removed:
        _emit(args, f"✓ removed: {args.url}", {"ok": True, "url": args.url})
    else:
        _emit(args, f"⚠ not found: {args.url}",
              {"ok": True, "url": args.url, "warning": "not found"})
    return 0


def cmd_feed_tag(args) -> int:
    with _open_reader(args) as reader:
        for tag in args.tags:
            reader.set_tag(args.url, tag)
        tags = sorted(reader.get_tag_keys(args.url))
    _emit(args, f"✓ tags now: [{', '.join(tags)}]",
          {"ok": True, "url": args.url, "tags": tags})
    return 0


def cmd_feed_untag(args) -> int:
    with _open_reader(args) as reader:
        for tag in args.tags:
            try:
                reader.delete_tag(args.url, tag)
            except TagNotFoundError:
                pass
        tags = sorted(reader.get_tag_keys(args.url))
    _emit(args, f"✓ tags now: [{', '.join(tags)}]",
          {"ok": True, "url": args.url, "tags": tags})
    return 0


def _update_once(args) -> tuple[dict, int]:
    """Run an update pass and return (payload, exit_code)."""
    with _open_reader(args) as reader:
        if args.feed:
            feed_urls = [args.feed]
        elif args.tag:
            feed_urls = [
                f.url for f in reader.get_feeds()
                if args.tag in reader.get_tag_keys(f.url)
            ]
        else:
            feed_urls = [f.url for f in reader.get_feeds()]

        results = []
        any_error = False
        for url in feed_urls:
            try:
                check_url(url)
            except UnsafeURL as e:
                any_error = True
                results.append({
                    "url": url,
                    "title": "",
                    "new": 0,
                    "error": str(e),
                })
                continue
            before = reader.get_entry_counts(feed=url).total
            list(reader.update_feeds_iter(feed=url))
            after = reader.get_entry_counts(feed=url).total
            feed = reader.get_feed(url)
            err = str(feed.last_exception) if feed.last_exception else None
            if err:
                any_error = True
            results.append({
                "url": url,
                "title": feed.title or "",
                "new": max(0, after - before),
                "error": err,
            })

        total_new = sum(r["new"] for r in results)
        payload = {"ok": not any_error, "total_new": total_new, "feeds": results}

    return payload, (2 if any_error else 0)


def cmd_update(args) -> int:
    payload, exit_code = _update_once(args)
    results = payload["feeds"]
    total_new = payload["total_new"]
    if args.json:
        _emit(args, "", payload)
    else:
        lines = []
        for r in results:
            if r["error"]:
                lines.append(f"✗ {r['title'] or r['url']} — {r['error']}")
            else:
                lines.append(f"✓ {r['title'] or r['url']} — {r['new']} new")
        lines.append(f"\nTotal: {total_new} new across {len(results)} feed(s)")
        _emit(args, "\n".join(lines), payload)
    return exit_code


def cmd_poll(args) -> int:
    runs = 0
    last_payload = None
    update_args = argparse.Namespace(
        db=args.db, json=True, tag=None, feed=None,
    )
    while args.iterations is None or runs < args.iterations:
        payload, _ = _update_once(update_args)
        last_payload = payload
        runs += 1
        if args.iterations is not None and runs >= args.iterations:
            break
        time.sleep(args.interval)

    result = {"ok": True, "runs": runs, "last": last_payload}
    if args.json:
        _emit(args, "", result)
    else:
        _emit(args, f"✓ poll completed {runs} run(s)", result)
    return 0


def entry_hash(feed_url: str, entry_id: str) -> str:
    # 16 hex chars = 64 bits. The previous 10-char (40-bit) form was
    # vulnerable to collision-driven entry confusion: a malicious feed
    # could craft an entry_id whose hash prefix matched a target entry,
    # and _resolve_hashes returns the first match it finds.
    raw = f"{feed_url}\x00{entry_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _entry_to_dict(reader: Reader, entry, include_content: bool = False) -> dict:
    feed_tags = sorted(reader.get_tag_keys(entry.feed_url))
    entry_tags = sorted(reader.get_tag_keys((entry.feed_url, entry.id)))
    return {
        "hash": entry_hash(entry.feed_url, entry.id),
        "title": entry.title or "(untitled)",
        "feed_url": entry.feed_url,
        "feed_title": entry.feed.title or "",
        "feed_tags": feed_tags,
        "entry_tags": entry_tags,
        "link": entry.link or "",
        "published": entry.published.isoformat() if entry.published else None,
        "important": bool(entry.important),
        "read": bool(entry.read),
        "summary": (entry.summary or "")[:500],
        "content": (entry.get_content().value if include_content and entry.get_content() else None),
    }


def _parse_since(value: str) -> timedelta:
    """Parse '24h', '3d', '1w' → timedelta."""
    unit = value[-1]
    n = int(value[:-1])
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    raise ValueError(f"invalid --since value: {value!r} (use 24h, 3d, 1w)")


def _render_entries_md(entries: list[dict], header: str) -> str:
    lines = [f"# {len(entries)} {header}", ""]
    for e in entries:
        bang = " · !!!" if e["important"] else ""
        tag_parts = list(e["feed_tags"]) + list(e["entry_tags"])
        tag_str = " ".join(tag_parts) if tag_parts else "(no tags)"
        published = (e["published"] or "")[:16].replace("T", " ")
        summary = e["summary"].split("\n")[0][:200]
        lines.append(f"## {e['title']}")
        lines.append(f"`{e['hash']}` · {tag_str} · {published}{bang} · {e['link']}")
        if summary:
            lines.append(f"> {summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _query_entries(args, *, read=None, important=None) -> list[dict]:
    kwargs = {}
    if read is not None:
        kwargs["read"] = read
    if important is not None:
        kwargs["important"] = important
    if getattr(args, "feed", None):
        kwargs["feed"] = args.feed
    if getattr(args, "tag", None):
        kwargs["feed_tags"] = [args.tag]

    with _open_reader(args) as reader:
        entries = list(reader.get_entries(**kwargs))

        if getattr(args, "since", None):
            cutoff = datetime.now(timezone.utc) - _parse_since(args.since)
            entries = [
                e for e in entries
                if e.published and e.published >= cutoff
            ]

        if getattr(args, "limit", None):
            entries = entries[: args.limit]

        return [_entry_to_dict(reader, e) for e in entries]


def cmd_list(args) -> int:
    read = None
    if args.read:
        read = True
    elif args.unread:
        read = False
    important = True if args.important else None
    entries = _query_entries(args, read=read, important=important)

    if args.json:
        _emit(args, "", entries)
    else:
        descriptors = []
        if read is True:
            descriptors.append("read")
        elif read is False:
            descriptors.append("unread")
        if important:
            descriptors.append("important")
        header = " ".join(descriptors) or "entries"
        if args.tag:
            header += f" · {args.tag}"
        text = _render_entries_md(entries, header) if entries else f"(no {header})"
        _emit(args, text, entries)
    return 0


def _resolve_hashes(reader: Reader, hashes: list[str]) -> list[tuple[str, str]]:
    """Map short hashes to (feed_url, entry_id) keys. Raises if any not found."""
    wanted = set(hashes)
    found: dict[str, tuple[str, str]] = {}
    for entry in reader.get_entries():
        h = entry_hash(entry.feed_url, entry.id)
        if h in wanted:
            found[h] = (entry.feed_url, entry.id)
            if len(found) == len(wanted):
                break
    missing = wanted - set(found)
    if missing:
        sys.stderr.write(
            f"error: no entry with hash {sorted(missing)[0]!r}\n"
        )
        sys.exit(1)
    return [found[h] for h in hashes]


def _bulk_set(args, *, read=None, important=None, action_label="updated") -> int:
    with _open_reader(args) as reader:
        keys = _resolve_hashes(reader, args.hashes)
        for key in keys:
            if read is not None:
                reader.mark_entry_as_read(key) if read else reader.mark_entry_as_unread(key)
            if important is not None:
                reader.mark_entry_as_important(key) if important else reader.mark_entry_as_unimportant(key)
    _emit(args, f"✓ marked {len(keys)} entries {action_label}",
          {"ok": True, "count": len(keys), "hashes": args.hashes})
    return 0


def cmd_read(args) -> int:
    return _bulk_set(args, read=True, action_label="read")


def cmd_unread(args) -> int:
    return _bulk_set(args, read=False, action_label="unread")


def cmd_important(args) -> int:
    return _bulk_set(args, important=True, action_label="important")


def cmd_unimportant(args) -> int:
    return _bulk_set(args, important=False, action_label="unimportant")


def cmd_tag_entry(args) -> int:
    with _open_reader(args) as reader:
        keys = _resolve_hashes(reader, [args.hash])
        key = keys[0]
        for tag in args.tags:
            reader.set_tag(key, tag)
        tags = sorted(reader.get_tag_keys(key))
    _emit(args, f"✓ tags now: [{', '.join(tags)}]",
          {"ok": True, "hash": args.hash, "tags": tags})
    return 0


def cmd_untag_entry(args) -> int:
    with _open_reader(args) as reader:
        keys = _resolve_hashes(reader, [args.hash])
        key = keys[0]
        for tag in args.tags:
            try:
                reader.delete_tag(key, tag)
            except TagNotFoundError:
                pass
        tags = sorted(reader.get_tag_keys(key))
    _emit(args, f"✓ tags now: [{', '.join(tags)}]",
          {"ok": True, "hash": args.hash, "tags": tags})
    return 0


def cmd_new(args) -> int:
    entries = _query_entries(args, read=False)
    if args.json:
        _emit(args, "", entries)
    else:
        header = "unread" + (f" · {args.tag}" if args.tag else "")
        text = _render_entries_md(entries, header) if entries else "(no unread entries)"
        _emit(args, text, entries)
    return 0


def cmd_show(args) -> int:
    with _open_reader(args) as reader:
        keys = _resolve_hashes(reader, [args.hash])
        feed_url, entry_id = keys[0]
        entry = reader.get_entry((feed_url, entry_id))
        data = _entry_to_dict(reader, entry, include_content=True)

    if args.json:
        _emit(args, "", data)
        return 0

    body = data["content"] or data["summary"] or ""
    if "<" in body and ">" in body:
        body = markdownify(body)

    tags = list(data["feed_tags"]) + list(data["entry_tags"])
    tag_str = " ".join(tags) if tags else "(no tags)"
    bang = " · !!!" if data["important"] else ""
    text = (
        f"# {data['title']}\n"
        f"`{data['hash']}` · {tag_str} · {data['published'] or ''}{bang}\n"
        f"**Link:** {data['link']}\n"
        f"\n---\n\n"
        f"{body}\n"
    )
    _emit(args, text, data)
    return 0


def cmd_search(args) -> int:
    with _open_reader(args) as reader:
        reader.enable_search()
        reader.update_search()

        kwargs = {}
        if args.tag:
            kwargs["feed_tags"] = [args.tag]

        results = []
        for sr in reader.search_entries(args.query, **kwargs):
            entry = reader.get_entry(sr.resource_id)
            results.append(_entry_to_dict(reader, entry))

    if args.json:
        _emit(args, "", results)
    else:
        text = (
            _render_entries_md(results, f"matches for {args.query!r}")
            if results else f"(no matches for {args.query!r})"
        )
        _emit(args, text, results)
    return 0


def cmd_triage(args) -> int:
    raw = sys.stdin.read()
    try:
        decisions = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"error: invalid JSON on stdin: {e}\n")
        return 1

    if not isinstance(decisions, list):
        sys.stderr.write("error: triage input must be a JSON array\n")
        return 1

    with _open_reader(args) as reader:
        # Phase 1: resolve all hashes (exits 1 before mutating anything)
        hashes = [d["hash"] for d in decisions]
        keys = _resolve_hashes(reader, hashes)
        decisions_with_keys = list(zip(decisions, keys))

        # Phase 2: apply
        for d, key in decisions_with_keys:
            if "read" in d:
                (reader.mark_entry_as_read if d["read"]
                 else reader.mark_entry_as_unread)(key)
            if "important" in d:
                (reader.mark_entry_as_important if d["important"]
                 else reader.mark_entry_as_unimportant)(key)
            for tag in d.get("tags_add", []):
                reader.set_tag(key, tag)
            for tag in d.get("tags_remove", []):
                try:
                    reader.delete_tag(key, tag)
                except TagNotFoundError:
                    pass

    _emit(args, f"✓ applied {len(decisions)} decisions",
          {"ok": True, "applied": len(decisions)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Shared parent parser for flags that apply to every leaf subcommand.
    # add_help=False prevents duplicate -h entries.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")

    p = argparse.ArgumentParser(prog="rss_monitor", description=__doc__, parents=[shared])
    p.add_argument("--db", type=Path, default=None,
                   help="Path to reader SQLite DB (default: reader's default location)")

    sub = p.add_subparsers(dest="cmd", required=True)

    feed = sub.add_parser("feed", help="Feed management")
    feed_sub = feed.add_subparsers(dest="feed_cmd", required=True)

    f_add = feed_sub.add_parser("add", help="Add a feed", parents=[shared])
    f_add.add_argument("url")
    f_add.add_argument("--tag", action="append", help="Tag (repeatable)")
    f_add.set_defaults(func=cmd_feed_add)

    f_list = feed_sub.add_parser("list", help="List feeds", parents=[shared])
    f_list.add_argument("--tag", help="Filter by tag")
    f_list.add_argument("--errors-only", action="store_true",
                        help="Only show feeds with stored errors")
    f_list.set_defaults(func=cmd_feed_list)

    f_rm = feed_sub.add_parser("remove", help="Remove a feed", parents=[shared])
    f_rm.add_argument("url")
    f_rm.set_defaults(func=cmd_feed_remove)

    f_tag = feed_sub.add_parser("tag", help="Add tags to a feed", parents=[shared])
    f_tag.add_argument("url")
    f_tag.add_argument("tags", nargs="+")
    f_tag.set_defaults(func=cmd_feed_tag)

    f_untag = feed_sub.add_parser("untag", help="Remove tags from a feed", parents=[shared])
    f_untag.add_argument("url")
    f_untag.add_argument("tags", nargs="+")
    f_untag.set_defaults(func=cmd_feed_untag)

    upd = sub.add_parser("update", help="Fetch new entries", parents=[shared])
    upd.add_argument("--tag", help="Only update feeds with this tag")
    upd.add_argument("--feed", help="Only update this feed URL")
    upd.set_defaults(func=cmd_update)

    poll_p = sub.add_parser("poll", help="Run update repeatedly", parents=[shared])
    poll_p.add_argument("--interval", type=int, default=900,
                        help="Seconds between runs (default: 900)")
    poll_p.add_argument("--iterations", type=int, default=None,
                        help="Number of runs (default: forever)")
    poll_p.set_defaults(func=cmd_poll)

    new_p = sub.add_parser("new", help="List unread entries", parents=[shared])
    new_p.add_argument("--tag", help="Filter by feed tag")
    new_p.add_argument("--feed", help="Filter by feed URL")
    new_p.add_argument("--limit", type=int, help="Max entries to return")
    new_p.add_argument("--since", help="Only entries newer than e.g. 24h, 3d, 1w")
    new_p.set_defaults(func=cmd_new)

    list_p = sub.add_parser("list", help="List entries with filters", parents=[shared])
    list_p.add_argument("--tag", help="Filter by feed tag")
    list_p.add_argument("--feed", help="Filter by feed URL")
    list_p.add_argument("--limit", type=int)
    list_p.add_argument("--since", help="e.g. 24h, 3d, 1w")
    rg = list_p.add_mutually_exclusive_group()
    rg.add_argument("--read", action="store_true", help="Only entries already marked read")
    rg.add_argument("--unread", action="store_true", help="Only unread entries")
    list_p.add_argument("--important", action="store_true",
                        help="Only important entries")
    list_p.set_defaults(func=cmd_list)

    for name, fn, hint in [
        ("read", cmd_read, "Mark entries as read"),
        ("unread", cmd_unread, "Mark entries as unread"),
        ("important", cmd_important, "Pin entries as important"),
        ("unimportant", cmd_unimportant, "Unpin entries"),
    ]:
        s = sub.add_parser(name, help=hint, parents=[shared])
        s.add_argument("hashes", nargs="+", metavar="HASH")
        s.set_defaults(func=fn)

    tag_p = sub.add_parser("tag", help="Tag an entry", parents=[shared])
    tag_p.add_argument("hash")
    tag_p.add_argument("tags", nargs="+")
    tag_p.set_defaults(func=cmd_tag_entry)

    untag_p = sub.add_parser("untag", help="Remove tags from an entry", parents=[shared])
    untag_p.add_argument("hash")
    untag_p.add_argument("tags", nargs="+")
    untag_p.set_defaults(func=cmd_untag_entry)

    show_p = sub.add_parser("show", help="Show full entry content", parents=[shared])
    show_p.add_argument("hash")
    show_p.set_defaults(func=cmd_show)

    search_p = sub.add_parser("search", help="Full-text search across entries", parents=[shared])
    search_p.add_argument("query")
    search_p.add_argument("--tag", help="Filter by feed tag")
    search_p.set_defaults(func=cmd_search)

    triage_p = sub.add_parser("triage", help="Bulk-apply triage decisions from JSON on stdin", parents=[shared])
    triage_p.set_defaults(func=cmd_triage)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
