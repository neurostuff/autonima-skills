"""The Claude Code agent definitions: well formed, installed, and consistent with the runner."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
JUDGES = {"abstract": "cbma-abstract-screener", "fulltext": "cbma-fulltext-screener",
          "extraction": "cbma-extractor", "selection": "cbma-selector"}


def frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("---\n", 2)[1])


def test_definitions_are_well_formed():
    names = set()
    for path in AGENTS.glob("*.md"):
        fm = frontmatter(path)
        assert fm["name"] == path.stem and fm["description"]
        assert fm["effort"] in EFFORTS
        names.add(fm["name"])
    assert names == set(JUDGES.values()) | {"cbma-stage-runner"}


def test_judges_cannot_start_subagents_and_runner_names_them():
    for judge in JUDGES.values():
        assert frontmatter(AGENTS / f"{judge}.md")["disallowedTools"] == "Agent"
    runner = (AGENTS / "cbma-stage-runner.md").read_text()
    for stage, judge in JUDGES.items():
        assert re.search(rf"\|\s*{stage}\s*\|\s*`{judge}`", runner), (stage, judge)


def test_install_copies_the_agents(tmp_path):
    subprocess.run(["bash", str(ROOT / "install.sh"), str(tmp_path), "--no-env"], check=True, capture_output=True)
    installed = {p.name for p in (tmp_path / ".claude" / "agents").glob("*.md")}
    assert installed == {p.name for p in AGENTS.glob("*.md")}
    assert not any(p.is_symlink() for p in (tmp_path / ".claude" / "agents").iterdir())


def package_copy(tmp_path) -> Path:
    """A copy of the package, so a test can edit its files the way a stray session did."""
    pkg = tmp_path / "pkg"
    for part in ("skills", "agents"):
        shutil.copytree(ROOT / part, pkg / part, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "install.sh", pkg / "install.sh")
    return pkg


def status_package(pkg: Path, review: Path) -> dict:
    ledger = pkg / "skills" / "cbma-review" / "scripts" / "ledger.py"
    out = subprocess.run([sys.executable, str(ledger), "status", str(review)], check=True,
                         capture_output=True, text=True)
    return json.loads(out.stdout)["package"]


def test_status_reports_edits_made_after_install(tmp_path):
    """Installed skills are linked and the judge definitions are copied, and both were edited by
    other sessions mid-review without any record. install.sh now locks what it installed, and
    status names every file that differs from it."""
    pkg, ws = package_copy(tmp_path), tmp_path / "ws"
    review = ws / "review"
    review.mkdir(parents=True)
    (review / "review.yaml").write_text("objective: t\nsearch: {query: q}\nscreening:\n"
                                        "  abstract: {inclusion: [H]}\n  fulltext: {inclusion: [W]}\n")
    subprocess.run(["bash", str(pkg / "install.sh"), str(ws), "--no-env"], check=True, capture_output=True)
    lock = json.loads((ws / ".autonima-skills.lock").read_text())
    assert lock["python"]
    deny = json.loads((ws / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    assert f"Edit(/{pkg}/skills/**)" in deny and "Edit(.claude/skills/**)" in deny
    assert "skills/cbma-review/scripts/run_stage.py" in lock["files"]
    assert "agents/cbma-selector.md" in lock["files"] and lock["package"] == str(pkg)
    assert status_package(pkg, review)["lock"] == "ok"

    selector = ws / ".claude" / "agents" / "cbma-selector.md"
    selector.write_text(selector.read_text().replace("effort: medium", "effort: high"))
    runner = pkg / "skills" / "cbma-review" / "scripts" / "run_stage.py"
    runner.write_text(runner.read_text() + "\n# edited\n")
    (ws / ".claude" / "agents" / "judge_input_views.json").write_text('{"extraction": "full"}')
    drift = status_package(pkg, review)
    assert drift["lock"] == "drift"
    assert drift["changed"] == ["agents/cbma-selector.md", "skills/cbma-review/scripts/run_stage.py"]
    assert drift["added"] == ["agents/judge_input_views.json"] and drift["removed"] == []


def test_status_without_a_lock_says_so(tmp_path):
    review = tmp_path / "review"
    review.mkdir()
    (review / "review.yaml").write_text("objective: t\nsearch: {query: q}\nscreening:\n"
                                        "  abstract: {inclusion: [H]}\n  fulltext: {inclusion: [W]}\n")
    assert status_package(ROOT, review)["lock"] == "none"
