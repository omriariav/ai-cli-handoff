# AI CLI Handoff

`ai-handoff` is an AI agent handoff CLI for carrying project context, guidance, and tooling between coding assistants.

The first supported flow is `Claude Code -> Codex`: when Claude Code context exists, Claude rate limits are gone, and you want Codex to continue from the same project reality instead of starting cold.

For that flow, the CLI reads the target folder, selected Claude Code conversations, `CLAUDE.md`, local Claude setup, and discovered MCP/skill/plugin usage. It then creates Codex-ready handoff artifacts such as `AGENTS.md` and `.codex/handoff/manifest.json`. Optional Codex user-level tooling carryover is reviewed separately and never runs by accident.

Current version: `0.3.0`

## Quick Start

From this repository:

```bash
bin/ai-handoff /path/to/project
```

Installed as a Python package:

```bash
ai-handoff /path/to/project
```

Example:

```bash
bin/ai-handoff /Users/omri.a/Code/speech-to-text-tools
```

The default command opens a one-time wizard. It does not write files or install Codex-wide tooling until the final review step.

## What It Does

`ai-handoff` is built around handoff flows. In `0.3.0`, the implemented flow is `Claude Code -> Codex`.

That flow prepares a project for Codex by:

- Selecting recent Claude Code conversations, defaulting to the latest three relevant sessions.
- Letting you choose more conversations with a static checkbox picker.
- Summarizing recent Claude work into Codex-readable context.
- Carrying `CLAUDE.md` guidance into a managed `AGENTS.md` section, including a first-run instruction for Codex to distill durable project rules.
- Listing the selected Claude transcript JSONL files in `AGENTS.md` so Codex can read the exact chosen conversations when it needs deeper context.
- Writing `.codex/handoff/summary.md`, `.codex/handoff/manifest.json`, and run snapshots.
- Detecting MCPs, user-level and project-local Claude skills, Claude plugins, hooks, rules, references, and statusline settings.
- Scanning selected transcripts for tooling that was actually used, not just installed somewhere.
- Checking whether used Claude plugins already exist in Codex.
- Checking GitHub origins for native Codex plugin metadata when `gh` is available and authenticated.
- Proposing safe carryover paths: flag native Codex metadata for review, bridge from source, bridge from Claude cache, or report why manual work is required.

## The Wizard

The interactive flow is optimized for a one-time handoff.

### Step 0: Handoff Direction

Choose the flow:

- `Claude Code -> Codex`: supported now.
- `Codex -> Claude Code`: planned for later.

Every step shows the current project folder so it is clear which repo is being prepared.

### Step 1/4: Claude Context

The wizard starts with the latest three relevant Claude Code conversations. You can continue, skip context, or choose more conversations.

The conversation picker supports:

- `/`: filter conversations.
- `f` / `b`: page forward and back.
- `d`: details.
- Space or row numbers: toggle visible rows.
- Enter: commit the draft selection.
- `q`: cancel without changing the previous selection.

If you select 10 of 10 conversations, the wizard shows the selected set instead of hiding it behind the original defaults.

### Step 2/4: Project Files

The wizard shows the project-local files that can be written:

- `AGENTS.md`
- `.codex/handoff/summary.md`
- `.codex/handoff/manifest.json`
- `.codex/handoff/runs/<run-id>.json`

This step queues the project files only. It does not write them yet. You can preview the diff, include them in the final plan, skip them, or quit.

### Step 3/4: Tooling & Claude Setup Carryover

The wizard scans selected transcripts and leads with tooling that was actually used in those conversations. That keeps the main path relevant to the current project rather than dumping every global Claude plugin and skill on the machine.

It also captures Claude setup that Codex should know about:

- Hooks: captured for review only because they can run commands.
- Rules: recorded and summarized for Codex.
- References: paths are recorded; external references are not copied automatically.
- Statusline: captured only because Codex does not currently use Claude statusline rendering.

Conversation-detected tooling appears in a checkbox picker. By default, detected tools are selected unless they are already available in Codex.

After choosing tools, you choose the scope:

- Project-only: record selected carryover in the handoff artifacts. No `~/.codex` changes.
- Install for this Codex user: queue user-level Codex installs under `~/.codex`.

Step 3 still only queues intent. It does not write project files and does not install anything.

### Step 4/4: Review & Run Plan

The final screen summarizes the whole plan:

- Project folder.
- Handoff flow.
- Selected conversation count.
- Project files included or skipped.
- Selected tooling and scope.
- Already-available Codex tooling.
- Captured Claude setup.
- Artifact paths.

You can preview the final diff, run, or quit.

