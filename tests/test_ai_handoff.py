from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
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

    def add_installed_claude_plugin_cache(self) -> Path:
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
        self.assertIn("session-1", stdout.getvalue())
        self.assertIn("--sessions session-1", stdout.getvalue())

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
        self.assertEqual(plugin["bridge_source_path"], str(plugin_dir))
        self.assertEqual(plugin["blocked_reason"], "")
        self.assertIn("bridge", plugin["risk_badges"])
        self.assertIn("codex-native", plugin["risk_badges"])
        self.assertIn("local Claude plugin cache", plugin["evidence"])
        self.assertEqual(plugin["origin_github_repo"], "acme/demo-market")
        self.assertEqual(plugin["codex_release_status"], "native-codex-source")
        self.assertIn("plugins/demo/.codex-plugin/plugin.json", plugin["codex_release_check_urls"][0])

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
        self.add_installed_claude_plugin_cache()
        manifest = self.module.build_manifest(str(self.project), last=1)
        manifest["selected_global_action_ids"] = ["plugin:demo@demo-market"]

        results = self.module.apply_selected_global_actions(manifest)

        self.assertEqual(results[0]["status"], "ok")
        bridge_dir = self.home / ".codex" / "plugins" / "cc-demo"
        manifest_path = bridge_dir / ".codex-plugin" / "plugin.json"
        agent_path = self.home / ".codex" / "agents" / "cc_demo_helper.toml"
        registry_path = self.home / ".agents" / "plugins" / "marketplace.json"
        self.assertTrue((bridge_dir / "skills" / "greet" / "SKILL.md").exists())
        self.assertTrue((bridge_dir / "scripts" / "helper.py").exists())
        self.assertFalse((bridge_dir / "commands").exists())
        bridged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(bridged_manifest["name"], "cc-demo")
        self.assertNotIn("hooks", bridged_manifest)
        self.assertEqual(bridged_manifest["x-cc-bridge"]["sourcePlugin"], "demo")
        self.assertEqual(bridged_manifest["x-cc-bridge"]["agents"], ["cc_demo_helper"])
        self.assertTrue(agent_path.read_text(encoding="utf-8").startswith("# x-cc-bridge: "))
        self.assertIn('name = "cc_demo_helper"', agent_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertIn("cc-demo", [item["name"] for item in registry["plugins"]])

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
        self.assertEqual(self.module.normalize_menu_key("8"), "toggle:8")

    def test_interactive_menu_marks_cursor(self) -> None:
        manifest = self.module.build_manifest(str(self.project), last=1)
        rendered = self.module.render_interactive_menu(manifest, cursor=2)

        self.assertIn("> 3. [x] Write .codex/handoff/summary.md", rendered)
        self.assertIn("[x] Claude context: 1 session selected", rendered)
        self.assertIn("[ ] Codex-wide installs: 0 selected, 0 executed", rendered)
        self.assertIn("Press g to review installs that affect every Codex project", rendered)
        self.assertIn("Space toggles/opens row", rendered)

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
        self.assertIn("Page: 1/1", rendered)
        self.assertIn("[x] Testing work", rendered)
        self.assertIn("--all-projects --search TEXT", rendered)

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
        self.assertIn("Filter: experimental", rendered)
        self.assertIn("[x] plugin:experimental@marketplace", rendered)
        self.assertIn("Manual / Unsafe:", rendered)

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


if __name__ == "__main__":
    unittest.main()
