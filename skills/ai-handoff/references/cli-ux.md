# AI Handoff CLI UX

## Principles

- Default to a one-time wizard for human use.
- Make project-local writes easy and Codex-wide installs deliberate.
- Prefer short, memorable commands over hidden flags.
- Keep status language concrete: found, missing, selected, discovered, written.
- Preserve auditability with `.codex/handoff/manifest.json` and run history.

## Command Surface

- `ai-handoff <path>`: interactive wizard.
- `ai-handoff conversations <path>`: list candidate Claude sessions and selected IDs.
- `ai-handoff conversations <path> --all-projects --search TEXT`: recover sessions after folder moves or renamed Claude project keys.
- `ai-handoff scan <path>`: non-mutating scan.
- `ai-handoff diff <path>`: preview exact project-local writes.
- `ai-handoff privacy <path>`: show the private/local context categories that may be persisted.
- `ai-handoff apply <path>`: project-local apply.
- `ai-handoff globals <path>`: list MCP, skill, and plugin import candidates.
- `ai-handoff globals <path> --project-only`: list only project-evidenced candidates.
- `ai-handoff globals <path> --portable-only`: list only candidates without obvious local-machine dependencies.
- `ai-handoff globals <path> --include-risky`: include unverified plugins and risky bulk-selection candidates.
- `ai-handoff globals <path> --check-github`: use authenticated `gh` to check for native Codex manifests on GitHub origins. This is the default.
- `ai-handoff globals <path> --no-check-github`: skip GitHub checks and use local metadata only.
- `ai-handoff globals select <path> --select ID[,ID] --yes --ack-privacy`: persist exact Codex-wide install selection without executing it.
- `ai-handoff globals apply <path>`: execute installs selected in the interactive Codex-wide install picker.
- `ai-handoff init <path>`: project-learning pass only.
- `ai-handoff history [path]`: show previous runs.
- `ai-handoff show <run-id> --path <path>`: inspect a manifest.
- `ai-handoff doctor [path]`: check local Claude/Codex readiness.

## Default Selection

Enabled by default:

- Summarize latest relevant Claude work.
- Create or update `AGENTS.md`.
- Write `.codex/handoff/summary.md`.
- Write `.codex/handoff/manifest.json`.

Disabled by default:

- MCP installs.
- Skill conversions.
- Plugin installs.
- Fuller transcript excerpts.
- Any write under `~/.codex`.

## Safety Details

`--yes` may skip prompts only for project-local writes. It must not install MCPs, copy skills into `~/.codex/skills`, bridge plugins into `~/.codex/plugins`, edit Codex-wide config, or install plugins.

Generated MCP commands and Claude plugin records are discovered candidates. They are recorded in the manifest so a user or agent can review and select them later with explicit approval. A Claude plugin record is not proof of a direct Codex plugin install. Plugin handling uses this order: install directly installable native Codex packaging from the source repo when present; otherwise bridge from the source repo at the Claude-used ref; otherwise use the installed Claude cache as a labeled fallback; otherwise fail with the exact reason and next step. A bridge copies the plugin body into `~/.codex/plugins/cc-<name>`, drops Claude-only `hooks/`, `commands/`, `agents/`, and plugin metadata dirs, converts Claude `commands/*.md` into Codex-visible skills, converts `agents/*.md` into Codex TOML under `~/.codex/agents`, adds `x-cc-bridge`, and upserts `~/.agents/plugins/marketplace.json`. After explicit Codex-wide apply confirmation, run `codex plugin add cc-<name>@cc-bridged-plugins`; if that install command fails after bridge writes succeed, report a partial result with the exact command and failure reason. Before presenting the action, inspect origin metadata from `known_marketplaces.json`, local marketplace `marketplace.json`, local `.git/config`, and cached `.codex-plugin/plugin.json`; mark `codex-native` when a native Codex manifest exists locally. By default, `globals` and the wizard review step may also run authenticated `gh` checks; only add `github-origin` after `gh` exists, `gh auth status` succeeds, and `gh api` checks the native manifest path. If `gh` is missing, unauthenticated, or the API check fails, say that the GitHub check failed and keep the source bridge/cache/manual fallback visible. `--no-check-github` disables this default network-backed check for `globals`.

Privacy acknowledgement is required when handoff artifacts include Claude sessions or local MCP/skill/plugin inventory. `privacy` should make the categories explicit before apply: prompts, assistant notes, commands, transcript paths, MCP configs, skill/plugin names, nearby Claude project keys, and local paths.

Codex-wide install selection must be persistable outside the interactive menu. `globals select` writes only `.codex/handoff/manifest.json` plus a run snapshot, never `AGENTS.md`, and never changes `~/.codex`. `--select` accepts exact IDs, skill names, category aliases (`mcps`, `skills`, `plugins`), and `all`; IDs should be stable and human-readable where possible, for example `mcp:filesystem` and `skill:amq-cli`.

The globals view should group candidates as Recommended, Review, and Manual/Unsafe. JSON candidates must include `source_scope`, `risk_badges`, `why_relevant`, `evidence`, `relevance_score`, `portable`, and `blocked_reason`. Bulk selectors (`all`, `skills`, `plugins`, etc.) must not include `secret`, `unverified`, or Codex-wide candidates unless `--include-risky` is present.

Transcript-derived usage should make Codex-wide install review sharper. Scan selected Claude JSONL transcripts for actual `Skill` tool invocations, `mcp__server__tool` names, loaded skill metadata (`<skill-format>true</skill-format>` / `<command-name>`), and `attributionSkill` / `attributionPlugin`. Ignore initial available-skill attachments. Matching candidates should be marked `used-in-transcripts`, sorted ahead of otherwise similar candidates, and keep execution safety unchanged. If any transcript-used candidates exist, the interactive picker should open in `used` view rather than `all`; `Tab` exposes the full discovered inventory.

