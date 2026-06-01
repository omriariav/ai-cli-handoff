#!/usr/bin/env python3
"""Prepare a Claude Code project for Codex handoff.

This script is intentionally dependency-free so a Codex skill can run it
directly from its bundled resources.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
import termios
import textwrap
import tempfile
import tty
import uuid
from pathlib import Path
from urllib.parse import quote
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import tomllib  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]


VERSION = "0.1.0"
MANAGED_START = "<!-- ai-handoff:start -->"
MANAGED_END = "<!-- ai-handoff:end -->"
DEFAULT_SELECTION = {
    "summarize_sessions": True,
    "write_agents": True,
    "write_manifest": True,
    "write_summary": True,
    "propose_mcps": False,
    "propose_skill_conversions": False,
    "propose_plugin_installs": False,
    "include_transcript_excerpts": False,
}
GLOBAL_TYPE_ALIASES = {
    "mcp": "mcp",
    "mcps": "mcp",
    "skill": "skill",
    "skills": "skill",
    "plugin": "plugin",
    "plugins": "plugin",
}
BRIDGE_MARKER_KEY = "x-cc-bridge"
BRIDGE_REGISTRY_NAME = "cc-bridged-plugins"
BRIDGE_REGISTRY_DISPLAY_NAME = "CC Bridged Plugins"
BRIDGE_TOOL_ID = f"ai-handoff/{VERSION}"
BRIDGE_CC_ONLY_MANIFEST_KEYS = {"hooks", "commands", "agents"}
BRIDGE_CC_ONLY_DIR_NAMES = {".claude-plugin", ".codex-plugin", "hooks", "commands", "agents"}
BRIDGE_COPY_IGNORE_NAMES = {"__pycache__", ".git", ".pytest_cache", ".venv", "node_modules", ".DS_Store", ".in_use"}
BRIDGE_AGENT_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
PROJECT_GUIDANCE_NAMES = (
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    "README.markdown",
    "README.txt",
    ".mcp.json",
)
SECRET_FIELD_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|client_secret|access_token|refresh_token|bearer)"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN)[A-Z0-9_]*)"
    r"\s*[:=]\s*['\"]?([^'\"\s,}]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class HandoffError(Exception):
    """User-facing handoff error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def home_dir() -> Path:
    override = os.environ.get("AI_HANDOFF_HOME")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("HOME")
    if home:
        return Path(home).expanduser()
    return Path.home()


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def compact(text: str, limit: int = 240) -> str:
    return truncate(" ".join(str(text or "").split()), limit)


def read_text(path: Path, limit: int = 12000) -> Optional[str]:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return data[:limit]


