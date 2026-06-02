from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "ai-handoff" / "scripts" / "ai_handoff.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ai_handoff_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AiHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        os.environ["AI_HANDOFF_HOME"] = str(self.home)

        (self.project / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Setup\n\nRun tests carefully.\n", encoding="utf-8"
        )
        (self.project / "package.json").write_text(
            json.dumps({"scripts": {"test": "pytest", "build": "python -m build"}}),
            encoding="utf-8",
        )
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(pytest:*)"], "token": "secret-value"}}),
            encoding="utf-8",
        )

        codex_dir = self.home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "skills").mkdir()
        (codex_dir / "config.toml").write_text(
            f'[projects."{self.project.resolve()}"]\ntrust_level = "trusted"\n'
            '[plugins."github@openai-curated"]\nenabled = true\n',
            encoding="utf-8",
        )

        claude_dir = self.home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-filesystem", str(claude_dir / "skills")],
                            "env": {"SECRET_TOKEN": "abc123"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (claude_dir / "skills" / "sample-skill").mkdir(parents=True)
        (claude_dir / "skills" / "sample-skill" / "SKILL.md").write_text(
            "---\nname: sample-skill\ndescription: sample\n---\n", encoding="utf-8"
        )
        (claude_dir / "plugins").mkdir()
        (claude_dir / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"experimental@marketplace": {}}}),
            encoding="utf-8",
        )

        project_sessions = claude_dir / "projects" / self.module.claude_project_key(self.project)
        project_sessions.mkdir(parents=True)
        session_path = project_sessions / "session-1.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps({"message": {"role": "user", "content": "fix the failing tests TOKEN=abc123"}}),
                    json.dumps(
                        {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "name": "Bash",
                                        "input": {"command": "pytest tests/test_example.py"},
                                    },
                                    {"type": "text", "text": "Tests now pass."},
                                ],
                            }
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (project_sessions / "sessions-index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "sessionId": "observer",
                            "fullPath": str(project_sessions / "observer.jsonl"),
                            "firstPrompt": "You are a Claude-Mem, a specialized observer tool",
                            "summary": "Memory observer",
                            "messageCount": 1,
                            "created": "2026-05-30T11:00:00Z",
                            "modified": "2026-05-30T11:05:00Z",
                            "gitBranch": "main",
                            "projectPath": str(self.project.resolve()),
                            "isSidechain": False,
                        },
                        {
                            "sessionId": "session-1",
                            "fullPath": str(session_path),
                            "firstPrompt": "fix the failing tests",
                            "summary": "Testing work",
                            "messageCount": 2,
                            "created": "2026-05-30T10:00:00Z",
                            "modified": "2026-05-30T10:05:00Z",
                            "gitBranch": "main",
                            "projectPath": str(self.project.resolve()),
                            "isSidechain": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.session_path = session_path

    def tearDown(self) -> None:
        os.environ.pop("AI_HANDOFF_HOME", None)
        self.tmp.cleanup()

    def append_transcript_usage_events(self) -> None:
        with self.session_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "<command-message>sample-skill</command-message>\n"
                                        "<command-name>sample-skill</command-name>\n"
                                        "<skill-format>true</skill-format>"
                                    ),
                                }
                            ],
                        }
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "attributionPlugin": "experimental",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "name": "Skill", "input": {"skill": "sample-skill"}},
                                {"type": "tool_use", "name": "mcp__filesystem__read_file", "input": {"path": "README.md"}},
                            ],
                        },
                    }
                )
                + "\n"
            )

    def add_installed_claude_plugin_cache(self, *, include_native_codex: bool = True) -> Path:
        marketplace_root = self.home / ".claude" / "plugins" / "marketplaces" / "demo-market"
        (marketplace_root / ".claude-plugin").mkdir(parents=True)
        (marketplace_root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "demo-market",
                    "plugins": [
                        {
                            "name": "demo",
                            "source": "./plugins/demo",
                            "description": "Demo plugin from marketplace",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (marketplace_root / ".git").mkdir()
        (marketplace_root / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://github.com/acme/demo-market.git\n',
            encoding="utf-8",
        )
        if include_native_codex:
            (marketplace_root / "plugins" / "demo" / ".codex-plugin").mkdir(parents=True)
            (marketplace_root / "plugins" / "demo" / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": "demo", "version": "1.0.0", "description": "Native Codex demo"}),
                encoding="utf-8",
            )
        plugin_dir = self.home / ".claude" / "plugins" / "cache" / "demo-market" / "demo" / "1.0.0"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "demo",
                    "version": "1.0.0",
                    "description": "Demo Claude plugin",
                    "hooks": {"UserPromptSubmit": []},
                    "commands": ["demo"],
                    "agents": ["helper"],
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "skills" / "greet").mkdir(parents=True)
        (plugin_dir / "skills" / "greet" / "SKILL.md").write_text(
            "---\nname: greet\ndescription: greet\n---\n", encoding="utf-8"
        )
        (plugin_dir / "scripts").mkdir()
        (plugin_dir / "scripts" / "helper.py").write_text("# helper\n", encoding="utf-8")
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "commands" / "demo.md").write_text("Claude-only command\n", encoding="utf-8")
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "helper.md").write_text(
            "---\nname: helper\ndescription: Helpful bridge agent\nmodel: opus\ntools: [Read]\n---\n\nHelp with demo tasks.\n",
            encoding="utf-8",
        )
        installed_path = self.home / ".claude" / "plugins" / "installed_plugins.json"
        data = json.loads(installed_path.read_text(encoding="utf-8"))
        data["plugins"]["demo@demo-market"] = [
            {
                "scope": "user",
                "installPath": str(plugin_dir),
                "version": "1.0.0",
                "gitCommitSha": "abcdef1234567890",
            }
        ]
        installed_path.write_text(json.dumps(data), encoding="utf-8")
        return plugin_dir

    def test_build_manifest_discovers_and_redacts(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)

        self.assertEqual(manifest["flow"]["id"], "claude_to_codex")
        self.assertEqual(manifest["codex"]["trust_level"], "trusted")
        self.assertEqual(manifest["claude"]["sessions"]["selected_count"], 1)
        self.assertEqual(manifest["claude"]["sessions"]["selected"][0]["session_id"], "session-1")
        self.assertIn("AGENTS.md", manifest["actions"]["project_writes"])
        self.assertIn("diagnostics", manifest)
        self.assertIn("writes", manifest)
        self.assertIn("global_candidates", manifest)
        self.assertEqual(manifest["writes"][0]["path"], "AGENTS.md")
        self.assertTrue(manifest["writes"][0]["contains_private_context"])
        self.assertTrue(manifest["privacy"]["ack_required_for_apply"])
        command = manifest["actions"]["mcp_commands"][0]
        self.assertIn("codex mcp add", command)
        self.assertIn("SECRET_TOKEN=<redacted>", command)

        prompt = manifest["claude"]["sessions"]["selected"][0]["transcript"]["user_prompts"][0]
        self.assertIn("TOKEN=<redacted>", prompt)

    def test_claude_setup_capture_records_hooks_rules_references_and_statusline(self) -> None:
        (self.project / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\nUse @docs/runbook.md and @/tmp/outside.md.\n",
            encoding="utf-8",
        )
        (self.project / "docs").mkdir()
        (self.project / "docs" / "runbook.md").write_text("Runbook\n", encoding="utf-8")
        settings = {
            "permissions": {
                "allow": ["Bash(pytest:*)"],
                "deny": ["Bash(rm:*)"],
                "defaultMode": "acceptEdits",
            },
            "hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}]},
            "references": ["docs/runbook.md", "/tmp/outside.md"],
            "statusLine": {"type": "command", "command": "echo status"},
        }
        (self.project / ".claude" / "settings.local.json").write_text(json.dumps(settings), encoding="utf-8")

        manifest = self.module.build_manifest(str(self.project), last=1)
        capture = manifest["claude"]["config"]["setup_capture"]
        counts = capture["summary"]

        self.assertGreaterEqual(counts["hooks"], 1)
        self.assertGreaterEqual(counts["rules"], 3)
        self.assertGreaterEqual(counts["references"], 2)
        self.assertEqual(counts["statusline"], 1)
        self.assertTrue(any(item["classification"] == "project-local" for item in capture["references"]))
        self.assertTrue(any(item["classification"] == "external" for item in capture["references"]))
        self.assertIn("Claude Setup Captured", self.module.render_summary(manifest))
        self.assertIn("Statusline:", self.module.render_agents_managed_section(manifest))

    def test_transcript_usage_annotates_global_candidates(self) -> None:
        self.append_transcript_usage_events()

        manifest = self.module.build_manifest(str(self.project), last=1)
        usage = manifest["claude"]["sessions"]["usage_summary"]

        self.assertIn("filesystem", [item["name"] for item in usage["mcp_servers"]])
        self.assertIn("sample-skill", [item["name"] for item in usage["skills"]])
        self.assertIn("experimental", [item["name"] for item in usage["plugins"]])

        candidates = self.module.global_action_candidates(manifest)
        mcp = next(item for item in candidates if item["id"] == "mcp:filesystem")
        skill = next(item for item in candidates if item["id"] == "skill:sample-skill")
        plugin = next(item for item in candidates if item["id"] == "plugin:experimental@marketplace")

        self.assertTrue(mcp["used_in_selected_sessions"])
        self.assertTrue(skill["used_in_selected_sessions"])
        self.assertTrue(plugin["used_in_selected_sessions"])
        self.assertEqual(plugin["confidence"], "medium")
        self.assertIn("Used in selected Claude transcript", plugin["why_relevant"])

    def test_used_low_confidence_plugin_is_visible_by_default(self) -> None:
        self.append_transcript_usage_events()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project)])

        self.assertEqual(code, 0)
        self.assertIn("plugin:experimental@marketplace", stdout.getvalue())
        self.assertIn("used-in-transcripts", stdout.getvalue())

    def test_globals_refreshes_stale_manifest_before_usage_matching(self) -> None:
        stale = self.module.build_manifest(str(self.project), last=1)
        self.module.write_manifest_artifacts(stale)
        self.append_transcript_usage_events()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project)])

        self.assertEqual(code, 0)
        self.assertIn("plugin:experimental@marketplace", stdout.getvalue())
        self.assertIn("used-in-transcripts", stdout.getvalue())

    def test_session_selection_refreshes_tooling_relevance(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin_before = next(
            item for item in manifest["global_candidates"] if item["id"] == "plugin:experimental@marketplace"
        )
        self.assertFalse(plugin_before["used_in_selected_sessions"])

        self.append_transcript_usage_events()
        self.module.update_session_selection(manifest, ["session-1"])

        plugin_after = next(
            item for item in manifest["global_candidates"] if item["id"] == "plugin:experimental@marketplace"
        )
        self.assertTrue(plugin_after["used_in_selected_sessions"])
        self.assertIn("Used in selected Claude transcript", plugin_after["why_relevant"])

    def test_claude_project_key_matches_dot_normalization(self) -> None:
        key = self.module.claude_project_key(Path("/Users/omri.a/Code/speech-to-text-tools"))

        self.assertEqual(key, "-Users-omri-a-Code-speech-to-text-tools")

    def test_apply_writes_project_local_artifacts(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        result = self.module.write_project_artifacts(manifest)

        self.assertIn("AGENTS.md", result["written"])
        self.assertTrue((self.project / "AGENTS.md").exists())
        self.assertTrue((self.project / ".codex" / "handoff" / "summary.md").exists())
        self.assertTrue((self.project / ".codex" / "handoff" / "manifest.json").exists())

        agents = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(self.module.MANAGED_START, agents)
        self.assertIn("Testing work", agents)
        self.assertIn("Codex loads AGENTS.md automatically", agents)
        self.assertIn("On the first Codex run after this handoff, ask Codex to:", agents)
        self.assertIn("## Selected Claude Transcript Files", agents)
        self.assertIn("For deeper context, read the selected Claude transcript JSONL files directly", agents)
        self.assertIn(str(self.session_path), agents)
        self.assertIn("Recent prompt: fix the failing tests", agents)
        self.assertIn("Recent assistant note: Tests now pass.", agents)
        self.assertIn("Commands seen: pytest tests/test_example.py", agents)
        self.assertNotIn("telemetry-session-start", agents)
        self.assertIn("No Codex-wide MCP, plugin, or skill installs were executed", agents)
        self.assertIn(
            "Recent prompts, assistant notes, commands, and local transcript paths in AGENTS.md and manifest JSON",
            manifest["privacy"]["written_context"],
        )

    def test_agents_lists_all_selected_conversations(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        base = dict(manifest["claude"]["sessions"]["selected"][0])
        selected = []
        for index in range(10):
            item = dict(base)
            item["session_id"] = f"session-{index}"
            item["title"] = f"conversation {index}"
            item["custom_title"] = f"conversation {index}"
            selected.append(item)
        manifest["claude"]["sessions"]["selected"] = selected
        manifest["claude"]["sessions"]["selected_count"] = len(selected)
        manifest["claude"]["sessions"]["found_count"] = len(selected)
        manifest["claude"]["sessions"]["selected_session_ids"] = [item["session_id"] for item in selected]

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("Selected sessions: 10 of 10 discovered.", agents)
        self.assertIn("conversation 0", agents)
        self.assertIn("conversation 9", agents)

    def test_agents_transcript_file_section_handles_missing_paths(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        for session in manifest["claude"]["sessions"]["selected"]:
            session.pop("transcript_path", None)

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("## Selected Claude Transcript Files", agents)
        self.assertIn("No transcript file paths were captured", agents)

    def test_agents_conversation_context_is_compact_and_filters_hook_noise(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        selected = manifest["claude"]["sessions"]["selected"][0]
        selected["first_prompt"] = "line one\nline two"
        selected["transcript"]["user_prompts"] = ["recent\nprompt"]
        selected["transcript"]["assistant_notes"] = ["assistant\nnote"]
        selected["transcript"]["commands"] = [
            "bash ${CLAUDE_PLUGIN_ROOT}/hooks/telemetry-session-start.sh",
            "Loading work context...\nexport PATH=\"$PATH\"\n_C=\"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"",
            "pytest tests/test_example.py",
        ]

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("First prompt: line one line two", agents)
        self.assertIn("Recent prompt: recent prompt", agents)
        self.assertIn("Recent assistant note: assistant note", agents)
        self.assertIn("Commands seen: pytest tests/test_example.py", agents)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", agents)
        self.assertNotIn("Loading work context", agents)

    def test_agents_embeds_claude_md_source_for_first_run_distillation(self) -> None:
        (self.project / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n"
            "## Hard Rules\n\n"
            "- Never touch generated reports.\n"
            "- API_TOKEN=should-not-leak\n\n"
            "```bash\npytest\n```\n",
            encoding="utf-8",
        )
        manifest = self.module.build_manifest(str(self.project), last=1)

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("## First-Run Codex Distillation", agents)
        self.assertIn("This AGENTS.md section was generated by ai-handoff", agents)
        self.assertIn("On the first Codex run after this handoff, ask Codex to:", agents)
        self.assertIn("Edit AGENTS.md outside the ai-handoff managed markers", agents)
        self.assertIn("merge any rules found in CLAUDE.md, Claude settings, or selected conversations", agents)
        self.assertIn("ghost rules", agents)
        self.assertIn("## CLAUDE.md Source Snapshot", agents)
        self.assertIn("Never touch generated reports.", agents)
        self.assertIn("API_TOKEN=<redacted>", agents)
        self.assertNotIn("should-not-leak", agents)
        self.assertIn("````markdown", agents)
        self.assertIn("```bash\npytest\n```", agents)

    def test_missing_claude_md_does_not_render_source_snapshot(self) -> None:
        (self.project / "CLAUDE.md").unlink()
        manifest = self.module.build_manifest(str(self.project), last=1)

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("## First-Run Codex Distillation", agents)
        self.assertIn("No CLAUDE.md source snapshot was available", agents)
        self.assertIn("No CLAUDE.md was found; use selected conversations", agents)
        self.assertNotIn("## CLAUDE.md Source Snapshot", agents)

    def test_summary_uses_project_snapshot_instead_of_readiness_failures(self) -> None:
        (self.project / "CLAUDE.md").unlink()
        manifest = self.module.build_manifest(str(self.project), last=1)

        summary = self.module.render_summary(manifest)

        self.assertIn("## Project Snapshot", summary)
        self.assertIn("CLAUDE.md source: not present for this project", summary)
        self.assertIn("Existing AGENTS.md before handoff: not present; handoff will create it", summary)
        self.assertNotIn("## Project Readiness", summary)
        self.assertNotIn("CLAUDE.md: missing", summary)

    def test_agents_mentions_native_codex_plugin_install_followup(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("Native Codex plugin install follow-up", agents)
        self.assertIn("plugin:demo@demo-market", agents)
        self.assertIn("install or enable the native Codex package instead of auto-bridging", agents)

    def test_agents_keeps_native_followup_when_other_action_selected(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["skill:sample-skill"]
        manifest["selected_global_actions"] = [
            candidate
            for candidate in self.module.global_action_candidates(manifest)
            if candidate["id"] == "skill:sample-skill"
        ]

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("Selected Codex-wide actions were recorded but not installed", agents)
        self.assertIn("skill:sample-skill copied as sample-skill", agents)
        self.assertIn("Native Codex plugin install follow-up", agents)
        self.assertIn("plugin:demo@demo-market", agents)

    def test_agents_reports_installed_codex_wide_actions(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:x@omri-cc-stuff"]
        manifest["selected_global_actions"] = [
            {
                "id": "plugin:x@omri-cc-stuff",
                "type": "plugin",
                "bridge_name": "cc-x",
                "label": "bridge Claude plugin x",
            }
        ]
        manifest["global_apply_results"] = [{"id": "plugin:x@omri-cc-stuff", "status": "ok"}]

        agents = self.module.render_agents_managed_section(manifest)

        self.assertIn("Installed Codex-wide for future Codex sessions", agents)
        self.assertIn("plugin:x@omri-cc-stuff bridged as cc-x", agents)
        self.assertIn("Open a new Codex session", agents)

    def test_apply_preserves_prior_codex_wide_install_results(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:x@omri-cc-stuff"]
        manifest["selected_global_actions"] = [
            {"id": "plugin:x@omri-cc-stuff", "type": "plugin", "bridge_name": "cc-x"}
        ]
        manifest["global_apply_results"] = [{"id": "plugin:x@omri-cc-stuff", "status": "ok"}]
        self.module.write_manifest_artifacts(manifest)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["apply", str(self.project), "--yes", "--ack-privacy"])

        self.assertEqual(code, 0)
        agents = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("plugin:x@omri-cc-stuff bridged as cc-x", agents)

    def test_apply_recovers_codex_wide_results_from_run_history(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:x@omri-cc-stuff"]
        manifest["selected_global_actions"] = [
            {"id": "plugin:x@omri-cc-stuff", "type": "plugin", "bridge_name": "cc-x"}
        ]
        manifest["global_apply_results"] = [{"id": "plugin:x@omri-cc-stuff", "status": "ok"}]
        self.module.write_manifest_artifacts(manifest)

        later = self.module.build_manifest(str(self.project), last=1)
        later["run_id"] = "later-without-global-state"
        self.module.write_manifest_artifacts(later)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["apply", str(self.project), "--yes", "--ack-privacy"])

        self.assertEqual(code, 0)
        agents = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("plugin:x@omri-cc-stuff bridged as cc-x", agents)

    def test_cli_scan_json_outputs_manifest(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["scan", str(self.project), "--last", "1", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["target_path"], str(self.project.resolve()))
        self.assertFalse(data["applied"])

    def test_cli_sessions_flag_selects_exact_session(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["scan", str(self.project), "--sessions", "session-1", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["claude"]["sessions"]["selection_strategy"], "user-selected session IDs")
        self.assertEqual(data["claude"]["sessions"]["selected_session_ids"], ["session-1"])

    def test_conversations_command_lists_session_ids(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["conversations", str(self.project)])

        self.assertEqual(code, 0)
        self.assertIn("Claude conversations for", stdout.getvalue())
        self.assertNotIn("cleanupPeriodDays", stdout.getvalue())
        self.assertIn("session-1", stdout.getvalue())
        self.assertIn("--sessions session-1", stdout.getvalue())

    def test_dry_run_zero_sessions_mentions_cleanup_period(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["claude"]["sessions"]["found_count"] = 0
        manifest["claude"]["sessions"]["selected_count"] = 0
        manifest["claude"]["sessions"]["selected"] = []
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.module.print_dry_run(manifest)

        self.assertIn("sessions found: 0", stdout.getvalue())
        self.assertIn("cleanupPeriodDays", stdout.getvalue())

    def test_all_projects_conversations_can_find_moved_project_sessions(self) -> None:
        moved_dir = self.home / ".claude" / "projects" / "-Users-omri-Code-old-project"
        moved_dir.mkdir(parents=True)
        moved_session = moved_dir / "moved-session.jsonl"
        moved_session.write_text(
            json.dumps({"message": {"role": "user", "content": "continue the speech migration"}}) + "\n",
            encoding="utf-8",
        )
        (moved_dir / "sessions-index.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "sessionId": "moved-session",
                            "fullPath": str(moved_session),
                            "firstPrompt": "continue the speech migration",
                            "summary": "Moved project work",
                            "created": "2026-05-31T08:00:00Z",
                            "modified": "2026-05-31T08:05:00Z",
                            "projectPath": "/Users/omri/Code/old-speech-project",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["conversations", str(self.project), "--all-projects", "--search", "speech", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertIn("-Users-omri-Code-old-project", data["source_project_keys"])
        self.assertTrue(any(item["session_id"] == "moved-session" for item in data["candidates"]))

    def test_explicit_session_selection_recovers_across_claude_projects(self) -> None:
        moved_dir = self.home / ".claude" / "projects" / "-Users-omri-Code-old-project"
        moved_dir.mkdir(parents=True)
        moved_session = moved_dir / "moved-session.jsonl"
        moved_session.write_text(
            json.dumps({"message": {"role": "user", "content": "recover this exact session"}}) + "\n",
            encoding="utf-8",
        )
        (moved_dir / "sessions-index.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "sessionId": "moved-session",
                            "fullPath": str(moved_session),
                            "firstPrompt": "recover this exact session",
                            "summary": "Moved project work",
                            "created": "2026-05-31T08:00:00Z",
                            "modified": "2026-05-31T08:05:00Z",
                            "projectPath": "/Users/omri/Code/old-speech-project",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["scan", str(self.project), "--sessions", "moved-session", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        selected = data["claude"]["sessions"]["selected"][0]
        self.assertEqual(selected["session_id"], "moved-session")
        self.assertEqual(selected["source_project_key"], "-Users-omri-Code-old-project")

    def test_loose_jsonl_sessions_are_merged_when_index_is_stale(self) -> None:
        project_sessions = self.home / ".claude" / "projects" / self.module.claude_project_key(self.project)
        loose_path = project_sessions / "loose-session.jsonl"
        loose_path.write_text(
            json.dumps({"message": {"role": "user", "content": "new loose transcript work"}, "gitBranch": "feature"})
            + "\n",
            encoding="utf-8",
        )

        manifest = self.module.build_manifest(str(self.project), last=3)
        ids = manifest["claude"]["sessions"]["selected_session_ids"]

        self.assertIn("loose-session", ids)
        loose = next(item for item in manifest["claude"]["sessions"]["candidates"] if item["session_id"] == "loose-session")
        self.assertIn("new loose transcript work", loose["first_prompt"])

    def test_loose_jsonl_sessions_use_prompt_and_timestamps_without_index(self) -> None:
        project = self.root / "raw-project"
        project.mkdir()
        sessions_dir = self.home / ".claude" / "projects" / self.module.claude_project_key(project)
        sessions_dir.mkdir(parents=True)
        session_path = sessions_dir / "raw-session.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "attachment",
                            "timestamp": "2026-05-31T08:00:00Z",
                            "cwd": str(project.resolve()),
                            "gitBranch": "main",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "isMeta": True,
                            "message": {
                                "role": "user",
                                "content": "<local-command-caveat>ignore this</local-command-caveat>",
                            },
                            "timestamp": "2026-05-31T08:00:01Z",
                            "cwd": str(project.resolve()),
                            "gitBranch": "main",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "ship the plugin release"},
                            "timestamp": "2026-05-31T08:01:00Z",
                            "cwd": str(project.resolve()),
                            "gitBranch": "main",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        manifest = self.module.build_manifest(str(project), last=1)

        candidate = manifest["claude"]["sessions"]["candidates"][0]
        self.assertEqual(candidate["title"], "ship the plugin release")
        self.assertEqual(candidate["modified"], "2026-05-31T08:01:00Z")
        self.assertNotEqual(candidate["title"], "Untitled")

    def test_loose_jsonl_command_only_sessions_use_command_label(self) -> None:
        sessions_dir = self.home / ".claude" / "projects" / self.module.claude_project_key(self.project)
        session_path = sessions_dir / "command-only.jsonl"
        session_path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "<command-name>/plugin</command-name>\n<command-message>plugin</command-message>",
                    },
                    "timestamp": "2026-05-31T08:01:00Z",
                    "cwd": str(self.project.resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        entry = self.module.loose_session_entry(
            session_path,
            self.module.claude_project_key(self.project),
            self.project.resolve(),
        )

        self.assertEqual(entry["firstPrompt"], "/plugin")

    def test_loose_jsonl_sessions_use_slug_as_custom_title(self) -> None:
        session_path = self.home / ".claude" / "projects" / self.module.claude_project_key(self.project) / "renamed.jsonl"
        session_path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "long first prompt should become description"},
                    "timestamp": "2026-05-31T08:01:00Z",
                    "cwd": str(self.project.resolve()),
                    "slug": "release-plan",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        entry = self.module.loose_session_entry(
            session_path,
            self.module.claude_project_key(self.project),
            self.project.resolve(),
        )

        self.assertEqual(entry["slug"], "release-plan")
        self.assertEqual(self.module.entry_session_custom_title(entry), "release plan")

    def test_loose_jsonl_sessions_use_rename_reminder_as_custom_title(self) -> None:
        session_path = self.home / ".claude" / "projects" / self.module.claude_project_key(self.project) / "named.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "start with this request"},
                            "timestamp": "2026-05-31T08:01:00Z",
                            "cwd": str(self.project.resolve()),
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "isMeta": True,
                            "message": {
                                "role": "user",
                                "content": '<system-reminder>The user named this session "release-plan". This may indicate the session focus.</system-reminder>',
                            },
                            "timestamp": "2026-05-31T08:02:00Z",
                            "cwd": str(self.project.resolve()),
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        entry = self.module.loose_session_entry(
            session_path,
            self.module.claude_project_key(self.project),
            self.project.resolve(),
        )

        self.assertEqual(entry["namedTitle"], "release-plan")
        self.assertEqual(self.module.entry_session_custom_title(entry), "release-plan")

    def test_diff_command_previews_project_writes(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["diff", str(self.project), "--last", "1"])

        self.assertEqual(code, 0)
        self.assertIn("--- a/AGENTS.md", stdout.getvalue())
        self.assertIn("+++ b/AGENTS.md", stdout.getvalue())
        self.assertNotIn("--- a/.codex/handoff/manifest.json", stdout.getvalue())

    def test_globals_command_lists_import_candidates(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project)])

        self.assertEqual(code, 0)
        self.assertIn("Codex-wide installs from Claude to Codex", stdout.getvalue())
        self.assertIn("mcp:filesystem", stdout.getvalue())
        self.assertIn("Review:", stdout.getvalue())
        self.assertIn("Manual / Unsafe:", stdout.getvalue())
        self.assertIn("medium", stdout.getvalue())
        self.assertNotIn("plugin:experimental@marketplace", stdout.getvalue())
        self.assertIn("Hidden: 1 candidates", stdout.getvalue())

    def test_globals_include_risky_shows_low_confidence_plugins(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project), "--include-risky"])

        self.assertEqual(code, 0)
        self.assertIn("plugin:experimental@marketplace", stdout.getvalue())
        self.assertIn("unverified", stdout.getvalue())

    def test_globals_checks_github_by_default_when_listing(self) -> None:
        self.add_installed_claude_plugin_cache()
        self.module.shutil.rmtree(
            self.home / ".claude" / "plugins" / "marketplaces" / "demo-market" / "plugins" / "demo" / ".codex-plugin"
        )
        stdout = io.StringIO()

        with mock.patch.object(
            self.module,
            "gh_auth_status",
            return_value={"available": False, "authenticated": False, "reason": "gh CLI not found"},
        ) as auth:
            with contextlib.redirect_stdout(stdout):
                code = self.module.main(["globals", str(self.project), "--include-risky"])

        self.assertEqual(code, 0)
        auth.assert_called()
        self.assertIn("checked-github", stdout.getvalue())
        self.assertIn("GitHub check warning: failed for", stdout.getvalue())
        self.assertIn("gh CLI not found", stdout.getvalue())
        self.assertIn("Native Codex detection may be incomplete", stdout.getvalue())
        self.assertIn(
            "GitHub check failed. Keeping Claude-to-Codex bridge candidate. Reason: gh CLI not found",
            stdout.getvalue(),
        )

    def test_globals_no_check_github_skips_gh(self) -> None:
        self.add_installed_claude_plugin_cache()
        stdout = io.StringIO()

        with mock.patch.object(self.module, "gh_auth_status") as auth:
            with contextlib.redirect_stdout(stdout):
                code = self.module.main(["globals", str(self.project), "--include-risky", "--no-check-github"])

        self.assertEqual(code, 0)
        auth.assert_not_called()
        self.assertNotIn("checked-github", stdout.getvalue())
        self.assertNotIn("GitHub check", stdout.getvalue())

    def test_globals_json_includes_risk_metadata(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project), "--include-risky", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        mcp = next(item for item in data if item["id"] == "mcp:filesystem")
        self.assertEqual(mcp["source_scope"], "global")
        self.assertIn("global-scope", mcp["risk_badges"])
        plugin = next(item for item in data if item["id"] == "plugin:experimental@marketplace")
        self.assertEqual(
            plugin["blocked_reason"],
            "Claude plugin record only; verify a Codex plugin manifest/marketplace entry or bridge it before install",
        )
        self.assertTrue(any("cc2codex" in step for step in plugin["manual_steps"]))

    def test_installed_claude_plugin_cache_becomes_bridge_candidate(self) -> None:
        plugin_dir = self.add_installed_claude_plugin_cache()

        manifest = self.module.build_manifest(str(self.project), last=1)
        candidates = self.module.global_action_candidates(manifest)
        plugin = next(item for item in candidates if item["id"] == "plugin:demo@demo-market")

        self.assertTrue(plugin["bridge"])
        self.assertEqual(plugin["bridge_name"], "cc-demo")
        self.assertEqual(plugin["bridge_source_kind"], "github-source")
        self.assertEqual(plugin["bridge_source_path"], "https://github.com/acme/demo-market.git")
        self.assertEqual(plugin["bridge_cache_fallback_path"], str(plugin_dir))
        self.assertIn("native Codex plugin metadata was found", plugin["blocked_reason"])
        self.assertIn("bridge", plugin["risk_badges"])
        self.assertIn("marketplace", plugin["risk_badges"])
        self.assertIn("git-source", plugin["risk_badges"])
        self.assertIn("network", plugin["risk_badges"])
        self.assertIn("codex-native", plugin["risk_badges"])
        self.assertIn("GitHub source plugin", plugin["evidence"])
        self.assertEqual(plugin["origin_github_repo"], "acme/demo-market")
        self.assertEqual(plugin["codex_release_status"], "native-codex-source")
        self.assertIn("plugins/demo/.codex-plugin/plugin.json", plugin["codex_release_check_urls"][0])

    def test_plugin_bridge_prefers_source_repo_when_claude_manifest_exists(self) -> None:
        cache_dir = self.add_installed_claude_plugin_cache()
        source_dir = self.home / ".claude" / "plugins" / "marketplaces" / "demo-market" / "plugins" / "demo"
        (source_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (source_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "version": "2.0.0", "description": "Source Claude demo"}),
            encoding="utf-8",
        )

        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:demo@demo-market"
        )

        self.assertEqual(plugin["bridge_source_kind"], "marketplace-source")
        self.assertEqual(plugin["bridge_source_path"], str(source_dir.resolve()))
        self.assertEqual(plugin["bridge_cache_fallback_path"], str(cache_dir))
        self.assertEqual(plugin["bridge_source_version"], "2.0.0")
        self.assertEqual(plugin["bridge_cache_version"], "1.0.0")
        self.assertTrue(plugin["bridge_cache_stale"])
        self.assertIn("marketplace source plugin", plugin["evidence"])
        self.assertIn("marketplace", plugin["risk_badges"])
        self.assertIn("git-source", plugin["risk_badges"])
        self.assertIn("cache version 1.0.0 differs from source version 2.0.0", plugin["evidence"])

    def test_plugin_url_source_is_remote_not_fake_local_source_path(self) -> None:
        self.add_installed_claude_plugin_cache()
        marketplace = self.home / ".claude" / "plugins" / "marketplaces" / "demo-market"
        data = json.loads((marketplace / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        data["plugins"].append(
            {
                "name": "remote-demo",
                "source": {"source": "url", "url": "https://github.com/acme/remote-demo.git"},
            }
        )
        (marketplace / ".claude-plugin" / "marketplace.json").write_text(json.dumps(data), encoding="utf-8")
        cache_dir = self.home / ".claude" / "plugins" / "cache" / "demo-market" / "remote-demo" / "1.0.0"
        (cache_dir / ".claude-plugin").mkdir(parents=True)
        (cache_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "remote-demo", "version": "1.0.0", "description": "cached remote"}),
            encoding="utf-8",
        )
        installed_path = self.home / ".claude" / "plugins" / "installed_plugins.json"
        installed = json.loads(installed_path.read_text(encoding="utf-8"))
        installed["plugins"]["remote-demo@demo-market"] = [
            {
                "scope": "user",
                "installPath": str(cache_dir),
                "version": "1.0.0",
                "gitCommitSha": "0123456789abcdef",
            }
        ]
        installed_path.write_text(json.dumps(installed), encoding="utf-8")

        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:remote-demo@demo-market"
        )

        self.assertEqual(plugin["origin_github_repo"], "acme/remote-demo")
        self.assertEqual(plugin["origin_source_path"], "")
        self.assertEqual(plugin["bridge_source_kind"], "github-source")
        self.assertEqual(plugin["bridge_source_path"], "https://github.com/acme/remote-demo.git")
        self.assertEqual(plugin["bridge_cache_fallback_path"], str(cache_dir))
        self.assertIn("GitHub source plugin", plugin["evidence"])
        self.assertIn("network", plugin["risk_badges"])
        self.assertIn("git-source", plugin["risk_badges"])

    def test_remote_plugin_source_without_cache_is_still_github_source_candidate(self) -> None:
        self.add_installed_claude_plugin_cache(include_native_codex=False)
        marketplace = self.home / ".claude" / "plugins" / "marketplaces" / "demo-market"
        data = json.loads((marketplace / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        data["plugins"].append(
            {
                "name": "remote-no-cache",
                "source": {"source": "url", "url": "https://github.com/acme/remote-no-cache.git"},
            }
        )
        (marketplace / ".claude-plugin" / "marketplace.json").write_text(json.dumps(data), encoding="utf-8")
        installed_path = self.home / ".claude" / "plugins" / "installed_plugins.json"
        installed = json.loads(installed_path.read_text(encoding="utf-8"))
        installed["plugins"]["remote-no-cache@demo-market"] = [
            {
                "scope": "user",
                "installPath": str(self.home / ".claude" / "plugins" / "cache" / "missing"),
                "version": "1.0.0",
                "gitCommitSha": "0123456789abcdef",
            }
        ]
        installed_path.write_text(json.dumps(installed), encoding="utf-8")

        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:remote-no-cache@demo-market"
        )

        self.assertTrue(plugin["bridge"])
        self.assertEqual(plugin["bridge_source_kind"], "github-source")
        self.assertEqual(plugin["bridge_remote_repo"], "acme/remote-no-cache")
        self.assertEqual(plugin["bridge_cache_fallback_path"], "")
        self.assertIn("no Claude cache fallback is available", plugin["evidence"])

    def test_skill_copy_prefers_formal_source_path_over_claude_cache(self) -> None:
        cache_skill = self.home / ".claude" / "skills" / "sample-skill" / "SKILL.md"
        source_skill_dir = self.root / "source-skills" / "sample-skill"
        source_skill_dir.mkdir(parents=True)
        (source_skill_dir.parent / ".git").mkdir()
        (source_skill_dir.parent / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:acme/source-skills.git\n',
            encoding="utf-8",
        )
        source_skill = source_skill_dir / "SKILL.md"
        source_skill.write_text(
            "---\nname: sample-skill\ndescription: source skill\n---\n\nUse source instructions.\n",
            encoding="utf-8",
        )
        cache_skill.write_text(
            (
                "---\n"
                "name: sample-skill\n"
                "description: cache skill\n"
                f"source_path: {source_skill_dir}\n"
                "---\n\nUse stale cache instructions.\n"
            ),
            encoding="utf-8",
        )

        manifest = self.module.build_manifest(str(self.project), last=1)
        skill = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "skill:sample-skill"
        )

        self.assertEqual(skill["skill_source_kind"], "source-repo")
        self.assertEqual(skill["source_path"], str(source_skill_dir))
        self.assertEqual(skill["skill_origin_github_repo"], "acme/source-skills")
        self.assertEqual(skill["skill_cache_fallback_path"], str(cache_skill.parent))
        self.assertIn("git-source", skill["risk_badges"])
        self.assertIn("skill source repo", skill["evidence"])

        manifest["selected_global_action_ids"] = ["skill:sample-skill"]
        results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "ok")
        copied = self.home / ".codex" / "skills" / "sample-skill" / "SKILL.md"
        self.assertIn("Use source instructions.", copied.read_text(encoding="utf-8"))
        self.assertNotIn("Use stale cache instructions.", copied.read_text(encoding="utf-8"))

    def test_skill_copy_materializes_github_source_before_cache_fallback(self) -> None:
        cache_skill = self.home / ".claude" / "skills" / "sample-skill" / "SKILL.md"
        cache_skill.write_text(
            (
                "---\n"
                "name: sample-skill\n"
                "description: cache skill\n"
                "repo: acme/sample-skill\n"
                "---\n\nUse stale cache instructions.\n"
            ),
            encoding="utf-8",
        )
        manifest = self.module.build_manifest(str(self.project), last=1)
        skill = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "skill:sample-skill"
        )
        self.assertEqual(skill["skill_source_kind"], "github-source")
        self.assertEqual(skill["skill_origin_github_repo"], "acme/sample-skill")
        self.assertIn("git-source", skill["risk_badges"])
        self.assertIn("GitHub skill source", skill["evidence"])

        temp_source = tempfile.TemporaryDirectory()
        source_dir = Path(temp_source.name) / "__skill_source__"
        source_dir.mkdir()
        (source_dir / "SKILL.md").write_text(
            "---\nname: sample-skill\ndescription: remote\n---\n\nUse remote source instructions.\n",
            encoding="utf-8",
        )
        manifest["selected_global_action_ids"] = ["skill:sample-skill"]
        with mock.patch.object(self.module, "materialize_github_skill_source", return_value=(temp_source, "")):
            results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "ok")
        copied = self.home / ".codex" / "skills" / "sample-skill" / "SKILL.md"
        self.assertIn("Use remote source instructions.", copied.read_text(encoding="utf-8"))
        self.assertNotIn("Use stale cache instructions.", copied.read_text(encoding="utf-8"))

    def test_skill_copy_reports_cache_fallback_when_github_source_fails(self) -> None:
        cache_skill = self.home / ".claude" / "skills" / "sample-skill" / "SKILL.md"
        cache_skill.write_text(
            (
                "---\n"
                "name: sample-skill\n"
                "description: cache skill\n"
                "repo: acme/sample-skill\n"
                "---\n\nUse cache fallback instructions.\n"
            ),
            encoding="utf-8",
        )
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["skill:sample-skill"]

        with mock.patch.object(self.module, "materialize_github_skill_source", return_value=(None, "gh failed")):
            results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["source_kind"], "claude-cache-fallback")
        self.assertIn("gh failed", results[0]["fallback_reason"])
        copied = self.home / ".codex" / "skills" / "sample-skill" / "SKILL.md"
        self.assertIn("Use cache fallback instructions.", copied.read_text(encoding="utf-8"))

    def test_safe_extract_rejects_archive_links(self) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            member = tarfile.TarInfo("unsafe-link")
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/outside"
            archive.addfile(member)

        with self.assertRaises(self.module.HandoffError) as raised:
            self.module.safe_extract_tar_bytes(payload.getvalue(), self.root / "extract")

        self.assertIn("archive link", str(raised.exception))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_skill_copy_rejects_symlinked_source_content(self) -> None:
        cache_skill = self.home / ".claude" / "skills" / "sample-skill" / "SKILL.md"
        source_skill_dir = self.root / "source-skills" / "sample-skill"
        source_skill_dir.mkdir(parents=True)
        (source_skill_dir / "SKILL.md").write_text(
            "---\nname: sample-skill\ndescription: source skill\n---\n\nUse source instructions.\n",
            encoding="utf-8",
        )
        outside = self.root / "outside-secret.txt"
        outside.write_text("do not copy\n", encoding="utf-8")
        os.symlink(outside, source_skill_dir / "linked-secret.txt")
        cache_skill.write_text(
            (
                "---\n"
                "name: sample-skill\n"
                "description: cache skill\n"
                f"source_path: {source_skill_dir}\n"
                "---\n\nUse stale cache instructions.\n"
            ),
            encoding="utf-8",
        )

        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["skill:sample-skill"]
        results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "error")
        self.assertIn("symlink", results[0]["reason"])
        self.assertFalse((self.home / ".codex" / "skills" / "sample-skill").exists())

    def test_github_codex_check_marks_native_manifest(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:demo@demo-market"
        )
        plugin["codex_release_status"] = "gh-check-needed"

        with mock.patch.object(
            self.module,
            "gh_auth_status",
            return_value={"available": True, "authenticated": True, "path": "/usr/bin/gh", "reason": ""},
        ):
            with mock.patch.object(self.module, "gh_api_path_exists", return_value=(True, "")):
                checked = self.module.annotate_github_codex_release(plugin)

        self.assertTrue(checked["github_codex_manifest_exists"])
        self.assertTrue(checked["github_codex_gh_authenticated"])
        self.assertEqual(checked["codex_release_status"], "github-native-codex-manifest")
        self.assertIn("github-origin", checked["risk_badges"])
        self.assertIn("codex-native", checked["risk_badges"])

    def test_github_codex_check_api_failure_is_reported_as_failure(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:demo@demo-market"
        )
        plugin["codex_release_status"] = "gh-check-needed"

        with mock.patch.object(
            self.module,
            "gh_auth_status",
            return_value={"available": True, "authenticated": True, "path": "/usr/bin/gh", "reason": ""},
        ):
            with mock.patch.object(self.module, "gh_api_path_exists", return_value=(False, "error connecting to api.github.com")):
                checked = self.module.annotate_github_codex_release(plugin)

        self.assertEqual(checked["codex_release_status"], "github-check-failed")
        self.assertIn("error connecting", checked["github_codex_check_error"])
        self.assertIn(
            "GitHub check failed. Keeping Claude-to-Codex bridge candidate. Reason: error connecting to api.github.com",
            self.module.github_check_status_text(checked),
        )

    def test_github_codex_check_without_gh_keeps_bridge_path(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        plugin = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:demo@demo-market"
        )
        plugin["risk_badges"] = [badge for badge in plugin["risk_badges"] if badge not in {"github-origin", "codex-native"}]
        plugin["codex_release_status"] = "gh-check-needed"

        with mock.patch.object(
            self.module,
            "gh_auth_status",
            return_value={"available": False, "authenticated": False, "reason": "gh CLI not found"},
        ):
            checked = self.module.annotate_github_codex_release(plugin)

        self.assertFalse(checked["github_codex_gh_available"])
        self.assertIn("gh CLI not found", checked["github_codex_check_error"])
        self.assertEqual(checked["codex_release_status"], "gh-check-needed")
        self.assertNotIn("github-origin", checked["risk_badges"])
        self.assertNotIn("codex-native", checked["risk_badges"])

    def test_selected_plugin_bridge_writes_codex_plugin_agent_and_registry(self) -> None:
        self.add_installed_claude_plugin_cache(include_native_codex=False)
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]

        with mock.patch.object(
            self.module,
            "install_bridged_plugin",
            return_value={
                "selector": "cc-demo@cc-bridged-plugins",
                "command": "codex plugin add cc-demo@cc-bridged-plugins",
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "installed": True,
                "reason": "",
            },
        ) as install:
            results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "ok")
        install.assert_called_once_with("cc-demo")
        self.assertTrue(results[0]["install"]["installed"])
        bridge_dir = self.home / ".codex" / "plugins" / "cc-demo"
        manifest_path = bridge_dir / ".codex-plugin" / "plugin.json"
        agent_path = self.home / ".codex" / "agents" / "cc_demo_helper.toml"
        registry_path = self.home / ".agents" / "plugins" / "marketplace.json"
        self.assertTrue((bridge_dir / "skills" / "greet" / "SKILL.md").exists())
        self.assertTrue((bridge_dir / "skills" / "demo-demo" / "SKILL.md").exists())
        self.assertTrue((bridge_dir / "scripts" / "helper.py").exists())
        self.assertFalse((bridge_dir / "commands").exists())
        bridged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(bridged_manifest["name"], "cc-demo")
        self.assertNotIn("hooks", bridged_manifest)
        self.assertEqual(bridged_manifest["x-cc-bridge"]["sourcePlugin"], "demo")
        self.assertEqual(bridged_manifest["x-cc-bridge"]["agents"], ["cc_demo_helper"])
        self.assertEqual(bridged_manifest["x-cc-bridge"]["commands"], ["demo-demo"])
        self.assertTrue(agent_path.read_text(encoding="utf-8").startswith("# x-cc-bridge: "))
        self.assertIn('name = "cc_demo_helper"', agent_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertIn("cc-demo", [item["name"] for item in registry["plugins"]])

    def test_selected_plugin_bridge_reports_partial_when_codex_install_fails(self) -> None:
        self.add_installed_claude_plugin_cache(include_native_codex=False)
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]

        with mock.patch.object(
            self.module,
            "install_bridged_plugin",
            return_value={
                "selector": "cc-demo@cc-bridged-plugins",
                "command": "codex plugin add cc-demo@cc-bridged-plugins",
                "returncode": 1,
                "stdout": "",
                "stderr": "failed",
                "installed": False,
                "reason": "failed",
            },
        ):
            results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "partial")
        self.assertEqual(results[0]["reason"], "failed")
        self.assertTrue((self.home / ".codex" / "plugins" / "cc-demo").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_selected_plugin_bridge_rejects_symlinked_source_content(self) -> None:
        plugin_dir = self.add_installed_claude_plugin_cache(include_native_codex=False)
        outside = self.root / "outside-secret.txt"
        outside.write_text("do not copy\n", encoding="utf-8")
        os.symlink(outside, plugin_dir / "scripts" / "linked-secret.txt")
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]

        with mock.patch.object(self.module, "materialize_github_source", return_value=(None, "gh failed")):
            with mock.patch.object(self.module, "install_bridged_plugin") as install:
                results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "error")
        self.assertIn("symlink", results[0]["reason"])
        self.assertFalse((self.home / ".codex" / "plugins" / "cc-demo").exists())
        install.assert_not_called()

    def test_interactive_global_picker_checks_github_origins(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]

        with mock.patch.object(
            self.module,
            "gh_auth_status",
            return_value={"available": True, "authenticated": True, "path": "/usr/bin/gh", "reason": ""},
        ):
            with mock.patch.object(self.module, "gh_api_path_exists", return_value=(False, "gh: Not Found (HTTP 404)")):
                with mock.patch.object(self.module, "read_menu_key", return_value="apply"):
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        self.module.global_picker(manifest)

        selected = manifest["selected_global_actions"][0]
        self.assertTrue(selected["github_codex_checked"])
        self.assertEqual(selected["codex_release_status"], "github-origin-checked-no-native")
        self.assertIn("github-origin", selected["risk_badges"])

    def test_selected_global_candidates_prefers_saved_github_metadata(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        saved = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:demo@demo-market"
        )
        saved["github_codex_checked"] = True
        saved["codex_release_status"] = "github-origin-checked-no-native"
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]
        manifest["selected_global_actions"] = [saved]

        selected = self.module.selected_global_candidates(manifest)

        self.assertTrue(selected[0]["github_codex_checked"])
        self.assertEqual(selected[0]["codex_release_status"], "github-origin-checked-no-native")

    def test_saved_github_native_candidate_keeps_manual_block_on_apply(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        saved = next(
            item
            for item in self.module.global_action_candidates(manifest)
            if item["id"] == "plugin:demo@demo-market"
        )
        saved["github_codex_checked"] = True
        saved["codex_release_status"] = "github-native-codex-manifest"
        saved["blocked_reason"] = "native Codex plugin metadata was found on GitHub"
        saved["risk_badges"] = list(dict.fromkeys([*saved.get("risk_badges", []), "codex-native"]))
        saved["manual_steps"] = ["install native Codex package"]
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]
        manifest["selected_global_actions"] = [saved]

        selected = self.module.selected_global_candidates(manifest)
        results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(selected[0]["codex_release_status"], "github-native-codex-manifest")
        self.assertIn("native Codex", selected[0]["blocked_reason"])
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("native Codex", results[0]["reason"])
        self.assertFalse((self.home / ".codex" / "plugins" / "cc-demo").exists())

    def test_globals_project_only_filters_global_inventory(self) -> None:
        (self.project / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "project-files": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-filesystem", str(self.project)],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project), "--project-only"])

        self.assertEqual(code, 0)
        self.assertIn("mcp:project-files", stdout.getvalue())
        self.assertNotIn("mcp:filesystem", stdout.getvalue())

    def test_globals_portable_only_filters_local_path_mcp(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", str(self.project), "--portable-only"])

        self.assertEqual(code, 0)
        self.assertIn("skill:sample-skill", stdout.getvalue())
        self.assertNotIn("mcp:filesystem", stdout.getvalue())

    def test_globals_select_records_selection_without_agents_write(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(
                [
                    "globals",
                    "select",
                    str(self.project),
                    "--select",
                    "skill:sample-skill",
                    "--yes",
                    "--ack-privacy",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("Recorded Codex-wide install selection.", stdout.getvalue())
        self.assertFalse((self.project / "AGENTS.md").exists())
        manifest = self.module.load_json(self.project / ".codex" / "handoff" / "manifest.json")
        self.assertEqual(manifest["selected_global_action_ids"], ["skill:sample-skill"])
        self.assertEqual(manifest["global_selection"]["selected_count"], 1)

    def test_globals_select_type_alias_and_json(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(
                [
                    "globals",
                    "select",
                    str(self.project),
                    "--select",
                    "skills",
                    "--include-risky",
                    "--yes",
                    "--ack-privacy",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertIn("skill:sample-skill", data["selected_global_action_ids"])

    def test_globals_select_all_excludes_global_scope_without_risky_flag(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(
                ["globals", "select", str(self.project), "--select", "all", "--yes", "--ack-privacy", "--json"]
            )

        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["selected_global_action_ids"], [])

    def test_globals_select_requires_privacy_ack(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(
                ["globals", "select", str(self.project), "--select", "skill:sample-skill", "--yes"]
            )

        self.assertEqual(code, 2)
        self.assertIn("--ack-privacy", stderr.getvalue())

    def test_globals_select_unknown_selector_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(
                ["globals", "select", str(self.project), "--select", "skill:missing", "--yes", "--ack-privacy"]
            )

        self.assertEqual(code, 2)
        self.assertIn("unknown Codex-wide install selector", stderr.getvalue())

    def test_globals_apply_without_selection_is_actionable(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["globals", "apply", str(self.project)])

        self.assertEqual(code, 1)
        self.assertIn("No selected Codex-wide installs found", stdout.getvalue())

    def test_globals_apply_yes_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["globals", "apply", str(self.project), "--yes"])

        self.assertEqual(code, 2)
        self.assertIn("--yes does not install Codex-wide changes", stderr.getvalue())

    def test_yes_apply_does_not_execute_global_changes(self) -> None:
        with mock.patch.object(self.module.subprocess, "run", wraps=self.module.subprocess.run) as run:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = self.module.main(["apply", str(self.project), "--last", "1", "--yes", "--ack-privacy"])

        self.assertEqual(code, 0)
        self.assertIn("Codex-wide MCP/plugin/skill installs were not executed", stdout.getvalue())
        calls = [" ".join(call.args[0]) for call in run.call_args_list if call.args]
        self.assertFalse(any("codex mcp add" in call for call in calls))

    def test_yes_apply_requires_privacy_ack(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["apply", str(self.project), "--last", "1", "--yes"])

        self.assertEqual(code, 2)
        self.assertIn("--ack-privacy", stderr.getvalue())

    def test_yes_apply_requires_privacy_ack_for_local_inventory_without_sessions(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["apply", str(self.project), "--last", "0", "--yes"])

        self.assertEqual(code, 2)
        self.assertIn("MCP/skill/plugin inventory", stderr.getvalue())

    def test_privacy_command_lists_written_context(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["privacy", str(self.project), "--last", "0"])

        self.assertEqual(code, 0)
        self.assertIn("AI Handoff privacy", stdout.getvalue())
        self.assertIn("global inventory candidates", stdout.getvalue())
        self.assertIn("MCP, skill, plugin", stdout.getvalue())

    def test_apply_global_yes_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["apply", str(self.project), "--apply-global", "--yes"])

        self.assertEqual(code, 2)
        self.assertIn("--yes only confirms project-local writes", stderr.getvalue())

    def test_non_tty_apply_without_yes_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["apply", str(self.project)])

        self.assertEqual(code, 2)
        self.assertIn("refusing to write in non-interactive mode without --yes", stderr.getvalue())

    def test_missing_requested_session_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["scan", str(self.project), "--sessions", "missing-session"])

        self.assertEqual(code, 2)
        self.assertIn("requested Claude session IDs were not found", stderr.getvalue())

    def test_missing_project_path_is_user_facing_error(self) -> None:
        missing = self.root / "missing"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["scan", str(missing)])

        self.assertEqual(code, 2)
        self.assertIn("project path not found", stderr.getvalue())

    def test_interactive_key_navigation_and_toggle(self) -> None:
        selection = dict(self.module.DEFAULT_SELECTION)

        action, cursor = self.module.apply_interactive_key("down", 0, selection)
        self.assertEqual(action, "continue")
        self.assertEqual(cursor, 1)

        action, cursor = self.module.apply_interactive_key("up", cursor, selection)
        self.assertEqual(action, "continue")
        self.assertEqual(cursor, 0)

        action, cursor = self.module.apply_interactive_key("toggle", cursor, selection)
        self.assertEqual(action, "sessions")
        self.assertEqual(cursor, 0)

        cursor = 1
        self.assertTrue(selection["write_agents"])
        action, cursor = self.module.apply_interactive_key("toggle", cursor, selection)
        self.assertEqual(action, "continue")
        self.assertFalse(selection["write_agents"])

        action, cursor = self.module.apply_interactive_key("toggle:5", cursor, selection)
        self.assertEqual(action, "globals")
        self.assertEqual(cursor, 4)

        action, cursor = self.module.apply_interactive_key("toggle:1", cursor, selection)
        self.assertEqual(action, "sessions")
        self.assertEqual(cursor, 0)

    def test_menu_key_normalization(self) -> None:
        self.assertEqual(self.module.normalize_menu_key("j"), "down")
        self.assertEqual(self.module.normalize_menu_key("k"), "up")
        self.assertEqual(self.module.normalize_menu_key(" "), "toggle")
        self.assertEqual(self.module.normalize_menu_key("space"), "toggle")
        self.assertEqual(self.module.normalize_menu_key(""), "apply")
        self.assertEqual(self.module.normalize_menu_key("p"), "preview")
        self.assertEqual(self.module.normalize_menu_key("/"), "filter")
        self.assertEqual(self.module.normalize_menu_key("?"), "help")
        self.assertEqual(self.module.normalize_menu_key("d"), "details")
        self.assertEqual(self.module.normalize_menu_key("\t"), "next-view")
        self.assertEqual(self.module.normalize_menu_key("A"), "select-visible")
        self.assertEqual(self.module.normalize_menu_key("u"), "clear-visible")
        self.assertEqual(self.module.normalize_menu_key("C"), "clear-all")
        self.assertEqual(self.module.normalize_menu_key("i"), "invert-visible")
        self.assertEqual(self.module.normalize_menu_key("e"), "expand")
        self.assertEqual(self.module.normalize_menu_key("s"), "skip")
        self.assertEqual(self.module.normalize_menu_key("8"), "toggle:8")

    def test_interactive_menu_marks_cursor(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        rendered = self.module.render_interactive_menu(manifest, cursor=2)

        self.assertIn("> 3. [x] Write .codex/handoff/summary.md", rendered)
        self.assertIn("[x] Claude context: 1 session selected", rendered)
        self.assertIn("[ ] Codex-wide installs: 0 selected, 0 executed", rendered)
        self.assertIn("Press g to review installs that affect every Codex project", rendered)
        self.assertIn("Space toggles/opens row", rendered)

    def test_default_tty_entry_uses_wizard_flow(self) -> None:
        with mock.patch.object(self.module.sys.stdin, "isatty", return_value=True):
            with mock.patch.object(self.module, "wizard_flow", return_value=0) as wizard:
                code = self.module.main([str(self.project)])

        self.assertEqual(code, 0)
        wizard.assert_called_once()

    def test_default_wizard_uses_recent_three_sessions_not_saved_selection(self) -> None:
        project_sessions = self.home / ".claude" / "projects" / self.module.claude_project_key(self.project)
        index_path = project_sessions / "sessions-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for number, hour in ((2, 12), (3, 13), (4, 14)):
            session_path = project_sessions / f"session-{number}.jsonl"
            session_path.write_text(
                json.dumps({"message": {"role": "user", "content": f"newer work {number}"}}) + "\n",
                encoding="utf-8",
            )
            index["entries"].append(
                {
                    "sessionId": f"session-{number}",
                    "fullPath": str(session_path),
                    "firstPrompt": f"newer work {number}",
                    "summary": f"Newer work {number}",
                    "messageCount": 1,
                    "created": f"2026-05-30T{hour}:00:00Z",
                    "modified": f"2026-05-30T{hour}:05:00Z",
                    "gitBranch": "main",
                    "projectPath": str(self.project.resolve()),
                    "isSidechain": False,
                }
            )
        index_path.write_text(json.dumps(index), encoding="utf-8")
        saved = self.module.build_manifest(str(self.project), selected_session_ids=["session-1"])
        self.module.write_manifest_artifacts(saved)

        with mock.patch.object(self.module.sys.stdin, "isatty", return_value=True):
            with mock.patch.object(self.module, "wizard_flow", return_value=0) as wizard:
                code = self.module.main([str(self.project)])

        self.assertEqual(code, 0)
        manifest = wizard.call_args.args[0]
        self.assertEqual(
            manifest["claude"]["sessions"]["selected_session_ids"],
            ["session-4", "session-3", "session-2"],
        )

    def test_wizard_claude_context_prompt_has_spacing_and_clear_copy(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        prompts = []

        def fake_answer(prompt: str, default: str = "") -> str:
            prompts.append(prompt)
            return "q"

        with mock.patch.object(self.module, "wizard_answer", side_effect=fake_answer):
            with contextlib.redirect_stdout(io.StringIO()):
                continued = self.module.wizard_review_sessions(manifest)

        self.assertFalse(continued)
        self.assertEqual(
            prompts,
            ["\nContinue, choose more conversations, skip context, or quit? [Enter/c/s/q] "],
        )

    def test_wizard_flow_requires_user_direction_choice(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        prompts = []

        def fake_answer(prompt: str, default: str = "") -> str:
            prompts.append(prompt)
            return "1"

        with mock.patch.object(self.module, "wizard_answer", side_effect=fake_answer):
            with contextlib.redirect_stdout(io.StringIO()):
                continued = self.module.wizard_select_flow(manifest)

        self.assertTrue(continued)
        self.assertEqual(manifest["flow"]["id"], "claude_to_codex")
        self.assertEqual(prompts, ["\nChoose handoff flow [1/q] "])

    def test_wizard_flow_step_clears_existing_terminal_in_static_mode(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with mock.patch.object(self.module, "wizard_answer", return_value="1"):
                with contextlib.redirect_stdout(stdout):
                    continued = self.module.wizard_select_flow(manifest)

        self.assertTrue(continued)
        rendered = stdout.getvalue()
        self.assertTrue(rendered.startswith(self.module.ANSI_CLEAR_VIEWPORT))
        self.assertIn("Flow: Handoff Direction", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)

    def test_wizard_flow_rejects_reverse_direction_for_now(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "wizard_answer", return_value="2"):
            with contextlib.redirect_stdout(stdout):
                continued = self.module.wizard_select_flow(manifest)

        self.assertFalse(continued)
        self.assertEqual(manifest["flow"]["id"], "codex_to_claude")
        self.assertIn("not implemented yet", stdout.getvalue())

    def test_wizard_claude_context_step_clears_flow_step_in_static_mode(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with mock.patch.object(self.module, "wizard_answer", return_value="q"):
                with contextlib.redirect_stdout(stdout):
                    continued = self.module.wizard_review_sessions(manifest)

        self.assertFalse(continued)
        rendered = stdout.getvalue()
        self.assertTrue(rendered.startswith(self.module.ANSI_CLEAR_VIEWPORT))
        self.assertIn("Step 1/4: Claude Context", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)
        self.assertIn("Conversation 1", rendered)
        self.assertIn("  Time: 2026-05-30T10:05:00Z", rendered)
        self.assertIn("  Description: Testing work", rendered)
        self.assertNotIn("  Summary:", rendered)
        self.assertNotIn("cleanupPeriodDays", rendered)
        self.assertNotIn("  \n", rendered)

    def test_session_bullets_uses_custom_title_without_duplicate_description(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        selected = manifest["claude"]["sessions"]["selected"][0]
        selected["custom_title"] = "Release planning"
        selected["title"] = "Release planning"
        selected["summary"] = "Release planning"

        rendered = "\n".join(self.module.session_bullets(manifest, limit=None))

        self.assertIn("Conversation 1: Release planning", rendered)
        self.assertNotIn("Description: Release planning", rendered)

    def test_session_bullets_visual_mode_adds_dividers_and_color_labels(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        first = manifest["claude"]["sessions"]["selected"][0]
        second = dict(first)
        second["session_id"] = "session-2"
        second["custom_title"] = "Follow-up"
        second["summary"] = "Follow-up work"
        manifest["claude"]["sessions"]["selected"] = [first, second]

        with mock.patch.object(self.module, "supports_color", return_value=True):
            rendered = "\n".join(self.module.session_bullets(manifest, limit=None, visual=True))

        self.assertIn(self.module.ANSI_BOLD + "Conversation 1" + self.module.ANSI_RESET, rendered)
        self.assertIn(self.module.ANSI_DIM + ("-" * 54) + self.module.ANSI_RESET, rendered)
        self.assertIn(self.module.ANSI_CYAN + "Time:" + self.module.ANSI_RESET, rendered)
        self.assertIn(self.module.ANSI_CYAN + "Description:" + self.module.ANSI_RESET, rendered)

    def test_wizard_project_files_step_clears_previous_step_in_static_mode(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with mock.patch.object(self.module, "wizard_answer", return_value="q"):
                with contextlib.redirect_stdout(stdout):
                    continued, applied = self.module.wizard_apply_project_files(manifest)

        self.assertFalse(continued)
        self.assertFalse(applied)
        rendered = stdout.getvalue()
        self.assertTrue(rendered.startswith(self.module.ANSI_CLEAR_VIEWPORT))
        self.assertIn("Step 2/4: Project Files", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)

    def test_wizard_project_files_step_queues_without_writing(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "wizard_answer", return_value="y"):
            with contextlib.redirect_stdout(stdout):
                continued, selected = self.module.wizard_apply_project_files(manifest)

        self.assertTrue(continued)
        self.assertTrue(selected)
        self.assertTrue(manifest["wizard_project_files_selected"])
        self.assertIn("Queued project-local handoff files", stdout.getvalue())
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertFalse((self.project / ".codex" / "handoff" / "manifest.json").exists())

    def test_wizard_project_files_preview_lists_json_artifacts_without_full_manifest_diff(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.module.show_project_files_preview(manifest)

        rendered = stdout.getvalue()
        self.assertIn("Step 2/4: Project Files Preview", rendered)
        self.assertIn("Human-readable diffs", rendered)
        self.assertIn("--- a/AGENTS.md", rendered)
        self.assertIn("--- a/.codex/handoff/summary.md", rendered)
        self.assertIn("Generated JSON artifacts", rendered)
        self.assertIn(".codex/handoff/manifest.json", rendered)
        self.assertIn(".codex/handoff/runs/", rendered)
        self.assertIn("Machine-readable JSON diffs are omitted", rendered)
        self.assertNotIn("--- a/.codex/handoff/manifest.json", rendered)
        self.assertNotIn("--- a/.codex/handoff/runs/", rendered)

    def test_wizard_project_files_preview_waits_before_static_redraw(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()
        answers = iter(["p", "q"])

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with mock.patch.object(self.module, "wizard_answer", side_effect=lambda *args: next(answers)):
                with mock.patch.object(self.module, "read_menu_key", return_value="done") as read_key:
                    with contextlib.redirect_stdout(stdout):
                        continued, applied = self.module.wizard_apply_project_files(manifest)

        self.assertFalse(continued)
        self.assertFalse(applied)
        read_key.assert_called_once()
        rendered = stdout.getvalue()
        self.assertIn("Step 2/4: Project Files Preview", rendered)
        self.assertIn("Press any key to return to Step 2.", rendered)

    def test_wizard_shows_selected_conversations_after_picker(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        answers = iter(["c", "q"])
        stdout = io.StringIO()

        def fake_answer(prompt: str, default: str = "") -> str:
            return next(answers)

        def fake_session_picker(picker_manifest: Dict[str, Any]) -> bool:
            base = dict(picker_manifest["claude"]["sessions"]["selected"][0])
            selected = []
            for index in range(10):
                item = dict(base)
                item["session_id"] = f"session-{index}"
                item["title"] = f"conversation {index}"
                item["custom_title"] = f"conversation {index}"
                item["modified"] = f"2026-05-30T{index:02d}:00:00Z"
                selected.append(item)
            picker_manifest["claude"]["sessions"]["selected"] = selected
            picker_manifest["claude"]["sessions"]["selected_count"] = len(selected)
            picker_manifest["claude"]["sessions"]["found_count"] = len(selected)
            picker_manifest["claude"]["sessions"]["selected_session_ids"] = [
                item["session_id"] for item in selected
            ]
            return True

        with mock.patch.object(self.module, "wizard_answer", side_effect=fake_answer):
            with mock.patch.object(self.module, "session_picker", side_effect=fake_session_picker):
                with contextlib.redirect_stdout(stdout):
                    continued = self.module.wizard_review_sessions(manifest)

        self.assertFalse(continued)
        renders = stdout.getvalue().split("Step 1/4: Claude Context")
        self.assertEqual(len(renders), 3)
        self.assertIn("conversation 0", renders[2])
        self.assertIn("conversation 9", renders[2])

    def test_wizard_global_summary_counts_candidates(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        summary = self.module.wizard_global_candidate_summary(manifest)

        self.assertGreaterEqual(summary["total"], 3)
        self.assertGreaterEqual(summary["plugins"], 1)
        self.assertGreaterEqual(summary["skills"], 1)
        self.assertGreaterEqual(summary["mcps"], 1)
        self.assertIn("used_plugins", summary)

    def test_wizard_globals_leads_with_conversation_matched_actions(self) -> None:
        self.append_transcript_usage_events()
        extra_skill = self.home / ".claude" / "skills" / "unused-skill"
        extra_skill.mkdir()
        (extra_skill / "SKILL.md").write_text(
            "---\nname: unused-skill\ndescription: unused\n---\n",
            encoding="utf-8",
        )
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "read_menu_key", return_value="skip"):
            with contextlib.redirect_stdout(stdout):
                continued = self.module.wizard_review_globals(manifest, project_applied=False)

        self.assertTrue(continued)
        self.assertIn("Step 3/4: Tooling & Claude Setup Carryover", stdout.getvalue())
        self.assertIn(f"Project: {self.project.resolve()}", stdout.getvalue())
        self.assertIn("Scanning selected Claude conversations", stdout.getvalue())
        self.assertIn("Capturing Claude hooks, rules, references, and statusline", stdout.getvalue())
        self.assertIn("possible carryover action", stdout.getvalue())
        self.assertIn("Ready:", stdout.getvalue())
        self.assertIn("Detected from selected conversations", stdout.getvalue())
        self.assertIn("Captured from Claude setup", stdout.getvalue())
        self.assertIn("Expand to full Claude setup", stdout.getvalue())
        self.assertIn("Choose Conversation-Detected Carryover", stdout.getvalue())
        self.assertIn("[x] skill:sample-skill", stdout.getvalue())
        self.assertIn("e expand full setup", stdout.getvalue())

    def test_wizard_tooling_project_only_records_matched_actions(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()
        answers = iter(["project-only"])

        with mock.patch.object(self.module, "wizard_answer", side_effect=lambda *args: next(answers)):
            with mock.patch.object(self.module, "read_menu_key", return_value="apply"):
                with contextlib.redirect_stdout(stdout):
                    continued = self.module.wizard_review_globals(manifest, project_applied=False)

        self.assertTrue(continued)
        self.assertIn("Queued selected tooling", stdout.getvalue())
        self.assertIn("skill:sample-skill", manifest["selected_global_action_ids"])
        self.assertEqual(manifest["wizard_tooling_scope"], "project-only")
        self.assertFalse(manifest["global_apply_results"])
        self.assertFalse((self.project / ".codex" / "handoff" / "manifest.json").exists())

    def test_wizard_tooling_can_pick_matched_subset_by_number(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()
        answers = iter(["project-only"])
        keys = iter(["clear-all", "toggle:1", "apply"])

        with mock.patch.object(self.module, "wizard_answer", side_effect=lambda *args: next(answers)):
            with mock.patch.object(self.module, "read_menu_key", side_effect=lambda: next(keys)):
                with contextlib.redirect_stdout(stdout):
                    continued = self.module.wizard_review_globals(manifest, project_applied=False)

        self.assertTrue(continued)
        self.assertEqual(len(manifest["selected_global_action_ids"]), 1)

    def test_wizard_tooling_reports_already_installed_bridged_plugins(self) -> None:
        self.add_installed_claude_plugin_cache()
        codex_plugin = self.home / ".codex" / "plugins" / "cc-demo"
        (codex_plugin / ".codex-plugin").mkdir(parents=True)
        (codex_plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "cc-demo", "version": "1.0.0"}),
            encoding="utf-8",
        )
        with self.session_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "attributionPlugin": "demo",
                        "message": {"role": "assistant", "content": "used demo plugin"},
                    }
                )
                + "\n"
            )

        manifest = self.module.build_manifest(str(self.project), last=1)
        candidate_ids = {item["id"] for item in self.module.global_action_candidates(manifest)}
        already_available = self.module.already_available_conversation_tools(manifest)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "wizard_answer", return_value="n"):
            with contextlib.redirect_stdout(stdout):
                continued = self.module.wizard_review_globals(manifest, project_applied=False)

        self.assertTrue(continued)
        self.assertNotIn("plugin:demo@demo-market", candidate_ids)
        self.assertIn("plugin:demo@demo-market", [item["id"] for item in already_available])
        self.assertIn("Already available for this Codex user", stdout.getvalue())
        self.assertIn("cc-demo", stdout.getvalue())

    def test_matched_tooling_picker_renders_checkboxes(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)
        candidates = self.module.conversation_matched_global_candidates(manifest)

        rendered = self.module.render_matched_tooling_picker(
            manifest,
            candidates,
            selected_ids=[],
            additional_count=2,
        )

        self.assertIn("Choose Conversation-Detected Carryover", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)
        self.assertIn("[ ]", rendered)
        self.assertIn("Expand to full Claude setup: 2 additional candidate(s) outside this picker", rendered)
        self.assertIn("Space/x toggle", rendered)

    def test_tooling_progress_static_draw_clears_viewport(self) -> None:
        stdout = io.StringIO()

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with contextlib.redirect_stdout(stdout):
                self.module.draw_tooling_progress(
                    ["Scanning selected Claude conversations..."],
                    project_path=str(self.project.resolve()),
                )

        rendered = stdout.getvalue()
        self.assertTrue(rendered.startswith(self.module.ANSI_CLEAR_VIEWPORT))
        self.assertIn("Step 3/4: Tooling & Claude Setup Carryover", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)
        self.assertIn("[info] Scanning selected Claude conversations", rendered)

    def test_github_progress_updates_current_line_without_throttled_jumps(self) -> None:
        progress_lines = ["Found 14 possible carryover action(s); 14 need GitHub/Codex compatibility checks."]
        stdout = io.StringIO()

        candidates = [
            {"origin_github_repo": "acme/one"},
            {"origin_github_repo": "acme/two"},
            {"origin_github_repo": "acme/three"},
        ]
        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with contextlib.redirect_stdout(stdout):
                for index, candidate in enumerate(candidates, start=1):
                    self.module.wizard_github_progress(
                        progress_lines,
                        index,
                        14,
                        candidate,
                        project_path=str(self.project.resolve()),
                    )

        self.assertEqual(len(progress_lines), 2)
        self.assertEqual(progress_lines[-1], "Checking GitHub/Codex compatibility 3/14: acme/three")
        rendered = stdout.getvalue()
        self.assertIn("Checking GitHub/Codex compatibility 1/14: acme/one", rendered)
        self.assertIn("Checking GitHub/Codex compatibility 2/14: acme/two", rendered)
        self.assertIn("Checking GitHub/Codex compatibility 3/14: acme/three", rendered)

    def test_github_progress_replaces_checking_line_with_finding(self) -> None:
        progress_lines = ["Found 1 possible carryover action(s); 1 need GitHub/Codex compatibility checks."]
        stdout = io.StringIO()

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with contextlib.redirect_stdout(stdout):
                self.module.wizard_github_progress(
                    progress_lines,
                    1,
                    1,
                    {"origin_github_repo": "acme/demo", "_github_progress_phase": "checking"},
                    project_path=str(self.project.resolve()),
                )
                self.module.wizard_github_progress(
                    progress_lines,
                    1,
                    1,
                    {
                        "origin_github_repo": "acme/demo",
                        "_github_progress_phase": "result",
                        "codex_release_status": "github-origin-checked-no-native",
                    },
                    project_path=str(self.project.resolve()),
                )

        self.assertEqual(len(progress_lines), 2)
        self.assertEqual(
            progress_lines[-1],
            "GitHub finding 1/1: acme/demo - no native Codex manifest found; Claude-to-Codex bridge remains the path",
        )
        self.assertNotIn("Checking GitHub/Codex compatibility", progress_lines[-1])

    def test_github_annotation_progress_emits_checking_and_result_events(self) -> None:
        events = []
        candidate = {
            "id": "plugin:demo@market",
            "origin_github_repo": "acme/demo",
            "origin_subdir": "./plugins/demo",
            "codex_release_check_urls": [
                "https://github.com/acme/demo/blob/HEAD/plugins/demo/.codex-plugin/plugin.json"
            ],
            "risk_badges": [],
            "bridge": True,
        }

        with mock.patch.object(
            self.module,
            "gh_auth_status",
            return_value={"available": True, "authenticated": True, "path": "/usr/bin/gh", "reason": ""},
        ):
            with mock.patch.object(self.module, "gh_api_path_exists", return_value=(False, "gh: Not Found (HTTP 404)")):
                self.module.annotate_github_codex_releases(
                    [candidate],
                    progress=lambda index, total, item: events.append(
                        (index, total, item.get("_github_progress_phase"), item.get("codex_release_status"))
                    ),
                )

        self.assertEqual(events[0], (1, 1, "checking", None))
        self.assertEqual(events[1], (1, 1, "result", "github-origin-checked-no-native"))

    def test_color_text_uses_ansi_only_when_supported(self) -> None:
        with mock.patch.object(self.module, "supports_color", return_value=False):
            self.assertEqual(self.module.color_text("hello", self.module.ANSI_GREEN), "hello")
        with mock.patch.object(self.module, "supports_color", return_value=True):
            self.assertEqual(
                self.module.color_text("hello", self.module.ANSI_GREEN),
                f"{self.module.ANSI_GREEN}hello{self.module.ANSI_RESET}",
            )

    def test_supports_color_can_be_forced_when_no_color_is_set(self) -> None:
        class FakeStdout:
            def isatty(self) -> bool:
                return True

        with mock.patch.object(self.module.sys, "stdout", FakeStdout()):
            with mock.patch.dict(
                self.module.os.environ,
                {"TERM": "xterm-256color", "NO_COLOR": "1", "AI_HANDOFF_COLOR": "always"},
                clear=False,
            ):
                self.assertTrue(self.module.supports_color())

    def test_supports_color_respects_no_color_by_default(self) -> None:
        class FakeStdout:
            def isatty(self) -> bool:
                return True

        with mock.patch.object(self.module.sys, "stdout", FakeStdout()):
            with mock.patch.dict(
                self.module.os.environ,
                {"TERM": "xterm-256color", "NO_COLOR": "1"},
                clear=False,
            ):
                self.module.os.environ.pop("AI_HANDOFF_COLOR", None)
                self.assertFalse(self.module.supports_color())

    def test_tooling_line_explains_bridge_when_github_has_no_native_manifest(self) -> None:
        line = self.module.tooling_candidate_line(
            {
                "id": "plugin:x@omri-cc-stuff",
                "type": "plugin",
                "bridge": True,
                "bridge_name": "cc-x",
                "codex_release_status": "github-origin-checked-no-native",
            }
        )

        self.assertIn("GitHub source checked", line)
        self.assertIn("no native Codex plugin manifest", line)

    def test_wizard_plan_review_applies_queued_project_files_and_tooling_record(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)
        selected = [
            candidate
            for candidate in self.module.global_action_candidates(manifest)
            if candidate["id"] == "skill:sample-skill"
        ]
        self.module.set_selected_global_actions(manifest, ["skill:sample-skill"], selected)
        manifest["wizard_tooling_scope"] = "project-only"
        stdout = io.StringIO()

        with mock.patch.object(self.module, "wizard_answer", return_value="y"):
            with contextlib.redirect_stdout(stdout):
                continued = self.module.wizard_review_plan_and_run(manifest, project_files_selected=True)

        self.assertTrue(continued)
        rendered = stdout.getvalue()
        self.assertIn("Step 4/4: Review & Run Plan", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)
        self.assertIn("Nothing has been written or installed yet.", rendered)
        self.assertIn("Applied project-local handoff.", rendered)
        self.assertIn("Tooling carryover recorded", rendered)
        self.assertTrue((self.project / "AGENTS.md").exists())
        written_manifest = self.module.load_json(self.project / ".codex" / "handoff" / "manifest.json")
        self.assertEqual(written_manifest["selected_global_action_ids"], ["skill:sample-skill"])

    def test_wizard_plan_review_can_quit_before_running(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "wizard_answer", return_value="q"):
            with contextlib.redirect_stdout(stdout):
                continued = self.module.wizard_review_plan_and_run(manifest, project_files_selected=True)

        self.assertFalse(continued)
        self.assertIn("Step 4/4: Review & Run Plan", stdout.getvalue())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_wizard_plan_install_user_requires_explicit_install(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)
        selected = [
            candidate
            for candidate in self.module.global_action_candidates(manifest)
            if candidate["id"] == "skill:sample-skill"
        ]
        self.module.set_selected_global_actions(manifest, ["skill:sample-skill"], selected)
        manifest["wizard_tooling_scope"] = "install-user"
        stdout = io.StringIO()
        prompts = []

        def fake_answer(prompt: str, default: str = "") -> str:
            prompts.append(prompt)
            return ""

        with mock.patch.object(self.module, "wizard_answer", side_effect=fake_answer):
            with mock.patch.object(self.module, "apply_selected_global_actions") as apply_globals:
                with contextlib.redirect_stdout(stdout):
                    continued = self.module.wizard_review_plan_and_run(manifest, project_files_selected=True)

        self.assertFalse(continued)
        apply_globals.assert_not_called()
        self.assertIn("Type install", prompts[0])
        self.assertIn("No plan executed.", stdout.getvalue())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_wizard_plan_preview_respects_skipped_project_files(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)
        selected = [
            candidate
            for candidate in self.module.global_action_candidates(manifest)
            if candidate["id"] == "skill:sample-skill"
        ]
        self.module.set_selected_global_actions(manifest, ["skill:sample-skill"], selected)
        manifest["wizard_tooling_scope"] = "project-only"
        stdout = io.StringIO()
        answers = iter(["p", "q"])

        with mock.patch.object(self.module, "wizard_answer", side_effect=lambda *args: next(answers)):
            with contextlib.redirect_stdout(stdout):
                continued = self.module.wizard_review_plan_and_run(manifest, project_files_selected=False)

        self.assertFalse(continued)
        rendered = stdout.getvalue()
        self.assertIn("--- a/.codex/handoff/manifest.json", rendered)
        self.assertNotIn("--- a/AGENTS.md", rendered)
        self.assertNotIn("--- a/.codex/handoff/summary.md", rendered)

    def test_used_bridge_candidate_group_is_conversation_matched(self) -> None:
        group = self.module.global_candidate_group(
            {
                "id": "plugin:x@omri-cc-stuff",
                "type": "plugin",
                "risk": "high",
                "confidence": "medium",
                "risk_badges": ["global-scope", "bridge"],
                "bridge": True,
                "used_in_selected_sessions": True,
            }
        )

        self.assertEqual(group, "Conversation Matched")

    def test_wizard_completion_lists_confidence_artifacts_and_installs(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["applied_actions"] = ["AGENTS.md", ".codex/handoff/summary.md"]
        manifest["selected_global_actions"] = [
            {"id": "plugin:x@omri-cc-stuff", "type": "plugin", "bridge_name": "cc-x"}
        ]
        manifest["global_apply_results"] = [{"id": "plugin:x@omri-cc-stuff", "status": "ok"}]
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.module.print_wizard_completion(manifest)

        rendered = stdout.getvalue()
        self.assertIn("Confidence:", rendered)
        self.assertIn("Done:", rendered)
        self.assertIn("Project files updated:", rendered)
        self.assertIn("AGENTS.md", rendered)
        self.assertIn("Codex-wide installs completed:", rendered)
        self.assertIn("plugin:x@omri-cc-stuff bridged as cc-x", rendered)
        self.assertIn("Pending user work:", rendered)
        self.assertIn("Review AGENTS.md", rendered)

    def test_wizard_completion_lists_failed_install_results(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_actions"] = [
            {"id": "plugin:x@omri-cc-stuff", "type": "plugin", "bridge_name": "cc-x"}
        ]
        manifest["global_apply_results"] = [
            {"id": "plugin:x@omri-cc-stuff", "status": "partial", "reason": "codex plugin add failed"}
        ]
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.module.print_wizard_completion(manifest)

        rendered = stdout.getvalue()
        self.assertIn("Codex-wide install results needing attention:", rendered)
        self.assertIn("partial (codex plugin add failed)", rendered)
        self.assertNotIn("selected but not executed", rendered)

    def test_wizard_completion_lists_recorded_and_native_pending_work(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        selected = [
            candidate
            for candidate in self.module.global_action_candidates(manifest)
            if candidate["id"] == "plugin:demo@demo-market"
        ]
        manifest["selected_global_actions"] = selected
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]
        manifest["global_apply_results"] = []
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            self.module.print_wizard_completion(manifest)

        rendered = stdout.getvalue()
        self.assertIn("Codex-wide installs recorded but not executed:", rendered)
        self.assertIn("Native Codex plugin install follow-up:", rendered)
        self.assertIn("install or enable the native Codex package", rendered)

    def test_static_menu_draw_clears_viewport_when_supported(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        stdout = io.StringIO()

        with mock.patch.object(self.module, "supports_static_menu", return_value=True):
            with contextlib.redirect_stdout(stdout):
                self.module.draw_static_menu(manifest, cursor=0)

        self.assertTrue(stdout.getvalue().startswith(self.module.ANSI_CLEAR_VIEWPORT))
        self.assertIn("> 1. [x] Claude context: 1 session selected", stdout.getvalue())

    def test_session_selection_can_be_changed(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)

        self.module.update_session_selection(manifest, [])

        self.assertEqual(manifest["claude"]["sessions"]["selection_strategy"], "user-selected conversations")
        self.assertEqual(manifest["claude"]["sessions"]["selected_count"], 0)
        self.assertEqual(manifest["claude"]["sessions"]["selected"], [])

    def test_session_picker_cancel_does_not_change_selection(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        original_ids = list(manifest["claude"]["sessions"]["selected_session_ids"])

        with mock.patch.object(self.module, "read_menu_key", side_effect=["toggle", "quit"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.module.session_picker(manifest)

        self.assertEqual(manifest["claude"]["sessions"]["selected_session_ids"], original_ids)
        self.assertEqual(manifest["claude"]["sessions"]["selected_count"], 1)

    def test_session_picker_render_filters_and_paginates(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)

        rendered = self.module.render_session_picker(
            manifest,
            cursor=0,
            selected_ids=["session-1"],
            filter_text="testing",
            page_size=1,
        )

        self.assertIn("Filter: testing", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)
        self.assertNotIn("cleanupPeriodDays", rendered)
        self.assertIn("Page: 1/1", rendered)
        self.assertIn("[x] Conversation 1", rendered)
        self.assertIn("branch main", rendered)
        self.assertIn("2 messages", rendered)
        self.assertIn("Description: Testing work", rendered)
        self.assertIn("e expand", rendered)
        self.assertIn("Enter hand off 1 selected", rendered)
        self.assertIn("--all-projects --search TEXT", rendered)

    def test_session_picker_expands_highlighted_conversation_context(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)

        rendered = self.module.render_session_picker(
            manifest,
            cursor=0,
            selected_ids=["session-1"],
            expanded_ids={"session-1"},
        )

        self.assertIn("Context:", rendered)
        self.assertIn("Session ID: session-1", rendered)
        self.assertIn("Project:", rendered)
        self.assertIn("Recent request: fix the failing tests", rendered)
        self.assertIn("Recent assistant note: Tests now pass.", rendered)
        self.assertNotIn("Commands: pytest tests/test_example.py", rendered)

    def test_session_picker_uses_custom_title_when_available(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        candidate = manifest["claude"]["sessions"]["candidates"][0]
        candidate["custom_title"] = "Release planning"
        candidate["title"] = "Release planning"
        candidate["summary"] = "Release planning"

        rendered = self.module.render_session_picker(manifest, cursor=0, selected_ids=["session-1"])

        self.assertIn("[x] Release planning", rendered)
        self.assertNotIn("Description: Release planning", rendered)

    def test_session_picker_empty_state_mentions_cleanup_period(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["claude"]["sessions"]["candidates"] = []
        manifest["claude"]["sessions"]["found_count"] = 0

        rendered = self.module.render_session_picker(manifest)

        self.assertIn("No Claude conversations found", rendered)
        self.assertIn("cleanupPeriodDays", rendered)

    def test_global_picker_render_filters_and_paginates(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)

        rendered = self.module.render_global_picker(
            manifest,
            cursor=0,
            selected_ids=["plugin:experimental@marketplace"],
            filter_text="experimental",
            mode="plugin",
        )

        self.assertIn("View: plugin", rendered)
        self.assertIn(f"Project: {self.project.resolve()}", rendered)
        self.assertIn("Filter: experimental", rendered)
        self.assertIn("[x] plugin:experimental@marketplace", rendered)
        self.assertIn("Manual / Unsafe:", rendered)

    def test_global_picker_renders_marketplace_badge_for_marketplace_plugins(self) -> None:
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)

        rendered = self.module.render_global_picker(manifest, cursor=0, filter_text="demo-market", mode="plugin")

        self.assertIn("plugin:demo@demo-market", rendered)
        self.assertIn("marketplace", rendered)
        self.assertIn("git-source", rendered)

    def test_global_picker_used_mode_only_shows_transcript_used_candidates(self) -> None:
        self.append_transcript_usage_events()
        manifest = self.module.build_manifest(str(self.project), last=1)

        self.assertEqual(self.module.initial_global_picker_mode(manifest), "used")
        rendered = self.module.render_global_picker(manifest, cursor=0, mode="used")

        self.assertIn("View: used", rendered)
        self.assertIn("Used: 3", rendered)
        self.assertIn("mcp:filesystem", rendered)
        self.assertIn("skill:sample-skill", rendered)
        self.assertIn("plugin:experimental@marketplace", rendered)
        self.assertIn("used-in-transcripts", rendered)
        self.assertNotIn("skill:ada", rendered)

    def test_global_picker_cancel_does_not_change_selection(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = []

        with mock.patch.object(self.module, "read_menu_key", side_effect=["toggle", "quit"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.module.global_picker(manifest)

        self.assertEqual(manifest["selected_global_action_ids"], [])
        self.assertFalse(manifest.get("selected_global_actions"))

    def test_global_picker_empty_filter_state_is_actionable(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)

        rendered = self.module.render_global_picker(manifest, cursor=0, filter_text="does-not-match", mode="plugin")

        self.assertIn("No Codex-wide install candidates match this filter/view.", rendered)
        self.assertIn("Clear the filter", rendered)

    def test_global_picker_key_navigation(self) -> None:
        action, cursor = self.module.apply_global_picker_key("page-down", 0, 25, page_size=10)
        self.assertEqual(action, "continue")
        self.assertEqual(cursor, 10)

        action, cursor = self.module.apply_global_picker_key("page-up", cursor, 25, page_size=10)
        self.assertEqual(action, "continue")
        self.assertEqual(cursor, 0)

        action, cursor = self.module.apply_global_picker_key("details", cursor, 25, page_size=10)
        self.assertEqual(action, "details")

        action, cursor = self.module.apply_global_picker_key("clear-all", cursor, 0, page_size=10)
        self.assertEqual(action, "clear-all")

    def test_global_picker_page_size_uses_terminal_height(self) -> None:
        with mock.patch.object(self.module.sys.stdout, "isatty", return_value=True):
            with mock.patch.object(self.module.shutil, "get_terminal_size", return_value=os.terminal_size((100, 18))):
                self.assertEqual(self.module.global_picker_page_size(), 5)

    def test_current_global_page_candidates_uses_cursor_page(self) -> None:
        candidates = [{"id": f"skill:{index}"} for index in range(25)]

        page = self.module.current_global_page_candidates(candidates, cursor=12, page_size=10)

        self.assertEqual([item["id"] for item in page], [f"skill:{index}" for index in range(10, 20)])

    def test_global_picker_details_include_risk_and_evidence(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        candidate = next(item for item in self.module.global_action_candidates(manifest) if item["id"] == "mcp:filesystem")

        details = self.module.render_global_candidate_details(candidate)

        self.assertIn("Codex-Wide Install Details", details)
        self.assertIn("mcp:filesystem", details)
        self.assertIn("codex-wide", details)
        self.assertIn("review filesystem scope", details)

    def test_select_visible_global_candidates_skips_risky_by_default(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        candidates = self.module.global_action_candidates(manifest)

        selected, skipped = self.module.select_visible_global_candidates([], candidates)

        self.assertEqual(selected, [])
        self.assertGreater(skipped, 0)

    def test_select_visible_global_candidates_allows_risky_when_explicit(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        candidates = self.module.visible_global_candidates(manifest, filter_text="sample-skill", mode="skill")

        selected, skipped = self.module.select_visible_global_candidates([], candidates, include_risky=True)

        self.assertEqual(skipped, 0)
        self.assertIn("skill:sample-skill", selected)

    def test_clear_and_invert_visible_global_candidates(self) -> None:
        safe = {"id": "skill:project", "type": "skill", "source_scope": "project", "portable": True, "risk_badges": []}
        risky = {"id": "skill:global", "type": "skill", "source_scope": "global", "risk_badges": ["global-scope"]}

        cleared = self.module.clear_visible_global_candidates(["skill:project", "skill:global", "skill:other"], [safe, risky])
        self.assertEqual(cleared, ["skill:other"])

        selected, skipped = self.module.invert_visible_global_candidates([], [safe, risky])
        self.assertEqual(selected, ["skill:project"])
        self.assertEqual(skipped, 1)

        selected, skipped = self.module.invert_visible_global_candidates(["skill:project"], [safe, risky])
        self.assertEqual(selected, [])
        self.assertEqual(skipped, 1)

    def test_selected_global_skill_copy(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["skill:sample-skill"]

        results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "ok")
        self.assertTrue((self.home / ".codex" / "skills" / "sample-skill" / "SKILL.md").exists())

    def test_low_confidence_global_import_is_manual_followup(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_actions"] = [
            {
                "id": "plugin:test",
                "type": "plugin",
                "confidence": "low",
                "command": "codex plugin add maybe@unknown",
                "label": "codex plugin add maybe@unknown",
            }
        ]

        results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("low-confidence", results[0]["reason"])

    def test_help_alias_after_path_normalizes(self) -> None:
        self.assertEqual(
            self.module.normalize_argv([str(self.project), "help"]),
            ["_default", str(self.project), "--help"],
        )

    def test_main_handles_keyboard_interrupt_gracefully(self) -> None:
        stderr = io.StringIO()

        with mock.patch.object(self.module, "command_scan", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stderr(stderr):
                code = self.module.main([str(self.project)])

        self.assertEqual(code, 130)
        self.assertIn("Interrupted by user.", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
