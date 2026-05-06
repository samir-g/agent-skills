# agent-skills Plugin Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the `agent-skills` repo into an installable Claude Code plugin with `web-fetch` and `rss-monitor` as the first two skills.

**Architecture:** Repo root becomes a Claude Code plugin (`.claude-plugin/plugin.json`). The two existing standalone scripts move into `skills/<name>/` directories, each paired with a `SKILL.md` describing the trigger and how to invoke the script. Scripts run via `uv` (PEP 723 inline metadata), so no separate dependency install step.

**Tech Stack:** Markdown + YAML frontmatter (SKILL.md), JSON (plugin manifest), Python via `uv run`. Reference design: `docs/superpowers/specs/2026-05-06-agent-skills-plugin-layout-design.md`.

---

## File Structure

Files to create:
- `skills/web-fetch/SKILL.md` — frontmatter + invocation recipe for the web-fetch skill.
- `skills/rss-monitor/SKILL.md` — frontmatter + invocation recipe for the rss-monitor skill.
- `.claude-plugin/plugin.json` — plugin manifest (schema verified at Task 1).
- `README.md` — repo overview, install instructions, skill list.

Files to move:
- `web_fetch.py` → `skills/web-fetch/web_fetch.py`
- `rss_monitor.py` → `skills/rss-monitor/rss_monitor.py`

Files untouched:
- `.gitignore`, `.claude/`, `docs/superpowers/specs/...`

---

## Task 1: Verify the plugin manifest schema

**Files:**
- Read-only: external Claude Code docs

The spec called out `plugin.json` schema as a risk. Resolve it before writing the manifest.

- [ ] **Step 1: Find and fetch the Claude Code plugin docs**

Use WebSearch first if you don't already know the canonical doc URL — search for "Claude Code plugin manifest plugin.json schema". Then WebFetch the page that documents the plugin manifest format.

Prompt the fetcher to extract: required fields in `plugin.json`, optional fields, expected directory layout for plugins (especially the path to skills), and how `${CLAUDE_PLUGIN_ROOT}` resolves in skill bodies.

- [ ] **Step 2: Record findings inline in the plan**

Append a short note under this task summarising:
- Required fields (e.g. `name`).
- Recommended optional fields (`version`, `description`, `author`).
- Skill discovery path the plugin loader expects (this plan assumes `skills/<name>/SKILL.md`; confirm or correct).
- Whether `${CLAUDE_PLUGIN_ROOT}` is the documented variable, or if a different name is canonical.

If the docs disagree with the spec's assumptions, stop and surface the conflict before continuing — do not silently rewrite tasks. The user can decide whether to amend the spec.

- [ ] **Step 3: No commit**

This task is research only.

---

## Task 2: Move scripts into skill directories

**Files:**
- Move: `web_fetch.py` → `skills/web-fetch/web_fetch.py`
- Move: `rss_monitor.py` → `skills/rss-monitor/rss_monitor.py`

- [ ] **Step 1: Create the skill directories**

Run:

    mkdir -p skills/web-fetch skills/rss-monitor

Expected: no output, both directories exist.

- [ ] **Step 2: Move web_fetch.py with git mv (preserves history)**

Run:

    git mv web_fetch.py skills/web-fetch/web_fetch.py

Expected: no output. Verify with:

    git status

Output should show:
- `renamed: web_fetch.py -> skills/web-fetch/web_fetch.py`

- [ ] **Step 3: Move rss_monitor.py with git mv**

Run:

    git mv rss_monitor.py skills/rss-monitor/rss_monitor.py

Verify with `git status`. Output should now show both renames.

- [ ] **Step 4: Smoke-test that both scripts still run from their new paths**

Run:

    uv run skills/web-fetch/web_fetch.py --help
    uv run skills/rss-monitor/rss_monitor.py --help

Expected: each prints its argparse help text and exits 0. Nothing should fail because the scripts are self-contained (no relative imports). If `uv` isn't installed, this is a blocker — surface to user.

- [ ] **Step 5: Commit**

Run:

    git add -A
    git commit -m "$(cat <<'EOF'
    refactor: move scripts under skills/<name>/

    Prepares the repo for Claude Code plugin packaging. No script behaviour
    changes — git mv preserves history.

    Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
    EOF
    )"

Expected: commit succeeds, two files renamed.

---

## Task 3: Write SKILL.md for web-fetch

**Files:**
- Create: `skills/web-fetch/SKILL.md`

- [ ] **Step 1: Create the file with the content below**

Path: `skills/web-fetch/SKILL.md`

