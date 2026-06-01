# AI CLI Handoff

`ai-handoff` prepares a Claude Code project for Codex continuity.

The default flow is a one-time wizard:

```bash
bin/ai-handoff /path/to/project
```

Inspect Claude conversations before applying:

```bash
bin/ai-handoff conversations /path/to/project
bin/ai-handoff conversations /path/to/project --all-projects --search speech
bin/ai-handoff scan /path/to/project --sessions session-1,session-7
bin/ai-handoff privacy /path/to/project
bin/ai-handoff diff /path/to/project
```

`diff` shows human-facing write artifacts by default. Add `--include-manifest` to include manifest JSON. If Claude conversations or local MCP/skill/plugin inventory are selected, non-interactive apply requires `--ack-privacy` because the manifest can contain Claude-derived prompts, commands, inventories, and local paths.

Project-local apply writes `AGENTS.md` plus `.codex/handoff/` artifacts. `AGENTS.md` is the Codex-loaded project instruction file, so the managed handoff section lists every selected Claude conversation and summarizes any Codex-wide tooling prepared by the run:

```bash
bin/ai-handoff apply /path/to/project --yes --ack-privacy
```

Codex-wide MCP, plugin, and skill installs are never executed by default. They can change `~/.codex`, so they affect every Codex project/folder on this machine. The wizard first asks which Claude conversations to use, then applies project-local handoff files, then offers a separate Codex-wide review step. Selected installs can be executed during the wizard after a second confirmation, or later with:

```bash
bin/ai-handoff globals /path/to/project
bin/ai-handoff globals /path/to/project --project-only
bin/ai-handoff globals /path/to/project --portable-only
bin/ai-handoff globals /path/to/project --check-github
bin/ai-handoff globals /path/to/project --no-check-github
bin/ai-handoff globals /path/to/project --include-risky
bin/ai-handoff globals select /path/to/project --select skill:amq-cli,mcp:filesystem --yes --ack-privacy
bin/ai-handoff globals apply /path/to/project
```

MCP installs run selected `codex mcp add` commands. Skill installs copy selected Claude skill folders into `~/.codex/skills`, making them available to all Codex projects, when the destination does not already exist.

Plugin records are handled in this order: use a directly installable native Codex package when the source repo exposes one; otherwise bridge from the source repo at the Claude-used ref when that source is available; otherwise fall back to the installed Claude cache and say so. A bridge copies the plugin into `~/.codex/plugins/cc-<name>`, strips Claude-only `hooks/`, `commands/`, and `agents/`, converts Claude commands into Codex-visible skills, converts `agents/*.md` into Codex TOML under `~/.codex/agents`, writes an `x-cc-bridge` marker, updates `~/.agents/plugins/marketplace.json`, and then runs `codex plugin add cc-<name>@cc-bridged-plugins` after explicit Codex-wide apply confirmation. GitHub origin checks are on by default for `globals` and the wizard review step. If `gh` is missing, unauthenticated, or the API check fails, the CLI prints a clear GitHub check failure and keeps the bridge/manual fallback. Use `--no-check-github` for a fully offline listing. After installing a bridged plugin, restart Codex or open a new session.

Claude plugin records without a local cache remain manual. A Claude marketplace plugin is not automatically a Codex plugin; verify a Codex manifest/marketplace entry or bridge it first, for example with `cc2codex plugin-sync`. The embedded bridge behavior follows the MIT-licensed `cc-plugin-to-codex` model.

`globals` groups candidates as Recommended, Review, and Manual/Unsafe. Low-confidence plugin records are hidden by default; add `--include-risky` to inspect them. `globals select` records intent only in `.codex/handoff/manifest.json`; it does not write `AGENTS.md` and does not install Codex-wide changes. `--select` accepts exact candidate IDs, skill names, type aliases such as `skills` or `mcps`, and `all`; bulk selectors skip unsafe/Codex-wide candidates unless `--include-risky` is present.

Selected Claude transcripts are scanned for actual tooling usage. `ai-handoff` records observed `Skill` invocations, `mcp__server__tool` calls, skill metadata, and Claude plugin attribution, then marks matching Codex-wide candidates as `used-in-transcripts`. The wizard defaults to reviewing conversation-matched Codex-wide actions when any exist; the picker opens in `used` view and `Tab` reviews the broader discovered inventory. If you choose more conversations in step 1, the transcript usage and Codex-wide relevance are refreshed before step 3.

The wizard completion screen prints what increased handoff confidence: selected conversation count, Claude tooling seen, project files written, Codex-wide installs completed or recorded, and the artifact paths to inspect.

In the interactive conversation picker, use `/` to filter, `f`/`b` to page, `d` for details, and Space or row numbers to toggle visible sessions. Enter commits the draft selection; `q` cancels it.

In the interactive Codex-wide install picker, use `/` to filter, `Tab` to cycle `used`/`all`/type views, `f`/`b` to page, `d` for details, `A` to select all safe visible candidates, `u` to clear visible candidates, `C` to clear all, `i` to invert safe visible candidates, and `?` for help. Bulk visible selection follows the same safety rules as `globals select`: risky, secret, unverified, blocked, and Codex-wide candidates are skipped unless selected explicitly.

## Commands

```bash
bin/ai-handoff conversations /path/to/project
bin/ai-handoff diff /path/to/project
bin/ai-handoff privacy /path/to/project
bin/ai-handoff scan /path/to/project --json
bin/ai-handoff conversations /path/to/project --all-projects --search TEXT
bin/ai-handoff scan /path/to/project --from-claude-project -Users-you-Code-old-project
bin/ai-handoff init /path/to/project
bin/ai-handoff globals /path/to/project
bin/ai-handoff globals /path/to/project --project-only
bin/ai-handoff globals /path/to/project --portable-only
bin/ai-handoff globals /path/to/project --no-check-github
bin/ai-handoff globals select /path/to/project --select skills --yes --ack-privacy
bin/ai-handoff globals apply /path/to/project
bin/ai-handoff history /path/to/project
bin/ai-handoff doctor /path/to/project
```

The Codex skill lives in `skills/ai-handoff`.
