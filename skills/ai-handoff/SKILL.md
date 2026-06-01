---
name: ai-handoff
description: Prepare a Claude Code project for Codex handoff. Use when the user wants to move work from Claude Code to Codex after rate limits, asks for `$ai-handoff FOLDER`, wants Codex to read recent Claude conversations, convert CLAUDE.md guidance into AGENTS.md, inspect Claude MCPs/skills/plugins, run a dry-run/apply handoff workflow, or make a project ready for Codex continuity.
---

# AI Handoff

## Overview

Use this skill to prepare a project folder for Codex using local Claude Code context. The bundled CLI scans `CLAUDE.md`, recent Claude Code sessions, local Claude settings, MCP candidates, skills, plugins, and Codex readiness; the default human flow is a one-time wizard. Project-local handoff files are applied only after confirmation, and Codex-wide installs run only after a separate explicit confirmation.

## Quick Start

Before running the CLI, set a goal when the goal tool is available:

`Prepare <folder> for Codex handoff from Claude Code context.`

Run the bundled CLI from this skill:

```bash
python3 /path/to/ai-handoff/scripts/ai_handoff.py /path/to/project
```

When this repo is available, the wrapper is:

```bash
bin/ai-handoff /path/to/project
```

## Workflow

1. Resolve the target folder and verify it exists.
2. Run the wizard for normal handoff work. It first asks for the handoff flow. `Claude Code -> Codex` is supported now; `Codex -> Claude Code` is shown as unavailable for a later flow.
3. In the Claude context step, default to the latest three relevant Claude conversations, show the selected conversations, choose more conversations, or skip Claude context. Changing the conversation selection must refresh transcript usage and Codex-wide candidate relevance before the Codex-wide step. If the user selects more conversations, show the resulting selected set instead of only a count.
4. In the project files step, preview the diff or apply project-local writes:
   - `AGENTS.md`
   - `.codex/handoff/summary.md`
   - `.codex/handoff/manifest.json`
   - `.codex/handoff/runs/<run-id>.json`
5. In the tooling carryover step, print `Step 3/3` before slower checks, then show progress while scanning tooling and checking GitHub/native-Codex compatibility. Lead with actions that match tooling used in the selected conversations, explain bridge-vs-native decisions inline, and let the user pick matched tools by number. After selection, offer a simple scope/action choice: record in project handoff only, install for this Codex user, or quit. Installing for this Codex user writes under `~/.codex` and makes the tooling available to every Codex project for that user. The broader Claude setup remains available through `expand full setup`.
   - Capture Claude hooks, permission rules, references, and statusline settings in the manifest and project handoff. Hooks are review-only because they can run commands. Statusline is captured-only because Codex does not use Claude statusline rendering yet. References are recorded by path; external references are not copied automatically.
   - In the conversation picker, use `/` filter, `f`/`b` page, `d` details, Space or row numbers to toggle, Enter to commit, and `q` to cancel draft changes.
   - In the Codex-wide install picker, use `/` filter, `Tab` view, `f`/`b` page, `d` details, `A` safe visible bulk-select, `u` clear visible, `C` clear all, `i` invert visible, and `?` help.
   - If no conversations are found, try `conversations --all-projects --search TEXT` or use `--from-claude-project KEY` from `doctor`/nearby match output.
   - TTY screens should redraw in place while long checks run. Use color for headings/status when supported, but respect `NO_COLOR`.
6. Apply selected Codex-wide installs only after explicit confirmation. These can change `~/.codex` and affect every Codex project/folder on this machine:
   - MCP imports run selected `codex mcp add ...` commands.
   - Skill imports copy selected Claude skill folders into `~/.codex/skills`.
   - Plugin imports prefer directly installable native Codex support from the source repo. If no native Codex package exists, bridge from the source repo at the Claude-used ref; use the installed Claude cache only as an explicit fallback when source resolution fails.
   - Bridged plugins are written into `~/.codex/plugins/cc-<name>`, Claude-only mechanisms are stripped, Claude commands become Codex-visible skills, agents are converted to `~/.codex/agents/*.toml`, `~/.agents/plugins/marketplace.json` is updated, and `codex plugin add cc-<name>@cc-bridged-plugins` is run. Report a partial result if the bridge write succeeds but Codex install fails.
   - Plugin bridge candidates inspect Claude marketplace metadata, known marketplace origins, local git remotes, and cached plugin paths. Mark `codex-native` when a local `.codex-plugin/plugin.json` exists. `globals` and the wizard review step check GitHub by default through authenticated `gh`; only mark `github-origin` after `gh` is installed/authenticated and performs a GitHub API check. If `gh` is missing, unauthenticated, or the API check fails, print a clear GitHub check failure and keep the bridge/manual fallback.
   - Claude plugin records without source or cache are manual by default; clearly report why the plugin cannot be prepared.
   - Non-interactive selection can be recorded with `ai-handoff globals select <path> --select ... --yes --ack-privacy`; execution remains a separate confirmation step.
7. Mark the goal complete after the dry-run summary or apply artifacts have been produced. The completion screen should list selected conversation count, project files written, Codex-wide installs completed or recorded, and artifact paths to inspect.

## Commands