```markdown
---
name: web-fetch
description: Use when the user wants to fetch and read a webpage's content as markdown. Includes a 24h cache, optional JSON output, an SPA fallback via Playwright, and an SSRF guard that refuses non-public addresses.
---

# Web fetch

Run the bundled script with `uv run` so dependencies install on first use:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/web-fetch/web_fetch.py" <url>

## Common flags

- `--json` / `-j` — emit a JSON object instead of markdown.
- `--no-cache` — bypass the 24h cache.
- `--render` — force Playwright rendering (skip the SPA heuristic).
- `--timeout N` — request timeout in seconds (default 30).

## Behaviour

- Default output: markdown with title, status, and the page's main content.
- Cache lives at `~/.cache/web_fetch/` and expires after 24 hours.
- Responses larger than 10 MiB are rejected.
- URLs that resolve to private/internal addresses (loopback, RFC1918, link-local, cloud metadata) are refused — including across redirects.
```

- [ ] **Step 2: Sanity-check the frontmatter**

Run:

    head -n 5 skills/web-fetch/SKILL.md

Expected: a YAML block delimited by `---`, with `name: web-fetch` on its own line and the `description:` field on a single line (no line break inside the description).

If `${CLAUDE_PLUGIN_ROOT}` was not the canonical variable per Task 1, replace it across this file before continuing.

- [ ] **Step 3: No commit yet — Task 4 also adds a SKILL.md, then both commit together in Task 5.**

---

## Task 4: Write SKILL.md for rss-monitor

**Files:**
- Create: `skills/rss-monitor/SKILL.md`

- [ ] **Step 1: Create the file with the content below**

Path: `skills/rss-monitor/SKILL.md`

```markdown
---
name: rss-monitor
description: Use when the user wants to manage RSS/Atom subscriptions — adding/removing feeds, fetching new entries, searching across stored entries, or triaging entries (mark read/important, tag).
---

# RSS monitor

Run the bundled script with `uv run`:

    uv run "${CLAUDE_PLUGIN_ROOT}/skills/rss-monitor/rss_monitor.py" <subcommand> [args...]

Add `--json` to any subcommand for machine-readable output. Use `--db PATH` to override the default reader database location.

## Feed management

- `feed add <url> [--tag T ...]` — add a feed (rejects private/internal URLs).
- `feed list [--tag T] [--errors-only]` — list feeds.
- `feed remove <url>` — remove a feed.
- `feed tag <url> <tags...>` / `feed untag <url> <tags...>` — manage feed tags.

## Updating

- `update [--feed URL] [--tag T]` — fetch new entries.
- `poll [--interval SEC] [--iterations N]` — run update repeatedly.

## Reading

- `new [--tag T] [--feed URL] [--limit N] [--since 24h|3d|1w]` — list unread entries.
- `list [--read|--unread] [--important] [--tag T] [--feed URL] [--limit N] [--since ...]`
- `show <hash>` — full entry content.
- `search <query> [--tag T]` — full-text search across entries.

## Mutation

Hashes are the 16-char prefix shown by `new`, `list`, and `search`.

- `read <hash...>` / `unread <hash...>`
- `important <hash...>` / `unimportant <hash...>`
- `tag <hash> <tags...>` / `untag <hash> <tags...>`

## Bulk triage

`triage` reads decisions from stdin as JSON:

    [
      {"hash": "abc...", "read": true, "important": false,
       "tags_add": ["ai"], "tags_remove": []}
    ]

## Behaviour

- Uses the [`reader`](https://reader.readthedocs.io/) library for storage and feed parsing.
- SQLite database at reader's default location unless `--db` is set.
- Refuses feed URLs that resolve to private/internal addresses (SSRF guard).
- Entry hashes are 16 hex chars (64 bits) of SHA-256 over `feed_url + entry_id`.
```

- [ ] **Step 2: Sanity-check the frontmatter**

Run:

    head -n 5 skills/rss-monitor/SKILL.md

Expected: YAML block delimited by `---`, `name: rss-monitor`, single-line `description:`.

If `${CLAUDE_PLUGIN_ROOT}` was not canonical per Task 1, replace it before continuing.

---

## Task 5: Commit both SKILL.md files

**Files:**
- Add: `skills/web-fetch/SKILL.md`, `skills/rss-monitor/SKILL.md`

- [ ] **Step 1: Stage the two new files**

Run:

    git add skills/web-fetch/SKILL.md skills/rss-monitor/SKILL.md

- [ ] **Step 2: Commit**

Run:

    git commit -m "$(cat <<'EOF'
    feat: add SKILL.md for web-fetch and rss-monitor

    Each skill describes when Claude should invoke it and the canonical
    uv-run recipe. Frontmatter follows Claude Code's SKILL.md format.

    Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
    EOF
    )"

Expected: commit succeeds, two files added.

---

## Task 6: Write the plugin manifest

**Files:**
- Create: `.claude-plugin/plugin.json`

- [ ] **Step 1: Create the directory**

Run:

    mkdir -p .claude-plugin

- [ ] **Step 2: Write `.claude-plugin/plugin.json`**

Adjust the field set to match what Task 1 confirmed. The default content (use this if Task 1 confirmed these fields are correct):

