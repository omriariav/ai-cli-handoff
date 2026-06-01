"""Console entry point for the bundled ai-handoff CLI."""

import os
from pathlib import Path
import runpy
import sys

try:
    from . import handoff_impl
except Exception:  # pragma: no cover - fallback is for broken installs.
    handoff_impl = None


def candidate_scripts() -> list[Path]:
    candidates = []
    override = os.environ.get("AI_HANDOFF_SCRIPT")
    if override:
        candidates.append(Path(override).expanduser())
    root = Path(__file__).resolve().parents[2]
    candidates.append(root / "skills" / "ai-handoff" / "scripts" / "ai_handoff.py")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    candidates.append(home / "skills" / "ai-handoff" / "scripts" / "ai_handoff.py")
    return candidates


def main() -> int:
    if handoff_impl is not None:
        return handoff_impl.main()

    script = next((path for path in candidate_scripts() if path.exists()), None)
    if script is None:
        print("error: ai-handoff script not found", file=sys.stderr)
        print("Set AI_HANDOFF_SCRIPT or install the ai-handoff Codex skill.", file=sys.stderr)
        return 2
    namespace = runpy.run_path(str(script))
    return namespace["main"]()


if __name__ == "__main__":
    raise SystemExit(main())
