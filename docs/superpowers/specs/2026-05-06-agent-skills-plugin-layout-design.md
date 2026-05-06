# Agent Skills Plugin Layout — Design

## Summary

Restructure the `agent-skills` repo as an installable Claude Code plugin. The two existing scripts (`web_fetch.py`, `rss_monitor.py`) become Claude Code skills under `skills/<name>/`, each paired with a `SKILL.md` that tells Claude when to invoke it and how to run the bundled script.

## Goals

- Repo root reads as a plugin, not a loose collection of scripts.
- Each skill is self-contained: `SKILL.md` + script in the same directory.
- Plugin is installable via `claude plugin install` from a clone of the repo.
- New skills can be added by dropping a new directory under `skills/`.

## Non-goals

- No marketplace listing (no remote yet; personal use).
- No skill template, `CONTRIBUTING.md`, or tests.
- No changes to script behaviour. Security fixes already landed in commits `852f960`, `14b40b8`, `c220284`, `25a307f`.

## Layout

```
agent-skills/
├── .claude-plugin/
│   └── plugin.json
├── skills/
│   ├── web-fetch/
│   │   ├── SKILL.md
│   │   └── web_fetch.py
│   └── rss-monitor/
│       ├── SKILL.md
│       └── rss_monitor.py
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-05-06-agent-skills-plugin-layout-design.md   # this file
├── README.md
├── .gitignore
└── .claude/                                                       # untouched
```

## SKILL.md format

Each `SKILL.md` is a markdown file with YAML frontmatter:

- `name` — kebab-case, must match the directory name.
- `description` — the trigger sentence Claude reads to decide when to invoke. Lead with "Use when…" and name the user-intent, not the implementation.

The body contains a short invocation recipe using `${CLAUDE_PLUGIN_ROOT}` to locate the bundled script. The scripts use PEP 723 inline metadata, so `uv run` handles dependencies — no extra install step.

### web-fetch description (draft)

> Use when the user wants to fetch and read a webpage's content. Output is markdown by default with optional JSON. Includes a 24h cache and SPA fallback via Playwright. Refuses non-public addresses (SSRF guard).

### rss-monitor description (draft)

> Use when the user wants to manage RSS/Atom subscriptions, fetch new entries from feeds, search across stored entries, or triage entries (mark read/important, tag).

## Plugin manifest

Minimal `.claude-plugin/plugin.json`:

```json
{
  "name": "agent-skills",
  "version": "0.1.0",
  "description": "Personal agent skills for fetching web pages and monitoring RSS feeds.",
  "author": { "name": "Samir" }
}
```

Exact field set will be verified against the current Claude Code plugin docs at implementation time.

## README

Repo-level `README.md` covers:

- One-paragraph overview of what the plugin is.
- Install instructions (`git clone` + `claude plugin install <path>`, or symlink into `~/.claude/plugins/`).
- One-line summary of each skill.
- Note that scripts run via `uv` and have no separate install step.

## Migration

Three commits:

1. **File moves.**
   - `git mv web_fetch.py skills/web-fetch/web_fetch.py`
   - `git mv rss_monitor.py skills/rss-monitor/rss_monitor.py`
2. **Skill content.** Create `skills/web-fetch/SKILL.md` and `skills/rss-monitor/SKILL.md`.
3. **Plugin manifest + README.** Create `.claude-plugin/plugin.json` and `README.md`.

After commits, verify locally: install the plugin from this clone and confirm Claude Code surfaces both skills, then run each one end-to-end. Not a commit on its own.

## Risks / open items

- **`plugin.json` schema** — not yet verified against current Claude Code docs. Will fetch the docs page before writing the manifest.
- **`${CLAUDE_PLUGIN_ROOT}` assumption** — the SKILL.md invocation recipe relies on this env var resolving to the plugin root. If it doesn't, fall back to a path relative to the SKILL.md location.
- **Local install verification** — before declaring done, install the plugin from this clone and run each skill end-to-end in a fresh Claude Code session.