```bash
# Interactive wizard
ai-handoff /path/to/project

# Non-mutating scan
ai-handoff scan /path/to/project
ai-handoff diff /path/to/project
ai-handoff privacy /path/to/project

# Non-interactive exact conversation selection
ai-handoff conversations /path/to/project
ai-handoff conversations /path/to/project --all-projects --search TEXT
ai-handoff scan /path/to/project --sessions session-1,session-7
ai-handoff scan /path/to/project --from-claude-project -Users-you-Code-old-project

# Project-local apply; --yes never authorizes Codex-wide installs
ai-handoff apply /path/to/project --yes --ack-privacy

# Advanced Codex-wide review/apply
ai-handoff globals /path/to/project
ai-handoff globals /path/to/project --project-only
ai-handoff globals /path/to/project --portable-only
ai-handoff globals /path/to/project --include-risky
ai-handoff globals /path/to/project --no-check-github
ai-handoff globals select /path/to/project --select skill:amq-cli,mcp:filesystem --yes --ack-privacy
ai-handoff globals apply /path/to/project

# Codex project-learning pass only
ai-handoff init /path/to/project

# Audit and automation
ai-handoff history /path/to/project
ai-handoff show <run-id> --path /path/to/project
ai-handoff doctor /path/to/project
ai-handoff scan /path/to/project --json
```

Useful flags:

- `--last N`: select the latest N Claude sessions.
- `--since 7d`: limit Claude session selection by age.
- `--sessions id1,id2`: select exact Claude sessions.
- `--all-projects`: search across all Claude project folders when the project moved or was renamed.
- `--from-claude-project KEY`: read sessions from a specific Claude project key.
- `--search TEXT`: filter Claude sessions by title, prompt, path, branch, or project key.
- `--branch NAME`: filter Claude sessions by git branch.
- `--include-transcripts`: include fuller redacted transcript excerpts.
- `--include-manifest`: include manifest JSON files in `diff`.
- `--json`: emit machine-readable output.
- `--debug`: accepted for compatibility; diagnostics are included in manifests by default.
- `--apply-global`: compatibility flag for executing Codex-wide installs previously selected with `g`; prefer `ai-handoff globals apply`.
- `--ack-privacy`: acknowledge that apply may write Claude-derived context into handoff artifacts.

Codex-wide `--select` values accept exact candidate IDs, skill names, type aliases such as `skills` or `mcps`, and `all`. `globals select` writes only `.codex/handoff/manifest.json` plus a run snapshot; it must not copy skills, install plugins, run MCP commands, or rewrite `AGENTS.md`.

Codex-wide install filters:

- `--project-only`: show/select only candidates directly tied to the target project.
- `--portable-only`: show/select only candidates without obvious local-machine dependencies.
- `--include-risky`: include unverified plugins and risky Codex-wide bulk-selection candidates.
- `--include-low-confidence`: include low-confidence plugin/import candidates for review.
- `--check-github`: use authenticated `gh` (`gh auth status`, then `gh api`) to check for native `.codex-plugin/plugin.json` files on GitHub origins. This is the default for `globals`.
- `--no-check-github`: skip `gh` checks and keep bridge candidates based only on local Claude/Codex metadata.

Candidate JSON includes `source_scope`, `risk`, `risk_badges`, `why_relevant`, `evidence`, `relevance_score`, `portable`, and `blocked_reason`. Plugin bridge candidates also include `bridge`, `bridge_name`, `bridge_source_path`, `bridge_destination_path`, `bridge_skill_count`, `bridge_agent_count`, `origin_github_repo`, `origin_source_url`, `codex_release_status`, `codex_release_evidence`, and `codex_release_check_urls`. Bulk selectors such as `all`, `skills`, and `plugins` skip `secret`, `unverified`, and Codex-wide candidates unless `--include-risky` is present. The internal `global-scope` badge is shown to users as `codex-wide`.

Selected Claude transcripts are scanned for actual tooling usage. The CLI records observed `Skill` invocations, `mcp__server__tool` calls, skill metadata, and Claude plugin attribution in `claude.sessions.usage_summary`; matching carryover candidates get `used_in_selected_sessions`, `transcript_usage`, high relevance evidence, and a `used-in-transcripts` marker in human output. The wizard should summarize conversation-matched tooling directly and reserve the picker for `expand full setup`. The picker should default to `used` view when matching candidates exist; `all` remains available through `Tab`. Project artifacts should list every selected conversation, not only the first page/sample. Claude setup capture is recorded under `claude.config.setup_capture` with hooks, rules, references, statusline, and summary counts.

The TTY conversation and Codex-wide install pickers must remain static in-place and must not use an alternate screen. Picker state is draft-only until Enter; `q` must leave prior selections unchanged. Selection state is ID-based, so filtering and paging cannot toggle the wrong conversation or install.

## Safety Rules

- Dry-run first.
- Apply mode defaults to project-local writes only.
- Never copy Claude credentials, OAuth tokens, bearer tokens, or raw secrets.
- Use `ai-handoff privacy <path>` when the user asks what may be written before apply.
- Preserve existing `AGENTS.md`; update only the managed `ai-handoff` section unless the user explicitly asks for a replacement.
- Do not run generated `codex mcp add` or `codex plugin add` commands without explicit user approval and selected Codex-wide actions.
- Do not bridge Claude plugins without explicit user approval and selected Codex-wide actions.
- Do not treat `--yes` as permission to execute Codex-wide installs. It can only record project-local selection state.
- Do not execute candidates with `blocked_reason`, `secret`, or `unverified` risk badges; report them as manual follow-up.
- For OAuth MCPs, configure only the server command and tell the user to run `codex mcp login <name>`.
- If the target project has uncommitted user work, do not revert or overwrite unrelated changes.

## Resources

- `scripts/ai_handoff.py`: dependency-free CLI implementation.
- `references/cli-ux.md`: CLI interaction model and UX principles.

Read `references/cli-ux.md` before changing command names, prompts, default selections, or safety behavior.