Conversation recovery must not depend on one exact Claude project folder. `conversations` and `scan` should support exact session IDs from moved projects, `--all-projects`, `--from-claude-project KEY`, `--search TEXT`, and `--branch NAME`. Candidate rows should include `source_project_key` so users know where recovered context came from.

## Wizard Flow

`ai-handoff <path>` is optimized for a one-time handoff. It should read as a wizard, not a dashboard:

1. Claude context: show found/selected sessions and transcript-used tools. Let the user continue, choose more conversations, skip context, or quit. After the user returns from the conversation picker, show only the selected/found count instead of repeating the earlier sample sessions.
2. Project files: show the exact project-local files, offer preview diff, apply, skip, or quit.
3. Tooling carryover: print the `Step 3/3` header before any slow compatibility checks, then show bounded progress lines so the user knows scanning and GitHub/native-Codex checks are running. Summarize conversation-matched MCPs, skills, and plugins first. Do not open the detailed install picker by default. List matched tools with stable numbers and bridge/native reasoning, let the user choose `Enter=all` or specific numbers, then ask whether to record in project handoff only or install for this Codex user. Broader Claude inventory counts can be mentioned as secondary context and remain available through Customize, but the primary Step 3 question should be about tooling relevant to the selected conversations.

The wizard must keep installs behind a second confirmation that explicitly says it can change `~/.codex` and make tooling available in every Codex project for this OS user. Codex CLI currently exposes these installs as user-level, not project-local; project-only means recording intent and context in AGENTS.md/manifest without installing.

The completion screen should increase confidence by listing what changed: selected conversation count, project files written, Codex-wide installs completed or recorded, artifact paths to inspect, and the next `codex` command. `AGENTS.md` should state that Codex loads it automatically for the project, list every selected conversation, and include a `Codex Tooling Prepared` section for installed or recorded MCP/skill/plugin actions.

## Legacy Static Menu

The menu should be static in TTY mode: redraw in place instead of appending a new menu after every keypress. Do not use an alternate screen; clear and redraw the current viewport so the interaction feels like Mole while still leaving final apply output in the normal terminal.

The menu should support both direct numeric toggles and cursor-driven operation:

- `Up` or `k`: move to the previous item.
- `Down` or `j`: move to the next item.
- `Space`: toggle the highlighted item.
- Number keys: toggle that numbered item.
- `c`: open the conversation picker. Defaults are latest relevant non-observer sessions, but the user must be able to select exact sessions before apply.
- `g`: open the Codex-wide install picker. This selects exact MCP, skill, and plugin installs. Discovery alone must not check the parent Codex-wide installs row.
- `p`: show the preview as a static screen, then return to the menu after a keypress.
- `Enter` or `a`: apply project-local writes.
- `q`: quit without changing files.

Final apply or quit output should clear the menu and print a normal terminal result.

### Conversation Picker

The conversation picker should use the same static viewport behavior as the main menu. It should expose:

- `/`: enter filter text across session ID, title, summary, first prompt, source project key, branch, transcript path, and project path. Empty filter clears it.
- `f` / `b`: page forward/backward.
- `d`: show full conversation details including source project key, branch, transcript path, prompt, and summary.
- `Space`/`x` and number keys: toggle the highlighted or visible numbered conversation by session ID.
- `Enter`/`a`: commit the draft conversation selection.
- `q`: cancel without changing the prior conversation selection.

Header text should include the active filter, visible range, page number, total filtered count, total candidate count, and selected count. The footer should show the all-project recovery command for moved or renamed projects.

Codex-wide installs should be explicit:

- MCP entries run the recorded `codex mcp add ...` command after confirmation.
- Skill entries copy the selected Claude skill folder into `~/.codex/skills` if no destination already exists.
- Plugin entries with `bridge=true` run the embedded bridge and then install the bridged plugin with `codex plugin add cc-<name>@cc-bridged-plugins`. Verified non-bridge plugin entries may run the recorded `codex plugin add ...` command after confirmation.
- Commands containing redacted values must be skipped and reported as manual follow-up.

### Global Picker

The Codex-wide install picker should use the same static viewport behavior as the main menu. It should expose:

- `/`: enter filter text across IDs, labels, commands, evidence, why-relevant text, risk badges, source path, and blocked reason. Empty filter clears it.
- `Tab`: cycle `used`, `all`, `mcp`, `skill`, `plugin`, and `manual` views.
- `f` / `b`: page forward/backward.
- `d`: show full candidate details without truncating commands, paths, badges, blocked reason, or manual steps.
- `A`: select all safe visible candidates. It must skip `secret`, `unverified`, `blocked_reason`, and Codex-wide candidates.
- `u`: clear selected visible candidates.
- `C`: clear all selected candidates.
- `i`: invert safe visible candidates while skipping `secret`, `unverified`, `blocked_reason`, and Codex-wide candidates.
- `Space`/`x` and number keys: toggle the highlighted or visible numbered candidate by candidate ID.
- `?`: show help.
- `Enter`/`a`: commit the draft import selection.
- `q`: cancel without changing the prior import selection.

Header text should include the active view, filter, visible range, page number, total filtered count, total candidate count, risky count, and selected count. Page size should adapt to terminal height where possible. Empty states should explain whether no candidates exist or no candidates match the active filter/view.
