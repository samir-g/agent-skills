---
name: script_builder
description: Use when building any custom script or CLI tool — establishes the house conventions for a single-file Python entrypoint runnable via `uv run`, a JSON config file for user input, a standardized state JSON, a halo spinner on stderr, and a thin `runner.sh` wrapper. Apply whenever a task calls for writing a new script, regardless of domain.
---

# Build a skill script

A specification for new scripts that drop into `skills/<skill-name>/` and behave like the existing siblings. Follow this when adding a new skill or rewriting one.

## Directory layout

    skills/<skill_name>/
      SKILL.md                # frontmatter + usage doc (this file's sibling pattern)
      <script>.py             # single-file Python entrypoint with PEP 723 metadata
      runner.sh               # optional thin wrapper; uses a co-located config.json
      config.json             # optional bundled config; user-specific, usually gitignored

Skill names use underscores (`rss_monitor`, not `rss-monitor`); the frontmatter `name` is the kebab-case version (`rss-monitor`). Plural scripts (e.g. `collect.py` + `sync.py`) are fine when a step boundary is genuinely meaningful — otherwise prefer one script with subcommands.

**Do not `chmod +x`** any file in this repo. Scripts are invoked via `uv run` or `bash runner.sh`, not executed directly.

## SKILL.md frontmatter

    ---
    name: <kebab-case-name>
    description: Use when the user wants to <user intent>. <one-line capability summary>.
    ---

The `description` is what Claude reads to decide when to load the skill. Lead with **"Use when…"** and name the user intent, not the implementation. Keep it under ~280 chars.

After frontmatter, document:

1. The one-line purpose.
2. The exact invocation (`uv run "${CLAUDE_PLUGIN_ROOT}/skills/<name>/<script>.py" --config /path/to/config.json [subcommand]`).
3. Config schema with required vs optional keys.
4. A worked example (or a `--print-example-config` / `example-config` pointer).
5. Subcommands and useful flags.
6. Behaviour notes (auth, SSRF, idempotency, exit codes).

## Python entrypoint

### PEP 723 inline metadata

Every script starts with a PEP 723 header so `uv run` resolves a venv automatically — no separate install step:

    #!/usr/bin/env python3
    # /// script
    # requires-python = ">=3.9"
    # dependencies = ["halo"]
    # ///
    """One-line purpose. Longer prose if needed.

    Required env:
        FOO_API_KEY    What it's for.
    """

Pin `requires-python = ">=3.9"` unless a newer feature forces higher (`rss_monitor` uses `>=3.10` for the `Reader` library). List only the dependencies you actually import — prefer stdlib (`urllib.request`, `json`, `argparse`) for HTTP and JSON work; reach for `requests`/`httpx` only when stdlib genuinely doesn't fit.

`halo` is the conventional dependency for the spinner. Stdlib-only scripts (e.g. `phantombuster_monitor/collect.py`) still depend on `halo`.

### CLI surface

Use `argparse` with subcommands when there's more than one operation (`update`, `entries`, `feeds`). Single-purpose scripts can skip subcommands.

Standard flags:

| Flag                     | Purpose |
|--------------------------|---------|
| `--config PATH`          | Path to JSON config. Required for any command that touches state or external systems. |
| `--json`                 | Emit machine-readable JSON instead of human-readable markdown/text. |
| `--dry-run`              | Run side-effect-free; print what would happen, don't write state or external systems. |
| `--env-file PATH`        | Repeatable. Overrides `envFiles`/`envFile` from the config. Later wins. |
| `--print-example-config` | Print the example config JSON and exit. (Or expose as an `example-config` subcommand.) |

Exit codes: `0` success, `2` failure (config error, network error, partial failure). Reserve `1` for argparse usage errors (argparse default).

### Config loading

Config is the single source of truth. The script:

1. Loads JSON from `--config`. Errors on missing file or non-object JSON with a clear stderr message and exits 2.
2. Resolves `statePath` from the config (see below).
3. Resolves `envFiles`/`envFile` from the config; loads `KEY=VALUE` lines and falls back to the process env. `--env-file` flags override the config (later wins over earlier).
4. Refuses to run if required env (e.g. `NOTION_TOKEN`) is missing — clear stderr message, exit 2.

### Path resolution

- `statePath` is a **directory**. The script writes the state JSON file (e.g. `rss_monitor.json`) inside it.
- Relative paths in the config resolve against **the config file's directory**, not the current working directory.
- `~` is always expanded.
- A `dbPath` (or analogous) override may be absolute or relative-to-`statePath`.

    state_dir = Path(config["statePath"]).expanduser()
    if not state_dir.is_absolute():
        state_dir = (config_path.resolve().parent / state_dir).resolve()

### State JSON

Every script that touches external systems writes a state JSON on every run. Use these standardized keys (omit ones that don't apply, don't invent synonyms):

    {
      "system":           "RSS Monitor",            // human-readable name of this skill
      "lastRun":          "2026-05-12T14:30:00+00:00",
      "lastSuccess":      "2026-05-12T14:30:00+00:00",  // updated only on full success
      "previousSuccess":  "2026-05-12T13:00:00+00:00",  // snapshot of prior lastSuccess
      "lastError":        "",                       // empty on success; first error message on failure
      "status":           "Healthy",                // "Healthy" or "Attention"
      "materialChange":   true,                     // did this run produce new/changed data?
      "notes":            "Updated 12 feed(s); 3 new entries."
      // plus skill-specific counters: feedCount, errorFeedCount, newEntryCount, etc.
    }

