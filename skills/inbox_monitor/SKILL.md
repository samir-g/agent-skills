---
name: inbox-monitor
description: Use when the user wants to check the assistant's email inbox for new mail — config-driven IMAP fetch that dedupes by UID, classifies messages as feed/newsletter vs other, and summarizes new mail since the last run.
---

# Inbox monitor

Config-driven IMAP inbox poller. One subcommand: `update`.

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/inbox-monitor/inbox_monitor.py" \
        --config /path/to/config.json update

Default behaviour: fetch only emails received **since the last successful run**, using a server-side IMAP `SINCE` filter plus UID dedup. On the very first run (no prior `lastSuccess`), falls back to scanning the most recent `scanLimit` UIDs.

Override with `--since N` to fetch emails from the past N days:

    update --since 1     # past day
    update --since 7     # past week

`--since` skips UID dedup (so previously-seen messages in the window are re-emitted) and does **not** write state, so it's safe to use as an ad-hoc query without disrupting the default since-last-run cursor.

Add `--json` for machine-readable output. `--dry-run` fetches and prints without writing state.

## Config

    {
      "statePath": "~/.openclaw/workspace/state",
      "envFile": "~/.openclaw/secrets/assistant-email.env",
      "imapHost": "imap.gmail.com",
      "imapPort": 993,
      "mailbox": "INBOX",
      "scanLimit": 100,
      "feedKeywords": ["google alerts", "newsletter", "digest"]
    }

Keys:

- `statePath` *(required)* — directory for the state JSON. Relative paths resolve against the config file's directory; `~` is expanded. The script writes `inbox_monitor.json` inside it.
- `envFile` / `envFiles` — path or list of paths to `KEY=VALUE` files providing `ASSISTANT_EMAIL_ADDRESS` and `ASSISTANT_EMAIL_APP_PASSWORD`. Falls back to the process env. `--env-file PATH` (repeatable) overrides; later wins.
- `imapHost` — IMAP server. Default `imap.gmail.com`. Resolved against a public-IP guard before connecting.
- `imapPort` — IMAP port. Default `993`.
- `mailbox` — mailbox to select. Default `INBOX`.
- `scanLimit` — most-recent N UIDs to scan per run. Default `100`.
- `feedKeywords` — case-insensitive substrings used to tag a message as `feed` (matched against `subject` + `from`). Default covers common newsletter/digest patterns.

Print a worked example:

    uv run inbox_monitor.py example-config

See `assistant_email_setup.md` for instructions on creating the env file.

## State JSON

Written on every run to `<statePath>/inbox_monitor.json`. Standard keys plus skill-specific counters:

- `system`, `lastRun`, `lastSuccess`, `previousSuccess`, `lastError`
- `status` (`Healthy` / `Attention`), `materialChange`, `notes`
- `newMessageCount`, `feedMessageCount`, `otherMessageCount`
- `lastUid` — highest IMAP UID seen so far

`previousSuccess` snapshots the prior `lastSuccess` on each successful run, so downstream consumers can ask "what landed since last run."

## Behaviour

- Uses IMAP `SEARCH SINCE <date>` to restrict the server-side scan to the relevant window (date-of-`lastSuccess` minus one day for timezone margin). UID dedup still runs on top of the result to be precise.
- Dedupes by IMAP UID against `lastUid` (the monotonic high-water mark). Re-running with no new mail is a no-op apart from the `lastRun` timestamp.
- SSRF guard rejects `imapHost` values that resolve to private/loopback/link-local addresses.
- Only `ASSISTANT_EMAIL_ADDRESS` / `ASSISTANT_EMAIL_APP_PASSWORD` are read from env. IMAP host/port live in the config (single source of truth).
- Exit `0` on success, `2` on failure (config error, missing env, IMAP error). Errors land on stderr; in `--json` mode the state JSON is emitted to stderr on failure so stdout stays clean.
- Progress spinner on stderr; suppressed when stderr is not a TTY or `--json` is set.
- `runner.sh` is a thin wrapper that runs `update` against the co-located `config.json`.
