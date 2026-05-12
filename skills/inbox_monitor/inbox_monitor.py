#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["halo>=0.0.31"]
# ///
"""Config-driven IMAP inbox monitor: fetch new messages, classify, summarize.

Required env (loaded from envFile, falls back to the process env):
    ASSISTANT_EMAIL_ADDRESS       IMAP login (email address).
    ASSISTANT_EMAIL_APP_PASSWORD  IMAP password (Gmail app password recommended).
"""

from __future__ import annotations

import argparse
import email
import imaplib
import ipaddress
import json
import os
import re
import socket
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path

from halo import Halo


DEFAULT_STATE_FILE_NAME = "inbox_monitor.json"

DEFAULT_FEED_KEYWORDS = [
    "google alerts", "news", "newsletter", "substack",
    "digest", "brief", "daily",
]

EXAMPLE_CONFIG = {
    "statePath": "~/.openclaw/workspace/state",
    "envFile": "~/.openclaw/secrets/assistant-email.env",
    "imapHost": "imap.gmail.com",
    "imapPort": 993,
    "mailbox": "INBOX",
    "scanLimit": 100,
    "feedKeywords": DEFAULT_FEED_KEYWORDS,
}


class UnsafeHost(ValueError):
    pass


def check_host(host: str) -> None:
    """Reject hosts that resolve to non-public addresses (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeHost(f"could not resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or not ip.is_global):
            raise UnsafeHost(f"refusing non-public address: {host} ({ip})")


class _NullSpinner:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def start(self, *a, **kw): return self
    def stop(self): pass


def _spinner(text: str, *, enabled: bool):
    if not enabled or not sys.stderr.isatty():
        return _NullSpinner()
    return Halo(text=text, spinner="dots", stream=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _imap_since_date(dt: datetime) -> str:
    """IMAP SEARCH SINCE date format (DD-Mon-YYYY)."""
    return dt.strftime("%d-%b-%Y")


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


def _resolve_state_path(config: dict, config_path: Path) -> Path:
    state_dir_raw = config.get("statePath")
    if not state_dir_raw:
        sys.stderr.write("error: config must set statePath\n")
        sys.exit(2)
    state_dir = Path(state_dir_raw).expanduser()
    if not state_dir.is_absolute():
        state_dir = (config_path.resolve().parent / state_dir).resolve()
    return state_dir / DEFAULT_STATE_FILE_NAME


def _resolve_env_files(config: dict, config_path: Path,
                       overrides: list[str]) -> list[Path]:
    raw: list[str] = []
    cfg_val = config.get("envFiles") or config.get("envFile")
    if isinstance(cfg_val, str):
        raw.append(cfg_val)
    elif isinstance(cfg_val, list):
        raw.extend(cfg_val)
    raw.extend(overrides)
    paths: list[Path] = []
    for r in raw:
        p = Path(r).expanduser()
        if not p.is_absolute():
            p = (config_path.resolve().parent / p).resolve()
        paths.append(p)
    return paths


def _load_env(paths: list[Path]) -> dict:
    env: dict[str, str] = {}
    for p in paths:
        if not p.exists():
            sys.stderr.write(f"warning: env file not found: {p}\n")
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _env_get(env: dict, key: str) -> str | None:
    return env.get(key) or os.environ.get(key)


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _classify(subject: str, sender: str, keywords: list[str]) -> str:
    hay = f"{subject} {sender}".lower()
    if any(k in hay for k in keywords):
        return "feed"
    return "other"


def _fetch_new_messages(*, env: dict, host: str, port: int, mailbox: str,
                        scan_limit: int, last_uid: int,
                        keywords: list[str], since_date: str | None,
                        apply_dedup: bool) -> tuple:
    addr = _env_get(env, "ASSISTANT_EMAIL_ADDRESS")
    pw = _env_get(env, "ASSISTANT_EMAIL_APP_PASSWORD")
    if not addr or not pw:
        raise RuntimeError(
            "missing ASSISTANT_EMAIL_ADDRESS or ASSISTANT_EMAIL_APP_PASSWORD"
        )

    mail = imaplib.IMAP4_SSL(host, port)
    try:
        mail.login(addr, pw)
        mail.select(mailbox)
        if since_date:
            status, data = mail.uid("search", None, "SINCE", since_date)
        else:
            status, data = mail.uid("search", None, "ALL")
        uids = [] if status != "OK" else [u for u in data[0].split() if u]
        candidate_uids = uids if since_date else uids[-scan_limit:]
        out: list = []
        max_uid = last_uid
        for uid in candidate_uids:
            uid_int = int(uid)
            if uid_int > max_uid:
                max_uid = uid_int
            if apply_dedup and uid_int <= last_uid:
                continue
            status, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = _clean(_decode(msg.get("Subject", "")))
            sender = _clean(_decode(msg.get("From", "")))
            date = _clean(msg.get("Date", ""))
            out.append({
                "uid": uid_int,
                "from": sender,
                "subject": subject,
                "date": date,
                "type": _classify(subject, sender, keywords),
            })
        return out, max_uid
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _render(messages: list) -> str:
    if not messages:
        return "Inbox monitor: no new email.\n"
    feeds = [m for m in messages if m["type"] == "feed"]
    other = [m for m in messages if m["type"] != "feed"]
    lines = [f"Inbox monitor: {len(messages)} new email(s).", ""]
    if feeds:
        lines.append(f"Feed/news items: {len(feeds)}")
        for m in feeds[:10]:
            lines.append(f"- {m['subject']} | {m['from']}")
        lines.append("")
    if other:
        lines.append(f"Other email: {len(other)}")
        for m in other[:10]:
            lines.append(f"- {m['subject']} | {m['from']}")
    return "\n".join(lines).rstrip() + "\n"


def _build_state(prev: dict, *, messages: list, last_uid: int,
                 error) -> dict:
    now = _now_iso()
    feeds = sum(1 for m in messages if m["type"] == "feed")
    other = sum(1 for m in messages if m["type"] != "feed")
    state = {
        "system": "Inbox Monitor",
        "lastRun": now,
        "lastSuccess": prev.get("lastSuccess"),
        "previousSuccess": prev.get("previousSuccess"),
        "lastError": "",
        "status": "Attention",
        "materialChange": bool(messages),
        "notes": "",
        "newMessageCount": len(messages),
        "feedMessageCount": feeds,
        "otherMessageCount": other,
        "lastUid": last_uid,
    }
    if error is None:
        state["previousSuccess"] = prev.get("lastSuccess")
        state["lastSuccess"] = now
        state["status"] = "Healthy"
        state["notes"] = (
            f"Fetched {len(messages)} new message(s); "
            f"{feeds} feed, {other} other."
        )
    else:
        state["lastError"] = error
        state["notes"] = f"Failed: {error}"
    return state


def cmd_update(args) -> int:
    cfg = args.config
    host = cfg.get("imapHost", "imap.gmail.com")
    port = int(cfg.get("imapPort", 993))
    mailbox = cfg.get("mailbox", "INBOX")
    scan_limit = int(cfg.get("scanLimit", 100))
    keywords = cfg.get("feedKeywords") or DEFAULT_FEED_KEYWORDS

    try:
        check_host(host)
    except UnsafeHost as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    env_paths = _resolve_env_files(cfg, args.config_path, args.env_file or [])
    env = _load_env(env_paths)

    prev = _load_state(args.state)
    last_uid = int(prev.get("lastUid", 0))

    apply_dedup = True
    write_state = True
    since_date: str | None = None
    if args.since is not None:
        if args.since <= 0:
            sys.stderr.write("error: --since must be a positive integer (days)\n")
            return 2
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        since_date = _imap_since_date(cutoff)
        apply_dedup = False
        write_state = False
        window_desc = f"past {args.since} day(s)"
    else:
        last_success = prev.get("lastSuccess")
        if last_success:
            # IMAP SINCE is day-granular; subtract a day so timezone drift
            # never hides a message — UID dedup handles the overlap.
            cutoff = _parse_iso(last_success) - timedelta(days=1)
            since_date = _imap_since_date(cutoff)
            window_desc = f"since last run ({last_success})"
        else:
            window_desc = f"first run (most recent {scan_limit} UIDs)"

    show_progress = not args.json
    error = None
    messages: list = []
    max_uid = last_uid

    spinner = _spinner(f"fetching {host}/{mailbox} — {window_desc}",
                       enabled=show_progress)
    spinner.start()
    try:
        messages, max_uid = _fetch_new_messages(
            env=env, host=host, port=port, mailbox=mailbox,
            scan_limit=scan_limit, last_uid=last_uid,
            keywords=keywords,
            since_date=since_date, apply_dedup=apply_dedup,
        )
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        spinner.stop()

    state = _build_state(prev, messages=messages, last_uid=max_uid,
                         error=error)

    if not args.dry_run and write_state:
        _save_state(args.state, state)

    if error is not None:
        if args.json:
            sys.stderr.write(json.dumps(state, indent=2) + "\n")
        else:
            sys.stderr.write(f"error: {error}\n")
        return 2

    if args.json:
        sys.stdout.write(json.dumps(messages, indent=2) + "\n")
    else:
        sys.stdout.write(_render(messages))
    return 0


def cmd_example_config(args) -> int:
    sys.stdout.write(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inbox_monitor", description=__doc__)
    p.add_argument("--config", default=None,
                   help="Path to config JSON (required for update)")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human-readable text")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and print, but do not write state")
    p.add_argument("--env-file", action="append", metavar="PATH",
                   help="Override envFile from config (repeatable, later wins)")

    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser(
        "update",
        help="Fetch new messages from the inbox (default: since last "
             "successful run)",
    )
    u.add_argument(
        "--since", type=int, metavar="DAYS",
        help="Fetch emails from the past N days instead of since-last-run "
             "(skips UID dedup; does not write state)",
    )
    u.set_defaults(func=cmd_update)

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
    state = _resolve_state_path(config, cfg_path)
    args.config_path = cfg_path
    args.config = config
    args.state = state


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _resolve_config_args(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