`previousSuccess` enables "since last run" queries — snapshot the prior `lastSuccess` onto `previousSuccess` only when the current run succeeds.

Write atomically-ish: `path.parent.mkdir(parents=True, exist_ok=True)` then `path.write_text(json.dumps(state, indent=2, sort_keys=True))`.

Timestamps: ISO-8601 with timezone. `datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")`.

### Spinner / progress (halo)

Progress goes to **stderr**. Stdout stays clean for JSON output that may be piped.

Two patterns are in use; pick one:

**Minimal (rss_monitor style):**

    from halo import Halo

    class _NullSpinner:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start(self, *a, **kw): return self
        def stop(self): pass

    def _spinner(text: str, *, enabled: bool):
        if not enabled or not sys.stderr.isatty():
            return _NullSpinner()
        return Halo(text=text, spinner="dots", stream=sys.stderr)

**Full (phantombuster style) — context manager that also logs `done/failed in Xs` when stderr is not a TTY:**

    class Spinner:
        def __init__(self, message): ...
        def __enter__(self):
            self._start = time.monotonic()
            if sys.stderr.isatty():
                self._halo = Halo(text=self.message, spinner='dots', stream=sys.stderr)
                self._halo.start()
            else:
                print(f'... {self.message}', file=sys.stderr, flush=True)
            return self
        def __exit__(self, exc_type, ...):
            elapsed = time.monotonic() - self._start
            if self._halo:
                marker = self._halo.succeed if exc_type is None else self._halo.fail
                marker(f'{self.message}: done in {elapsed:.1f}s')
            else:
                print(f'    {self.message}: done in {elapsed:.1f}s', file=sys.stderr)

Either way: animate only when `sys.stderr.isatty()`. Suppress in `--json` mode if the script's stdout result is paired with the spinner output.

### Output discipline

- **stdout**: machine-readable JSON (in `--json` mode) or human-readable text/markdown (default). Nothing else.
- **stderr**: progress, spinner output, warnings, error messages. Everything not part of the result payload.
- On failure with `--json`, emit the JSON state to stderr (not stdout) and exit non-zero — so consumers see a clean stdout=empty + non-zero exit, with diagnostics on stderr.

### Security guards

- **SSRF**: for any user-supplied URL, resolve the host and reject private/loopback/link-local/multicast addresses before fetching. See `check_url` in `rss_monitor.py`.
- **Re-validate redirects** when the HTTP client follows them — only the initial URL is checked by default.
- **Refuse unknown schemes** (`http`/`https` only).

## runner.sh

A thin zsh wrapper that runs the script against a co-located `config.json`. Always:

    #!/bin/zsh
    set -euo pipefail
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    uv run "$SCRIPT_DIR/<script>.py" --config "$SCRIPT_DIR/config.json" "${@:-<default-subcommand>}"

`"${@:-<default>}"` lets the user pass through subcommands and flags while defaulting to the common case. For multi-step pipelines (`collect.py` then `sync.py`), accept a step argument:

    step="${1:-all}"
    case "$step" in
      collect) uv run "$SCRIPT_DIR/collect.py" --config "$CONFIG" ;;
      sync)    uv run "$SCRIPT_DIR/sync.py"    --config "$CONFIG" ;;
      all)
        uv run "$SCRIPT_DIR/collect.py" --config "$CONFIG"
        uv run "$SCRIPT_DIR/sync.py"    --config "$CONFIG"
        ;;
      -h|--help) echo "usage: $(basename "$0") [collect|sync|all]" >&2 ;;
      *) echo "unknown step: $step" >&2; exit 2 ;;
    esac

The runner never owns logic — it just routes to scripts and passes a config path. Step coordination (e.g. `sync.py` finding the right archive dir) goes through the state file, not the runner.

## Idempotency and authoritative config

When the config defines a set (feeds, columns, properties), make the script **authoritative**: items present in storage but absent from the config are removed on the next run. The only way to manage the set is by editing the config. This mirrors `rss_monitor`'s feed/tag sync and avoids hidden state.

Re-running with the same config and no upstream changes should be a no-op (or near-it). Deduplication keys live in the config (`dedupBy`, `notionImport.dedupBy`), not in the script.

## Checklist for a new skill

- [ ] `skills/<name>/SKILL.md` with `Use when…` description.
- [ ] Single-file Python entrypoint with PEP 723 metadata; `halo` for spinner; stdlib for HTTP where possible.
- [ ] `--config` flag; `--print-example-config` (or `example-config` subcommand).
- [ ] `statePath` resolves relative to the config file, `~`-expanded.
- [ ] State JSON uses the standard keys (`system`, `lastRun`, `lastSuccess`, `previousSuccess`, `lastError`, `status`, `materialChange`, `notes`) plus skill-specific counters.
- [ ] Spinner on stderr, TTY-aware, falls back to plain log lines when piped.
- [ ] stdout reserved for machine-readable output; `--json` for explicit JSON mode.
- [ ] SSRF guard on any user-supplied URL.
- [ ] `runner.sh` (zsh, `set -euo pipefail`, `SCRIPT_DIR`-relative).
- [ ] Exit `0` on success, `2` on failure.
- [ ] No `chmod` — leave file permissions alone.