def load_json(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def split_toml_section(section: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    in_quote = False
    quote_char = ""
    for char in section:
        if char in {'"', "'"}:
            if not in_quote:
                in_quote = True
                quote_char = char
                continue
            if quote_char == char:
                in_quote = False
                quote_char = ""
                continue
        if char == "." and not in_quote:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [part for part in parts if part]


def parse_toml_scalar(value: str) -> Any:
    value = value.strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def load_simple_toml(path: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    current = data
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            parts = split_toml_section(line.strip("[]"))
            current = data
            for part in parts:
                current = current.setdefault(part, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = parse_toml_scalar(value)
    return data


def load_toml(path: Path) -> Dict[str, Any]:
    if tomllib is None:
        return load_simple_toml(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError:
        return {}
    except Exception:
        return {}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_FIELD_RE.search(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    text = BEARER_RE.sub("Bearer <redacted>", value)
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text


def quote_cmd(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part) != "")


def action_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def stable_id_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.@-]+", "-", value.strip()).strip("-").lower()
    return cleaned or fallback


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(parent.expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False


def string_mentions_project(value: str, project: Path) -> bool:
    project_text = str(project)
    return bool(project_text and project_text in value)


def resolve_project_path(project_input: str) -> Path:
    project = Path(project_input).expanduser().resolve()
    if not project.exists() or not project.is_dir():
        raise HandoffError(f"project path not found: {project}")
    return project


def run_git(path: Path, args: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def claude_project_key(path: Path) -> str:
    resolved = str(path.expanduser().resolve())
    return re.sub(r"[^A-Za-z0-9_-]+", "-", resolved)


def parse_duration(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    match = re.fullmatch(r"(\d+)([dhmw])", value.strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    delta_map = {
        "d": dt.timedelta(days=amount),
        "h": dt.timedelta(hours=amount),
        "m": dt.timedelta(minutes=amount),
        "w": dt.timedelta(weeks=amount),
    }
    return dt.datetime.now(dt.timezone.utc) - delta_map[unit]


def parse_time(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp = stamp / 1000.0
        return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return None


def package_scripts(project: Path) -> Dict[str, str]:
    package = load_json(project / "package.json")
    if not isinstance(package, dict):
        return {}
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def pyproject_commands(project: Path) -> List[str]:
    data = load_toml(project / "pyproject.toml")
    commands: List[str] = []
    project_table = data.get("project") if isinstance(data, dict) else None
    if isinstance(project_table, dict):
        scripts = project_table.get("scripts")
        if isinstance(scripts, dict):
            commands.extend(f"{name}: {target}" for name, target in scripts.items())
    tool = data.get("tool") if isinstance(data, dict) else None
    if isinstance(tool, dict):
        if "pytest" in tool:
            commands.append("pytest configured")
        if "ruff" in tool:
            commands.append("ruff configured")
    return commands


def top_level_files(project: Path) -> List[str]:
    try:
        entries = sorted(project.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    names = []
    for entry in entries[:80]:
        name = entry.name + ("/" if entry.is_dir() else "")
        if name in {".git/"}:
            continue
        names.append(name)
    return names


def extract_headings(text: Optional[str], limit: int = 12) -> List[str]:
    if not text:
        return []
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            headings.append(truncate(stripped.lstrip("#").strip(), 120))
        if len(headings) >= limit:
            break
    return headings


def discover_project(project: Path) -> Dict[str, Any]:
    guidance = []
    for name in PROJECT_GUIDANCE_NAMES:
        candidate = project / name
        if candidate.exists():
            guidance.append(
                {
                    "path": name,
                    "bytes": candidate.stat().st_size,
                    "headings": extract_headings(read_text(candidate, 20000)),
                }
            )
    claude_md = read_text(project / "CLAUDE.md", 30000)
    agents_md = read_text(project / "AGENTS.md", 30000)
    return {
        "path": str(project),
        "exists": project.exists(),
        "guidance_files": guidance,
        "has_claude_md": (project / "CLAUDE.md").exists(),
        "has_agents_md": (project / "AGENTS.md").exists(),
        "claude_md_excerpt": truncate(redact(claude_md or ""), 4000) if claude_md else "",
        "agents_md_has_managed_section": bool(agents_md and MANAGED_START in agents_md and MANAGED_END in agents_md),
        "package_scripts": package_scripts(project),
        "pyproject_commands": pyproject_commands(project),
        "top_level_files": top_level_files(project),
        "git": {
            "branch": run_git(project, ["branch", "--show-current"]),
            "status_short": run_git(project, ["status", "--short"]),
            "recent_commits": run_git(project, ["log", "--oneline", "-5"]),
        },
    }


def content_to_text(content: Any) -> Tuple[str, List[str]]:
    texts: List[str] = []
    commands: List[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif item_type == "tool_use":
                name = str(item.get("name", "tool"))
                tool_input = item.get("input")
                if name.lower() in {"bash", "shell"} and isinstance(tool_input, dict):
                    command = tool_input.get("command") or tool_input.get("cmd")
                    if command:
                        commands.append(str(command))
                else:
                    texts.append(f"Tool used: {name}")
            elif isinstance(item.get("content"), str):
                texts.append(item["content"])
    elif isinstance(content, dict):
        if isinstance(content.get("text"), str):
            texts.append(content["text"])
        if isinstance(content.get("command"), str):
            commands.append(content["command"])
    return redact("\n".join(texts)), [redact(command) for command in commands]


USAGE_KINDS = ("tools", "mcp_servers", "skills", "plugins")


def add_usage(
    usage: Dict[str, Dict[str, Dict[str, Any]]],
    kind: str,
    name: Any,
    evidence: str,
    *,
    count: int = 1,
) -> None:
    if kind not in USAGE_KINDS:
        return
    clean_name = truncate(redact(str(name or "").strip()), 140)
    if not clean_name:
        return
    bucket = usage.setdefault(kind, {})
    item = bucket.setdefault(clean_name, {"name": clean_name, "count": 0, "evidence": []})
    item["count"] += max(0, count)
    clean_evidence = truncate(redact(evidence), 220)
    if clean_evidence and clean_evidence not in item["evidence"] and len(item["evidence"]) < 3:
        item["evidence"].append(clean_evidence)


def empty_usage_map() -> Dict[str, Dict[str, Dict[str, Any]]]:
    return {kind: {} for kind in USAGE_KINDS}


def finalize_usage(usage: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    finalized: Dict[str, List[Dict[str, Any]]] = {}
    for kind in USAGE_KINDS:
        finalized[kind] = sorted(
            usage.get(kind, {}).values(),
            key=lambda item: (-int(item.get("count") or 0), str(item.get("name") or "").lower()),
        )
    return finalized


def merge_finalized_usage(
    usage: Dict[str, Dict[str, Dict[str, Any]]],
    finalized: Dict[str, List[Dict[str, Any]]],
) -> None:
    for kind in USAGE_KINDS:
        for item in finalized.get(kind) or []:
            evidence_items = item.get("evidence") or []
            evidence = str(evidence_items[0]) if evidence_items else "selected Claude transcript"
            add_usage(usage, kind, item.get("name"), evidence, count=int(item.get("count") or 1))
            for extra in evidence_items[1:3]:
                add_usage(usage, kind, item.get("name"), str(extra), count=0)


def parse_skill_metadata_text(text: str, usage: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    if "<skill-format>true" in text:
        for command_name in re.findall(r"<command-name>([^<]+)</command-name>", text):
            add_usage(usage, "skills", command_name, "Claude skill metadata loaded in transcript")
    for marketplace, plugin, skill in re.findall(
        r"\.claude/plugins/cache/([^/\s]+)/([^/\s]+)/[^/\s]+/skills/([^/\s]+)",
        text,
    ):
        add_usage(usage, "plugins", plugin, f"Claude plugin skill path: {marketplace}/{plugin}")
        add_usage(usage, "plugins", f"{plugin}@{marketplace}", f"Claude plugin skill path: {marketplace}/{plugin}")
        add_usage(usage, "skills", skill, f"Claude plugin skill path: {plugin}/{skill}")
        add_usage(usage, "skills", f"{plugin}:{skill}", f"Claude plugin skill path: {plugin}/{skill}")


def extract_usage_from_tool_use(item: Dict[str, Any], usage: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    name = str(item.get("name") or "")
    if not name:
        return
    add_usage(usage, "tools", name, "Claude tool_use entry")
    mcp_match = re.match(r"mcp__([^_][^_]*)__", name)
    if mcp_match:
        add_usage(usage, "mcp_servers", mcp_match.group(1), f"Claude MCP tool used: {name}")
    tool_input = item.get("input")
    if name.lower() == "skill" and isinstance(tool_input, dict):
        skill_name = tool_input.get("skill") or tool_input.get("name") or tool_input.get("command")
        if skill_name:
            add_usage(usage, "skills", skill_name, "Claude Skill tool invocation")
            if ":" in str(skill_name):
                plugin, _, short_name = str(skill_name).partition(":")
                add_usage(usage, "plugins", plugin, f"Claude Skill tool invocation: {skill_name}")
                if short_name:
                    add_usage(usage, "skills", short_name, f"Claude Skill tool invocation: {skill_name}")


def extract_usage_from_content(content: Any, usage: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    if isinstance(content, str):
        parse_skill_metadata_text(content, usage)
        return
    if isinstance(content, list):
        for item in content:
            extract_usage_from_content(item, usage)
        return
    if not isinstance(content, dict):
        return
    item_type = content.get("type")
    if item_type == "tool_result":
        return
    if item_type == "tool_use":
        extract_usage_from_tool_use(content, usage)
    for key in ("text", "content"):
        value = content.get(key)
        if isinstance(value, str):
            parse_skill_metadata_text(value, usage)
        elif isinstance(value, list):
            extract_usage_from_content(value, usage)


def extract_usage_from_record(obj: Dict[str, Any], usage: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    if obj.get("isInitial"):
        return
    if obj.get("attributionSkill"):
        add_usage(usage, "skills", obj.get("attributionSkill"), "Claude attributionSkill field")
        if ":" in str(obj.get("attributionSkill")):
            plugin, _, skill = str(obj.get("attributionSkill")).partition(":")
            add_usage(usage, "plugins", plugin, f"Claude attributionSkill field: {obj.get('attributionSkill')}")
            if skill:
                add_usage(usage, "skills", skill, f"Claude attributionSkill field: {obj.get('attributionSkill')}")
    if obj.get("attributionPlugin"):
        add_usage(usage, "plugins", obj.get("attributionPlugin"), "Claude attributionPlugin field")
    message = obj.get("message") if isinstance(obj.get("message"), dict) else None
    if message:
        extract_usage_from_content(message.get("content"), usage)
    else:
        extract_usage_from_content(obj.get("content") or obj.get("attachment"), usage)


def summarize_transcript(path: Path, include_excerpts: bool = False, max_bytes: int = 2_000_000) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "line_count": 0,
        "user_prompts": [],
        "assistant_notes": [],
        "commands": [],
        "usage": finalize_usage(empty_usage_map()),
        "excerpt": "",
        "skipped_reason": None,
    }
    try:
        size = path.stat().st_size
    except OSError:
        summary["skipped_reason"] = "missing"
        return summary
    if size > max_bytes:
        summary["skipped_reason"] = f"larger than {max_bytes} bytes"
    excerpt_parts: List[str] = []
    usage = empty_usage_map()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                summary["line_count"] += 1
                if summary["line_count"] > 1200:
                    summary["skipped_reason"] = "line cap reached"
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    extract_usage_from_record(obj, usage)
                message = obj.get("message") if isinstance(obj, dict) else None
                role = None
                content = None
                if isinstance(message, dict):
                    role = message.get("role")
                    content = message.get("content")
                if role is None:
                    role = obj.get("type")
                    content = obj.get("content") or obj.get("attachment")
                text, commands = content_to_text(content)
                if commands:
                    summary["commands"].extend(truncate(command, 220) for command in commands[:20])
                if not text:
                    continue
                if role == "user":
                    summary["user_prompts"].append(truncate(text, 260))
                    if include_excerpts:
                        excerpt_parts.append("User: " + truncate(text, 900))
                elif role == "assistant":
                    summary["assistant_notes"].append(truncate(text, 260))
                    if include_excerpts:
                        excerpt_parts.append("Assistant: " + truncate(text, 900))
    except OSError:
        summary["skipped_reason"] = "read failed"
    summary["user_prompts"] = summary["user_prompts"][-5:]
    summary["assistant_notes"] = summary["assistant_notes"][-3:]
    summary["commands"] = list(dict.fromkeys(summary["commands"]))[:20]
    summary["usage"] = finalize_usage(usage)
    summary["excerpt"] = truncate("\n\n".join(excerpt_parts[-8:]), 6000)
    return summary


def aggregate_session_usage(sessions: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    usage = empty_usage_map()
    for session in sessions:
        transcript = session.get("transcript") if isinstance(session, dict) else None
        if isinstance(transcript, dict) and isinstance(transcript.get("usage"), dict):
            merge_finalized_usage(usage, transcript["usage"])
    return finalize_usage(usage)


def usage_kind_count(usage_summary: Dict[str, List[Dict[str, Any]]], kind: str) -> int:
    return len(usage_summary.get(kind) or [])


def usage_kind_names(usage_summary: Dict[str, List[Dict[str, Any]]], kind: str, limit: int = 4) -> str:
    names = [str(item.get("name")) for item in usage_summary.get(kind) or [] if item.get("name")]
    if not names:
        return "none"
    shown = names[:limit]
    suffix = f", +{len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(shown) + suffix


def display_risk_badges(candidate: Dict[str, Any]) -> str:
    return ",".join("codex-wide" if badge == "global-scope" else str(badge) for badge in candidate.get("risk_badges") or [])


def plugin_selector_parts(selector: str) -> Tuple[str, str]:
    if "@" not in selector:
        return selector, ""
    plugin, marketplace = selector.split("@", 1)
    return plugin, marketplace


def plugin_install_records(value: Any, project: Path) -> List[Dict[str, Any]]:
    records = value if isinstance(value, list) else [value]
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            item = {}
        path = str(item.get("installPath") or "")
        project_path = str(item.get("projectPath") or "")
        scope = str(item.get("scope") or "")
        score = 0
        if path:
            score += 10
        if scope == "user":
            score += 6
        if project_path and Path(project_path).expanduser().resolve() == project.resolve():
            score += 12
        normalized.append(
            {
                "index": index,
                "scope": scope,
                "project_path": project_path,
                "install_path": path,
                "version": str(item.get("version") or ""),
                "git_commit_sha": str(item.get("gitCommitSha") or ""),
                "installed_at": str(item.get("installedAt") or ""),
                "last_updated": str(item.get("lastUpdated") or ""),
                "score": score,
            }
        )
    normalized.sort(key=lambda item: (item["score"], item["index"]), reverse=True)
    return normalized


def load_plugin_manifest(plugin_dir: Path) -> Optional[Dict[str, Any]]:
    for relative in [Path(".codex-plugin") / "plugin.json", Path(".claude-plugin") / "plugin.json"]:
        manifest = load_json(plugin_dir / relative)
        if isinstance(manifest, dict):
            return manifest
    return None


def git_origin_from_config(root: Path) -> str:
    config = root / ".git" / "config"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_origin = False
    first_url = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_origin = 'remote "origin"' in line
            continue
        if line.startswith("url ="):
            url = line.split("=", 1)[1].strip()
            if not first_url:
                first_url = url
            if in_origin:
                return url
    return first_url


def known_marketplaces() -> Dict[str, Any]:
    data = load_json(home_dir() / ".claude" / "plugins" / "known_marketplaces.json")
    return data if isinstance(data, dict) else {}


def marketplace_root(marketplace: str) -> Optional[Path]:
    if not marketplace:
        return None
    known = known_marketplaces().get(marketplace)
    if isinstance(known, dict) and known.get("installLocation"):
        return Path(str(known["installLocation"])).expanduser()
    path = home_dir() / ".claude" / "plugins" / "marketplaces" / marketplace
    return path if path.exists() else None


def marketplace_source_url(marketplace: str) -> str:
    known = known_marketplaces().get(marketplace)
    if not isinstance(known, dict):
        return ""
    source = known.get("source")
    if not isinstance(source, dict):
        return ""
    if source.get("repo"):
        return f"https://github.com/{source['repo']}.git"
    return str(source.get("url") or source.get("path") or "")


def marketplace_plugin_entry(marketplace: str, plugin_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[Path]]:
    root = marketplace_root(marketplace)
    if not root:
        return None, None
    data = load_json(root / ".claude-plugin" / "marketplace.json")
    if not isinstance(data, dict):
        return None, root
    for entry in data.get("plugins") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "") == plugin_name:
            return entry, root
    return None, root


def source_path_from_entry(root: Optional[Path], source: Any, plugin_name: str) -> Optional[Path]:
    if not root:
        return None
    rel = ""
    if isinstance(source, str):
        rel = source
    elif isinstance(source, dict):
        rel = str(source.get("path") or "")
    if not rel and plugin_name:
        rel = f"./plugins/{plugin_name}"
    if not rel or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", rel):
        return None
    return (root / rel).expanduser().resolve()


def source_url_from_entry(source: Any) -> str:
    if isinstance(source, dict):
        return str(source.get("url") or source.get("repo") or "")
    if isinstance(source, str) and re.match(r"^(git@|https?://|ssh://|git://)", source):
        return source
    return ""


def github_repo_from_url(url: str) -> str:
    if not url:
        return ""
    cleaned = url.strip()
    patterns = [
        r"^https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:[/#?].*)?$",
        r"^git@github\.com:([^/\s]+)/([^/\s#?]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", cleaned):
        return cleaned
    return ""


def github_repo_url(repo: str) -> str:
    return f"https://github.com/{repo}" if repo else ""


def github_codex_check_urls(repo: str, ref: str = "", subdir: str = "") -> List[str]:
    if not repo:
        return []
    safe_ref = ref or "HEAD"
    clean_subdir = subdir.strip("/")
    if clean_subdir.startswith("./"):
        clean_subdir = clean_subdir[2:]
    prefix = f"{github_repo_url(repo)}/blob/{safe_ref}"
    if clean_subdir:
        prefix += f"/{clean_subdir}"
    return [
        f"{prefix}/.codex-plugin/plugin.json",
        f"{github_repo_url(repo)}/releases",
    ]


def github_blob_to_raw_url(url: str) -> str:
    match = re.match(r"^https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.*)$", url)
    if not match:
        return ""
    return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/{match.group(3)}"


def gh_auth_status() -> Dict[str, Any]:
    gh_path = shutil.which("gh")
    if not gh_path:
        return {"available": False, "authenticated": False, "reason": "gh CLI not found"}
    try:
        proc = subprocess.run(
            [gh_path, "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )
    except Exception as exc:
        return {"available": True, "authenticated": False, "path": gh_path, "reason": str(exc)}
    output = (proc.stderr or proc.stdout or "").strip()
    return {
        "available": True,
        "authenticated": proc.returncode == 0,
        "path": gh_path,
        "reason": "" if proc.returncode == 0 else compact(output, 240) or "gh auth status failed",
    }


def gh_api_path_exists(repo: str, path: str, ref: str = "HEAD") -> Tuple[bool, str]:
    gh_path = shutil.which("gh") or "gh"
    api_path = f"repos/{repo}/contents/{path}?ref={quote(ref or 'HEAD', safe='')}"
    try:
        proc = subprocess.run(
            [gh_path, "api", "--method", "GET", api_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    return False, compact(proc.stderr or proc.stdout, 240) or "gh api failed"


def github_api_not_found(error: str) -> bool:
    return "HTTP 404" in error or "Not Found" in error


def annotate_github_codex_release(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not candidate.get("origin_github_repo"):
        return candidate
    urls = list(candidate.get("codex_release_check_urls") or [])
    manifest_url = next((url for url in urls if "/.codex-plugin/plugin.json" in url), "")
    raw_manifest = github_blob_to_raw_url(manifest_url)
    candidate["github_codex_checked"] = True
    candidate["github_codex_manifest_url"] = manifest_url
    candidate["github_codex_manifest_raw_url"] = raw_manifest
    auth = gh_auth_status()
    candidate["github_codex_gh_available"] = bool(auth.get("available"))
    candidate["github_codex_gh_authenticated"] = bool(auth.get("authenticated"))
    if not auth.get("available") or not auth.get("authenticated"):
        candidate["github_codex_check_error"] = str(auth.get("reason") or "gh CLI is not authenticated")
        return candidate
    if not raw_manifest:
        candidate["github_codex_check_error"] = "missing GitHub manifest URL"
        return candidate
    subdir = str(candidate.get("origin_subdir") or "").strip("/")
    if subdir.startswith("./"):
        subdir = subdir[2:]
    manifest_path = f"{subdir}/.codex-plugin/plugin.json" if subdir else ".codex-plugin/plugin.json"
    exists, error = gh_api_path_exists(
        str(candidate.get("origin_github_repo") or ""),
        manifest_path,
        str(candidate.get("origin_ref") or candidate.get("origin_sha") or "HEAD"),
    )
    candidate["github_codex_manifest_exists"] = exists
    badges = list(candidate.get("risk_badges") or [])
    if "github-origin" not in badges:
        badges.append("github-origin")
    candidate["risk_badges"] = badges
    if exists:
        candidate["codex_release_status"] = "github-native-codex-manifest"
        candidate["codex_release_evidence"] = f"GitHub contains {manifest_url}"
        if "codex-native" not in badges:
            badges.append("codex-native")
        candidate["risk_badges"] = badges
    else:
        if github_api_not_found(error):
            candidate["codex_release_status"] = "github-origin-checked-no-native"
        else:
            candidate["codex_release_status"] = "github-check-failed"
        candidate["github_codex_check_error"] = error
    return candidate


def annotate_github_codex_releases(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [annotate_github_codex_release(dict(candidate)) for candidate in candidates]


def github_check_fallback_text(candidate: Dict[str, Any]) -> str:
    if candidate.get("bridge"):
        return "Keeping Claude-to-Codex bridge candidate."
    if candidate.get("blocked_reason"):
        return "Keeping manual review candidate."
    return "Keeping existing candidate."


def github_check_status_text(candidate: Dict[str, Any]) -> str:
    status = str(candidate.get("codex_release_status") or "not-detected")
    error = str(candidate.get("github_codex_check_error") or "")
    if status == "github-native-codex-manifest":
        return "GitHub check passed: native Codex plugin manifest found."
    if status == "github-origin-checked-no-native":
        suffix = f" Reason: {error}" if error else ""
        return f"GitHub check completed: no native Codex plugin manifest found. {github_check_fallback_text(candidate)}{suffix}"
    if error:
        return f"GitHub check failed. {github_check_fallback_text(candidate)} Reason: {error}"
    return f"GitHub check: {status}."


def plugin_origin_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    selector = str(item.get("name") or "")
    plugin_name, marketplace = plugin_selector_parts(selector)
    install_path_text = str(item.get("install_path") or "")
    install_path = Path(install_path_text).expanduser() if install_path_text else None
    entry, root = marketplace_plugin_entry(marketplace, plugin_name)
    entry_source = entry.get("source") if isinstance(entry, dict) else None
    source_path = source_path_from_entry(root, entry_source, plugin_name)
    source_url = source_url_from_entry(entry_source)
    known_source_url = marketplace_source_url(marketplace)
    if not source_url:
        source_url = known_source_url
    if root and not github_repo_from_url(source_url):
        git_origin = git_origin_from_config(root)
        if github_repo_from_url(git_origin):
            source_url = git_origin
    if not github_repo_from_url(source_url) and github_repo_from_url(known_source_url):
        source_url = known_source_url
    github_repo = github_repo_from_url(source_url)
    source_ref = ""
    source_subdir = ""
    source_sha = str(item.get("git_commit_sha") or "")
    if isinstance(entry_source, dict):
        source_ref = str(entry_source.get("ref") or "")
        source_subdir = str(entry_source.get("path") or "")
        source_sha = str(entry_source.get("sha") or source_sha)
    elif isinstance(entry_source, str):
        source_subdir = entry_source if not source_url_from_entry(entry_source) else ""
    native_cache = install_path / ".codex-plugin" / "plugin.json" if install_path else None
    native_source = source_path / ".codex-plugin" / "plugin.json" if source_path else None
    status = "not-detected"
    evidence = ""
    manifest_path = ""
    if native_cache and native_cache.exists():
        status = "native-codex-cache"
        evidence = f"local Claude cache includes {native_cache}"
        manifest_path = str(native_cache)
    elif native_source and native_source.exists():
        status = "native-codex-source"
        evidence = f"marketplace source includes {native_source}"
        manifest_path = str(native_source)
    elif github_repo:
        status = "gh-check-needed"
        evidence = f"GitHub origin detected but not checked: {github_repo}"
    return {
        "origin_marketplace": marketplace,
        "origin_marketplace_root": str(root) if root else "",
        "origin_source_path": str(source_path) if source_path else "",
        "origin_source_url": source_url,
        "origin_github_repo": github_repo,
        "origin_github_url": github_repo_url(github_repo),
        "origin_ref": source_ref,
        "origin_subdir": source_subdir,
        "origin_sha": source_sha,
        "codex_release_status": status,
        "codex_release_evidence": evidence,
        "codex_release_manifest_path": manifest_path,
        "codex_release_check_urls": github_codex_check_urls(github_repo, source_ref or source_sha, source_subdir),
    }


def installed_plugin_bridge_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    selector = str(item.get("name") or "")
    plugin_name, marketplace_name = plugin_selector_parts(selector)
    install_path = str(item.get("install_path") or "")
    if not selector or not plugin_name:
        return {}
    cache_path = Path(install_path).expanduser() if install_path else None
    origin_source_path = Path(str(item.get("origin_source_path") or "")).expanduser() if item.get("origin_source_path") else None
    source_path = (
        origin_source_path
        if origin_source_path and (origin_source_path / ".claude-plugin" / "plugin.json").exists()
        else cache_path
    )
    source_kind = "source-repo" if source_path and origin_source_path and source_path == origin_source_path else "claude-cache"
    if not source_path:
        return {}
    manifest = load_plugin_manifest(source_path)
    if not source_path.is_dir() or manifest is None:
        return {}
    agents_dir = source_path / "agents"
    skills_dir = source_path / "skills"
    bridge_name = f"cc-{plugin_name}"
    destination = home_dir() / ".codex" / "plugins" / bridge_name
    return {
        "bridge": True,
        "bridge_name": bridge_name,
        "bridge_source_path": str(source_path),
        "bridge_cache_fallback_path": str(cache_path) if cache_path and cache_path != source_path else "",
        "bridge_destination_path": str(destination),
        "bridge_marketplace": marketplace_name,
        "bridge_source_plugin": plugin_name,
        "bridge_source_selector": selector,
        "bridge_source_kind": source_kind,
        "bridge_source_ref": str(item.get("origin_ref") or item.get("origin_sha") or item.get("git_commit_sha") or ""),
        "bridge_source_subdir": str(item.get("origin_subdir") or ""),
        "bridge_source_repo_root": str(item.get("origin_marketplace_root") or ""),
        "bridge_commit": str(item.get("git_commit_sha") or "local"),
        "bridge_version": str(item.get("version") or manifest.get("version") or ""),
        "bridge_plugin_description": str(manifest.get("description") or ""),
        "bridge_agent_count": len(list(agents_dir.glob("*.md"))) if agents_dir.is_dir() else 0,
        "bridge_skill_count": sum(1 for path in skills_dir.iterdir() if path.is_dir()) if skills_dir.is_dir() else 0,
    }


def codex_bridge_paths() -> Dict[str, Path]:
    root = home_dir()
    return {
        "root": root,
        "plugins_dir": root / ".codex" / "plugins",
        "agents_dir": root / ".codex" / "agents",
        "registry": root / ".agents" / "plugins" / "marketplace.json",
    }


def bridge_relative_plugin_path(bridge_name: str) -> str:
    paths = codex_bridge_paths()
    rel = (paths["plugins_dir"] / bridge_name).relative_to(paths["root"])
    return f"./{rel.as_posix()}"


def json_comment_payload(prefix: str, line: str) -> Optional[Dict[str, Any]]:
    if not line.startswith(prefix):
        return None
    try:
        payload = json.loads(line[len(prefix) :].strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def read_bridge_manifest_marker(manifest_path: Path) -> Optional[Dict[str, Any]]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        return None
    marker = manifest.get(BRIDGE_MARKER_KEY)
    return marker if isinstance(marker, dict) else None


def read_bridge_agent_marker(agent_path: Path) -> Optional[Dict[str, Any]]:
    try:
        with agent_path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline().rstrip("\n")
    except OSError:
        return None
    return json_comment_payload("# x-cc-bridge: ", first_line)


def bridge_marker(
    *,
    source_plugin: str,
    source_selector: str,
    source_path: Path,
    marketplace: str,
    commit: str,
    agents: List[str],
    synced_at: str,
) -> Dict[str, Any]:
    return {
        "sourcePlugin": source_plugin,
        "sourceSelector": source_selector,
        "source": str(source_path),
        "sourceKind": "local",
        "ref": None,
        "commit": commit or "local",
        "marketplace": marketplace,
        "syncedAt": synced_at,
        "tool": BRIDGE_TOOL_ID,
        "agents": agents,
    }


def bridge_agent_marker_line(
    *, source_plugin: str, source_agent: str, bridge_plugin: str, synced_at: str
) -> str:
    payload = {
        "sourcePlugin": source_plugin,
        "sourceAgent": source_agent,
        "bridgePlugin": bridge_plugin,
        "syncedAt": synced_at,
    }
    return "# x-cc-bridge: " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def parse_simple_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    match = BRIDGE_AGENT_FRONTMATTER_RE.match(text)
    if not match:
        raise HandoffError("agent markdown is missing YAML frontmatter")
    frontmatter_text, body = match.group(1), match.group(2).lstrip("\n")
    data: Dict[str, Any] = {}
    for raw_line in frontmatter_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key:
            continue
        if not value:
            data[key] = ""
        elif value[0:1] in {'"', "'"} and value[-1:] == value[0]:
            data[key] = value[1:-1]
        elif value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value.replace("'", '"'))
            except json.JSONDecodeError:
                parsed = [part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()]
            data[key] = parsed
        elif value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        else:
            data[key] = value
    return data, body


def snake_case_bridge_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")


def toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def render_agent_toml(data: Dict[str, Any]) -> str:
    return "\n".join(f"{key} = {toml_literal(value)}" for key, value in data.items()) + "\n"


def bridge_skill_name(source_plugin: str, command_name: str) -> str:
    fallback = action_hash({"plugin": source_plugin, "command": command_name})
    return stable_id_part(f"{source_plugin}-{command_name}", fallback)


def normalize_command_allowed_tools(value: Any) -> str:
    return str(value).replace(":*", "*")


def render_command_skill(source_plugin: str, command_path: Path, existing_skill_names: set) -> Dict[str, str]:
    raw_text = command_path.read_text(encoding="utf-8")
    try:
        frontmatter, body = parse_simple_frontmatter(raw_text)
    except HandoffError:
        frontmatter, body = {}, raw_text
    command_name = command_path.stem
    skill_name = bridge_skill_name(source_plugin, command_name)
    if skill_name in existing_skill_names:
        skill_name = stable_id_part(f"{source_plugin}-command-{command_name}", action_hash(str(command_path)))
    description = str(frontmatter.get("description") or f"Converted Claude command /{source_plugin}:{command_name}")
    lines = [
        "---",
        f"name: {skill_name}",
        f"description: {description}",
    ]
    for key in ("argument-hint", "allowed-tools"):
        if key in frontmatter and str(frontmatter[key]).strip():
            value = normalize_command_allowed_tools(frontmatter[key]) if key == "allowed-tools" else str(frontmatter[key])
            lines.append(f"{key}: {value}")
    lines.extend(
        [
            "---",
            "",
            f"# /{source_plugin}:{command_name}",
            "",
            "Converted from a Claude Code plugin command. From this skill directory, resolve any `CLAUDE_PLUGIN_ROOT` references to `../..`.",
            "When the command delegates to bundled implementation details, prefer the sibling skills and scripts copied with this bridge.",
            "",
            body,
        ]
    )
    return {"skill_name": skill_name, "text": "\n".join(lines).rstrip() + "\n"}


def convert_bridge_commands(source: Path, staged: Path, *, source_plugin: str) -> List[str]:
    commands_dir = source / "commands"
    if not commands_dir.is_dir():
        return []
    skills_dir = staged / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    existing = {path.name for path in skills_dir.iterdir() if path.is_dir()}
    converted: List[str] = []
    for command_path in sorted(commands_dir.glob("*.md")):
        rendered = render_command_skill(source_plugin, command_path, existing)
        skill_name = rendered["skill_name"]
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(rendered["text"], encoding="utf-8")
        existing.add(skill_name)
        converted.append(skill_name)
    return converted


def convert_bridge_agent(md_path: Path, *, bridge_name: str, source_plugin: str, synced_at: str) -> Dict[str, Any]:
    frontmatter, body = parse_simple_frontmatter(md_path.read_text(encoding="utf-8"))
    source_agent = str(frontmatter.get("name") or "")
    if not source_agent:
        raise HandoffError(f"{md_path}: frontmatter missing required name")
    agent_name = f"cc_{snake_case_bridge_name(source_plugin)}_{snake_case_bridge_name(source_agent)}"
    toml_data: Dict[str, Any] = {
        "name": agent_name,
        "description": str(frontmatter.get("description") or f"Bridged from Claude Code plugin {source_plugin}"),
        "developer_instructions": body,
    }
    if "nickname_candidates" in frontmatter:
        toml_data["nickname_candidates"] = frontmatter["nickname_candidates"]
    return {
        "agent_name": agent_name,
        "source_agent": source_agent,
        "toml": bridge_agent_marker_line(
            source_plugin=source_plugin,
            source_agent=source_agent,
            bridge_plugin=bridge_name,
            synced_at=synced_at,
        )
        + "\n"
        + render_agent_toml(toml_data),
    }


def bridge_copy_ignore(directory: str, names: List[str]) -> set:
    ignored = set()
    for name in names:
        if name in BRIDGE_COPY_IGNORE_NAMES or name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def copy_bridge_plugin_body(source: Path, staged: Path) -> None:
    for item in source.iterdir():
        if item.name in BRIDGE_CC_ONLY_DIR_NAMES or item.name in BRIDGE_COPY_IGNORE_NAMES or item.name.endswith(".pyc"):
            continue
        target = staged / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=bridge_copy_ignore)
        else:
            shutil.copy2(item, target)


def atomic_replace_dir(target: Path, staged: Path) -> None:
    if not target.exists():
        staged.rename(target)
        return
    old_dir = target.parent / f".{target.name}.old-{uuid.uuid4().hex[:8]}"
    target.rename(old_dir)
    try:
        staged.rename(target)
    except Exception:
        try:
            old_dir.rename(target)
        finally:
            raise
    shutil.rmtree(old_dir, ignore_errors=True)


def load_bridge_registry(path: Path) -> Dict[str, Any]:
    data = load_json(path)
    if not isinstance(data, dict):
        data = {
            "name": BRIDGE_REGISTRY_NAME,
            "interface": {"displayName": BRIDGE_REGISTRY_DISPLAY_NAME},
            "plugins": [],
        }
    data.setdefault("name", BRIDGE_REGISTRY_NAME)
    data.setdefault("interface", {"displayName": BRIDGE_REGISTRY_DISPLAY_NAME})
    if not isinstance(data.get("plugins"), list):
        data["plugins"] = []
    return data


def upsert_bridge_registry_entry(registry: Dict[str, Any], bridge_name: str) -> None:
    entry = {
        "name": bridge_name,
        "source": {"source": "local", "path": bridge_relative_plugin_path(bridge_name)},
        "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_USE"},
        "category": "Productivity",
    }
    plugins = registry.setdefault("plugins", [])
    for index, plugin in enumerate(plugins):
        if isinstance(plugin, dict) and plugin.get("name") == bridge_name:
            plugins[index] = entry
            return
    plugins.append(entry)


def write_bridge_registry(path: Path, registry: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_extract_tar_bytes(payload: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if not path_is_within(target, destination):
                raise HandoffError(f"refusing to extract unsafe archive member {member.name}")
        archive.extractall(destination)


def materialize_git_source_at_ref(repo_root: Path, ref: str, subdir: str) -> Tuple[Optional[tempfile.TemporaryDirectory], str]:
    clean_subdir = subdir.strip("/")
    if clean_subdir.startswith("./"):
        clean_subdir = clean_subdir[2:]
    if not repo_root.is_dir() or not ref or not clean_subdir:
        return None, "missing source repo root, ref, or plugin subdir"
    git_path = shutil.which("git") or "git"
    treeish = f"{ref}:{clean_subdir}"
    try:
        proc = subprocess.run(
            [git_path, "-C", str(repo_root), "archive", "--format=tar", treeish],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except Exception as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, compact(proc.stderr.decode("utf-8", errors="replace") or "git archive failed", 240)
    temp_dir = tempfile.TemporaryDirectory(prefix="ai-handoff-plugin-")
    destination = Path(temp_dir.name)
    try:
        safe_extract_tar_bytes(proc.stdout, destination)
    except Exception as exc:
        temp_dir.cleanup()
        return None, str(exc)
    if load_plugin_manifest(destination) is None:
        temp_dir.cleanup()
        return None, f"{treeish} does not contain a Claude or Codex plugin manifest"
    return temp_dir, ""


def bridge_plugin_to_codex(candidate: Dict[str, Any]) -> Dict[str, Any]:
    requested_source_path = Path(str(candidate.get("bridge_source_path") or "")).expanduser()
    source_path = requested_source_path
    bridge_name = str(candidate.get("bridge_name") or "")
    source_plugin = str(candidate.get("bridge_source_plugin") or candidate.get("name") or "")
    source_selector = str(candidate.get("bridge_source_selector") or candidate.get("name") or "")
    marketplace = str(candidate.get("bridge_marketplace") or "")
    commit = str(candidate.get("bridge_commit") or "local")
    source_kind = str(candidate.get("bridge_source_kind") or "claude-cache")
    source_ref = str(candidate.get("bridge_source_ref") or "")
    source_subdir = str(candidate.get("bridge_source_subdir") or "")
    source_repo_root = Path(str(candidate.get("bridge_source_repo_root") or "")).expanduser()
    cache_fallback_path = Path(str(candidate.get("bridge_cache_fallback_path") or "")).expanduser()
    source_resolution = ""
    temp_source: Optional[tempfile.TemporaryDirectory] = None
    if source_kind == "source-repo" and source_ref and source_subdir and source_repo_root:
        temp_source, source_resolution = materialize_git_source_at_ref(source_repo_root, source_ref, source_subdir)
        if temp_source:
            source_path = Path(temp_source.name)
        elif cache_fallback_path.is_dir() and load_plugin_manifest(cache_fallback_path):
            source_path = cache_fallback_path
            source_kind = "claude-cache-fallback"
        else:
            raise HandoffError(f"source repo bridge failed: {source_resolution}")
    if not source_path.is_dir() or not bridge_name or not source_plugin:
        raise HandoffError("missing bridge source plugin metadata")
    try:
        manifest = load_plugin_manifest(source_path)
        if manifest is None:
            raise HandoffError(f"missing .claude-plugin/plugin.json under {source_path}")

        paths = codex_bridge_paths()
        plugins_dir = paths["plugins_dir"]
        agents_dir = paths["agents_dir"]
        registry_path = paths["registry"]
        bridge_dir = plugins_dir / bridge_name
        manifest_path = bridge_dir / ".codex-plugin" / "plugin.json"
        existing_marker = read_bridge_manifest_marker(manifest_path)
        marker_source = str(requested_source_path)
        if manifest_path.exists() and existing_marker is None:
            raise HandoffError(f"{manifest_path} exists and is not an ai-handoff bridge")
        if (
            existing_marker
            and existing_marker.get("source") not in {str(source_path), marker_source}
            and existing_marker.get("sourceSelector") != source_selector
        ):
            raise HandoffError(f"{bridge_name} already exists from a different source")

        synced_at = utc_now()
        old_agents = list(existing_marker.get("agents") or []) if existing_marker else []
        conversions = [
            convert_bridge_agent(path, bridge_name=bridge_name, source_plugin=source_plugin, synced_at=synced_at)
            for path in sorted((source_path / "agents").glob("*.md"))
        ] if (source_path / "agents").is_dir() else []
        new_agent_names = [str(item["agent_name"]) for item in conversions]
        for conversion in conversions:
            target = agents_dir / f"{conversion['agent_name']}.toml"
            if target.exists() and read_bridge_agent_marker(target) is None:
                raise HandoffError(f"{target} exists and is user-authored")

        plugins_dir.mkdir(parents=True, exist_ok=True)
        staged = plugins_dir / f".{bridge_name}.stage-{uuid.uuid4().hex[:8]}"
        try:
            staged.mkdir(parents=True, exist_ok=False)
            copy_bridge_plugin_body(source_path, staged)
            command_skills = convert_bridge_commands(source_path, staged, source_plugin=source_plugin)
            bridged_manifest = dict(manifest)
            for key in BRIDGE_CC_ONLY_MANIFEST_KEYS:
                bridged_manifest.pop(key, None)
            bridged_manifest["name"] = bridge_name
            bridged_manifest[BRIDGE_MARKER_KEY] = bridge_marker(
                source_plugin=source_plugin,
                source_selector=source_selector,
                source_path=Path(marker_source),
                marketplace=marketplace,
                commit=commit,
                agents=new_agent_names,
                synced_at=synced_at,
            )
            bridged_manifest[BRIDGE_MARKER_KEY]["sourceKind"] = source_kind
            resolved_source = str(source_path)
            if source_kind == "source-repo" and source_ref:
                resolved_source = f"{marker_source}@{source_ref}"
            bridged_manifest[BRIDGE_MARKER_KEY]["resolvedSource"] = resolved_source
            bridged_manifest[BRIDGE_MARKER_KEY]["commands"] = command_skills
            if source_resolution:
                bridged_manifest[BRIDGE_MARKER_KEY]["sourceResolution"] = source_resolution
            staged_manifest = staged / ".codex-plugin" / "plugin.json"
            staged_manifest.parent.mkdir(parents=True, exist_ok=True)
            staged_manifest.write_text(json.dumps(bridged_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            atomic_replace_dir(bridge_dir, staged)
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise

        agents_dir.mkdir(parents=True, exist_ok=True)
        for conversion in conversions:
            (agents_dir / f"{conversion['agent_name']}.toml").write_text(str(conversion["toml"]), encoding="utf-8")
        for stale in set(old_agents) - set(new_agent_names):
            target = agents_dir / f"{stale}.toml"
            if target.exists() and read_bridge_agent_marker(target) is not None:
                target.unlink()

        registry = load_bridge_registry(registry_path)
        upsert_bridge_registry_entry(registry, bridge_name)
        write_bridge_registry(registry_path, registry)
        return {
            "bridge": bridge_name,
            "source": str(source_path),
            "source_kind": source_kind,
            "source_resolution": source_resolution,
            "destination": str(bridge_dir),
            "manifest": str(manifest_path),
            "registry": str(registry_path),
            "agents": new_agent_names,
            "commands": command_skills,
        }
    finally:
        if temp_source:
            temp_source.cleanup()


def install_bridged_plugin(bridge_name: str) -> Dict[str, Any]:
    if not bridge_name:
        raise HandoffError("missing bridged plugin name")
    selector = f"{bridge_name}@{BRIDGE_REGISTRY_NAME}"
    codex_path = shutil.which("codex") or "codex"
    add_command = [codex_path, "plugin", "add", selector]
    remove_command = [codex_path, "plugin", "remove", selector]

    def run_command(command: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )

    try:
        proc = run_command(add_command)
    except Exception as exc:
        return {
            "selector": selector,
            "command": " ".join(shlex.quote(part) for part in add_command),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "installed": False,
            "reason": str(exc),
        }
    output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
    already_installed = proc.returncode != 0 and "already installed" in output.lower()
    remove_result: Optional[Dict[str, Any]] = None
    if already_installed:
        try:
            remove_proc = run_command(remove_command)
            remove_result = {
                "command": " ".join(shlex.quote(part) for part in remove_command),
                "returncode": remove_proc.returncode,
                "stdout": truncate(remove_proc.stdout, 1200),
                "stderr": truncate(remove_proc.stderr, 1200),
            }
            if remove_proc.returncode == 0:
                proc = run_command(add_command)
                output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
                already_installed = False
        except Exception as exc:
            return {
                "selector": selector,
                "command": " ".join(shlex.quote(part) for part in add_command),
                "remove": remove_result,
                "returncode": None,
                "stdout": truncate(proc.stdout, 1200),
                "stderr": truncate(proc.stderr, 1200),
                "installed": False,
                "already_installed": True,
                "reason": f"plugin was already installed, but reinstall failed: {exc}",
            }
    installed = proc.returncode == 0 or already_installed
    reason = "" if installed else truncate(output or "codex plugin add failed", 1200)
    if already_installed and remove_result and remove_result.get("returncode") != 0:
        installed = False
        reason = "plugin was already installed, but remove before reinstall failed"
    return {
        "selector": selector,
        "command": " ".join(shlex.quote(part) for part in add_command),
        "remove": remove_result,
        "returncode": proc.returncode,
        "stdout": truncate(proc.stdout, 1200),
        "stderr": truncate(proc.stderr, 1200),
        "installed": installed,
        "already_installed": already_installed,
        "reason": reason,
    }


def discover_claude_sessions(
    project: Path,
    last: int = 3,
    since: Optional[str] = None,
    include_excerpts: bool = False,
    selected_session_ids: Optional[List[str]] = None,
    all_projects: bool = False,
    from_claude_project: Optional[str] = None,
    search: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    home = home_dir()
    projects_root = home / ".claude" / "projects"
    exact_key = claude_project_key(project)
    if from_claude_project:
        project_dirs = [(from_claude_project, projects_root / from_claude_project)]
    elif all_projects:
        project_dirs = [
            (path.name, path)
            for path in sorted(projects_root.iterdir(), key=lambda item: item.name)
            if path.is_dir()
        ] if projects_root.exists() else []
    else:
        project_dirs = [(exact_key, projects_root / exact_key)]
    entries: List[Dict[str, Any]] = []
    for source_key, project_dir in project_dirs:
        entries.extend(read_claude_session_entries(project_dir, source_key, project))
    explicit_ids = [str(item) for item in selected_session_ids or [] if item]
    if explicit_ids and not all_projects and not from_claude_project and projects_root.exists():
        found_ids = {str(entry.get("sessionId")) for entry in entries if entry.get("sessionId")}
        missing_ids = set(explicit_ids) - found_ids
        if missing_ids:
            for other_dir in sorted(projects_root.iterdir(), key=lambda item: item.name):
                if not other_dir.is_dir() or other_dir.name == exact_key:
                    continue
                for entry in read_claude_session_entries(other_dir, other_dir.name, project):
                    if str(entry.get("sessionId")) in missing_ids:
                        entries.append(entry)
                        found_ids.add(str(entry.get("sessionId")))
                        missing_ids.discard(str(entry.get("sessionId")))
                if not missing_ids:
                    break
    entries = dedupe_session_entries(entries)
    if search:
        entries = [entry for entry in entries if entry_matches_search(entry, search)]
    if branch:
        entries = [entry for entry in entries if str(entry.get("gitBranch") or "") == branch]
    cutoff = parse_duration(since)
    if cutoff:
        entries = [
            entry
            for entry in entries
            if (parse_time(entry.get("modified") or entry.get("fileMtime")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
            >= cutoff
        ]
    entries.sort(
        key=lambda entry: parse_time(entry.get("modified") or entry.get("fileMtime"))
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    missing_requested_ids: List[str] = []
    if explicit_ids:
        available_ids = {str(entry.get("sessionId")) for entry in entries if entry.get("sessionId")}
        missing_requested_ids = [session_id for session_id in explicit_ids if session_id not in available_ids]
        explicit_set = set(explicit_ids)
        selected = [entry for entry in entries if str(entry.get("sessionId")) in explicit_set]
        selected.sort(key=lambda entry: explicit_ids.index(str(entry.get("sessionId"))))
        strategy = "user-selected session IDs"
    else:
        selected = entries[: max(0, last)]
        if from_claude_project:
            strategy = f"latest {last} non-observer sessions from Claude project {from_claude_project}"
        elif all_projects:
            strategy = f"latest {last} non-observer sessions across Claude projects"
        else:
            strategy = f"latest {last} non-observer sessions"
        if search:
            strategy += f" matching {search!r}"
        if branch:
            strategy += f" on branch {branch}"
    candidates = [session_candidate(entry) for entry in entries]
    sessions = []
    for entry in selected:
        full_path = Path(str(entry.get("fullPath") or Path(str(entry.get("_source_project_dir") or "")) / f"{entry.get('sessionId')}.jsonl"))
        transcript = summarize_transcript(full_path, include_excerpts=include_excerpts)
        sessions.append(
            {
                "session_id": entry.get("sessionId"),
                "title": entry.get("customTitle") or entry.get("summary") or entry.get("firstPrompt") or "Untitled",
                "summary": entry.get("summary"),
                "first_prompt": truncate(redact(str(entry.get("firstPrompt") or "")), 320),
                "created": entry.get("created"),
                "modified": entry.get("modified"),
                "message_count": entry.get("messageCount"),
                "git_branch": entry.get("gitBranch"),
                "project_path": entry.get("projectPath"),
                "source_project_key": entry.get("_source_project_key"),
                "transcript": transcript,
            }
        )
    exact_project_dir = projects_root / exact_key
    return {
        "project_dir": str(exact_project_dir),
        "project_dirs": [{"key": key, "path": str(path)} for key, path in project_dirs],
        "source_project_keys": sorted({str(entry.get("_source_project_key")) for entry in entries if entry.get("_source_project_key")}),
        "index_path": str(exact_project_dir / "sessions-index.json"),
        "index_exists": (exact_project_dir / "sessions-index.json").exists(),
        "found_count": len(entries),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "close_project_matches": close_claude_project_matches(project, exact_key),
        "all_projects": all_projects,
        "from_claude_project": from_claude_project,
        "search": search,
        "branch": branch,
        "selection_strategy": strategy,
        "requested_session_ids": explicit_ids,
        "missing_requested_session_ids": missing_requested_ids,
        "selected_session_ids": [str(entry.get("sessionId")) for entry in selected if entry.get("sessionId")],
        "selected_count": len(sessions),
        "selected": sessions,
        "usage_summary": aggregate_session_usage(sessions),
    }


def read_claude_session_entries(project_dir: Path, source_key: str, project: Path) -> List[Dict[str, Any]]:
    index_path = project_dir / "sessions-index.json"
    index = load_json(index_path)
    entries: List[Dict[str, Any]] = []
    if isinstance(index, dict) and isinstance(index.get("entries"), list):
        for item in index["entries"]:
            if not isinstance(item, dict):
                continue
            if item.get("isSidechain"):
                continue
            if is_observer_session(item):
                continue
            entry = dict(item)
            session_id = str(entry.get("sessionId") or "")
            if session_id and not entry.get("fullPath"):
                entry["fullPath"] = str(project_dir / f"{session_id}.jsonl")
            entry["_source_project_key"] = source_key
            entry["_source_project_dir"] = str(project_dir)
            entries.append(entry)
    elif project_dir.exists():
        for file_path in project_dir.glob("*.jsonl"):
            entry = {
                "sessionId": file_path.stem,
                "fullPath": str(file_path),
                "fileMtime": file_path.stat().st_mtime,
                "projectPath": str(project),
                "_source_project_key": source_key,
                "_source_project_dir": str(project_dir),
            }
            if not is_observer_session(entry):
                entries.append(entry)
    indexed_ids = {str(entry.get("sessionId")) for entry in entries if entry.get("sessionId")}
    if project_dir.exists():
        for file_path in sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
            if file_path.stem in indexed_ids:
                continue
            entry = loose_session_entry(file_path, source_key, project)
            if not is_observer_session(entry):
                entries.append(entry)
    return entries


def loose_session_entry(file_path: Path, source_key: str, project: Path) -> Dict[str, Any]:
    first_prompt = ""
    summary = ""
    message_count = 0
    git_branch = None
    project_path = str(project)
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if message_count >= 200:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                message_count += 1
                git_branch = git_branch or obj.get("gitBranch")
                project_path = str(obj.get("cwd") or obj.get("projectPath") or project_path)
                message = obj.get("message") if isinstance(obj.get("message"), dict) else None
                if not message or message.get("role") != "user" or first_prompt:
                    continue
                content = message.get("content")
                if isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content):
                    continue
                text, _ = content_to_text(content)
                if text and "<skill-format>true" not in text:
                    first_prompt = truncate(text, 320)
                    summary = truncate(text, 160)
    except OSError:
        pass
    modified = dt.datetime.fromtimestamp(file_path.stat().st_mtime, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "sessionId": file_path.stem,
        "fullPath": str(file_path),
        "firstPrompt": first_prompt,
        "summary": summary,
        "messageCount": message_count or None,
        "modified": modified,
        "fileMtime": file_path.stat().st_mtime,
        "gitBranch": git_branch,
        "projectPath": project_path,
        "_source_project_key": source_key,
        "_source_project_dir": str(file_path.parent),
    }


def dedupe_session_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for entry in entries:
        key = (str(entry.get("_source_project_key") or ""), str(entry.get("sessionId") or ""), str(entry.get("fullPath") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def entry_matches_search(entry: Dict[str, Any], search: str) -> bool:
    needle = search.lower()
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in (
            "sessionId",
            "customTitle",
            "summary",
            "firstPrompt",
            "projectPath",
            "gitBranch",
            "_source_project_key",
        )
    ).lower()
    return needle in haystack


def close_claude_project_matches(project: Path, exact_key: str, limit: int = 8) -> List[Dict[str, Any]]:
    projects_root = home_dir() / ".claude" / "projects"
    if not projects_root.exists():
        return []
    project_name = project.name.lower()
    matches: List[Tuple[int, Dict[str, Any]]] = []
    for directory in projects_root.iterdir():
        if not directory.is_dir() or directory.name == exact_key:
            continue
        score = 0
        reasons: List[str] = []
        key_lower = directory.name.lower()
        if project_name and project_name in key_lower:
            score += 4
            reasons.append("project-name-in-key")
        index = load_json(directory / "sessions-index.json")
        entry_count = 0
        if isinstance(index, dict) and isinstance(index.get("entries"), list):
            entry_count = len(index["entries"])
            for item in index["entries"][:20]:
                project_path = str(item.get("projectPath") or "").lower() if isinstance(item, dict) else ""
                if project_name and project_name in project_path:
                    score += 6
                    reasons.append("project-name-in-index-path")
                    break
        else:
            entry_count = len(list(directory.glob("*.jsonl")))
        if score:
            matches.append(
                (
                    score,
                    {
                        "key": directory.name,
                        "path": str(directory),
                        "entry_count": entry_count,
                        "reason": ",".join(dict.fromkeys(reasons)),
                    },
                )
            )
    matches.sort(key=lambda item: (-item[0], item[1]["key"]))
    return [item for _, item in matches[:limit]]


def session_candidate(entry: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(entry.get("sessionId") or "")
    source_dir = Path(str(entry.get("_source_project_dir") or ""))
    full_path = str(entry.get("fullPath") or source_dir / f"{session_id}.jsonl")
    return {
        "session_id": session_id,
        "title": truncate(
            str(entry.get("customTitle") or entry.get("summary") or entry.get("firstPrompt") or "Untitled"),
            160,
        ),
        "summary": entry.get("summary"),
        "first_prompt": truncate(redact(str(entry.get("firstPrompt") or "")), 260),
        "created": entry.get("created"),
        "modified": entry.get("modified"),
        "message_count": entry.get("messageCount"),
        "git_branch": entry.get("gitBranch"),
        "project_path": entry.get("projectPath"),
        "source_project_key": entry.get("_source_project_key"),
        "transcript_path": full_path,
    }


def is_observer_session(entry: Dict[str, Any]) -> bool:
    text = " ".join(
        str(entry.get(key) or "")
        for key in ("firstPrompt", "summary", "customTitle")
    ).lower()
    observer_markers = (
        "claude-mem",
        "specialized observer tool",
        "creating searchable memory",
        "record what was learned",
    )
    return any(marker in text for marker in observer_markers)


def read_mcp_servers_from_json(path: Path) -> List[Dict[str, Any]]:
    data = load_json(path)
    if not isinstance(data, dict):
        return []
    servers = data.get("mcpServers") or data.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return []
    result = []
    for name, cfg in servers.items():
        if isinstance(cfg, dict):
            result.append({"name": str(name), "source": str(path), "config": redact(cfg)})
    return result


def mcp_command(name: str, cfg: Dict[str, Any]) -> Optional[str]:
    if cfg.get("url"):
        parts = ["codex", "mcp", "add", name, "--url", str(cfg["url"])]
        bearer_env = cfg.get("bearerTokenEnvVar") or cfg.get("bearer_token_env_var")
        if bearer_env:
            parts.extend(["--bearer-token-env-var", str(bearer_env)])
        return quote_cmd(parts)
    command = cfg.get("command")
    if not command:
        return None
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
    parts = ["codex", "mcp", "add"]
    for key in sorted(env):
        value = env[key]
        shown = "<redacted>" if SECRET_FIELD_RE.search(str(key)) else str(value)
        parts.extend(["--env", f"{key}={shown}"])
    parts.extend([name, "--", str(command)])
    parts.extend(str(arg) for arg in args)
    return quote_cmd(parts)


def discover_codex() -> Dict[str, Any]:
    home = home_dir()
    config_path = home / ".codex" / "config.toml"
    config = load_toml(config_path)
    mcp_tables = config.get("mcp_servers") or config.get("mcpServers") or {}
    plugins = config.get("plugins") or {}
    return {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "projects": config.get("projects") if isinstance(config.get("projects"), dict) else {},
        "mcp_names": sorted(mcp_tables.keys()) if isinstance(mcp_tables, dict) else [],
        "plugin_names": sorted(plugins.keys()) if isinstance(plugins, dict) else [],
        "skills_dir": str(home / ".codex" / "skills"),
    }


def codex_trust_for_project(project: Path, codex: Dict[str, Any]) -> Optional[str]:
    projects = codex.get("projects")
    if not isinstance(projects, dict):
        return None
    entry = projects.get(str(project))
    if isinstance(entry, dict):
        value = entry.get("trust_level")
        return str(value) if value is not None else None
    return None


def skill_names(skill_dir: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not skill_dir.exists():
        return result
    for skill_md in skill_dir.glob("*/SKILL.md"):
        text = read_text(skill_md, 4000) or ""
        name_match = re.search(r"(?m)^name:\s*([A-Za-z0-9_-]+)\s*$", text)
        name = name_match.group(1) if name_match else skill_md.parent.name
        result[name] = {"name": name, "path": str(skill_md.parent), "bytes": skill_md.stat().st_size}
    return result


def discover_claude_config(project: Path, codex: Dict[str, Any]) -> Dict[str, Any]:
    home = home_dir()
    mcp_candidates = []
    for path in [
        home / ".claude" / "claude_desktop_config.json",
        project / ".mcp.json",
    ]:
        mcp_candidates.extend(read_mcp_servers_from_json(path))
    for candidate in mcp_candidates:
        name = candidate["name"]
        candidate["already_in_codex"] = name in set(codex.get("mcp_names") or [])
        candidate["proposed_command"] = mcp_command(name, candidate["config"])
    claude_settings = []
    for path in sorted((project / ".claude").glob("settings*.json")) if (project / ".claude").exists() else []:
        data = load_json(path)
        if isinstance(data, dict):
            claude_settings.append({"path": str(path), "config": redact(data)})
    claude_skills = skill_names(home / ".claude" / "skills")
    codex_skills = skill_names(home / ".codex" / "skills")
    skill_candidates = []
    for name, info in sorted(claude_skills.items()):
        skill_candidates.append(
            {
                "name": name,
                "path": info["path"],
                "already_in_codex": name in codex_skills,
                "compatible_action": "already installed" if name in codex_skills else "review before copying",
            }
        )
    plugin_file = home / ".claude" / "plugins" / "installed_plugins.json"
    plugin_data = load_json(plugin_file)
    plugin_candidates = []
    if isinstance(plugin_data, dict):
        raw_plugins = plugin_data.get("plugins") or plugin_data.get("installed") or plugin_data
        if isinstance(raw_plugins, dict):
            for name in sorted(raw_plugins):
                records = plugin_install_records(raw_plugins.get(name), project)
                record = records[0] if records else {}
                plugin_name, marketplace_name = plugin_selector_parts(str(name))
                plugin_candidates.append(
                    {
                        "name": str(name),
                        "plugin_name": plugin_name,
                        "marketplace": marketplace_name,
                        "source": str(plugin_file),
                        "install_path": record.get("install_path", ""),
                        "version": record.get("version", ""),
                        "git_commit_sha": record.get("git_commit_sha", ""),
                        "installed_scope": record.get("scope", ""),
                        "installed_project_path": record.get("project_path", ""),
                        "install_records": records,
                        "already_in_codex": str(name) in set(codex.get("plugin_names") or []),
                        "proposed_command": f"codex plugin add {shlex.quote(str(name))}",
                    }
                )
    for candidate in mcp_candidates:
        candidate.update(candidate_scope_and_risk("mcp", candidate, project))
    for candidate in skill_candidates:
        candidate.update(candidate_scope_and_risk("skill", candidate, project))
    for candidate in plugin_candidates:
        candidate.update(candidate_scope_and_risk("plugin", candidate, project))
    return {
        "mcp_candidates": mcp_candidates,
        "project_settings": claude_settings,
        "skill_candidates": skill_candidates,
        "plugin_candidates": plugin_candidates,
    }


def candidate_scope_and_risk(action_type: str, item: Dict[str, Any], project: Path) -> Dict[str, Any]:
    source = str(item.get("source") or item.get("path") or "")
    command = str(item.get("proposed_command") or "")
    source_scope = "global"
    if source and path_is_within(Path(source), project):
        source_scope = "project"
    elif string_mentions_project(command, project):
        source_scope = "project"

    requires_network = False
    portable = False
    risk = "medium"
    why_relevant = "Imported from Claude global configuration."
    evidence = "global Claude inventory"
    relevance_score = "low"
    blocked_reason = ""
    manual_steps: List[str] = []
    risk_badges: List[str] = []

    if action_type == "mcp":
        lower_command = command.lower()
        requires_network = any(token in lower_command for token in (" npx ", " uvx ", " npm ", " pipx "))
        has_redacted = "<redacted>" in command
        has_local_path = bool(re.search(r"(?<![A-Za-z0-9_./-])/(Users|private|tmp|var|opt|home)/", command))
        local_path_outside_project = has_local_path and not string_mentions_project(command, project)
        portable = source_scope == "project" and "<redacted>" not in command
        why_relevant = (
            "Project-local MCP configuration." if source_scope == "project" else "Global Claude MCP configuration."
        )
        evidence = f"from {source}" if source else "from Claude MCP command"
        relevance_score = "high" if source_scope == "project" else "low"
        risk = "medium" if source_scope == "project" else "high"
        if "server-filesystem" in lower_command and source_scope != "project":
            risk = "high"
            manual_steps.append("review filesystem scope before adding to Codex")
        if requires_network:
            risk_badges.append("network")
        if has_redacted:
            portable = False
            risk_badges.append("secret")
            blocked_reason = "redacted secrets require manual repair"
            manual_steps.append("restore redacted environment values manually")
        if local_path_outside_project:
            portable = False
            risk_badges.append("local-path")
        if "--url" in command or "oauth" in lower_command:
            risk_badges.append("login")
            manual_steps.append(f"run codex mcp login {item.get('name')}")
    elif action_type == "skill":
        skill_path = Path(str(item.get("path") or "")).expanduser()
        skill_text = read_text(skill_path / "SKILL.md", 12000) or ""
        has_local_path = "/Users/" in skill_text or str(home_dir()) in skill_text
        portable = (skill_path / "SKILL.md").exists() and not has_local_path
        source_scope = "project" if source and path_is_within(skill_path, project) else "global"
        why_relevant = "Claude skill folder with SKILL.md." if portable else "Claude skill folder needs manual review."
        evidence = f"from {skill_path}"
        relevance_score = "high" if source_scope == "project" else "low"
        risk = "medium"
        if has_local_path:
            risk_badges.append("local-path")
        manual_steps.append("review copied skill instructions before relying on them")
    elif action_type == "plugin":
        origin = plugin_origin_metadata(item)
        item.update(origin)
        bridge = installed_plugin_bridge_metadata(item)
        if bridge:
            item.update(bridge)
            item["proposed_command"] = (
                f"bridge Claude plugin {item.get('name')} -> "
                f"{bridge['bridge_destination_path']}"
            )
            requires_network = False
            portable = False
            risk = "high"
            why_relevant = (
                "Claude plugin can be bridged into a Codex plugin with command, skill, and agent conversion."
            )
            if bridge.get("bridge_source_kind") == "source-repo":
                evidence = f"source repo plugin: {bridge['bridge_source_path']}"
            else:
                evidence = f"Claude installed cache fallback: {bridge['bridge_source_path']}"
            relevance_score = "medium"
            risk_badges.extend(["local-path", "bridge"])
            if str(origin.get("codex_release_status") or "").startswith("native-codex"):
                risk_badges.append("codex-native")
                manual_steps.append("native Codex plugin metadata was found locally; prefer that package if it is complete")
            elif origin.get("origin_github_repo"):
                manual_steps.append("optional: run ai-handoff globals PATH --check-github to verify native Codex packaging with gh")
            manual_steps.append("restart Codex or open a new session after bridging")
            manual_steps.append("run /plugins and install the bridge from CC Bridged Plugins")
        else:
            requires_network = True
            portable = False
            risk = "high"
            why_relevant = "Claude plugin install record; not proof of a direct Codex plugin install."
            evidence = f"from {item.get('source')}" if item.get("source") else "from Claude plugin inventory"
            relevance_score = "low"
            blocked_reason = (
                "Claude plugin record only; verify a Codex plugin manifest/marketplace entry or bridge it before install"
            )
            risk_badges.extend(["network", "unverified"])
            if str(origin.get("codex_release_status") or "").startswith("native-codex"):
                risk_badges.append("codex-native")
            elif origin.get("origin_github_repo"):
                manual_steps.append("optional: run ai-handoff globals PATH --check-github to verify native Codex packaging with gh")
            manual_steps.append("verify this plugin has a Codex .codex-plugin/plugin.json or Codex marketplace entry")
            manual_steps.append(
                "for Claude Code marketplace plugins, consider cc2codex: "
                "uvx --from cc-plugin-to-codex cc2codex plugin-sync "
                "--source <marketplace-url-or-path> --plugin <name> --scope <project|global>"
            )

    risk_badges.append(f"{source_scope}-scope")
    if not risk_badges or (source_scope == "project" and not blocked_reason and not requires_network):
        risk_badges.append("safe")

    return {
        "source_scope": source_scope,
        "risk": risk,
        "risk_badges": list(dict.fromkeys(risk_badges)),
        "requires_network": requires_network,
        "portable": portable,
        "why_relevant": why_relevant,
        "evidence": evidence,
        "relevance_score": relevance_score,
        "blocked_reason": blocked_reason,
        "manual_steps": manual_steps,
    }


def default_run_id(project: Path, generated_at: str) -> str:
    digest = hashlib.sha1(f"{project}:{generated_at}".encode("utf-8")).hexdigest()[:10]
    stamp = generated_at.replace(":", "").replace("-", "").replace("Z", "")
    return f"{stamp}-{digest}"


def build_manifest(
    project_input: str,
    *,
    last: int = 3,
    since: Optional[str] = None,
    include_transcripts: bool = False,
    init_only: bool = False,
    selection: Optional[Dict[str, bool]] = None,
    selected_session_ids: Optional[List[str]] = None,
    all_projects: bool = False,
    from_claude_project: Optional[str] = None,
    search: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    project = resolve_project_path(project_input)
    effective_selection = dict(DEFAULT_SELECTION)
    if selection:
        effective_selection.update(selection)
    if include_transcripts:
        effective_selection["include_transcript_excerpts"] = True
    if init_only:
        effective_selection["propose_mcps"] = False
        effective_selection["propose_skill_conversions"] = False
        effective_selection["propose_plugin_installs"] = False
    generated_at = utc_now()
    codex = discover_codex()
    claude_sessions = discover_claude_sessions(
        project,
        last=last,
        since=since,
        include_excerpts=effective_selection["include_transcript_excerpts"],
        selected_session_ids=selected_session_ids,
        all_projects=all_projects,
        from_claude_project=from_claude_project,
        search=search,
        branch=branch,
    )
    claude_config = discover_claude_config(project, codex) if not init_only else {
        "mcp_candidates": [],
        "project_settings": [],
        "skill_candidates": [],
        "plugin_candidates": [],
    }
    project_data = discover_project(project)
    run_id = default_run_id(project, generated_at)
    writes = []
    if effective_selection["write_agents"]:
        writes.append("AGENTS.md")
    if effective_selection["write_summary"]:
        writes.append(".codex/handoff/summary.md")
    if effective_selection["write_manifest"]:
        writes.append(".codex/handoff/manifest.json")
        writes.append(f".codex/handoff/runs/{run_id}.json")
    manifest = {
        "version": VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "suggested_goal": f"Prepare {project} for Codex handoff from Claude Code context.",
        "target_path": str(project),
        "selection": effective_selection,
        "project": project_data,
        "codex": {
            "config_path": codex["config_path"],
            "config_exists": codex["config_exists"],
            "trust_level": codex_trust_for_project(project, codex),
            "mcp_names": codex["mcp_names"],
            "plugin_names": codex["plugin_names"],
            "skills_dir": codex["skills_dir"],
        },
        "claude": {
            "sessions": claude_sessions,
            "config": claude_config,
        },
        "actions": {
            "project_writes": writes,
            "global_changes_default": "disabled",
            "mcp_commands": [
                candidate["proposed_command"]
                for candidate in claude_config.get("mcp_candidates", [])
                if candidate.get("proposed_command") and not candidate.get("already_in_codex")
            ],
            "skill_conversions": [
                candidate
                for candidate in claude_config.get("skill_candidates", [])
                if not candidate.get("already_in_codex")
            ],
            "plugin_commands": [
                candidate["proposed_command"]
                for candidate in claude_config.get("plugin_candidates", [])
                if candidate.get("proposed_command") and not candidate.get("already_in_codex")
            ],
        },
        "selected_global_action_ids": [],
        "selected_global_actions": [],
        "applied": False,
        "applied_at": None,
        "applied_actions": [],
        "global_apply_results": [],
    }
    manifest["writes"] = project_write_metadata(manifest)
    manifest["diagnostics"] = build_diagnostics(manifest)
    manifest["global_candidates"] = global_action_candidates(manifest)
    manifest["privacy"] = privacy_metadata(manifest)
    manifest["handoff_confidence"] = handoff_confidence(manifest)
    return manifest


def handoff_confidence(manifest: Dict[str, Any]) -> Dict[str, Any]:
    has_guidance = bool(manifest["project"].get("has_claude_md"))
    selected_sessions = int(manifest["claude"]["sessions"].get("selected_count") or 0)
    if has_guidance and selected_sessions:
        level = "high"
        reason = "CLAUDE.md and Claude conversations are available."
    elif has_guidance or selected_sessions:
        level = "medium"
        reason = "Some Claude handoff context is available."
    else:
        level = "low"
        reason = "No CLAUDE.md and no Claude conversations were found for this path."
    return {"level": level, "reason": reason}


def project_write_metadata(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    project = Path(manifest["target_path"])
    has_claude_context = bool(manifest["claude"]["sessions"].get("selected_count"))
    rows = []
    for relative in manifest["actions"].get("project_writes", []):
        target = project / relative
        rows.append(
            {
                "path": relative,
                "mode": "update" if target.exists() else "create",
                "contains_private_context": has_claude_context,
            }
        )
    return rows


def privacy_metadata(manifest: Dict[str, Any]) -> Dict[str, Any]:
    selected_count = int(manifest["claude"]["sessions"].get("selected_count") or 0)
    include_transcripts = bool(manifest["selection"].get("include_transcript_excerpts"))
    config = manifest["claude"].get("config", {})
    global_inventory_count = (
        len(config.get("mcp_candidates") or [])
        + len(config.get("skill_candidates") or [])
        + len(config.get("plugin_candidates") or [])
    )
    close_match_count = len(manifest["claude"]["sessions"].get("close_project_matches") or [])
    contains_local_inventory = bool(global_inventory_count or close_match_count)
    written_context = [
        "Project path and handoff metadata",
    ]
    if selected_count:
        written_context.append("Claude session titles and summaries")
        written_context.append("Recent prompts, assistant notes, commands, and local transcript paths in manifest JSON")
    if contains_local_inventory:
        written_context.append("Claude/Codex MCP, skill, plugin, and nearby project inventory with local paths")
    if include_transcripts:
        written_context.append("Fuller transcript excerpts")
    usage_summary = manifest["claude"]["sessions"].get("usage_summary") or {}
    if any(usage_kind_count(usage_summary, kind) for kind in ("mcp_servers", "skills", "plugins")):
        written_context.append("MCP, skill, and plugin usage inferred from selected Claude transcripts")
    return {
        "contains_claude_context": selected_count > 0 or contains_local_inventory,
        "selected_session_count": selected_count,
        "includes_transcript_excerpts": include_transcripts,
        "contains_local_inventory": contains_local_inventory,
        "global_inventory_count": global_inventory_count,
        "nearby_project_match_count": close_match_count,
        "ack_required_for_apply": selected_count > 0 or contains_local_inventory,
        "redaction": "best-effort",
        "written_context": written_context,
    }


def build_diagnostics(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    diagnostics: List[Dict[str, Any]] = []
    sessions = manifest["claude"]["sessions"]
    if not sessions.get("found_count"):
        diagnostics.append(
            {
                "severity": "warn",
                "code": "NO_CLAUDE_SESSIONS",
                "message": "No Claude conversations were found for this project path.",
                "next_command": f"ai-handoff doctor {shlex.quote(manifest['target_path'])}",
            }
        )
    elif not sessions.get("selected_count"):
        diagnostics.append(
            {
                "severity": "warn",
                "code": "NO_SELECTED_SESSIONS",
                "message": "No Claude conversations are selected for the handoff.",
                "next_command": f"ai-handoff conversations {shlex.quote(manifest['target_path'])}",
            }
        )
    missing = sessions.get("missing_requested_session_ids") or []
    if missing:
        diagnostics.append(
            {
                "severity": "error",
                "code": "MISSING_REQUESTED_SESSIONS",
                "message": "One or more requested Claude session IDs were not found.",
                "missing_session_ids": missing,
                "next_command": f"ai-handoff conversations {shlex.quote(manifest['target_path'])}",
            }
        )
    if not manifest["project"].get("has_claude_md"):
        diagnostics.append(
            {
                "severity": "warn",
                "code": "NO_CLAUDE_MD",
                "message": "CLAUDE.md was not found, so project guidance will rely on repo scanning and sessions.",
            }
        )
    if not manifest["codex"].get("trust_level"):
        diagnostics.append(
            {
                "severity": "info",
                "code": "CODEX_PROJECT_NOT_TRUSTED",
                "message": "The target project has no explicit Codex trust entry.",
            }
        )
    if manifest["project"].get("git", {}).get("status_short"):
        diagnostics.append(
            {
                "severity": "info",
                "code": "DIRTY_WORKTREE",
                "message": "The target project has uncommitted changes; ai-handoff will not revert them.",
            }
        )
    return diagnostics


def session_bullets(manifest: Dict[str, Any], limit: Optional[int] = 5, include_private: bool = False) -> List[str]:
    sessions = manifest["claude"]["sessions"].get("selected", [])
    bullets = []
    shown_sessions = sessions if limit is None else sessions[:limit]
    for session in shown_sessions:
        title = truncate(str(session.get("title") or "Untitled"), 120)
        modified = session.get("modified") or session.get("created") or "unknown time"
        bullets.append(f"- {title} ({modified})")
        summary = session.get("summary")
        if summary:
            bullets.append(f"  Summary: {truncate(str(summary), 220)}")
        if not include_private:
            continue
        prompt = session.get("first_prompt") or ""
        if prompt:
            bullets.append(f"  First prompt: {truncate(prompt, 220)}")
        transcript = session.get("transcript") or {}
        prompts = transcript.get("user_prompts") or []
        if prompts:
            bullets.append(f"  Recent prompt: {truncate(str(prompts[-1]), 220)}")
        commands = transcript.get("commands") or []
        if commands:
            bullets.append(f"  Commands seen: {truncate('; '.join(commands[:3]), 260)}")
    return bullets


def global_action_display_name(action: Dict[str, Any]) -> str:
    action_id = str(action.get("id") or action.get("name") or "unknown")
    if action.get("type") == "plugin" and action.get("bridge_name"):
        return f"{action_id} bridged as {action.get('bridge_name')}"
    if action.get("type") == "skill" and action.get("name"):
        return f"{action_id} copied as {action.get('name')}"
    return action_id


def codex_tooling_status_lines(manifest: Dict[str, Any]) -> List[str]:
    selected_actions = [
        item for item in manifest.get("selected_global_actions") or [] if isinstance(item, dict)
    ]
    actions_by_id = {str(item.get("id")): item for item in selected_actions if item.get("id")}
    results = [item for item in manifest.get("global_apply_results") or [] if isinstance(item, dict)]
    ok_results = [item for item in results if item.get("status") == "ok"]
    skipped_results = [item for item in results if item.get("status") and item.get("status") != "ok"]
    ok_ids = {str(item.get("id")) for item in ok_results if item.get("id")}
    recorded_only = [item for item in selected_actions if str(item.get("id")) not in ok_ids]
    lines: List[str] = []
    if ok_results:
        lines.append("- Installed Codex-wide for future Codex sessions:")
        for result in ok_results:
            action = actions_by_id.get(str(result.get("id")), {"id": result.get("id")})
            lines.append(f"  - {global_action_display_name(action)}")
        lines.append("- Open a new Codex session after Codex-wide plugin or skill installs.")
    if recorded_only:
        lines.append("- Selected Codex-wide actions were recorded but not installed:")
        for action in recorded_only:
            lines.append(f"  - {global_action_display_name(action)}")
    if not ok_results and not recorded_only:
        lines.append("- No Codex-wide MCP, plugin, or skill installs were executed by this run.")
    if skipped_results:
        lines.append("- Skipped/manual Codex-wide actions:")
        for result in skipped_results:
            reason = str(result.get("reason") or "manual follow-up required")
            lines.append(f"  - {result.get('id')}: {reason}")
    lines.append("- Full install details are in .codex/handoff/manifest.json.")
    return lines


def render_summary(manifest: Dict[str, Any]) -> str:
    project = manifest["project"]
    codex = manifest["codex"]
    sessions = manifest["claude"]["sessions"]
    lines = [
        "# AI Handoff Summary",
        "",
        f"Generated: {manifest['generated_at']}",
        f"Project: {manifest['target_path']}",
        f"Suggested Codex goal: {manifest['suggested_goal']}",
        "",
        "## Project Readiness",
        f"- CLAUDE.md: {'found' if project['has_claude_md'] else 'missing'}",
        f"- AGENTS.md: {'found' if project['has_agents_md'] else 'missing'}",
        f"- Codex trust level: {codex.get('trust_level') or 'not configured'}",
        f"- Git branch: {project['git'].get('branch') or 'unknown'}",
        "",
        "## Recent Claude Context",
        f"- Claude project index: {'found' if sessions.get('index_exists') else 'missing'}",
        f"- Sessions found: {sessions.get('found_count', 0)}",
        f"- Sessions selected: {sessions.get('selected_count', 0)}",
    ]
    bullets = session_bullets(
        manifest,
        limit=None,
        include_private=bool(manifest["selection"].get("include_transcript_excerpts")),
    )
    lines.extend(bullets if bullets else ["- No recent Claude sessions selected."])
    usage_summary = sessions.get("usage_summary") or {}
    if any(usage_kind_count(usage_summary, kind) for kind in ("mcp_servers", "skills", "plugins")):
        lines.extend(
            [
                "",
                "## Claude Tooling Used",
                f"- MCPs: {usage_kind_names(usage_summary, 'mcp_servers')}",
                f"- Skills: {usage_kind_names(usage_summary, 'skills')}",
                f"- Plugins: {usage_kind_names(usage_summary, 'plugins')}",
            ]
        )
    lines.extend(["", "## Commands And Entrypoints"])
    scripts = project.get("package_scripts") or {}
    if scripts:
        lines.extend(f"- npm run {name}: {command}" for name, command in scripts.items())
    py_commands = project.get("pyproject_commands") or []
    lines.extend(f"- {command}" for command in py_commands)
    if not scripts and not py_commands:
        lines.append("- No package or pyproject commands detected.")
    lines.extend(["", "## Project-Local Writes"])
    lines.extend(f"- {path}" for path in manifest["actions"].get("project_writes", []))
    lines.extend(["", "## Codex-Wide Install Candidates"])
    mcp_count = len(manifest["actions"].get("mcp_commands") or [])
    skill_count = len(manifest["actions"].get("skill_conversions") or [])
    plugin_count = len(manifest["actions"].get("plugin_commands") or [])
    lines.extend(
        [
            f"- MCP installs: {mcp_count} discovered, disabled by default",
            f"- Skill imports: {skill_count} discovered, disabled by default",
            f"- Plugin records: {plugin_count} discovered, disabled by default",
        ]
    )
    lines.extend(["", "## Codex Tooling Prepared"])
    lines.extend(codex_tooling_status_lines(manifest))
    return "\n".join(lines).rstrip() + "\n"


def render_agents_managed_section(manifest: Dict[str, Any]) -> str:
    project = manifest["project"]
    lines = [
        MANAGED_START,
        "# Codex Handoff Context",
        "",
        "This section is managed by ai-handoff. Keep durable project rules outside the managed markers.",
        "Codex loads AGENTS.md automatically when it works in this project; use this managed section as handoff context before starting work.",
        "",
        f"Generated: {manifest['generated_at']}",
        f"Source project: {manifest['target_path']}",
        f"Suggested goal: {manifest['suggested_goal']}",
        "",
        "## Project Guidance",
    ]
    if project["has_claude_md"]:
        lines.append("- CLAUDE.md was found and used as source guidance.")
        headings = next((item.get("headings") for item in project["guidance_files"] if item.get("path") == "CLAUDE.md"), [])
        if headings:
            lines.append("- CLAUDE.md headings: " + "; ".join(headings[:10]))
    else:
        lines.append("- CLAUDE.md was not found.")
    lines.extend(["", "## Recent Claude Work"])
    lines.append(f"- Selected sessions: {manifest['claude']['sessions'].get('selected_count', 0)} of {manifest['claude']['sessions'].get('found_count', 0)} discovered.")
    bullets = session_bullets(
        manifest,
        limit=None,
        include_private=bool(manifest["selection"].get("include_transcript_excerpts")),
    )
    lines.extend(bullets if bullets else ["- No recent Claude sessions selected."])
    usage_summary = manifest["claude"]["sessions"].get("usage_summary") or {}
    if any(usage_kind_count(usage_summary, kind) for kind in ("mcp_servers", "skills", "plugins")):
        lines.extend(
            [
                "",
                "## Claude Tooling Used",
                f"- MCPs: {usage_kind_names(usage_summary, 'mcp_servers')}",
                f"- Skills: {usage_kind_names(usage_summary, 'skills')}",
                f"- Plugins: {usage_kind_names(usage_summary, 'plugins')}",
            ]
        )
    lines.extend(["", "## Codex Tooling Prepared"])
    lines.extend(codex_tooling_status_lines(manifest))
    lines.extend(["", "## Commands Detected"])
    scripts = project.get("package_scripts") or {}
    if scripts:
        lines.extend(f"- npm run {name}: {command}" for name, command in scripts.items())
    py_commands = project.get("pyproject_commands") or []
    lines.extend(f"- {command}" for command in py_commands)
    if not scripts and not py_commands:
        lines.append("- No package or pyproject commands detected; inspect README and source before running tests.")
    lines.extend(
        [
            "",
            "## Handoff Artifacts",
            "- Detailed summary: .codex/handoff/summary.md",
            "- Latest manifest: .codex/handoff/manifest.json",
            "",
            "## Safety Notes",
            "- Codex-wide MCP, plugin, and skill installs require explicit confirmation.",
            "- Review Codex-wide install details in the manifest before running or changing them.",
            MANAGED_END,
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def merge_agents(existing: Optional[str], managed: str) -> str:
    if not existing:
        return "# AGENTS.md\n\n" + managed
    if MANAGED_START in existing and MANAGED_END in existing:
        pattern = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), re.S)
        return pattern.sub(managed.rstrip(), existing).rstrip() + "\n"
    return existing.rstrip() + "\n\n" + managed


def plan_project_writes(manifest: Dict[str, Any]) -> List[Tuple[Path, str, str]]:
    project = Path(manifest["target_path"])
    selection = manifest["selection"]
    handoff_dir = project / ".codex" / "handoff"
    runs_dir = handoff_dir / "runs"
    planned: List[Tuple[Path, str, str]] = []
    if selection.get("write_agents"):
        existing = read_text(project / "AGENTS.md", 100000)
        content = merge_agents(existing, render_agents_managed_section(manifest))
        planned.append((project / "AGENTS.md", content, "AGENTS.md"))
    if selection.get("write_summary"):
        planned.append((handoff_dir / "summary.md", render_summary(manifest), ".codex/handoff/summary.md"))
    if selection.get("write_manifest"):
        data = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        planned.append((handoff_dir / "manifest.json", data, ".codex/handoff/manifest.json"))
        planned.append((runs_dir / f"{manifest['run_id']}.json", data, f".codex/handoff/runs/{manifest['run_id']}.json"))
    return planned


def atomic_write_planned(planned: List[Tuple[Path, str, str]], run_id: str) -> List[str]:
    written = [relative for _, _, relative in planned]
    temp_paths: List[Path] = []
    try:
        for target, content, _ in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.ai-handoff-{run_id}.tmp")
            temp.write_text(content, encoding="utf-8")
            temp_paths.append(temp)
        for temp, (target, _, _) in zip(temp_paths, planned):
            os.replace(temp, target)
    except OSError as exc:
        raise HandoffError(f"could not write handoff artifacts: {exc}") from exc
    finally:
        for temp in temp_paths:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
    return written


def write_manifest_artifacts(manifest: Dict[str, Any]) -> Dict[str, Any]:
    project = Path(manifest["target_path"])
    handoff_dir = project / ".codex" / "handoff"
    runs_dir = handoff_dir / "runs"
    data = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    planned = [
        (handoff_dir / "manifest.json", data, ".codex/handoff/manifest.json"),
        (runs_dir / f"{manifest['run_id']}.json", data, f".codex/handoff/runs/{manifest['run_id']}.json"),
    ]
    written = atomic_write_planned(planned, str(manifest["run_id"]))
    return {"written": written, "manifest": manifest}


def write_project_artifacts(manifest: Dict[str, Any]) -> Dict[str, Any]:
    planned = plan_project_writes(manifest)
    written = [relative for _, _, relative in planned]
    manifest["applied"] = True
    manifest["applied_at"] = utc_now()
    manifest["applied_actions"] = written.copy()
    planned = plan_project_writes(manifest)
    written = atomic_write_planned(planned, str(manifest["run_id"]))
    return {"written": written, "manifest": manifest}


def render_diff(manifest: Dict[str, Any], include_manifest: bool = False) -> str:
    sections: List[str] = []
    for target, content, relative in plan_project_writes(manifest):
        if not include_manifest and (
            relative == ".codex/handoff/manifest.json" or relative.startswith(".codex/handoff/runs/")
        ):
            continue
        old_text = read_text(target, 1_000_000)
        old_lines = [] if old_text is None else old_text.splitlines(keepends=True)
        new_lines = content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
        rendered = "\n".join(diff)
        if rendered:
            sections.append(rendered)
    return "\n\n".join(sections) + ("\n" if sections else "")


def print_dry_run(manifest: Dict[str, Any]) -> None:
    project = manifest["project"]
    sessions = manifest["claude"]["sessions"]
    config = manifest["claude"]["config"]
    print("AI Handoff dry run")
    print()
    print(f"Project: {manifest['target_path']}")
    print(f"Goal: {manifest['suggested_goal']}")
    confidence = manifest.get("handoff_confidence", {})
    print(f"Handoff confidence: {confidence.get('level', 'unknown')} - {confidence.get('reason', '')}")
    print()
    print("Project guidance:")
    print(f"  [info] CLAUDE.md: {'found' if project['has_claude_md'] else 'missing'}")
    print(f"  [info] AGENTS.md: {'found' if project['has_agents_md'] else 'missing'}")
    print(f"  [info] Codex trust: {manifest['codex'].get('trust_level') or 'not configured'}")
    print()
    print("Claude context:")
    print(f"  [info] sessions found: {sessions.get('found_count', 0)}")
    print(f"  [info] sessions selected: {sessions.get('selected_count', 0)}")
    source_keys = sessions.get("source_project_keys") or []
    if source_keys:
        print(f"  [info] Claude project keys: {', '.join(source_keys[:3])}" + (" ..." if len(source_keys) > 3 else ""))
    usage_summary = sessions.get("usage_summary") or {}
    if any(usage_kind_count(usage_summary, kind) for kind in ("mcp_servers", "skills", "plugins")):
        print(f"  [info] MCPs used in transcripts: {usage_kind_names(usage_summary, 'mcp_servers')}")
        print(f"  [info] skills used in transcripts: {usage_kind_names(usage_summary, 'skills')}")
        print(f"  [info] plugins used in transcripts: {usage_kind_names(usage_summary, 'plugins')}")
    for bullet in session_bullets(manifest, limit=3):
        print("  " + bullet)
    if not sessions.get("found_count") and sessions.get("close_project_matches"):
        print("  [info] nearby Claude project folders found; run conversations --all-projects --search TEXT")
    print()
    print("Codex readiness:")
    print(f"  [info] MCP candidates: {len(config.get('mcp_candidates', []))}")
    print(f"  [info] skill candidates: {len(config.get('skill_candidates', []))}")
    print(f"  [info] plugin candidates: {len(config.get('plugin_candidates', []))}")
    diagnostics = manifest.get("diagnostics") or []
    if diagnostics:
        print()
        print("Diagnostics:")
        for diagnostic in diagnostics[:5]:
            severity = diagnostic.get("severity", "info")
            code = diagnostic.get("code", "INFO")
            message = diagnostic.get("message", "")
            print(f"  [{severity}] {code}: {message}")
    print()
    print("Would write:")
    for path in manifest["actions"].get("project_writes", []):
        print(f"  - {path}")
    print()
    print("Would not change by default:")
    print("  - ~/.codex/config.toml (Codex-wide settings)")
    print("  - ~/.codex/skills (available to every Codex project)")
    print("  - Codex MCP/plugin registries")
    print()
    print("No files changed.")
    print()
    print("Next:")
    print(f"  Review conversations: ai-handoff conversations {shlex.quote(manifest['target_path'])}")
    print(f"  Preview writes:        ai-handoff diff {shlex.quote(manifest['target_path'])}")
    if confidence.get("level") == "low":
        print(f"  Diagnose context:      ai-handoff doctor {shlex.quote(manifest['target_path'])}")
    else:
        print(f"  Apply project files:   ai-handoff apply {shlex.quote(manifest['target_path'])} --yes --ack-privacy")
    print(f"  Inspect Codex-wide:    ai-handoff globals {shlex.quote(manifest['target_path'])}")


INTERACTIVE_OPTIONS = [
    ("claude_context", "Claude context"),
    ("write_agents", "Create/update AGENTS.md"),
    ("write_summary", "Write .codex/handoff/summary.md"),
    ("write_manifest", "Write .codex/handoff/manifest.json"),
    ("global_imports", "Codex-wide installs"),
    ("include_transcript_excerpts", "Include fuller transcript excerpts on next scan"),
]
ANSI_CLEAR_VIEWPORT = "\033[2J\033[H"
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"
GLOBAL_ACTION_TYPES = {"mcp", "skill", "plugin"}
GLOBAL_PICKER_MODES = ("used", "all", "mcp", "skill", "plugin", "manual")
GLOBAL_PICKER_PAGE_SIZE = 10


def supports_static_menu() -> bool:
    term = os.environ.get("TERM", "")
    return bool(sys.stdin.isatty() and sys.stdout.isatty() and term and term != "dumb")


def render_interactive_menu(manifest: Dict[str, Any], cursor: int = 0) -> str:
    selection = manifest["selection"]
    session_ids = manifest["claude"]["sessions"].get("selected_session_ids") or []
    selected_session_text = ", ".join(session_ids[:3]) if session_ids else "none"
    if len(session_ids) > 3:
        selected_session_text += f", +{len(session_ids) - 3} more"
    lines = [
        "",
        "AI Handoff",
        f"Project: {manifest['target_path']}",
        (
            f"Claude sessions: {manifest['claude']['sessions'].get('found_count', 0)} found, "
            f"{manifest['claude']['sessions'].get('selected_count', 0)} selected"
        ),
        f"Selected sessions: {selected_session_text}",
        f"Codex-wide installs: {manifest['actions']['global_changes_default']} by default",
        "Press g to review installs that affect every Codex project; project-file apply never changes ~/.codex by itself.",
        "",
    ]
    for index, (key, label) in enumerate(INTERACTIVE_OPTIONS, start=1):
        label, checked = interactive_option_state(manifest, key, label)
        mark = "x" if checked else " "
        pointer = ">" if index - 1 == cursor else " "
        lines.append(f"{pointer} {index}. [{mark}] {label}")
    lines.extend(
        [
            "",
            "Commands: Up/k and Down/j move, Space toggles/opens row, number toggles/opens row, c conversations, g globals, p preview, Enter/a apply, q quit",
        ]
    )
    return "\n".join(lines)


def interactive_option_state(manifest: Dict[str, Any], key: str, default_label: str) -> Tuple[str, bool]:
    selection = manifest["selection"]
    if key == "claude_context":
        selected_count = int(manifest["claude"]["sessions"].get("selected_count") or 0)
        found_count = int(manifest["claude"]["sessions"].get("found_count") or 0)
        session_word = "session" if selected_count == 1 else "sessions"
        return f"Claude context: {selected_count} {session_word} selected ({found_count} found)", selected_count > 0
    if key == "global_imports":
        selected_count = len(manifest.get("selected_global_action_ids") or [])
        executed_count = len([item for item in manifest.get("global_apply_results") or [] if item.get("status") == "ok"])
        return f"Codex-wide installs: {selected_count} selected, {executed_count} executed", selected_count > 0
    return default_label, bool(selection.get(key))


def draw_static_menu(manifest: Dict[str, Any], cursor: int) -> None:
    if supports_static_menu():
        sys.stdout.write(ANSI_CLEAR_VIEWPORT)
    print(render_interactive_menu(manifest, cursor), flush=True)


def show_static_preview(manifest: Dict[str, Any]) -> None:
    if supports_static_menu():
        sys.stdout.write(ANSI_CLEAR_VIEWPORT)
        sys.stdout.flush()
    print_dry_run(manifest)
    if supports_static_menu():
        print()
        print("Press any key to return to the menu.", flush=True)
        read_menu_key()


def read_menu_key() -> str:
    if not sys.stdin.isatty():
        raw = input("> ")
        if raw == " ":
            return "toggle"
        return normalize_menu_key(raw.strip())

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        char = sys.stdin.read(1)
        if char == "\x1b":
            sequence = sys.stdin.read(2)
            if sequence == "[A":
                return "up"
            if sequence == "[B":
                return "down"
            return "unknown"
        return normalize_menu_key(char)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def normalize_menu_key(raw: str) -> str:
    if raw == "A":
        return "select-visible"
    if raw == "C":
        return "clear-all"
    if raw == "\t":
        return "next-view"
    lowered = raw.lower()
    if lowered in {"k", "up"}:
        return "up"
    if lowered in {"j", "down"}:
        return "down"
    if lowered in {" ", "space"}:
        return "toggle"
    if lowered in {"x", "select"}:
        return "toggle"
    if lowered in {"", "\r", "\n", "a", "apply"}:
        return "apply"
    if lowered in {"p", "preview"}:
        return "preview"
    if lowered in {"/", "filter"}:
        return "filter"
    if lowered in {"?", "help"}:
        return "help"
    if lowered in {"d", "details"}:
        return "details"
    if lowered in {"u", "clear-visible"}:
        return "clear-visible"
    if lowered in {"i", "invert", "invert-visible"}:
        return "invert-visible"
    if lowered in {"f", "pagedown", "page-down"}:
        return "page-down"
    if lowered in {"b", "pageup", "page-up"}:
        return "page-up"
    if lowered in {"c", "conversations", "sessions"}:
        return "sessions"
    if lowered in {"g", "globals", "global"}:
        return "globals"
    if lowered in {"q", "quit"}:
        return "quit"
    if lowered.isdigit():
        return f"toggle:{lowered}"
    return "unknown"


def apply_interactive_key(key: str, cursor: int, selection: Dict[str, bool]) -> Tuple[str, int]:
    if key == "up":
        return "continue", (cursor - 1) % len(INTERACTIVE_OPTIONS)
    if key == "down":
        return "continue", (cursor + 1) % len(INTERACTIVE_OPTIONS)
    if key == "toggle":
        option_key = INTERACTIVE_OPTIONS[cursor][0]
        if option_key == "claude_context":
            return "sessions", cursor
        if option_key == "global_imports":
            return "globals", cursor
        selection[option_key] = not selection.get(option_key)
        return "continue", cursor
    if key.startswith("toggle:"):
        _, value = key.split(":", 1)
        index = int(value) - 1
        if 0 <= index < len(INTERACTIVE_OPTIONS):
            option_key = INTERACTIVE_OPTIONS[index][0]
            if option_key == "claude_context":
                return "sessions", index
            if option_key == "global_imports":
                return "globals", index
            selection[option_key] = not selection.get(option_key)
            return "continue", index
        return "unknown", cursor
    if key == "preview":
        return "preview", cursor
    if key == "sessions":
        return "sessions", cursor
    if key == "globals":
        return "globals", cursor
    if key == "apply":
        return "apply", cursor
    if key == "quit":
        return "quit", cursor
    return "unknown", cursor


def session_picker_page_size() -> int:
    return global_picker_page_size()


def session_picker_matches(candidate: Dict[str, Any], filter_text: str = "") -> bool:
    if not filter_text.strip():
        return True
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in (
            "session_id",
            "title",
            "summary",
            "first_prompt",
            "source_project_key",
            "git_branch",
            "transcript_path",
            "project_path",
        )
    ).lower()
    return filter_text.lower() in haystack


def visible_session_candidates(manifest: Dict[str, Any], filter_text: str = "") -> List[Dict[str, Any]]:
    sessions = manifest["claude"]["sessions"]
    return [
        candidate
        for candidate in sessions.get("candidates") or []
        if session_picker_matches(candidate, filter_text=filter_text)
    ]


def current_session_page_candidates(
    candidates: List[Dict[str, Any]],
    cursor: int,
    page_size: int = GLOBAL_PICKER_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    start, end = global_picker_page_window(cursor, len(candidates), page_size=page_size)
    return candidates[start:end]


def render_session_details(candidate: Dict[str, Any]) -> str:
    lines = [
        "Claude Conversation Details",
        "",
        f"ID: {candidate.get('session_id')}",
        f"Title: {candidate.get('title') or 'Untitled'}",
        f"Modified: {candidate.get('modified') or candidate.get('created') or 'unknown time'}",
        f"Messages: {candidate.get('message_count', 'unknown')}",
        f"Branch: {candidate.get('git_branch') or 'unknown'}",
        f"Source project: {candidate.get('source_project_key') or 'current project'}",
    ]
    if candidate.get("transcript_path"):
        lines.append(f"Transcript: {candidate['transcript_path']}")
    if candidate.get("first_prompt"):
        lines.extend(["", "First prompt:", str(candidate["first_prompt"])])
    if candidate.get("summary"):
        lines.extend(["", "Summary:", str(candidate["summary"])])
    return "\n".join(lines)


def render_session_picker(
    manifest: Dict[str, Any],
    cursor: int = 0,
    selected_ids: Optional[List[str]] = None,
    filter_text: str = "",
    page_size: int = GLOBAL_PICKER_PAGE_SIZE,
) -> str:
    sessions = manifest["claude"]["sessions"]
    all_candidates = sessions.get("candidates") or []
    candidates = visible_session_candidates(manifest, filter_text=filter_text)
    selected_id_set = set(selected_ids if selected_ids is not None else sessions.get("selected_session_ids") or [])
    start, end = global_picker_page_window(cursor, len(candidates), page_size=page_size)
    page = candidates[start:end]
    page_count = max(1, (len(candidates) + page_size - 1) // page_size)
    current_page = 1 if not candidates else (start // page_size) + 1
    lines = [
        "",
        "Choose Claude Conversations",
        f"Selection: {sessions.get('selection_strategy')}",
        (
            f"Filter: {filter_text or 'none'} | "
            f"Showing: {start + 1 if candidates else 0}-{end} of {len(candidates)} | "
            f"Page: {current_page}/{page_count} | Selected: {len(selected_id_set)} | "
            f"Found: {len(all_candidates)}"
        ),
        "",
    ]
    if not candidates:
        if all_candidates and filter_text:
            lines.append("No Claude conversations match this filter.")
            lines.append("Clear the filter with / then Enter.")
        else:
            lines.append("No Claude conversations found for this project.")
            lines.append(
                f"Try: ai-handoff conversations {shlex.quote(manifest['target_path'])} "
                "--all-projects --search TEXT"
            )
    for offset, candidate in enumerate(page, start=1):
        absolute_index = start + offset - 1
        session_id = str(candidate.get("session_id") or "")
        mark = "x" if session_id in selected_id_set else " "
        pointer = ">" if absolute_index == cursor else " "
        modified = candidate.get("modified") or candidate.get("created") or "unknown time"
        title = truncate(str(candidate.get("title") or "Untitled"), 90)
        source = candidate.get("source_project_key")
        source_text = f" | {source}" if source else ""
        lines.append(f"{pointer} {offset}. [{mark}] {title} ({modified}{source_text})")
        prompt = candidate.get("first_prompt")
        if prompt:
            lines.append(f"     {truncate(str(prompt), 120)}")
    lines.extend(
        [
            "",
            "Commands: Up/k Down/j, f/b page, Space/x toggle, number toggles, / filter, d details, Enter done, q cancel",
            f"Recovery: ai-handoff conversations {shlex.quote(manifest['target_path'])} --all-projects --search TEXT",
        ]
    )
    return "\n".join(lines)


def update_session_selection(manifest: Dict[str, Any], selected_ids: List[str]) -> None:
    sessions = manifest["claude"]["sessions"]
    candidates = sessions.get("candidates") or []
    candidate_by_id = {str(candidate.get("session_id")): candidate for candidate in candidates}
    selected = []
    for session_id in selected_ids:
        candidate = candidate_by_id.get(str(session_id))
        if not candidate:
            continue
        transcript = summarize_transcript(
            Path(str(candidate.get("transcript_path"))),
            include_excerpts=bool(manifest["selection"].get("include_transcript_excerpts")),
        )
        selected.append(
            {
                "session_id": candidate.get("session_id"),
                "title": candidate.get("title"),
                "summary": candidate.get("summary"),
                "first_prompt": candidate.get("first_prompt"),
                "created": candidate.get("created"),
                "modified": candidate.get("modified"),
                "message_count": candidate.get("message_count"),
                "git_branch": candidate.get("git_branch"),
                "transcript": transcript,
            }
        )
    sessions["selected_session_ids"] = [str(item.get("session_id")) for item in selected if item.get("session_id")]
    sessions["selected_count"] = len(selected)
    sessions["selected"] = selected
    sessions["usage_summary"] = aggregate_session_usage(selected)
    sessions["selection_strategy"] = "user-selected conversations"
    manifest["writes"] = project_write_metadata(manifest)
    manifest["diagnostics"] = build_diagnostics(manifest)
    manifest["global_candidates"] = global_action_candidates(manifest)
    manifest["privacy"] = privacy_metadata(manifest)
    manifest["handoff_confidence"] = handoff_confidence(manifest)
    manifest.pop("_display_global_action_candidates", None)


def session_picker(manifest: Dict[str, Any]) -> None:
    sessions = manifest["claude"]["sessions"]
    all_candidates = sessions.get("candidates") or []
    if not all_candidates:
        show_message_screen("No Claude conversations found for this project.")
        return
    selected_ids = list(sessions.get("selected_session_ids") or [])
    cursor = 0
    filter_text = ""
    while True:
        page_size = session_picker_page_size()
        candidates = visible_session_candidates(manifest, filter_text=filter_text)
        if cursor >= len(candidates):
            cursor = max(0, len(candidates) - 1)
        if supports_static_menu():
            sys.stdout.write(ANSI_CLEAR_VIEWPORT)
        print(
            render_session_picker(
                manifest,
                cursor,
                selected_ids=selected_ids,
                filter_text=filter_text,
                page_size=page_size,
            ),
            flush=True,
        )
        action, cursor = apply_picker_key(read_menu_key(), cursor, len(candidates), page_size=page_size)
        if action == "done":
            update_session_selection(manifest, selected_ids)
            return
        if action == "cancel":
            return
        if action == "filter":
            if supports_static_menu():
                sys.stdout.write(ANSI_CLEAR_VIEWPORT)
            filter_text = prompt_static_line("Filter conversations (empty clears): ").strip()
            cursor = 0
            continue
        if action == "details":
            if candidates:
                show_message_screen(render_session_details(candidates[cursor]))
            continue
        if action == "toggle":
            if not candidates:
                continue
            session_id = str(candidates[cursor].get("session_id") or "")
            if session_id in selected_ids:
                selected_ids.remove(session_id)
            elif session_id:
                selected_ids.append(session_id)
        elif action.startswith("toggle:"):
            visible_number = int(action.split(":", 1)[1])
            visible_index = 9 if visible_number == 0 else visible_number - 1
            start, end = global_picker_page_window(cursor, len(candidates), page_size=page_size)
            index = start + visible_index
            if start <= index < end and index < len(candidates):
                cursor = index
                session_id = str(candidates[cursor].get("session_id") or "")
                if session_id in selected_ids:
                    selected_ids.remove(session_id)
                elif session_id:
                    selected_ids.append(session_id)


def apply_picker_key(
    key: str,
    cursor: int,
    count: int,
    page_size: int = GLOBAL_PICKER_PAGE_SIZE,
) -> Tuple[str, int]:
    if count <= 0:
        if key in {"filter", "help"}:
            return key, 0
        if key == "quit":
            return "cancel", 0
        if key == "apply":
            return "done", 0
        return "continue", 0
    if key == "up":
        return "continue", (cursor - 1) % count
    if key == "down":
        return "continue", (cursor + 1) % count
    if key == "page-up":
        return "continue", max(0, cursor - page_size)
    if key == "page-down":
        return "continue", min(count - 1, cursor + page_size)
    if key == "toggle":
        return "toggle", cursor
    if key in {"filter", "details"}:
        return key, cursor
    if key.startswith("toggle:"):
        return key, cursor
    if key == "apply":
        return "done", cursor
    if key == "quit":
        return "cancel", cursor
    return "continue", cursor


def normalized_usage_tokens(value: Any) -> set:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    tokens = {text}
    if text.startswith("/"):
        tokens.add(text[1:])
    if "@" in text:
        tokens.add(text.split("@", 1)[0])
    if ":" in text:
        tokens.add(text.split(":", 1)[1])
        tokens.add(text.split(":", 1)[0])
    return {token for token in tokens if token}


def usage_items_for_candidate(candidate_type: str, usage_summary: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    kind_by_type = {"mcp": "mcp_servers", "skill": "skills", "plugin": "plugins"}
    return usage_summary.get(kind_by_type.get(candidate_type, ""), []) or []


def candidate_matches_usage_name(candidate: Dict[str, Any], usage_name: Any) -> bool:
    candidate_tokens = normalized_usage_tokens(candidate.get("name"))
    candidate_tokens.update(normalized_usage_tokens(candidate.get("id")))
    candidate_tokens.update(normalized_usage_tokens(candidate.get("label")))
    usage_tokens = normalized_usage_tokens(usage_name)
    if not candidate_tokens or not usage_tokens:
        return False
    return bool(candidate_tokens.intersection(usage_tokens))


def annotate_candidate_with_transcript_usage(
    candidate: Dict[str, Any],
    usage_summary: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    matches = [
        item
        for item in usage_items_for_candidate(str(candidate.get("type") or ""), usage_summary)
        if candidate_matches_usage_name(candidate, item.get("name"))
    ]
    if not matches:
        candidate["used_in_selected_sessions"] = False
        candidate["transcript_usage"] = {"count": 0, "matches": []}
        return candidate
    total_count = sum(int(item.get("count") or 0) for item in matches)
    match_names = ", ".join(str(item.get("name")) for item in matches[:3])
    evidence = []
    for item in matches:
        evidence.extend(str(entry) for entry in item.get("evidence") or [])
    candidate["used_in_selected_sessions"] = True
    candidate["transcript_usage"] = {
        "count": total_count,
        "matches": matches[:5],
    }
    candidate["relevance_score"] = "high"
    candidate["why_relevant"] = f"Used in selected Claude transcript(s): {match_names}"
    if evidence:
        candidate["evidence"] = "transcript usage: " + "; ".join(dict.fromkeys(evidence[:3]))
    if candidate.get("type") == "plugin" and candidate.get("confidence") == "low":
        candidate["confidence"] = "medium"
    return candidate


def global_action_candidates(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    config = manifest.get("claude", {}).get("config", {})
    project = Path(str(manifest.get("target_path") or "."))
    usage_summary = manifest.get("claude", {}).get("sessions", {}).get("usage_summary") or {}
    mcp_items = [
        item
        for item in config.get("mcp_candidates", [])
        if isinstance(item, dict) and item.get("proposed_command") and not item.get("already_in_codex")
    ]
    if not mcp_items:
        mcp_items = [
            {"name": action_hash(command), "proposed_command": command}
            for command in manifest["actions"].get("mcp_commands") or []
        ]
    for item in mcp_items:
        item = dict(item)
        if "risk_badges" not in item:
            item.update(candidate_scope_and_risk("mcp", item, project))
        command = str(item.get("proposed_command") or "")
        name = stable_id_part(str(item.get("name") or action_hash(command)), action_hash(command))
        candidates.append(
            annotate_candidate_with_transcript_usage(
                {
                "id": f"mcp:{name}",
                "type": "mcp",
                "label": command,
                "command": command,
                "name": name,
                "source_path": item.get("source"),
                "hash": action_hash({"type": "mcp", "command": command}),
                "confidence": "medium",
                "reason": "global-claude-mcp-config",
                "source_scope": item.get("source_scope", "global"),
                "risk": item.get("risk", "medium"),
                "risk_badges": item.get("risk_badges") or [],
                "requires_network": bool(item.get("requires_network")),
                "requires_login": "--url" in command or "oauth" in command.lower(),
                "portable": bool(item.get("portable")),
                "why_relevant": item.get("why_relevant", "Global Claude MCP configuration."),
                "evidence": item.get("evidence", item.get("source") or "Claude MCP configuration"),
                "relevance_score": item.get("relevance_score", "low"),
                "blocked_reason": item.get("blocked_reason", ""),
                "manual_steps": item.get("manual_steps") or [],
                "safe_to_auto_apply": False,
                },
                usage_summary,
            )
        )
    for item in manifest["actions"].get("skill_conversions") or []:
        item = dict(item)
        if "risk_badges" not in item:
            item.update(candidate_scope_and_risk("skill", item, project))
        name = str(item.get("name") or Path(str(item.get("path") or "")).name)
        candidates.append(
            annotate_candidate_with_transcript_usage(
                {
                "id": f"skill:{name}",
                "type": "skill",
                "label": f"Copy Claude skill {name} -> ~/.codex/skills/{name}",
                "name": name,
                "source_path": item.get("path"),
                "destination_path": str(home_dir() / ".codex" / "skills" / name),
                "hash": action_hash({"type": "skill", "name": name, "source_path": item.get("path")}),
                "confidence": "medium",
                "reason": "claude-skill-folder",
                "source_scope": item.get("source_scope", "global"),
                "risk": item.get("risk", "medium"),
                "risk_badges": item.get("risk_badges") or [],
                "requires_network": bool(item.get("requires_network")),
                "requires_login": False,
                "portable": bool(item.get("portable")),
                "why_relevant": item.get("why_relevant", "Claude skill folder."),
                "evidence": item.get("evidence", item.get("path") or "Claude skill folder"),
                "relevance_score": item.get("relevance_score", "low"),
                "blocked_reason": item.get("blocked_reason", ""),
                "manual_steps": item.get("manual_steps") or [],
                "safe_to_auto_apply": False,
                },
                usage_summary,
            )
        )
    plugin_items = [
        item
        for item in config.get("plugin_candidates", [])
        if isinstance(item, dict) and item.get("proposed_command") and not item.get("already_in_codex")
    ]
    if not plugin_items:
        plugin_items = [
            {"name": action_hash(command), "proposed_command": command}
            for command in manifest["actions"].get("plugin_commands") or []
        ]
    for item in plugin_items:
        item = dict(item)
        if "risk_badges" not in item:
            item.update(candidate_scope_and_risk("plugin", item, project))
        command = str(item.get("proposed_command") or "")
        name = stable_id_part(str(item.get("name") or action_hash(command)), action_hash(command))
        candidates.append(
            annotate_candidate_with_transcript_usage(
                {
                "id": f"plugin:{name}",
                "type": "plugin",
                "label": command,
                "command": command,
                "name": name,
                "source_path": item.get("source"),
                "install_path": item.get("install_path"),
                "bridge": bool(item.get("bridge")),
                "bridge_name": item.get("bridge_name"),
                "bridge_source_path": item.get("bridge_source_path"),
                "bridge_cache_fallback_path": item.get("bridge_cache_fallback_path"),
                "bridge_destination_path": item.get("bridge_destination_path"),
                "bridge_marketplace": item.get("bridge_marketplace"),
                "bridge_source_plugin": item.get("bridge_source_plugin"),
                "bridge_source_selector": item.get("bridge_source_selector"),
                "bridge_source_kind": item.get("bridge_source_kind"),
                "bridge_source_ref": item.get("bridge_source_ref"),
                "bridge_source_subdir": item.get("bridge_source_subdir"),
                "bridge_source_repo_root": item.get("bridge_source_repo_root"),
                "bridge_commit": item.get("bridge_commit"),
                "bridge_version": item.get("bridge_version"),
                "bridge_skill_count": item.get("bridge_skill_count", 0),
                "bridge_agent_count": item.get("bridge_agent_count", 0),
                "origin_marketplace": item.get("origin_marketplace", ""),
                "origin_marketplace_root": item.get("origin_marketplace_root", ""),
                "origin_source_path": item.get("origin_source_path", ""),
                "origin_source_url": item.get("origin_source_url", ""),
                "origin_github_repo": item.get("origin_github_repo", ""),
                "origin_github_url": item.get("origin_github_url", ""),
                "origin_ref": item.get("origin_ref", ""),
                "origin_subdir": item.get("origin_subdir", ""),
                "origin_sha": item.get("origin_sha", ""),
                "codex_release_status": item.get("codex_release_status", "not-detected"),
                "codex_release_evidence": item.get("codex_release_evidence", ""),
                "codex_release_manifest_path": item.get("codex_release_manifest_path", ""),
                "codex_release_check_urls": item.get("codex_release_check_urls") or [],
                "hash": action_hash({"type": "plugin", "command": command}),
                "confidence": item.get("confidence", "low"),
                "reason": "claude-plugin-selector-needs-codex-marketplace-verification",
                "source_scope": item.get("source_scope", "global"),
                "risk": item.get("risk", "high"),
                "risk_badges": item.get("risk_badges") or [],
                "requires_network": bool(item.get("requires_network")),
                "requires_login": False,
                "portable": bool(item.get("portable")),
                "why_relevant": item.get("why_relevant", "Claude plugin install record."),
                "evidence": item.get("evidence", item.get("source") or "Claude plugin inventory"),
                "relevance_score": item.get("relevance_score", "low"),
                "blocked_reason": item.get("blocked_reason", ""),
                "manual_steps": item.get("manual_steps") or [],
                "safe_to_auto_apply": False,
                },
                usage_summary,
            )
        )
    return candidates


def candidate_is_risky(candidate: Dict[str, Any]) -> bool:
    badges = set(candidate.get("risk_badges") or [])
    return bool(
        candidate.get("blocked_reason")
        or candidate.get("confidence") == "low"
        or candidate.get("risk") == "high"
        or badges.intersection({"secret", "unverified", "global-scope"})
    )


def filtered_global_candidates(
    manifest: Dict[str, Any],
    *,
    include_low_confidence: bool = False,
    include_risky: bool = False,
    project_only: bool = False,
    portable_only: bool = False,
) -> List[Dict[str, Any]]:
    candidates = global_action_candidates(manifest)
    result = []
    for candidate in candidates:
        if project_only and candidate.get("source_scope") != "project":
            continue
        if portable_only and not candidate.get("portable"):
            continue
        if not include_low_confidence and not include_risky and candidate.get("confidence") == "low":
            continue
        if not include_risky and candidate.get("blocked_reason") and candidate.get("confidence") == "low":
            continue
        result.append(candidate)
    return result


def hidden_global_candidate_counts(manifest: Dict[str, Any], visible: List[Dict[str, Any]]) -> Dict[str, int]:
    visible_ids = {candidate.get("id") for candidate in visible}
    hidden = [candidate for candidate in global_action_candidates(manifest) if candidate.get("id") not in visible_ids]
    return {
        "total": len(hidden),
        "low_confidence": sum(1 for candidate in hidden if candidate.get("confidence") == "low"),
        "risky": sum(1 for candidate in hidden if candidate_is_risky(candidate)),
    }


def candidate_can_be_carried_over(candidate: Dict[str, Any]) -> bool:
    badges = set(candidate.get("risk_badges") or [])
    if candidate.get("blocked_reason") or "secret" in badges:
        return False
    if candidate.get("confidence") == "low" and not candidate.get("bridge"):
        return False
    action_type = candidate.get("type")
    if action_type == "plugin":
        return bool(candidate.get("bridge") or candidate.get("command"))
    if action_type == "skill":
        return bool(candidate.get("source_path") and candidate.get("destination_path"))
    if action_type == "mcp":
        return bool(candidate.get("command"))
    return False


def conversation_matched_global_candidates(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        candidate
        for candidate in display_global_action_candidates(manifest)
        if candidate.get("used_in_selected_sessions") and candidate_can_be_carried_over(candidate)
    ]


def global_candidate_group(candidate: Dict[str, Any]) -> str:
    if candidate.get("used_in_selected_sessions") and candidate_can_be_carried_over(candidate):
        return "Conversation Matched"
    if candidate.get("blocked_reason") or candidate.get("risk") == "high" or candidate.get("confidence") == "low":
        return "Manual / Unsafe"
    if candidate.get("source_scope") == "project" and candidate.get("portable"):
        return "Recommended"
    return "Review"


def global_candidate_sort_key(candidate: Dict[str, Any]) -> Tuple[int, str, str]:
    group_order = {"Conversation Matched": 0, "Recommended": 1, "Review": 2, "Manual / Unsafe": 3}
    return (
        group_order.get(global_candidate_group(candidate), 9),
        0 if candidate.get("used_in_selected_sessions") else 1,
        str(candidate.get("type") or ""),
        str(candidate.get("id") or ""),
    )


def global_picker_matches(candidate: Dict[str, Any], filter_text: str = "", mode: str = "all") -> bool:
    if mode == "used" and not candidate.get("used_in_selected_sessions"):
        return False
    if mode in GLOBAL_ACTION_TYPES and candidate.get("type") != mode:
        return False
    if mode == "manual" and global_candidate_group(candidate) != "Manual / Unsafe":
        return False
    if filter_text:
        needle = filter_text.lower()
        haystack = " ".join(
            str(candidate.get(key) or "")
            for key in (
                "id",
                "type",
                "label",
                "name",
                "source_path",
                "install_path",
                "bridge_name",
                "bridge_source_path",
                "bridge_destination_path",
                "origin_source_url",
                "origin_github_repo",
                "codex_release_status",
                "codex_release_evidence",
                "source_scope",
                "risk",
                "why_relevant",
                "evidence",
                "blocked_reason",
            )
        ).lower()
        badges = " ".join(str(item) for item in candidate.get("risk_badges") or []).lower()
        if needle not in haystack and needle not in badges:
            return False
    return True


def visible_global_candidates(manifest: Dict[str, Any], filter_text: str = "", mode: str = "all") -> List[Dict[str, Any]]:
    return sorted(
        [
            candidate
            for candidate in display_global_action_candidates(manifest)
            if global_picker_matches(candidate, filter_text=filter_text, mode=mode)
        ],
        key=global_candidate_sort_key,
    )


def used_global_candidate_count(manifest: Dict[str, Any]) -> int:
    return sum(1 for candidate in display_global_action_candidates(manifest) if candidate.get("used_in_selected_sessions"))


def display_global_action_candidates(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = manifest.get("_display_global_action_candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    return global_action_candidates(manifest)


def initial_global_picker_mode(manifest: Dict[str, Any]) -> str:
    return "used" if used_global_candidate_count(manifest) else "all"


def global_picker_page_size() -> int:
    if not sys.stdout.isatty():
        return GLOBAL_PICKER_PAGE_SIZE
    height = shutil.get_terminal_size(fallback=(100, 30)).lines
    return max(3, min(20, (height - 8) // 2))


def global_picker_page_window(cursor: int, count: int, page_size: int = GLOBAL_PICKER_PAGE_SIZE) -> Tuple[int, int]:
    if count <= 0:
        return 0, 0
    safe_cursor = min(max(cursor, 0), count - 1)
    start = (safe_cursor // page_size) * page_size
    end = min(start + page_size, count)
    return start, end


def render_global_candidate_details(candidate: Dict[str, Any]) -> str:
    lines = [
        "Codex-Wide Install Details",
        "",
        f"ID: {candidate.get('id')}",
        f"Type: {candidate.get('type')}",
        f"Risk: {candidate.get('risk', 'unknown')} ({display_risk_badges(candidate) or 'none'})",
        f"Source scope: {candidate.get('source_scope', 'unknown')}",
        f"Portable: {'yes' if candidate.get('portable') else 'no'}",
        f"Evidence: {candidate.get('evidence') or 'unknown'}",
        f"Why: {candidate.get('why_relevant') or candidate.get('reason') or 'unknown'}",
    ]
    if candidate.get("source_path"):
        lines.append(f"Source path: {candidate['source_path']}")
    if candidate.get("destination_path"):
        lines.append(f"Destination: {candidate['destination_path']}")
    if candidate.get("bridge"):
        lines.append(f"Bridge name: {candidate.get('bridge_name')}")
        lines.append(f"Bridge source kind: {candidate.get('bridge_source_kind') or 'unknown'}")
        lines.append(f"Bridge source: {candidate.get('bridge_source_path')}")
        if candidate.get("bridge_source_ref"):
            lines.append(f"Bridge ref: {candidate.get('bridge_source_ref')}")
        if candidate.get("bridge_cache_fallback_path"):
            lines.append(f"Cache fallback: {candidate.get('bridge_cache_fallback_path')}")
        lines.append(f"Bridge destination: {candidate.get('bridge_destination_path')}")
        lines.append(f"Bridge skills: {candidate.get('bridge_skill_count', 0)}")
        lines.append(f"Bridge agents: {candidate.get('bridge_agent_count', 0)}")
    if candidate.get("origin_source_url") or candidate.get("origin_source_path") or candidate.get("origin_github_repo"):
        lines.append("")
        lines.append("Origin:")
        if candidate.get("origin_marketplace"):
            lines.append(f"Marketplace: {candidate.get('origin_marketplace')}")
        if candidate.get("origin_source_url"):
            lines.append(f"Source URL: {candidate.get('origin_source_url')}")
        if candidate.get("origin_source_path"):
            lines.append(f"Source path: {candidate.get('origin_source_path')}")
        if candidate.get("origin_github_repo"):
            lines.append(f"GitHub repo: {candidate.get('origin_github_repo')}")
        lines.append(f"Codex release status: {candidate.get('codex_release_status', 'not-detected')}")
        if candidate.get("codex_release_evidence"):
            lines.append(f"Codex release evidence: {candidate.get('codex_release_evidence')}")
        for url in candidate.get("codex_release_check_urls") or []:
            lines.append(f"Check: {url}")
    if candidate.get("command"):
        lines.append(f"Command: {candidate['command']}")
    if candidate.get("blocked_reason"):
        lines.append(f"Blocked: {candidate['blocked_reason']}")
    transcript_usage = candidate.get("transcript_usage") or {}
    if candidate.get("used_in_selected_sessions"):
        lines.append("")
        lines.append(f"Used in selected transcripts: yes ({transcript_usage.get('count', 0)} observed use(s))")
        for item in transcript_usage.get("matches") or []:
            lines.append(f"- {item.get('name')}: {item.get('count', 0)}")
            for evidence in item.get("evidence") or []:
                lines.append(f"  evidence: {evidence}")
    manual_steps = candidate.get("manual_steps") or []
    if manual_steps:
        lines.append("")
        lines.append("Manual steps:")
        lines.extend(f"- {step}" for step in manual_steps)
    return "\n".join(lines)


def render_global_picker(
    manifest: Dict[str, Any],
    cursor: int = 0,
    selected_ids: Optional[List[str]] = None,
    filter_text: str = "",
    mode: str = "all",
    page_size: int = GLOBAL_PICKER_PAGE_SIZE,
) -> str:
    all_candidates = display_global_action_candidates(manifest)
    candidates = visible_global_candidates(manifest, filter_text=filter_text, mode=mode)
    selected_id_set = set(selected_ids if selected_ids is not None else manifest.get("selected_global_action_ids") or [])
    start, end = global_picker_page_window(cursor, len(candidates), page_size=page_size)
    page = candidates[start:end]
    risky_count = sum(1 for candidate in all_candidates if candidate_is_risky(candidate))
    used_count = sum(1 for candidate in all_candidates if candidate.get("used_in_selected_sessions"))
    page_count = max(1, (len(candidates) + page_size - 1) // page_size)
    current_page = 1 if not candidates else (start // page_size) + 1
    lines = [
        "",
        "Customize Tooling Carryover",
        "Installs run for this Codex user under ~/.codex, so they are available in every Codex project after final confirmation.",
        (
            f"View: {mode} | Filter: {filter_text or 'none'} | "
            f"Showing: {start + 1 if candidates else 0}-{end} of {len(candidates)} | "
            f"Page: {current_page}/{page_count} | Selected: {len(selected_id_set)} | "
            f"Used: {used_count} | Total: {len(all_candidates)} | Risky: {risky_count}"
        ),
        "",
    ]
    if not candidates:
        if all_candidates and mode == "used" and not filter_text:
            lines.append("No transcript-used Codex-wide install candidates found.")
            lines.append("Press Tab to review all discovered candidates.")
        elif all_candidates and (filter_text or mode != "all"):
            lines.append("No Codex-wide install candidates match this filter/view.")
            lines.append("Clear the filter with / then Enter, or press Tab to change views.")
        else:
            lines.append("No Codex-wide install candidates found.")
    github_failures = [
        candidate
        for candidate in all_candidates
        if candidate.get("github_codex_check_error")
        and candidate.get("codex_release_status") != "github-origin-checked-no-native"
    ]
    if github_failures:
        lines.append(
            f"GitHub check failed for {len(github_failures)} candidate(s); bridge/manual fallbacks remain selectable."
        )
    last_group = None
    for offset, candidate in enumerate(page, start=1):
        absolute_index = start + offset - 1
        mark = "x" if candidate["id"] in selected_id_set else " "
        pointer = ">" if absolute_index == cursor else " "
        label = truncate(str(candidate["label"]), 120)
        badges = display_risk_badges(candidate)
        used = " | used-in-transcripts" if candidate.get("used_in_selected_sessions") else ""
        group = global_candidate_group(candidate)
        if group != last_group:
            lines.append(f"{group}:")
            last_group = group
        lines.append(
            f"{pointer} {offset}. [{mark}] {candidate['id']} | "
            f"risk={candidate.get('risk', 'unknown')} | {badges}{used}"
        )
        lines.append(f"    {label}")
    lines.extend(
        [
            "",
            "Commands: Up/k Down/j, f/b page, Space/x toggle, A select safe, u clear visible, C clear all, i invert visible, / filter, Tab view, d details, ? help, Enter done, q cancel",
        ]
    )
    return "\n".join(lines)


def apply_global_picker_key(
    key: str,
    cursor: int,
    count: int,
    page_size: int = GLOBAL_PICKER_PAGE_SIZE,
) -> Tuple[str, int]:
    if count <= 0:
        if key in {"apply", "quit", "filter", "help", "next-view", "clear-all"}:
            return key if key != "apply" else "done", 0
        return "continue", 0
    if key == "up":
        return "continue", (cursor - 1) % count
    if key == "down":
        return "continue", (cursor + 1) % count
    if key == "page-up":
        return "continue", max(0, cursor - page_size)
    if key == "page-down":
        return "continue", min(count - 1, cursor + page_size)
    if key in {
        "toggle",
        "details",
        "filter",
        "help",
        "next-view",
        "select-visible",
        "clear-visible",
        "clear-all",
        "invert-visible",
    }:
        return key, cursor
    if key.startswith("toggle:"):
        return key, cursor
    if key == "apply":
        return "done", cursor
    if key == "quit":
        return "cancel", cursor
    return "continue", cursor


def next_global_picker_mode(mode: str) -> str:
    try:
        index = GLOBAL_PICKER_MODES.index(mode)
    except ValueError:
        return GLOBAL_PICKER_MODES[0]
    return GLOBAL_PICKER_MODES[(index + 1) % len(GLOBAL_PICKER_MODES)]


def select_visible_global_candidates(
    selected_ids: List[str],
    candidates: List[Dict[str, Any]],
    *,
    include_risky: bool = False,
) -> Tuple[List[str], int]:
    selected = list(selected_ids)
    skipped = 0
    for candidate in candidates:
        action_id = str(candidate.get("id") or "")
        if not action_id:
            continue
        if not selectable_by_bulk(candidate, include_risky=include_risky):
            skipped += 1
            continue
        if action_id not in selected:
            selected.append(action_id)
    return selected, skipped


def clear_visible_global_candidates(selected_ids: List[str], candidates: List[Dict[str, Any]]) -> List[str]:
    visible_ids = {str(candidate.get("id") or "") for candidate in candidates}
    return [action_id for action_id in selected_ids if action_id not in visible_ids]


def invert_visible_global_candidates(
    selected_ids: List[str],
    candidates: List[Dict[str, Any]],
    *,
    include_risky: bool = False,
) -> Tuple[List[str], int]:
    selected = list(selected_ids)
    skipped = 0
    for candidate in candidates:
        action_id = str(candidate.get("id") or "")
        if not action_id:
            continue
        if action_id in selected:
            selected.remove(action_id)
            continue
        if not selectable_by_bulk(candidate, include_risky=include_risky):
            skipped += 1
            continue
        selected.append(action_id)
    return selected, skipped


def current_global_page_candidates(
    candidates: List[Dict[str, Any]],
    cursor: int,
    page_size: int = GLOBAL_PICKER_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    start, end = global_picker_page_window(cursor, len(candidates), page_size=page_size)
    return candidates[start:end]


def global_picker_help() -> str:
    return "\n".join(
        [
            "Codex-Wide Install Picker Help",
            "",
            "Up/k and Down/j: move cursor",
            "f and b: page forward/back",
            "Space or x: toggle highlighted candidate",
            "Number keys: toggle visible row number",
            "/: filter by text",
            "Tab: cycle used/all/mcp/skill/plugin/manual views",
            "d: show full candidate details",
            "A: select all safe visible candidates",
            "u: clear visible candidates",
            "C: clear all selected candidates",
            "i: invert visible safe candidates",
            "Enter/a: accept selection",
            "q: cancel without changing Codex-wide install selection",
            "",
            "Bulk selection skips secret, unverified, blocked, and Codex-wide candidates.",
            "Codex-wide install apply still requires final confirmation and skips blocked imports.",
        ]
    )


def global_picker(manifest: Dict[str, Any]) -> None:
    all_candidates = annotate_github_codex_releases(global_action_candidates(manifest))
    manifest["_display_global_action_candidates"] = all_candidates
    if not all_candidates:
        show_message_screen("No Codex-wide install candidates found.")
        return
    selected_ids = list(manifest.get("selected_global_action_ids") or [])
    cursor = 0
    filter_text = ""
    mode = initial_global_picker_mode(manifest)
    while True:
        page_size = global_picker_page_size()
        candidates = visible_global_candidates(manifest, filter_text=filter_text, mode=mode)
        if cursor >= len(candidates):
            cursor = max(0, len(candidates) - 1)
        if supports_static_menu():
            sys.stdout.write(ANSI_CLEAR_VIEWPORT)
        print(
            render_global_picker(
                manifest,
                cursor,
                selected_ids=selected_ids,
                filter_text=filter_text,
                mode=mode,
                page_size=page_size,
            ),
            flush=True,
        )
        key = read_menu_key()
        action, cursor = apply_global_picker_key(key, cursor, len(candidates), page_size=page_size)
        if action == "done":
            all_candidates = display_global_action_candidates(manifest)
            manifest["selected_global_action_ids"] = selected_ids
            manifest["selected_global_actions"] = [candidate for candidate in all_candidates if candidate["id"] in selected_ids]
            return
        if action == "cancel":
            return
        if action == "filter":
            if supports_static_menu():
                sys.stdout.write(ANSI_CLEAR_VIEWPORT)
            filter_text = prompt_static_line("Filter Codex-wide installs (empty clears): ").strip()
            cursor = 0
            continue
        if action == "next-view":
            mode = next_global_picker_mode(mode)
            cursor = 0
            continue
        if action == "help":
            show_message_screen(global_picker_help())
            continue
        if action == "details":
            if candidates:
                show_message_screen(render_global_candidate_details(candidates[cursor]))
            continue
        if action == "select-visible":
            page_candidates = current_global_page_candidates(candidates, cursor, page_size=page_size)
            selected_ids, skipped = select_visible_global_candidates(selected_ids, page_candidates)
            if skipped:
                show_message_screen(f"Selected safe visible candidates. Skipped {skipped} risky/manual candidates.")
            continue
        if action == "clear-visible":
            page_candidates = current_global_page_candidates(candidates, cursor, page_size=page_size)
            selected_ids = clear_visible_global_candidates(selected_ids, page_candidates)
            continue
        if action == "clear-all":
            selected_ids = []
            continue
        if action == "invert-visible":
            page_candidates = current_global_page_candidates(candidates, cursor, page_size=page_size)
            selected_ids, skipped = invert_visible_global_candidates(selected_ids, page_candidates)
            if skipped:
                show_message_screen(f"Inverted safe visible candidates. Skipped {skipped} risky/manual candidates.")
            continue
        if action == "toggle":
            if not candidates:
                continue
            action_id = candidates[cursor]["id"]
            if action_id in selected_ids:
                selected_ids.remove(action_id)
            else:
                selected_ids.append(action_id)
        elif action.startswith("toggle:"):
            visible_number = int(action.split(":", 1)[1])
            visible_index = 9 if visible_number == 0 else visible_number - 1
            start, end = global_picker_page_window(cursor, len(candidates), page_size=page_size)
            index = start + visible_index
            if start <= index < end and index < len(candidates):
                cursor = index
                action_id = candidates[cursor]["id"]
                if action_id in selected_ids:
                    selected_ids.remove(action_id)
                else:
                    selected_ids.append(action_id)


def show_message_screen(message: str) -> None:
    if supports_static_menu():
        sys.stdout.write(ANSI_CLEAR_VIEWPORT)
    print(message)
    if supports_static_menu():
        print()
        print("Press any key to return to the menu.", flush=True)
        read_menu_key()


def prompt_static_line(prompt: str) -> str:
    if supports_static_menu():
        sys.stdout.write(ANSI_SHOW_CURSOR)
        sys.stdout.flush()
    try:
        return input(prompt)
    finally:
        if supports_static_menu():
            sys.stdout.write(ANSI_HIDE_CURSOR)
            sys.stdout.flush()


def selected_global_candidates(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    selected_ids = set(manifest.get("selected_global_action_ids") or [])
    saved = manifest.get("selected_global_actions")
    if selected_ids and isinstance(saved, list):
        saved_candidates = [
            item
            for item in saved
            if isinstance(item, dict) and item.get("type") in GLOBAL_ACTION_TYPES and item.get("id") in selected_ids
        ]
        if saved_candidates:
            saved_by_id = {str(item.get("id")): item for item in saved_candidates}
            fresh_by_id = {
                str(candidate.get("id")): candidate
                for candidate in global_action_candidates(manifest)
                if candidate.get("id") in selected_ids
            }
            return [
                merge_saved_global_action(fresh_by_id.get(action_id, {}), saved_by_id[action_id])
                for action_id in manifest.get("selected_global_action_ids") or []
                if action_id in saved_by_id
            ]
    if selected_ids:
        fresh = [candidate for candidate in global_action_candidates(manifest) if candidate["id"] in selected_ids]
        if fresh:
            return fresh
    if isinstance(saved, list) and saved:
        return [item for item in saved if isinstance(item, dict) and item.get("type") in GLOBAL_ACTION_TYPES]
    return []


def merge_saved_global_action(fresh: Dict[str, Any], saved: Dict[str, Any]) -> Dict[str, Any]:
    if not fresh:
        return dict(saved)
    merged = dict(fresh)
    for key, value in saved.items():
        if key.startswith("github_codex_") or key in {
            "codex_release_status",
            "codex_release_evidence",
            "global_apply_results",
        }:
            merged[key] = value
    return merged


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def selectable_by_bulk(candidate: Dict[str, Any], include_risky: bool = False) -> bool:
    if include_risky:
        return True
    badges = set(candidate.get("risk_badges") or [])
    if candidate.get("blocked_reason"):
        return False
    if badges.intersection({"secret", "unverified", "global-scope"}):
        return False
    return True


def resolve_global_selectors(
    manifest: Dict[str, Any],
    selector_text: Optional[str],
    *,
    candidates: Optional[List[Dict[str, Any]]] = None,
    include_risky: bool = False,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    selectors = parse_csv(selector_text)
    if not selectors:
        raise HandoffError("missing --select value; use candidate IDs, skill names, types, or all")
    candidates = candidates if candidates is not None else global_action_candidates(manifest)
    selected_ids = set(manifest.get("selected_global_action_ids") or [])
    missing: List[str] = []

    for selector in selectors:
        lowered = selector.lower()
        matched: List[Dict[str, Any]] = []
        recognized_empty = False
        if lowered in {"all", "*"}:
            matched = [candidate for candidate in candidates if selectable_by_bulk(candidate, include_risky)]
            recognized_empty = True
        elif lowered in GLOBAL_TYPE_ALIASES:
            action_type = GLOBAL_TYPE_ALIASES[lowered]
            matched = [
                candidate
                for candidate in candidates
                if candidate.get("type") == action_type and selectable_by_bulk(candidate, include_risky)
            ]
            recognized_empty = True
        elif lowered.endswith(":*") and lowered[:-2] in GLOBAL_TYPE_ALIASES:
            action_type = GLOBAL_TYPE_ALIASES[lowered[:-2]]
            matched = [
                candidate
                for candidate in candidates
                if candidate.get("type") == action_type and selectable_by_bulk(candidate, include_risky)
            ]
            recognized_empty = True
        else:
            matched = [
                candidate
                for candidate in candidates
                if selector == candidate.get("id")
                or selector == candidate.get("name")
                or selector == candidate.get("hash")
            ]
        if not matched and not recognized_empty:
            missing.append(selector)
            continue
        selected_ids.update(str(candidate["id"]) for candidate in matched)

    if missing:
        raise HandoffError("unknown Codex-wide install selector(s): " + ", ".join(missing))
    ordered_ids = [candidate["id"] for candidate in candidates if candidate["id"] in selected_ids]
    selected = [candidate for candidate in candidates if candidate["id"] in selected_ids]
    return ordered_ids, selected


def set_selected_global_actions(manifest: Dict[str, Any], action_ids: List[str], actions: List[Dict[str, Any]]) -> None:
    manifest["selected_global_action_ids"] = action_ids
    manifest["selected_global_actions"] = actions
    manifest["global_selection"] = {
        "recorded_at": utc_now(),
        "selected_count": len(action_ids),
        "selected_ids": action_ids,
    }


def global_apply_plan_group(candidate: Dict[str, Any]) -> Tuple[str, str]:
    badges = set(candidate.get("risk_badges") or [])
    if candidate.get("blocked_reason"):
        return "Will skip/manual", str(candidate.get("blocked_reason"))
    if "secret" in badges:
        return "Will skip/manual", "secret-bearing import requires manual repair"
    action_type = candidate.get("type")
    if action_type == "plugin" and candidate.get("bridge"):
        return "Will bridge", str(candidate.get("bridge_destination_path") or "~/.codex/plugins")
    if candidate.get("confidence") == "low":
        return "Will skip/manual", "low-confidence import recorded as manual follow-up"
    if action_type == "skill":
        return "Will copy", str(candidate.get("destination_path") or "~/.codex/skills")
    if action_type in {"mcp", "plugin"}:
        return "Will run", str(candidate.get("command") or "missing command")
    return "Will skip/manual", "unknown import type"


def confirm_global_apply(manifest: Dict[str, Any]) -> bool:
    selected = selected_global_candidates(manifest)
    if not selected:
        return False
    if supports_static_menu():
        sys.stdout.write(ANSI_SHOW_CURSOR)
        sys.stdout.flush()
    try:
        print()
        print("Selected installs for this Codex user:")
        print("These can change ~/.codex and become available in every Codex project for this user.")
        print("Blocked/manual imports will be skipped.")
        for group_name in ("Will copy", "Will bridge", "Will run", "Will skip/manual"):
            group_items = [candidate for candidate in selected if global_apply_plan_group(candidate)[0] == group_name]
            if not group_items:
                continue
            print(f"{group_name}:")
            for candidate in group_items:
                _, detail = global_apply_plan_group(candidate)
                badges = display_risk_badges(candidate) or "none"
                scope = candidate.get("source_scope", "unknown")
                print(
                    f"  - {candidate['id']} | risk={candidate.get('risk', 'unknown')} | "
                    f"badges={badges} | scope={scope}"
                )
                print(f"    {truncate(str(candidate['label']), 160)}")
                if detail:
                    print(f"    {truncate(detail, 160)}")
                if candidate.get("bridge_source_kind"):
                    source_kind = str(candidate.get("bridge_source_kind"))
                    source_ref = str(candidate.get("bridge_source_ref") or candidate.get("bridge_commit") or "")
                    print(f"    bridge source: {source_kind}" + (f" @ {source_ref[:12]}" if source_ref else ""))
                if candidate.get("origin_source_url"):
                    print(f"    source: {truncate(str(candidate.get('origin_source_url')), 160)}")
                if candidate.get("origin_github_repo"):
                    source_path = str(candidate.get("origin_subdir") or "").strip("/")
                    suffix = f" ({source_path})" if source_path else ""
                    print(f"    github: {candidate.get('origin_github_repo')}{suffix}")
                if candidate.get("codex_release_status") and candidate.get("codex_release_status") != "not-detected":
                    print(f"    codex release: {candidate.get('codex_release_status')}")
        answer = input("Install selected changes for this Codex user now? [y/N] ").strip().lower()
        return answer in {"y", "yes"}
    finally:
        if supports_static_menu():
            sys.stdout.write(ANSI_HIDE_CURSOR)
            sys.stdout.flush()


def privacy_prompt(manifest: Dict[str, Any]) -> str:
    return (
        "Privacy: this may write Claude session titles/summaries, recent prompts, "
        "assistant notes, commands, MCP/skill/plugin inventory, nearby Claude project keys, and local paths "
        "to .codex/handoff/manifest.json. "
        "Secrets are best-effort redacted; review with "
        f"ai-handoff diff {shlex.quote(manifest['target_path'])} --include-manifest before committing."
    )


def confirm_privacy_apply(manifest: Dict[str, Any]) -> bool:
    if not manifest.get("privacy", {}).get("ack_required_for_apply"):
        return True
    print(privacy_prompt(manifest))
    answer = input("Continue? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def apply_selected_global_actions(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for candidate in selected_global_candidates(manifest):
        if candidate.get("blocked_reason"):
            results.append(
                {
                    "id": candidate["id"],
                    "status": "skipped",
                    "reason": candidate.get("blocked_reason"),
                    "command": candidate.get("command"),
                }
            )
            continue
        if "secret" in set(candidate.get("risk_badges") or []):
            results.append(
                {
                    "id": candidate["id"],
                    "status": "skipped",
                    "reason": "secret-bearing import requires manual repair",
                    "command": candidate.get("command"),
                }
            )
            continue
        if candidate.get("confidence") == "low" and not candidate.get("bridge"):
            results.append(
                {
                    "id": candidate["id"],
                    "status": "skipped",
                    "reason": "low-confidence import recorded as manual follow-up",
                    "command": candidate.get("command"),
                }
            )
            continue
        action_type = candidate.get("type")
        if action_type == "plugin" and candidate.get("bridge"):
            try:
                bridge_result = bridge_plugin_to_codex(candidate)
            except Exception as exc:
                results.append(
                    {
                        "id": candidate["id"],
                        "status": "error",
                        "reason": str(exc),
                        "source": candidate.get("bridge_source_path"),
                        "destination": candidate.get("bridge_destination_path"),
                    }
                )
                continue
            install_result = install_bridged_plugin(str(bridge_result.get("bridge") or ""))
            if not install_result.get("installed"):
                results.append(
                    {
                        "id": candidate["id"],
                        "status": "partial",
                        **bridge_result,
                        "install": install_result,
                        "reason": install_result.get("reason") or "bridged plugin was not installed by codex plugin add",
                        "next_steps": [
                            f"run {install_result.get('command')}",
                            "restart Codex or open a new session",
                        ],
                    }
                )
                continue
            results.append(
                {
                    "id": candidate["id"],
                    "status": "ok",
                    **bridge_result,
                    "install": install_result,
                    "next_steps": [
                        "restart Codex or open a new session",
                        "use the bridged plugin skills in a new Codex session",
                    ],
                }
            )
            continue
        if action_type in {"mcp", "plugin"}:
            command = str(candidate.get("command") or "")
            if not command:
                results.append({"id": candidate["id"], "status": "skipped", "reason": "missing command"})
                continue
            if "<redacted>" in command:
                results.append(
                    {
                        "id": candidate["id"],
                        "status": "skipped",
                        "reason": "command contains redacted secrets; run manually after restoring env values",
                        "command": command,
                    }
                )
                continue
            try:
                proc = subprocess.run(
                    shlex.split(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=120,
                )
            except Exception as exc:
                results.append({"id": candidate["id"], "status": "error", "reason": str(exc), "command": command})
                continue
            results.append(
                {
                    "id": candidate["id"],
                    "status": "ok" if proc.returncode == 0 else "error",
                    "returncode": proc.returncode,
                    "command": command,
                    "stdout": truncate(proc.stdout, 1200),
                    "stderr": truncate(proc.stderr, 1200),
                }
            )
            continue
        if action_type == "skill":
            name = str(candidate.get("name") or "")
            source = Path(str(candidate.get("source_path") or "")).expanduser()
            destination = home_dir() / ".codex" / "skills" / name
            if not name or not source.exists():
                results.append({"id": candidate["id"], "status": "skipped", "reason": "missing source skill"})
                continue
            if destination.exists():
                results.append(
                    {
                        "id": candidate["id"],
                        "status": "skipped",
                        "reason": "destination already exists",
                        "destination": str(destination),
                    }
                )
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination)
            except Exception as exc:
                results.append(
                    {
                        "id": candidate["id"],
                        "status": "error",
                        "reason": str(exc),
                        "source": str(source),
                        "destination": str(destination),
                    }
                )
                continue
            results.append(
                {
                    "id": candidate["id"],
                    "status": "ok",
                    "source": str(source),
                    "destination": str(destination),
                }
            )
    manifest["global_apply_results"] = results
    return results


def interactive_flow(manifest: Dict[str, Any]) -> int:
    selection = manifest["selection"]
    cursor = 0
    static_menu = supports_static_menu()
    if static_menu:
        sys.stdout.write(ANSI_HIDE_CURSOR)
        sys.stdout.flush()
    try:
        while True:
            draw_static_menu(manifest, cursor)
            key = read_menu_key()
            action, cursor = apply_interactive_key(key, cursor, selection)
            if action == "quit":
                if static_menu:
                    sys.stdout.write(ANSI_CLEAR_VIEWPORT)
                print("No files changed.")
                return 0
            if action == "continue":
                continue
            if action == "preview":
                show_static_preview(manifest)
                continue
            if action == "sessions":
                session_picker(manifest)
                continue
            if action == "globals":
                global_picker(manifest)
                continue
            if action == "apply":
                if static_menu:
                    sys.stdout.write(ANSI_CLEAR_VIEWPORT)
                try:
                    result = write_project_artifacts(manifest)
                except HandoffError as exc:
                    print_handoff_error(exc, manifest["target_path"])
                    return 2
                print("Applied project-local handoff.")
                for path in result["written"]:
                    print(f"  - {path}")
                if selected_global_candidates(manifest):
                    if confirm_global_apply(manifest):
                        global_results = apply_selected_global_actions(manifest)
                        try:
                            write_project_artifacts(manifest)
                        except HandoffError as exc:
                            print_handoff_error(exc, manifest["target_path"])
                            return 2
                        print("Codex-wide install results:")
                        for item in global_results:
                            label = item.get("id")
                            status = item.get("status")
                            reason = item.get("reason")
                            suffix = f" ({reason})" if reason else ""
                            print(f"  - {label}: {status}{suffix}")
                    else:
                        print("Selected Codex-wide installs were recorded in the manifest; none were executed.")
                elif any(selection.get(key) for key in ("propose_mcps", "propose_skill_conversions", "propose_plugin_installs")):
                    print("Codex-wide install candidates were recorded in the manifest; press g before apply to select them.")
                return 0
            print("Unknown command.")
    finally:
        if static_menu:
            sys.stdout.write(ANSI_SHOW_CURSOR)
            sys.stdout.flush()


def wizard_answer(prompt: str, default: str = "") -> str:
    answer = input(prompt).strip().lower()
    return answer or default


def print_wizard_header(manifest: Dict[str, Any]) -> None:
    sessions = manifest["claude"]["sessions"]
    confidence = manifest.get("handoff_confidence", {})
    print("AI Handoff Wizard")
    print(f"Project: {manifest['target_path']}")
    print(f"Confidence: {confidence.get('level', 'unknown')} - {confidence.get('reason', '')}")
    print(
        f"Claude sessions: {sessions.get('found_count', 0)} found, "
        f"{sessions.get('selected_count', 0)} selected"
    )
    usage_summary = sessions.get("usage_summary") or {}
    used_plugins = usage_kind_names(usage_summary, "plugins")
    if used_plugins != "none":
        print(f"Transcript-used plugins: {used_plugins}")
    print()


def wizard_review_sessions(manifest: Dict[str, Any]) -> bool:
    show_session_sample = True
    while True:
        sessions = manifest["claude"]["sessions"]
        print("Step 1/3: Claude Context")
        print(f"Selected {sessions.get('selected_count', 0)} of {sessions.get('found_count', 0)} discovered session(s).")
        if show_session_sample:
            for bullet in session_bullets(manifest, limit=3):
                print("  " + bullet)
        answer = wizard_answer("\nContinue, choose more conversations, skip context, or quit? [Enter/c/s/q] ", "continue")
        if answer in {"q", "quit"}:
            print("No files changed.")
            return False
        if answer in {"c", "choose", "conversations"}:
            session_picker(manifest)
            show_session_sample = False
            print()
            continue
        if answer in {"s", "skip"}:
            update_session_selection(manifest, [])
            print("Claude context skipped.")
            print()
            return True
        if answer in {"continue", "y", "yes"}:
            print()
            return True
        print("Choose Enter, c, s, or q.")


def wizard_apply_project_files(manifest: Dict[str, Any]) -> Tuple[bool, bool]:
    while True:
        print("Step 2/3: Project Files")
        print("Will write project-local handoff files only:")
        for path in manifest["actions"].get("project_writes", []):
            print(f"  - {path}")
        print("This does not change ~/.codex.")
        answer = wizard_answer("Apply project-local files, preview diff, skip, or quit? [Y/p/s/q] ", "y")
        if answer in {"q", "quit"}:
            print("No more changes.")
            return False, False
        if answer in {"p", "preview", "diff"}:
            diff = render_diff(manifest, include_manifest=False)
            print()
            print(diff or "No project-local diff.")
            print()
            continue
        if answer in {"s", "skip", "n", "no"}:
            print("Project-local files skipped.")
            print()
            return True, False
        if answer in {"y", "yes", "a", "apply"}:
            try:
                result = write_project_artifacts(manifest)
            except HandoffError as exc:
                print_handoff_error(exc, manifest["target_path"])
                return False, False
            print("Applied project-local handoff.")
            for path in result["written"]:
                print(f"  - {path}")
            print()
            return True, True
        print("Choose y, p, s, or q.")


def wizard_global_candidate_summary(manifest: Dict[str, Any]) -> Dict[str, int]:
    candidates = display_global_action_candidates(manifest)
    used_candidates = [candidate for candidate in candidates if candidate.get("used_in_selected_sessions")]
    return {
        "total": len(candidates),
        "used": len(used_candidates),
        "plugins": sum(1 for candidate in candidates if candidate.get("type") == "plugin"),
        "skills": sum(1 for candidate in candidates if candidate.get("type") == "skill"),
        "mcps": sum(1 for candidate in candidates if candidate.get("type") == "mcp"),
        "used_plugins": sum(1 for candidate in used_candidates if candidate.get("type") == "plugin"),
        "used_skills": sum(1 for candidate in used_candidates if candidate.get("type") == "skill"),
        "used_mcps": sum(1 for candidate in used_candidates if candidate.get("type") == "mcp"),
    }


def tooling_candidate_line(candidate: Dict[str, Any]) -> str:
    action_type = str(candidate.get("type") or "tool")
    if action_type == "plugin" and candidate.get("bridge"):
        return f"{candidate.get('id')} -> {candidate.get('bridge_name')} (bridged plugin)"
    if action_type == "skill":
        return f"{candidate.get('id')} -> {candidate.get('destination_path')}"
    if action_type == "mcp":
        return f"{candidate.get('id')} -> {candidate.get('command')}"
    return str(candidate.get("id") or candidate.get("label") or "unknown")


def persist_wizard_tooling_state(manifest: Dict[str, Any], project_applied: bool) -> bool:
    try:
        if project_applied:
            write_project_artifacts(manifest)
        else:
            write_manifest_artifacts(manifest)
    except HandoffError as exc:
        print_handoff_error(exc, manifest["target_path"])
        return False
    return True


def wizard_review_globals(manifest: Dict[str, Any], project_applied: bool) -> bool:
    all_candidates = annotate_github_codex_releases(global_action_candidates(manifest))
    manifest["_display_global_action_candidates"] = all_candidates
    summary = wizard_global_candidate_summary(manifest)
    matched_candidates = conversation_matched_global_candidates(manifest)
    print("Step 3/3: Tooling Carryover")
    if summary["used"]:
        print(
            f"Found in selected Claude conversations: "
            f"{summary['used_plugins']} plugin(s), {summary['used_skills']} skill(s), "
            f"{summary['used_mcps']} MCP(s)."
        )
    else:
        print("No MCP, skill, or plugin install candidates matched the selected Claude conversations.")
        print(
            f"Broader Claude inventory has {summary['total']} candidate(s): "
            f"{summary['plugins']} plugin(s), {summary['skills']} skill(s), {summary['mcps']} MCP(s)."
        )
    if summary["total"] == 0:
        print("No tooling carryover actions found.")
        return True

    if matched_candidates:
        for candidate in matched_candidates[:8]:
            print(f"  - {tooling_candidate_line(candidate)}")
        if len(matched_candidates) > 8:
            print(f"  - +{len(matched_candidates) - 8} more")
        additional = summary["total"] - len(matched_candidates)
        if additional > 0:
            print(f"{additional} additional inventory candidate(s) are available with Customize.")
        print("Project-only records this in AGENTS.md/manifest without touching ~/.codex.")
        print("Install for this Codex user writes under ~/.codex and is available in every Codex project for this user.")
        prompt = "\nCarry over conversation-matched tooling? [Enter=project-only/i install for user/c customize/s skip/q] "
        answer = wizard_answer(prompt, "project-only")
    else:
        prompt = "\nReview broader tooling inventory? [y/N] "
        answer = wizard_answer(prompt, "n")

    if answer in {"q", "quit"}:
        print("Stopped before tooling carryover.")
        return False
    if answer in {"s", "skip", "n", "no"}:
        print("Skipped tooling carryover.")
        return True

    if answer in {"project-only", "p", "record", "r", "yes", "y"} and matched_candidates:
        set_selected_global_actions(
            manifest,
            [str(candidate["id"]) for candidate in matched_candidates],
            matched_candidates,
        )
        if not persist_wizard_tooling_state(manifest, project_applied):
            return False
        print("Recorded conversation-matched tooling in the project handoff; no ~/.codex changes were made.")
        return True

    if answer in {"i", "install", "user", "install-user"} and matched_candidates:
        set_selected_global_actions(
            manifest,
            [str(candidate["id"]) for candidate in matched_candidates],
            matched_candidates,
        )
    elif answer in {"c", "customize", "review", "all"} or not matched_candidates:
        global_picker(manifest)
    else:
        print("Choose Enter, i, c, s, or q.")
        return wizard_review_globals(manifest, project_applied)

    if not selected_global_candidates(manifest):
        print("No tooling carryover actions selected.")
        return True
    if confirm_global_apply(manifest):
        global_results = apply_selected_global_actions(manifest)
        if not persist_wizard_tooling_state(manifest, project_applied):
            return False
        print("Install results:")
        for item in global_results:
            label = item.get("id")
            status = item.get("status")
            reason = item.get("reason")
            suffix = f" ({reason})" if reason else ""
            print(f"  - {label}: {status}{suffix}")
    else:
        if not persist_wizard_tooling_state(manifest, project_applied):
            return False
        print("Selected tooling carryover actions were recorded in the project handoff; none were installed.")
    return True


def print_wizard_completion(manifest: Dict[str, Any]) -> None:
    sessions = manifest["claude"]["sessions"]
    confidence = manifest.get("handoff_confidence", {})
    usage_summary = sessions.get("usage_summary") or {}
    applied_actions = manifest.get("applied_actions") or []
    global_results = [item for item in manifest.get("global_apply_results") or [] if isinstance(item, dict)]
    ok_results = [item for item in global_results if item.get("status") == "ok"]
    selected_global = manifest.get("selected_global_actions") or []
    print()
    print("AI handoff wizard complete.")
    print(f"Confidence: {confidence.get('level', 'unknown')} - {confidence.get('reason', '')}")
    print(f"Claude context: {sessions.get('selected_count', 0)} of {sessions.get('found_count', 0)} conversation(s) selected.")
    if any(usage_kind_count(usage_summary, kind) for kind in ("mcp_servers", "skills", "plugins")):
        print(
            "Claude tooling seen: "
            f"MCPs={usage_kind_names(usage_summary, 'mcp_servers')}; "
            f"skills={usage_kind_names(usage_summary, 'skills')}; "
            f"plugins={usage_kind_names(usage_summary, 'plugins')}."
        )
    if applied_actions:
        print("Project files updated:")
        for path in applied_actions:
            print(f"  - {path}")
    else:
        print("Project files updated: none.")
    if ok_results:
        print("Codex-wide installs completed:")
        actions_by_id = {
            str(item.get("id")): item for item in selected_global if isinstance(item, dict) and item.get("id")
        }
        for result in ok_results:
            action = actions_by_id.get(str(result.get("id")), {"id": result.get("id")})
            print(f"  - {global_action_display_name(action)}")
    elif selected_global:
        print("Codex-wide installs selected but not executed:")
        for action in selected_global:
            if isinstance(action, dict):
                print(f"  - {global_action_display_name(action)}")
    else:
        print("Codex-wide installs: none selected.")
    print("Inspect:")
    print(f"  - {shlex.quote(str(Path(manifest['target_path']) / 'AGENTS.md'))}")
    print(f"  - {shlex.quote(str(Path(manifest['target_path']) / '.codex' / 'handoff' / 'summary.md'))}")
    print(f"  - {shlex.quote(str(Path(manifest['target_path']) / '.codex' / 'handoff' / 'manifest.json'))}")
    print(f"Next: cd {shlex.quote(manifest['target_path'])} && codex")


def wizard_flow(manifest: Dict[str, Any]) -> int:
    print_wizard_header(manifest)
    if not wizard_review_sessions(manifest):
        return 0
    continue_flow, project_applied = wizard_apply_project_files(manifest)
    if not continue_flow:
        return 0
    if not wizard_review_globals(manifest, project_applied):
        return 0
    print_wizard_completion(manifest)
    return 0


def latest_manifest_path(project: Path) -> Path:
    return project / ".codex" / "handoff" / "manifest.json"


def load_latest_selection(project: Path) -> Dict[str, bool]:
    data = load_json(latest_manifest_path(project))
    if isinstance(data, dict) and isinstance(data.get("selection"), dict):
        return {str(key): bool(value) for key, value in data["selection"].items()}
    return {}


def load_latest_session_ids(project: Path) -> List[str]:
    data = load_json(latest_manifest_path(project))
    sessions = data.get("claude", {}).get("sessions", {}) if isinstance(data, dict) else {}
    ids = sessions.get("selected_session_ids") if isinstance(sessions, dict) else None
    if isinstance(ids, list):
        return [str(item) for item in ids if item]
    return []


def has_global_handoff_state(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return bool(
        data.get("selected_global_action_ids")
        or data.get("selected_global_actions")
        or data.get("global_apply_results")
    )


def load_latest_global_state(project: Path) -> Dict[str, Any]:
    data = load_json(latest_manifest_path(project))
    if has_global_handoff_state(data):
        return data
    runs_dir = project / ".codex" / "handoff" / "runs"
    if runs_dir.exists():
        for path in sorted(runs_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            run_data = load_json(path)
            if has_global_handoff_state(run_data):
                return run_data
    return data if isinstance(data, dict) else {}


def load_latest_global_action_ids(project: Path) -> List[str]:
    data = load_latest_global_state(project)
    ids = data.get("selected_global_action_ids") if isinstance(data, dict) else None
    if isinstance(ids, list):
        return [str(item) for item in ids if item]
    return []


def load_latest_global_actions(project: Path) -> List[Dict[str, Any]]:
    data = load_latest_global_state(project)
    actions = data.get("selected_global_actions") if isinstance(data, dict) else None
    if isinstance(actions, list):
        return [item for item in actions if isinstance(item, dict)]
    return []


def load_latest_global_apply_results(project: Path) -> List[Dict[str, Any]]:
    data = load_latest_global_state(project)
    results = data.get("global_apply_results") if isinstance(data, dict) else None
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return []


def parse_session_ids(value: Optional[str]) -> List[str]:
    return parse_csv(value)


def print_handoff_error(error: HandoffError, path: Optional[str] = None) -> None:
    print(f"error: {error}", file=sys.stderr)
    if path:
        print("", file=sys.stderr)
        print("Try:", file=sys.stderr)
        print(f"  ai-handoff doctor {shlex.quote(path)}", file=sys.stderr)
        print("  ai-handoff help", file=sys.stderr)


def validate_requested_sessions(manifest: Dict[str, Any], allow_empty: bool = False) -> Optional[int]:
    sessions = manifest["claude"]["sessions"]
    missing = sessions.get("missing_requested_session_ids") or []
    requested = sessions.get("requested_session_ids") or []
    if missing:
        print("error: requested Claude session IDs were not found:", file=sys.stderr)
        for session_id in missing:
            print(f"  - {session_id}", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"Run: ai-handoff conversations {shlex.quote(manifest['target_path'])}", file=sys.stderr)
        return 2
    if requested and not sessions.get("selected_count") and not allow_empty:
        print("error: requested session selection is empty", file=sys.stderr)
        print("Use --allow-empty-selection to continue with no Claude conversations.", file=sys.stderr)
        return 2
    return None


def print_conversations(manifest: Dict[str, Any]) -> None:
    sessions = manifest["claude"]["sessions"]
    selected_ids = set(sessions.get("selected_session_ids") or [])
    print(f"Claude conversations for {manifest['target_path']}")
    print(f"Selection: {sessions.get('selection_strategy')}")
    print(f"Found: {sessions.get('found_count', 0)} | Selected: {sessions.get('selected_count', 0)}")
    print()
    candidates = sessions.get("candidates") or []
    if not candidates:
        print("No Claude conversations found for this project.")
        return
    display_limit = 40
    for candidate in candidates[:display_limit]:
        session_id = str(candidate.get("session_id") or "")
        mark = "x" if session_id in selected_ids else " "
        modified = candidate.get("modified") or candidate.get("created") or "unknown time"
        title = truncate(str(candidate.get("title") or "Untitled"), 100)
        source = candidate.get("source_project_key")
        source_text = f" | {source}" if source else ""
        print(f"[{mark}] {session_id} | {modified}{source_text} | {title}")
        prompt = candidate.get("first_prompt")
        if prompt:
            print(f"    {truncate(str(prompt), 140)}")
    if len(candidates) > display_limit:
        print(f"... showing {display_limit} of {len(candidates)} candidates. Narrow with --search/--branch or use --json.")
    usage_summary = sessions.get("usage_summary") or {}
    if any(usage_kind_count(usage_summary, kind) for kind in ("mcp_servers", "skills", "plugins")):
        print()
        print("Tooling observed in selected transcripts:")
        print(f"  MCPs: {usage_kind_names(usage_summary, 'mcp_servers')}")
        print(f"  Skills: {usage_kind_names(usage_summary, 'skills')}")
        print(f"  Plugins: {usage_kind_names(usage_summary, 'plugins')}")
    close_matches = manifest["claude"]["sessions"].get("close_project_matches") or []
    if close_matches and not manifest["claude"]["sessions"].get("all_projects"):
        print()
        print("Nearby Claude project folders:")
        for item in close_matches[:5]:
            print(f"  - {item['key']} ({item['entry_count']} sessions, {item['reason']})")
        print("Search across them with:")
        print(f"  ai-handoff conversations {shlex.quote(manifest['target_path'])} --all-projects --search TEXT")
    print()
    print("Select exact sessions with:")
    selected_csv = ",".join(str(session_id) for session_id in sessions.get("selected_session_ids") or [] if session_id)
    session_arg = shlex.quote(selected_csv) if selected_csv else "id1,id2"
    print(f"  ai-handoff scan {shlex.quote(manifest['target_path'])} --sessions {session_arg}")
    print(f"  ai-handoff apply {shlex.quote(manifest['target_path'])} --sessions {session_arg}")


def command_conversations(args: argparse.Namespace) -> int:
    selected_session_ids = parse_session_ids(getattr(args, "sessions", None))
    try:
        manifest = build_manifest(
            args.path,
            last=args.last,
            since=args.since,
            include_transcripts=args.include_transcripts,
            selected_session_ids=selected_session_ids or None,
            all_projects=getattr(args, "all_projects", False),
            from_claude_project=getattr(args, "from_claude_project", None),
            search=getattr(args, "search", None),
            branch=getattr(args, "branch", None),
        )
    except HandoffError as exc:
        print_handoff_error(exc, args.path)
        return 2
    validation_code = validate_requested_sessions(manifest, bool(getattr(args, "allow_empty_selection", False)))
    if validation_code is not None:
        return validation_code
    if args.json:
        print(json.dumps(manifest["claude"]["sessions"], indent=2, sort_keys=True))
        return 0
    print_conversations(manifest)
    return 0


def command_privacy(args: argparse.Namespace) -> int:
    selected_session_ids = parse_session_ids(getattr(args, "sessions", None))
    try:
        manifest = build_manifest(
            args.path,
            last=args.last,
            since=args.since,
            include_transcripts=args.include_transcripts,
            selected_session_ids=selected_session_ids or None,
            all_projects=getattr(args, "all_projects", False),
            from_claude_project=getattr(args, "from_claude_project", None),
            search=getattr(args, "search", None),
            branch=getattr(args, "branch", None),
        )
    except HandoffError as exc:
        print_handoff_error(exc, args.path)
        return 2
    validation_code = validate_requested_sessions(manifest, bool(getattr(args, "allow_empty_selection", False)))
    if validation_code is not None:
        return validation_code
    privacy = manifest.get("privacy", {})
    if args.json:
        print(json.dumps(privacy, indent=2, sort_keys=True))
        return 0
    print(f"AI Handoff privacy for {manifest['target_path']}")
    print(f"Ack required for apply: {'yes' if privacy.get('ack_required_for_apply') else 'no'}")
    print(f"Redaction: {privacy.get('redaction', 'unknown')}")
    print()
    print("May write:")
    for item in privacy.get("written_context") or []:
        print(f"  - {item}")
    print()
    print("Counts:")
    print(f"  - selected Claude sessions: {privacy.get('selected_session_count', 0)}")
    print(f"  - global inventory candidates: {privacy.get('global_inventory_count', 0)}")
    print(f"  - nearby Claude project matches: {privacy.get('nearby_project_match_count', 0)}")
    print()
    print("Review the full manifest diff with:")
    print(f"  ai-handoff diff {shlex.quote(manifest['target_path'])} --include-manifest")
    return 0


def print_globals(
    manifest: Dict[str, Any],
    *,
    include_low_confidence: bool = False,
    include_risky: bool = False,
    project_only: bool = False,
    portable_only: bool = False,
    check_github: bool = False,
) -> None:
    candidates = filtered_global_candidates(
        manifest,
        include_low_confidence=include_low_confidence,
        include_risky=include_risky,
        project_only=project_only,
        portable_only=portable_only,
    )
    if check_github:
        candidates = annotate_github_codex_releases(candidates)
    selected_ids = set(manifest.get("selected_global_action_ids") or [])
    print(f"Codex-wide installs from Claude to Codex for {manifest['target_path']}")
    print("These are not project files. They may edit ~/.codex and affect every Codex project/folder on this machine.")
    print("Selecting installs records intent; execution requires final confirmation.")
    active_filters = []
    if project_only:
        active_filters.append("project-only")
    if portable_only:
        active_filters.append("portable-only")
    if include_risky or include_low_confidence:
        active_filters.append("including-risky")
    if check_github:
        active_filters.append("checked-github")
    if active_filters:
        print("Filters: " + ", ".join(active_filters))
    github_failures = []
    if check_github:
        github_failures = [
            candidate
            for candidate in candidates
            if candidate.get("github_codex_check_error")
            and candidate.get("codex_release_status") != "github-origin-checked-no-native"
        ]
        if github_failures:
            print(
                f"GitHub check warning: failed for {len(github_failures)} candidate(s); "
                "local bridge/manual fallbacks are still available."
            )
    print()
    if not candidates:
        print("No Codex-wide install candidates found.")
        return
    for group_name in ("Conversation Matched", "Recommended", "Review", "Manual / Unsafe"):
        group_items = [candidate for candidate in candidates if global_candidate_group(candidate) == group_name]
        if not group_items:
            continue
        print(f"{group_name}:")
        for candidate in group_items:
            mark = "x" if candidate["id"] in selected_ids else " "
            manual = " manual" if candidate.get("blocked_reason") or "<redacted>" in str(candidate.get("command") or "") else ""
            badges = display_risk_badges(candidate)
            confidence = candidate.get("confidence", "unknown")
            why = candidate.get("why_relevant") or candidate.get("reason", "unspecified")
            used = " | used-in-transcripts" if candidate.get("used_in_selected_sessions") else ""
            print(
                f"  [{mark}] {candidate['id']} | {candidate['type']}{manual} | {confidence} | "
                f"risk={candidate.get('risk', 'unknown')} | {badges}{used}"
            )
            print(f"      {truncate(str(candidate['label']), 120)}")
            print(f"      {truncate(str(why), 140)}")
            if check_github and candidate.get("github_codex_checked"):
                print(f"      {truncate(github_check_status_text(candidate), 180)}")
            if candidate.get("blocked_reason"):
                print(f"      blocked: {candidate['blocked_reason']}")
    hidden_counts = hidden_global_candidate_counts(manifest, candidates)
    if hidden_counts["total"]:
        print()
        print(
            f"Hidden: {hidden_counts['total']} candidates "
            f"({hidden_counts['low_confidence']} low-confidence, {hidden_counts['risky']} risky)."
        )
        print("Show them with: --include-risky")
    print()
    print("Choose Codex-wide installs non-interactively with:")
    print(
        f"  ai-handoff globals select {shlex.quote(manifest['target_path'])} "
        "--select skill:name,mcp:name --yes --ack-privacy"
    )
    print()
    print("Or choose them in the interactive menu with:")
    print(f"  ai-handoff {shlex.quote(manifest['target_path'])}")
    print("Then press g.")
    print()
    print("Install selected Codex-wide changes later with:")
    print(f"  ai-handoff globals apply {shlex.quote(manifest['target_path'])}")


def load_manifest_or_build(path: str) -> Tuple[Optional[Dict[str, Any]], int]:
    project = Path(path).expanduser().resolve()
    saved = load_json(latest_manifest_path(project))
    try:
        manifest = build_manifest(path)
    except HandoffError as exc:
        if isinstance(saved, dict):
            return saved, 0
        print_handoff_error(exc, path)
        return None, 2
    if isinstance(saved, dict):
        selected_ids = [str(item) for item in saved.get("selected_global_action_ids") or [] if item]
        manifest["selected_global_action_ids"] = selected_ids
        saved_actions = [
            item
            for item in saved.get("selected_global_actions") or []
            if isinstance(item, dict) and item.get("id") in set(selected_ids)
        ]
        fresh_actions = [candidate for candidate in global_action_candidates(manifest) if candidate.get("id") in set(selected_ids)]
        fresh_by_id = {str(item.get("id")): item for item in fresh_actions}
        if saved_actions:
            manifest["selected_global_actions"] = [
                merge_saved_global_action(fresh_by_id.get(str(item.get("id")), {}), item) for item in saved_actions
            ]
        else:
            manifest["selected_global_actions"] = fresh_actions
        if saved.get("global_selection"):
            manifest["global_selection"] = saved["global_selection"]
        if saved.get("global_apply_results"):
            manifest["global_apply_results"] = saved["global_apply_results"]
        if saved.get("applied"):
            manifest["applied"] = True
            manifest["applied_at"] = saved.get("applied_at")
            manifest["applied_actions"] = [
                str(item) for item in saved.get("applied_actions") or [] if item
            ]
    return manifest, 0


def command_globals(args: argparse.Namespace) -> int:
    globals_args = list(getattr(args, "globals_args", []) or [])
    if globals_args and globals_args[0] == "apply":
        if len(globals_args) < 2:
            print("error: missing project path", file=sys.stderr)
            print("usage: ai-handoff globals apply PATH", file=sys.stderr)
            return 2
        args.path = globals_args[1]
        return command_globals_apply(args)
    if globals_args and globals_args[0] == "select":
        if len(globals_args) < 2:
            print("error: missing project path", file=sys.stderr)
            print("usage: ai-handoff globals select PATH --select ID[,ID]", file=sys.stderr)
            return 2
        args.path = globals_args[1]
        return command_globals_select(args)
    if not globals_args:
        print("error: missing project path", file=sys.stderr)
        print("usage: ai-handoff globals PATH", file=sys.stderr)
        return 2
    args.path = globals_args[0]
    manifest, code = load_manifest_or_build(args.path)
    if manifest is None:
        return code
    include_low_confidence = bool(getattr(args, "include_low_confidence", False) or getattr(args, "include_risky", False))
    include_risky = bool(getattr(args, "include_risky", False))
    project_only = bool(getattr(args, "project_only", False))
    portable_only = bool(getattr(args, "portable_only", False))
    check_github = not bool(getattr(args, "no_check_github", False))
    if args.json:
        candidates = filtered_global_candidates(
            manifest,
            include_low_confidence=include_low_confidence,
            include_risky=include_risky,
            project_only=project_only,
            portable_only=portable_only,
        )
        if check_github:
            candidates = annotate_github_codex_releases(candidates)
        print(
            json.dumps(
                candidates,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print_globals(
        manifest,
        include_low_confidence=include_low_confidence,
        include_risky=include_risky,
        project_only=project_only,
        portable_only=portable_only,
        check_github=check_github,
    )
    return 0


def command_globals_select(args: argparse.Namespace) -> int:
    manifest, code = load_manifest_or_build(args.path)
    if manifest is None:
        return code
    include_low_confidence = bool(getattr(args, "include_low_confidence", False) or getattr(args, "include_risky", False))
    candidates = filtered_global_candidates(
        manifest,
        include_low_confidence=include_low_confidence,
        include_risky=bool(getattr(args, "include_risky", False)),
        project_only=bool(getattr(args, "project_only", False)),
        portable_only=bool(getattr(args, "portable_only", False)),
    )
    if not bool(getattr(args, "no_check_github", False)):
        candidates = annotate_github_codex_releases(candidates)
    try:
        action_ids, actions = resolve_global_selectors(
            manifest,
            getattr(args, "select", None),
            candidates=candidates,
            include_risky=bool(getattr(args, "include_risky", False)),
        )
    except HandoffError as exc:
        print_handoff_error(exc, args.path)
        return 2
    set_selected_global_actions(manifest, action_ids, actions)
    if not getattr(args, "yes", False) and sys.stdin.isatty():
        print("Selected Codex-wide installs to record:")
        for candidate in actions:
            print(f"  - {candidate['id']}: {truncate(str(candidate['label']), 160)}")
        if not confirm_privacy_apply(manifest):
            print("No files changed.")
            return 0
        answer = input("Record this selection in .codex/handoff/manifest.json? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No files changed.")
            return 0
    if not getattr(args, "yes", False) and not sys.stdin.isatty():
        print("error: refusing to record Codex-wide install selection in non-interactive mode without --yes", file=sys.stderr)
        print(
            f"Run: ai-handoff globals select {shlex.quote(manifest['target_path'])} "
            "--select ID[,ID] --yes --ack-privacy",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "yes", False) and manifest.get("privacy", {}).get("ack_required_for_apply") and not getattr(args, "ack_privacy", False):
        print("error: refusing to write private handoff context without --ack-privacy", file=sys.stderr)
        print(privacy_prompt(manifest), file=sys.stderr)
        print(
            f"Run: ai-handoff globals select {shlex.quote(manifest['target_path'])} "
            "--select ID[,ID] --yes --ack-privacy",
            file=sys.stderr,
        )
        return 2
    try:
        result = write_manifest_artifacts(manifest)
    except HandoffError as exc:
        print_handoff_error(exc, manifest["target_path"])
        return 2
    if args.json:
        print(json.dumps(result["manifest"], indent=2, sort_keys=True))
        return 0
    print("Recorded Codex-wide install selection.")
    for action_id in action_ids:
        print(f"  - {action_id}")
    print("Wrote:")
    for path in result["written"]:
        print(f"  - {path}")
    print()
    print("Install selected Codex-wide changes later with:")
    print(f"  ai-handoff globals apply {shlex.quote(manifest['target_path'])}")
    return 0


def command_globals_apply(args: argparse.Namespace) -> int:
    if getattr(args, "yes", False):
        print("error: --yes does not install Codex-wide changes.", file=sys.stderr)
        print("Run globals apply from an interactive terminal and confirm the listed changes.", file=sys.stderr)
        return 2
    manifest, code = load_manifest_or_build(args.path)
    if manifest is None:
        return code
    if getattr(args, "select", None):
        include_low_confidence = bool(getattr(args, "include_low_confidence", False) or getattr(args, "include_risky", False))
        candidates = filtered_global_candidates(
            manifest,
            include_low_confidence=include_low_confidence,
            include_risky=bool(getattr(args, "include_risky", False)),
            project_only=bool(getattr(args, "project_only", False)),
            portable_only=bool(getattr(args, "portable_only", False)),
        )
        if not bool(getattr(args, "no_check_github", False)):
            candidates = annotate_github_codex_releases(candidates)
        try:
            action_ids, actions = resolve_global_selectors(
                manifest,
                getattr(args, "select", None),
                candidates=candidates,
                include_risky=bool(getattr(args, "include_risky", False)),
            )
        except HandoffError as exc:
            print_handoff_error(exc, args.path)
            return 2
        set_selected_global_actions(manifest, action_ids, actions)
    if not selected_global_candidates(manifest):
        print("No selected Codex-wide installs found.")
        print(f"Run: ai-handoff globals select {shlex.quote(manifest['target_path'])} --select ID[,ID] --yes --ack-privacy")
        print(f"Or run: ai-handoff {shlex.quote(manifest['target_path'])} and press g.")
        return 1
    if not sys.stdin.isatty():
        print("error: refusing to install Codex-wide changes in non-interactive mode", file=sys.stderr)
        print("Run this command from an interactive terminal so you can confirm changes to ~/.codex.", file=sys.stderr)
        return 2
    if not confirm_global_apply(manifest):
        print("No Codex-wide installs executed.")
        return 0
    results = apply_selected_global_actions(manifest)
    try:
        if manifest.get("applied"):
            write_project_artifacts(manifest)
        else:
            write_manifest_artifacts(manifest)
    except HandoffError as exc:
        print_handoff_error(exc, manifest["target_path"])
        return 2
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0
    print("Codex-wide install results:")
    for item in results:
        label = item.get("id")
        status = item.get("status")
        reason = item.get("reason")
        suffix = f" ({reason})" if reason else ""
        print(f"  - {label}: {status}{suffix}")
    return 0


def command_scan(args: argparse.Namespace, *, default_interactive: bool = False) -> int:
    project = Path(args.path).expanduser().resolve()
    use_latest = bool(default_interactive or getattr(args, "use_latest_selection", False))
    selection = load_latest_selection(project) if use_latest else {}
    selected_session_ids = parse_session_ids(getattr(args, "sessions", None))
    if default_interactive and not selected_session_ids:
        selected_session_ids = load_latest_session_ids(project)
    try:
        manifest = build_manifest(
            args.path,
            last=args.last,
            since=args.since,
            include_transcripts=args.include_transcripts,
            init_only=getattr(args, "init_only", False),
            selection=selection,
            selected_session_ids=selected_session_ids or None,
            all_projects=getattr(args, "all_projects", False),
            from_claude_project=getattr(args, "from_claude_project", None),
            search=getattr(args, "search", None),
            branch=getattr(args, "branch", None),
        )
    except HandoffError as exc:
        print_handoff_error(exc, args.path)
        return 2
    validation_code = validate_requested_sessions(manifest, bool(getattr(args, "allow_empty_selection", False)))
    if validation_code is not None:
        return validation_code
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if default_interactive and sys.stdin.isatty() and not args.no_interactive:
        manifest["selected_global_action_ids"] = load_latest_global_action_ids(project)
        manifest["selected_global_actions"] = load_latest_global_actions(project)
        manifest["global_apply_results"] = load_latest_global_apply_results(project)
        return wizard_flow(manifest)
    print_dry_run(manifest)
    return 0


def command_diff(args: argparse.Namespace) -> int:
    selection = load_latest_selection(Path(args.path).expanduser().resolve()) if getattr(args, "use_latest_selection", False) else {}
    selected_session_ids = parse_session_ids(getattr(args, "sessions", None))
    try:
        manifest = build_manifest(
            args.path,
            last=args.last,
            since=args.since,
            include_transcripts=args.include_transcripts,
            init_only=getattr(args, "init_only", False),
            selection=selection,
            selected_session_ids=selected_session_ids or None,
            all_projects=getattr(args, "all_projects", False),
            from_claude_project=getattr(args, "from_claude_project", None),
            search=getattr(args, "search", None),
            branch=getattr(args, "branch", None),
        )
    except HandoffError as exc:
        print_handoff_error(exc, args.path)
        return 2
    validation_code = validate_requested_sessions(manifest, bool(getattr(args, "allow_empty_selection", False)))
    if validation_code is not None:
        return validation_code
    rendered = render_diff(manifest, include_manifest=bool(getattr(args, "include_manifest", False)))
    if not rendered:
        print("No project-local write changes.")
        return 0
    print(rendered, end="")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    project = Path(args.path).expanduser().resolve()
    if getattr(args, "apply_global", False) and args.yes:
        print("error: --yes only confirms project-local writes.", file=sys.stderr)
        print("Codex-wide installs can change ~/.codex, bridge plugins, or run codex commands for every Codex project on this machine.", file=sys.stderr)
        print(f"Run: ai-handoff apply {shlex.quote(str(project))} --apply-global", file=sys.stderr)
        return 2
    selection = load_latest_selection(project)
    selected_session_ids = parse_session_ids(getattr(args, "sessions", None)) or load_latest_session_ids(project)
    selected_global_action_ids = load_latest_global_action_ids(project)
    selected_global_actions = load_latest_global_actions(project)
    global_apply_results = load_latest_global_apply_results(project)
    if args.yes:
        selection.update(
            {
                "propose_mcps": False,
                "propose_skill_conversions": False,
                "propose_plugin_installs": False,
            }
        )
    try:
        manifest = build_manifest(
            str(project),
            last=args.last,
            since=args.since,
            include_transcripts=args.include_transcripts,
            init_only=getattr(args, "init_only", False),
            selection=selection,
            selected_session_ids=selected_session_ids,
            all_projects=getattr(args, "all_projects", False),
            from_claude_project=getattr(args, "from_claude_project", None),
            search=getattr(args, "search", None),
            branch=getattr(args, "branch", None),
        )
    except HandoffError as exc:
        print_handoff_error(exc, args.path)
        return 2
    validation_code = validate_requested_sessions(manifest, bool(getattr(args, "allow_empty_selection", False)))
    if validation_code is not None:
        return validation_code
    manifest["selected_global_action_ids"] = selected_global_action_ids
    manifest["selected_global_actions"] = selected_global_actions
    manifest["global_apply_results"] = global_apply_results
    if not args.yes and sys.stdin.isatty():
        print_dry_run(manifest)
        if not confirm_privacy_apply(manifest):
            print("No files changed.")
            return 0
    if not args.yes and not sys.stdin.isatty():
        print("error: refusing to write in non-interactive mode without --yes", file=sys.stderr)
        print(f"Run: ai-handoff apply {shlex.quote(str(project))} --yes", file=sys.stderr)
        return 2
    if args.yes and manifest.get("privacy", {}).get("ack_required_for_apply") and not getattr(args, "ack_privacy", False):
        print("error: refusing to write private handoff context without --ack-privacy", file=sys.stderr)
        print(privacy_prompt(manifest), file=sys.stderr)
        print(f"Run: ai-handoff apply {shlex.quote(str(project))} --yes --ack-privacy", file=sys.stderr)
        return 2
    try:
        result = write_project_artifacts(manifest)
    except HandoffError as exc:
        print_handoff_error(exc, str(project))
        return 2
    if args.json:
        print(json.dumps(result["manifest"], indent=2, sort_keys=True))
        return 0
    print("Applied project-local handoff.")
    for path in result["written"]:
        print(f"  - {path}")
    if getattr(args, "apply_global", False):
        if not selected_global_action_ids:
            print("No selected Codex-wide installs found. Run the interactive menu and press g to choose them.")
        else:
            if confirm_global_apply(manifest):
                global_results = apply_selected_global_actions(manifest)
                try:
                    write_project_artifacts(manifest)
                except HandoffError as exc:
                    print_handoff_error(exc, str(project))
                    return 2
                print("Codex-wide install results:")
                for item in global_results:
                    label = item.get("id")
                    status = item.get("status")
                    reason = item.get("reason")
                    suffix = f" ({reason})" if reason else ""
                    print(f"  - {label}: {status}{suffix}")
    elif selected_global_action_ids:
        print("Selected Codex-wide installs were preserved in the manifest; rerun with --apply-global to install them.")
    if args.yes and not getattr(args, "apply_global", False):
        print("Codex-wide MCP/plugin/skill installs were not executed because --yes is project-local only.")
    return 0


def iter_run_manifests(project: Path) -> List[Path]:
    runs_dir = project / ".codex" / "handoff" / "runs"
    if not runs_dir.exists():
        return []
    return sorted(runs_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def command_history(args: argparse.Namespace) -> int:
    project = Path(args.path or ".").expanduser().resolve()
    manifests = iter_run_manifests(project)
    if args.json:
        rows = [load_json(path) for path in manifests]
        print(json.dumps([row for row in rows if row], indent=2, sort_keys=True))
        return 0
    if not manifests:
        print(f"No ai-handoff history found for {project}.")
        return 0
    print(f"AI Handoff history for {project}")
    for path in manifests:
        data = load_json(path) or {}
        print(
            f"- {data.get('run_id', path.stem)} | {data.get('generated_at', 'unknown')} | "
            f"applied={data.get('applied')} | writes={len(data.get('applied_actions') or [])}"
        )
    return 0


def command_show(args: argparse.Namespace) -> int:
    project = Path(args.path or ".").expanduser().resolve()
    run_id = args.run_id
    candidates = [
        project / ".codex" / "handoff" / "runs" / f"{run_id}.json",
        project / ".codex" / "handoff" / "manifest.json",
    ]
    data = None
    for path in candidates:
        loaded = load_json(path)
        if isinstance(loaded, dict) and (loaded.get("run_id") == run_id or path.name == "manifest.json"):
            data = loaded
            break
    if data is None:
        print(f"No manifest found for run {run_id} in {project}.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(render_summary(data))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    home = home_dir()
    project = Path(args.path).expanduser().resolve() if args.path else None
    rows = [
        ("home", str(home), home.exists()),
        ("Claude config", str(home / ".claude" / "claude_desktop_config.json"), (home / ".claude" / "claude_desktop_config.json").exists()),
        ("Codex config", str(home / ".codex" / "config.toml"), (home / ".codex" / "config.toml").exists()),
        ("Claude skills", str(home / ".claude" / "skills"), (home / ".claude" / "skills").exists()),
        ("Codex skills", str(home / ".codex" / "skills"), (home / ".codex" / "skills").exists()),
    ]
    if project:
        key = claude_project_key(project)
        rows.extend(
            [
                ("project", str(project), project.exists()),
                ("Claude project", str(home / ".claude" / "projects" / key), (home / ".claude" / "projects" / key).exists()),
                ("CLAUDE.md", str(project / "CLAUDE.md"), (project / "CLAUDE.md").exists()),
                ("AGENTS.md", str(project / "AGENTS.md"), (project / "AGENTS.md").exists()),
            ]
        )
    if args.json:
        print(json.dumps([{"name": name, "path": path, "ok": ok} for name, path, ok in rows], indent=2))
        return 0
    print("AI Handoff doctor")
    for name, path, ok in rows:
        state = "ok" if ok else "missing"
        print(f"- {name}: {state} ({path})")
    if project:
        sessions = discover_claude_sessions(project, last=0)
        close_matches = sessions.get("close_project_matches") or []
        if close_matches:
            print()
            print("Nearby Claude project folders:")
            for item in close_matches[:5]:
                print(f"- {item['key']}: {item['entry_count']} sessions ({item['reason']})")
            print(f"Use: ai-handoff conversations {shlex.quote(str(project))} --all-projects --search TEXT")
    return 0


def add_scan_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path")
    parser.add_argument("--last", type=int, default=3, help="Number of recent Claude sessions to select.")
    parser.add_argument("--since", help="Only include Claude sessions newer than a duration such as 7d or 12h.")
    parser.add_argument("--sessions", help="Comma-separated Claude session IDs to select exactly.")
    parser.add_argument("--all-projects", action="store_true", help="Search Claude sessions across all Claude project folders.")
    parser.add_argument("--from-claude-project", help="Read sessions from a specific Claude project key.")
    parser.add_argument("--search", help="Filter Claude sessions by text across title, prompt, path, branch, or project key.")
    parser.add_argument("--branch", help="Filter Claude sessions by git branch.")
    parser.add_argument("--allow-empty-selection", action="store_true", help="Allow an explicit session selection that resolves to no conversations.")
    parser.add_argument("--include-transcripts", action="store_true", help="Include fuller redacted transcript excerpts.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--debug", action="store_true", help="Accepted for compatibility; diagnostics are included by default.")
    parser.add_argument("--no-interactive", action="store_true", help="Disable the interactive menu.")
    parser.add_argument("--ack-privacy", action="store_true", help="Acknowledge that apply may write Claude-derived context into handoff artifacts.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-handoff",
        description="Prepare a Claude Code project for Codex handoff.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Golden path:
              ai-handoff PATH                  Interactive dry run
              ai-handoff conversations PATH    Inspect and choose Claude sessions
              ai-handoff diff PATH             Preview project-local writes
              ai-handoff privacy PATH          Explain private/local context that may be written
              ai-handoff apply PATH --yes --ack-privacy
                                                Write project-local handoff files only
              ai-handoff globals PATH          Inspect Codex-wide MCP/skill/plugin install candidates
              ai-handoff globals select PATH --select skill:name --yes --ack-privacy
                                                Persist selected Codex-wide install choices
              ai-handoff globals apply PATH    Install selected Codex-wide changes

            Examples:
              ai-handoff ~/Code/speech-to-text-tools
              ai-handoff conversations ~/Code/speech-to-text-tools
              ai-handoff conversations ~/Code/speech-to-text-tools --all-projects --search speech
              ai-handoff diff ~/Code/speech-to-text-tools
              ai-handoff privacy ~/Code/speech-to-text-tools
              ai-handoff scan ~/Code/speech-to-text-tools --json
              ai-handoff scan ~/Code/speech-to-text-tools --sessions session-1,session-7
              ai-handoff apply ~/Code/speech-to-text-tools --yes --ack-privacy
              ai-handoff globals ~/Code/speech-to-text-tools
              ai-handoff globals ~/Code/speech-to-text-tools --project-only
              ai-handoff globals ~/Code/speech-to-text-tools --portable-only
              ai-handoff globals select ~/Code/speech-to-text-tools --select skill:amq-cli --yes --ack-privacy
              ai-handoff globals apply ~/Code/speech-to-text-tools
              ai-handoff doctor ~/Code/speech-to-text-tools
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"ai-handoff {VERSION}")
    subparsers = parser.add_subparsers(dest="command")
    scan = subparsers.add_parser("scan", help="Run a non-mutating handoff scan.")
    add_scan_flags(scan)
    conversations = subparsers.add_parser("conversations", help="List Claude conversations and selected session IDs.")
    add_scan_flags(conversations)
    diff = subparsers.add_parser("diff", help="Preview project-local handoff file changes.")
    add_scan_flags(diff)
    diff.add_argument("--include-manifest", action="store_true", help="Include manifest JSON files in the diff.")
    privacy = subparsers.add_parser("privacy", help="Explain private/local context that handoff artifacts may write.")
    add_scan_flags(privacy)
    apply = subparsers.add_parser("apply", help="Apply project-local handoff files.")
    add_scan_flags(apply)
    apply.add_argument("--yes", action="store_true", help="Skip prompts for project-local writes only.")
    apply.add_argument("--apply-global", action="store_true", help="Also install Codex-wide changes selected in a prior interactive run.")
    init = subparsers.add_parser("init", help="Run the project-learning pass without Codex-wide install candidates.")
    add_scan_flags(init)
    init.add_argument("--apply", action="store_true", help="Write project-local init artifacts.")
    init.add_argument("--yes", action="store_true", help="Skip prompts for project-local writes only.")
    history = subparsers.add_parser("history", help="Show ai-handoff run history.")
    history.add_argument("path", nargs="?", help="Project path. Defaults to current directory.")
    history.add_argument("--json", action="store_true")
    show = subparsers.add_parser("show", help="Show a previous handoff manifest.")
    show.add_argument("run_id")
    show.add_argument("--path", help="Project path. Defaults to current directory.")
    show.add_argument("--json", action="store_true")
    globals_parser = subparsers.add_parser(
        "globals",
        help="Inspect or apply selected Codex-wide MCP/skill/plugin installs.",
        usage=(
            "ai-handoff globals PATH [--json] | "
            "ai-handoff globals select PATH --select ID[,ID] | "
            "ai-handoff globals apply PATH [--select ID[,ID]]"
        ),
    )
    globals_parser.add_argument(
        "globals_args",
        nargs="*",
        metavar="ACTION_OR_PATH",
        help="PATH, or 'select PATH' / 'apply PATH'.",
    )
    globals_parser.add_argument("--select", help="Comma-separated candidate IDs, skill names, types, or all.")
    globals_parser.add_argument("--project-only", action="store_true", help="Show or select only target-project-scoped import candidates.")
    globals_parser.add_argument("--portable-only", action="store_true", help="Show or select only candidates without obvious local-machine dependencies.")
    globals_parser.add_argument("--include-low-confidence", action="store_true", help="Include low-confidence plugin/import candidates.")
    globals_parser.add_argument("--include-risky", action="store_true", help="Include risky bulk-selection candidates such as unverified plugins and Codex-wide installs.")
    globals_parser.add_argument("--check-github", action="store_true", help="Use authenticated gh to check GitHub origins for a native .codex-plugin/plugin.json. This is the default for listing.")
    globals_parser.add_argument("--no-check-github", action="store_true", help="Do not run gh checks when listing Codex-wide candidates.")
    globals_parser.add_argument("--yes", action="store_true", help="Skip prompt when recording Codex-wide selection only.")
    globals_parser.add_argument("--ack-privacy", action="store_true", help="Acknowledge that selection writes may persist private local context.")
    globals_parser.add_argument("--json", action="store_true")
    doctor = subparsers.add_parser("doctor", help="Check Claude/Codex handoff prerequisites.")
    doctor.add_argument("path", nargs="?", help="Optional project path.")
    doctor.add_argument("--json", action="store_true")
    return parser


def normalize_argv(argv: List[str]) -> List[str]:
    commands = {"scan", "apply", "init", "history", "show", "doctor", "conversations", "globals", "diff", "privacy"}
    if not argv:
        return argv
    argv = ["--help" if arg == "help" else arg for arg in argv]
    if argv[0] in {"-h", "--help", "--version"}:
        return argv
    if argv[0] not in commands and not argv[0].startswith("-"):
        return ["_default", *argv]
    return argv


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized = normalize_argv(raw_argv)
    if normalized and normalized[0] == "_default":
        default_parser = argparse.ArgumentParser(
            prog="ai-handoff",
            description="Prepare a Claude Code project for Codex handoff.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=textwrap.dedent(
                """\
                Golden path:
                  ai-handoff PATH                  Interactive dry run
                  ai-handoff conversations PATH    Inspect and choose Claude sessions
                  ai-handoff diff PATH             Preview project-local writes
                  ai-handoff privacy PATH          Explain private/local context that may be written
                  ai-handoff apply PATH --yes --ack-privacy
                                                    Write project-local handoff files only
                  ai-handoff globals PATH          Inspect Codex-wide install candidates
                  ai-handoff globals select PATH --select skill:name --yes --ack-privacy
                                                    Persist selected Codex-wide install choices
                  ai-handoff globals apply PATH    Install selected Codex-wide changes

                Pickers:
                  press c                          Choose exact Claude conversations
                  press g                          Choose exact Codex-wide installs
                """
            ),
        )
        add_scan_flags(default_parser)
        default_parser.add_argument("--apply", action="store_true", help="Apply project-local writes.")
        default_parser.add_argument("--yes", action="store_true", help="Skip prompts for project-local writes only.")
        default_parser.add_argument("--apply-global", action="store_true", help="Also install selected Codex-wide changes.")
        args = default_parser.parse_args(normalized[1:])
        if args.apply:
            return command_apply(args)
        return command_scan(args, default_interactive=True)
    parser = build_parser()
    args = parser.parse_args(normalized)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "scan":
        return command_scan(args)
    if args.command == "diff":
        return command_diff(args)
    if args.command == "apply":
        return command_apply(args)
    if args.command == "conversations":
        return command_conversations(args)
    if args.command == "privacy":
        return command_privacy(args)
    if args.command == "globals":
        return command_globals(args)
    if args.command == "init":
        args.init_only = True
        if args.apply:
            return command_apply(args)
        return command_scan(args)
    if args.command == "history":
        return command_history(args)
    if args.command == "show":
        return command_show(args)
    if args.command == "doctor":
        return command_doctor(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