If the plan includes Codex user-level installs, Enter is not enough. You must type `install` because those actions can change `~/.codex` and affect every Codex project for the OS user.

## Safety Model

`ai-handoff` separates project-local writes from Codex user-level changes.

Project-local writes:

- Stay inside the target project.
- Update only the managed `ai-handoff` section in `AGENTS.md`.
- Write handoff artifacts under `.codex/handoff/`.
- Are safe to inspect with `diff` before applying.

Codex user-level changes:

- Write under `~/.codex` or run `codex mcp add` / `codex plugin add`.
- Affect every Codex project for that OS user.
- Are never executed by the default scan.
- Are never authorized by `--yes` alone.
- Require explicit selection and final confirmation.
- Require typing `install` in the wizard when user-level installs are queued.

Privacy handling:

- Claude prompts, summaries, commands, local paths, MCP names, skill names, plugin names, and nearby Claude project keys may be written to the manifest.
- Secrets are best-effort redacted.
- Non-interactive apply requires `--ack-privacy` when private Claude-derived context or local inventory may be persisted.
- Use `privacy` before applying if you want a clear list of categories that may be written.

```bash
bin/ai-handoff privacy /path/to/project
bin/ai-handoff diff /path/to/project --include-manifest
```

## Tooling Carryover

Tooling carryover is deliberately conservative.

### MCPs

MCP candidates come from Claude configuration. Selected MCP imports run the corresponding `codex mcp add ...` command only after explicit user-level install approval. Commands containing redacted secrets are skipped and reported as manual follow-up.

### Skills

Claude skills can be copied into `~/.codex/skills` when selected. Existing Codex skill destinations are skipped rather than overwritten.

In `0.3.0`, skill discovery includes user-level `~/.claude/skills`, project-local `PROJECT/.claude/skills`, and project-local `PROJECT/.agents/skills`. Symlinked skill folders are resolved before copying so Codex receives the real skill content. If Claude settings record `npx skills add`, `npx skills install`, or `npx skills update` for a discovered skill, the candidate is labeled with `npx-source` and the command is kept as manual origin evidence; `ai-handoff` does not run `npx` automatically.

### Plugins

Claude plugin records are not assumed to be Codex plugins. In `0.2.0`, `ai-handoff` uses this order:

1. Resolve the formal Claude source first: a local marketplace/source repo, a cloned GitHub marketplace, or a remote GitHub source URL.
2. Detect native Codex plugin metadata when the source exposes `.codex-plugin/plugin.json`, and surface that evidence for review.
3. Bridge from the authoritative source when `.claude-plugin/plugin.json` is available locally.
4. For remote GitHub sources, materialize the GitHub repo at the Claude-used ref during the approved install step.
5. Use the installed Claude cache only as a labeled fallback, with the fallback reason and stale-cache evidence recorded in the manifest.
6. If neither source nor cache exists, report a manual action with the reason.

For bridged plugins, `ai-handoff` writes `~/.codex/plugins/cc-<name>`, strips Claude-only runtime metadata, converts Claude commands into Codex-visible skills, converts Claude agents into Codex TOML agents, adds bridge metadata, updates the local marketplace registry, and then runs `codex plugin add cc-<name>@cc-bridged-plugins` only after explicit approval.

GitHub origin checks are on by default for the wizard and `globals` listing. They require `gh` to be installed and authenticated. If `gh` is missing, unauthenticated, or the API check fails, the CLI says so and keeps the bridge/manual fallback visible.

```bash
bin/ai-handoff globals /path/to/project
bin/ai-handoff globals /path/to/project --no-check-github
```

## Commands

### Interactive

```bash
bin/ai-handoff /path/to/project
```

### Scan And Inspect

```bash
bin/ai-handoff scan /path/to/project
bin/ai-handoff scan /path/to/project --json
bin/ai-handoff diff /path/to/project
bin/ai-handoff diff /path/to/project --include-manifest
bin/ai-handoff privacy /path/to/project
bin/ai-handoff doctor /path/to/project
```

### Conversations

```bash
bin/ai-handoff conversations /path/to/project
bin/ai-handoff conversations /path/to/project --all-projects --search TEXT
bin/ai-handoff scan /path/to/project --sessions session-1,session-7
bin/ai-handoff scan /path/to/project --from-claude-project -Users-you-Code-old-project
```

Useful filters:

- `--last N`: select the latest N sessions.
- `--since 7d`: select sessions newer than a duration.
- `--sessions id1,id2`: select exact sessions.
- `--all-projects`: search across all Claude project folders.
- `--from-claude-project KEY`: read from a specific Claude project key.
- `--search TEXT`: filter by title, prompt, path, branch, or project key.
- `--branch NAME`: filter by git branch.
- `--include-transcripts`: include fuller redacted transcript excerpts.

