#!/usr/bin/env bash
# Install autonima-skills into a workspace (the folder where you keep your reviews).
#   ./install.sh [WORKSPACE] [--no-env]      (default: the current directory)
#
# 1. Links the skills into .claude/skills (Claude Code) and .agents/skills (Codex).
# 2. Copies the agent definitions into .claude/agents.
# 3. Builds WORKSPACE/.venv from the package's uv.lock (uv if installed, else python3 -m venv
#    and the pinned requirements.lock.txt), so every review runs on the tested versions and
#    nothing is installed into the system Python. --no-env skips this and uses python3.
#    AUTONIMA_SKILLS_GROUPS chooses the optional groups (default: "meta elsevier").
# 4. Writes .claude/settings.json rules that stop Claude Code from editing the installed
#    skills and agent definitions (they are links into this package).
# 5. Records every installed file, and the interpreter, in .autonima-skills.lock.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ws="." make_env=1
for arg in "$@"; do
  case "$arg" in
    --no-env) make_env=0 ;;
    *) ws="$arg" ;;
  esac
done
ws="$(cd "$ws" && pwd)"

for target in "$ws/.claude/skills" "$ws/.agents/skills"; do
  mkdir -p "$target"
  for skill in "$here"/skills/*/; do
    name="$(basename "$skill")"
    ln -sfn "${skill%/}" "$target/$name"
  done
  echo "linked $(ls "$here/skills" | wc -l) skills into $target"
done

# Claude Code agent definitions: the stage runner and the per-stage judges, with their
# reasoning effort. Copied, not linked: a session reads them at start, and a copy keeps a
# running review on the definitions it started with. Re-run install.sh to update them.
mkdir -p "$ws/.claude/agents"
cp "$here"/agents/*.md "$ws/.claude/agents/"
echo "copied $(ls "$here"/agents/*.md | wc -l) agent definitions into $ws/.claude/agents"

py="$(command -v python3)"
if [ "$make_env" = 1 ]; then
  env="$ws/.venv"
  if command -v uv >/dev/null 2>&1; then
    groups=()
    for g in ${AUTONIMA_SKILLS_GROUPS-meta elsevier}; do groups+=(--group "$g"); done
    UV_PROJECT_ENVIRONMENT="$env" uv sync --frozen --quiet --project "$here" "${groups[@]}"
  else
    python3 -m venv "$env"
    "$env/bin/python" -m pip install --quiet --upgrade pip
    "$env/bin/python" -m pip install --quiet -r "$here/requirements.lock.txt"
  fi
  py="$env/bin/python"
  echo "built the workspace environment $env ($("$py" --version))"
fi

# Guard: Claude Code refuses to edit the installed skills, the package they link to, or the
# agent definitions. A failing script is reported to the user, never patched in place.
"$py" - "$ws" "$here" <<'PY'
import json, sys
from pathlib import Path
ws, pkg = Path(sys.argv[1]), Path(sys.argv[2])
path = ws / ".claude" / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
deny = settings.setdefault("permissions", {}).setdefault("deny", [])
rules = [f"Edit(/{d}/**)" for d in (ws / ".claude" / "skills", ws / ".claude" / "agents",
                                    ws / ".agents" / "skills", pkg / "skills", pkg / "agents")]
rules += ["Edit(.claude/skills/**)", "Edit(.claude/agents/**)"]
deny += [r for r in rules if r not in deny]
path.write_text(json.dumps(settings, indent=2) + "\n")
print(f"wrote {len(rules)} edit-deny rules into {path}")
PY

# Record what was installed, with the interpreter. `ledger.py status` compares against it and
# reports any later edit to the skills, scripts or judge settings.
"$py" "$here/skills/cbma-review/scripts/ledger.py" lock "$ws" >/dev/null
echo "recorded the installed files in $ws/.autonima-skills.lock"
echo "run the scripts with: $py"
