# agent-skills

A Claude Code plugin bundling personal agent skills.

## Skills

- **web-fetch** — fetch and analyse a webpage's content (HTML → markdown, with a 24h cache, optional JSON, an SPA fallback via Playwright, and an SSRF guard).
- **rss-monitor** — manage RSS/Atom subscriptions and triage entries from the CLI.

## Install

Clone the repo, then load it via Claude Code's `--plugin-dir` flag:

    git clone <repo-url> agent-skills
    claude --plugin-dir ./agent-skills

Skills appear as `/agent-skills:web-fetch` and `/agent-skills:rss-monitor`. After editing skill content, run `/reload-plugins` inside Claude Code.

For persistent loading without the per-session flag, convert this repo into a Claude Code marketplace (out of scope here) and add it via `/plugin marketplace add ./agent-skills` followed by `/plugin install`.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — the bundled scripts use PEP 723 inline metadata, so `uv run` resolves dependencies on first use. No separate install step.
- The `web-fetch` skill optionally uses Playwright for SPA rendering; `uv` installs it on demand the first time `--render` is used or the SPA heuristic triggers.

## Layout

    skills/
      web-fetch/    SKILL.md + web_fetch.py
      rss-monitor/  SKILL.md + rss_monitor.py
    .claude-plugin/
      plugin.json
    docs/
      superpowers/  design specs and implementation plans

## Adding a new skill

Drop a new directory under `skills/<name>/` containing a `SKILL.md` (with `name` and `description` in YAML frontmatter) and any scripts the skill invokes. The `description` is what Claude reads to decide when to invoke the skill — lead with "Use when…" and name the user-intent rather than the implementation.