### Project-Local Apply

```bash
bin/ai-handoff apply /path/to/project --yes --ack-privacy
```

This writes project-local handoff files only. It does not install MCPs, skills, or plugins into `~/.codex`.

### Codex User-Level Tooling

```bash
bin/ai-handoff globals /path/to/project
bin/ai-handoff globals /path/to/project --project-only
bin/ai-handoff globals /path/to/project --portable-only
bin/ai-handoff globals /path/to/project --include-risky
bin/ai-handoff globals select /path/to/project --select skill:name,mcp:name --yes --ack-privacy
bin/ai-handoff globals apply /path/to/project
```

`globals select` records intent in `.codex/handoff/manifest.json`. It does not write `AGENTS.md` and does not install anything. `globals apply` executes previously selected user-level actions after confirmation.

### History

```bash
bin/ai-handoff history /path/to/project
bin/ai-handoff show <run-id> --path /path/to/project
```

## Generated Files

`AGENTS.md`

- Codex reads this automatically as project instructions.
- `ai-handoff` updates only the managed section between markers.
- The managed section includes a first-run Codex distillation prompt plus a best-effort-redacted `CLAUDE.md` source snapshot.
- The first ask tells Codex to read selected conversations, captured Claude setup, and the `CLAUDE.md` snapshot, then edit durable project context outside the managed markers.
- The managed section lists selected Claude transcript JSONL paths so Codex can open the chosen transcripts directly when summaries are not enough.
- That distillation should merge rules from `CLAUDE.md` and Claude permission settings while dropping stale, duplicated, or one-off handoff details.
- Keep durable project rules outside the managed section.

`.codex/handoff/summary.md`

- Human-readable handoff summary.
- Includes selected conversations, recent work, commands detected, and captured setup notes.

`.codex/handoff/manifest.json`

- Machine-readable audit trail.
- Includes selected session IDs, transcript usage summaries, global tooling candidates, selected carryover, diagnostics, and privacy metadata.

`.codex/handoff/runs/<run-id>.json`

- Immutable run snapshot for history and debugging.

## Installation

For local development, no third-party runtime dependencies are required.

```bash
git clone https://github.com/omriariav/ai-cli-handoff.git
cd ai-cli-handoff
bin/ai-handoff --version
```

Install as a Python package:

```bash
python3 -m pip install .
ai-handoff --version
```

Install the bundled Codex skill manually:

```bash
mkdir -p ~/.codex/skills/ai-handoff
cp -R skills/ai-handoff/* ~/.codex/skills/ai-handoff/
```

## Development

Run the test suite:

```bash
python3 -m unittest discover -s tests
```

Compile-check the bundled script and package implementation:

```bash
env PYTHONPYCACHEPREFIX=/private/tmp/ai-handoff-pyc \
  python3 -m py_compile \
  skills/ai-handoff/scripts/ai_handoff.py \
  src/ai_handoff/handoff_impl.py \
  tests/test_ai_handoff.py
```

The implementation is intentionally duplicated in two places:

- `skills/ai-handoff/scripts/ai_handoff.py`
- `src/ai_handoff/handoff_impl.py`

Keep them in sync when editing. The package entry point imports `src/ai_handoff/handoff_impl.py`; the Codex skill and `bin/ai-handoff` use the bundled skill script.

Validate the installed skill when updating skill docs or scripts:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py ~/.codex/skills/ai-handoff
```

## Release Checklist

Version `0.3.0` is declared in:

- `pyproject.toml`
- `src/ai_handoff/__init__.py`
- `README.md`
- `README.html`
- the CLI `--version` output

Before tagging:

```bash
python3 -m unittest discover -s tests
pandoc README.md -o README.html --metadata title='AI CLI Handoff'
git status --short
git tag -a v0.3.0 -m "ai-handoff 0.3.0"
git push origin v0.3.0
```

Recommended order: merge the PR first, then tag the merge commit on `main`.

## Status

`0.3.0` adds project-local skill discovery for `PROJECT/.claude/skills` and `PROJECT/.agents/skills`, resolves symlinked skill folders before Codex carryover, and records `npx skills` installer commands as origin evidence. `0.2.0` improved source preference for Claude tooling carryover by preferring formal plugin and skill sources over installed Claude cache copies, recording evidence for the chosen source, and labeling cache fallback clearly. `Claude Code -> Codex` remains the first supported flow; the reverse `Codex -> Claude Code` flow is intentionally visible in the wizard but not implemented yet.