```json
{
  "name": "agent-skills",
  "version": "0.1.0",
  "description": "Personal agent skills for fetching web pages and monitoring RSS feeds.",
  "author": { "name": "Samir" }
}
```

If Task 1 surfaced additional required fields (e.g. `homepage`, `license`, `repository`), add them. If `author` requires an email, ask the user — do not invent one.

- [ ] **Step 3: Validate the JSON**

Run:

    python3 -m json.tool .claude-plugin/plugin.json

Expected: pretty-printed JSON, exit 0. If it errors, fix syntax before continuing.

- [ ] **Step 4: No commit yet — Task 7 also adds the README, then both commit together in Task 8.**

---

## Task 7: Write the README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create the file with the content below**

Path: `README.md`

```markdown
# agent-skills

A Claude Code plugin bundling personal agent skills.

## Skills

- **web-fetch** — fetch and analyse a webpage's content (HTML → markdown, with a 24h cache, optional JSON, an SPA fallback via Playwright, and an SSRF guard).
- **rss-monitor** — manage RSS/Atom subscriptions and triage entries from the CLI.

## Install

Clone the repo and install as a Claude Code plugin:

    git clone <repo-url> agent-skills
    claude plugin install ./agent-skills

Or symlink into `~/.claude/plugins/`:

    ln -s "$(pwd)/agent-skills" ~/.claude/plugins/agent-skills

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — the scripts use PEP 723 inline metadata, so `uv run` resolves dependencies on first use. No separate install step.
- `web-fetch` optionally uses Playwright for SPA rendering; `uv` installs it on demand the first time `--render` is used or the SPA heuristic triggers.

## Layout

    skills/
      web-fetch/    SKILL.md + web_fetch.py
      rss-monitor/  SKILL.md + rss_monitor.py
    .claude-plugin/
      plugin.json
    docs/
      superpowers/  design specs and plans

## Adding a new skill

Drop a new directory under `skills/<name>/` containing a `SKILL.md` (with `name` and `description` in frontmatter) and any scripts the skill invokes.
```

- [ ] **Step 2: Spot-check that the markdown renders cleanly**

Run:

    head -n 30 README.md

Expected: heading, intro paragraph, skill list visible. No stray template tokens.

If the install command syntax (`claude plugin install ...`) doesn't match what Task 1 found in the docs, correct it before committing.

---

## Task 8: Commit manifest + README

**Files:**
- Add: `.claude-plugin/plugin.json`, `README.md`

- [ ] **Step 1: Stage**

Run:

    git add .claude-plugin/plugin.json README.md

- [ ] **Step 2: Commit**

Run:

    git commit -m "$(cat <<'EOF'
    feat: add plugin manifest and README

    Repo is now installable as a Claude Code plugin. README documents the
    skill set and install path.

    Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
    EOF
    )"

Expected: commit succeeds, two files added.

---

## Task 9: Verify the plugin loads end-to-end

**Files:**
- No file changes; runtime verification only.

- [ ] **Step 1: Install the plugin locally**

From the repo root:

    claude plugin install .

Expected: success message naming `agent-skills`. If the command differs per Task 1, use the documented form.

If install fails, surface the exact error to the user before proceeding — do not edit files speculatively.

- [ ] **Step 2: Verify the install on disk**

Run:

    ls -la ~/.claude/plugins/ | grep -i agent-skills
    find ~/.claude/plugins -maxdepth 4 -name SKILL.md 2>/dev/null | grep agent-skills

Expected: the plugin directory (or symlink) is present, and both SKILL.md files are reachable. Exact path will depend on what the install command did — adjust the `find` command if Task 1 documented a different layout.

- [ ] **Step 3: Smoke-test both scripts directly through the recipe**

Run the same command Claude would run via the SKILL.md (substituting the actual plugin root):

    PLUGIN_ROOT="$(find ~/.claude/plugins -maxdepth 4 -type d -name agent-skills | head -1)"
    uv run "$PLUGIN_ROOT/skills/web-fetch/web_fetch.py" https://example.com | head -20
    uv run "$PLUGIN_ROOT/skills/rss-monitor/rss_monitor.py" feed list

Expected: web-fetch prints the example.com markdown; rss-monitor prints `(no feeds)` (or the user's existing feed list). If `$PLUGIN_ROOT` resolves to the source clone (because of a symlink install), that's fine — the same scripts run.

- [ ] **Step 4: No commit**

This task is verification only. If anything fails, capture the failure and either patch in-plan (correcting the SKILL.md / manifest) or surface to the user.

---

## Notes

- **Commits expected:** three (Tasks 2, 5, 8). Tasks 1, 3, 4, 6, 7, 9 do not commit.
- **No script behaviour changes.** Security fixes already landed in commits `852f960`, `14b40b8`, `c220284`, `25a307f`. This plan is purely structural.
- **YAGNI checks in scope:** no skill template, no `CONTRIBUTING.md`, no tests, no marketplace listing.
