"""pilot.py: a pilot's decisions reviewed with the user, and the disagreements grouped by criterion."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ledger = load("ledger", SKILLS / "cbma-review/scripts/ledger.py")
pilot = load("pilot", SKILLS / "cbma-review/scripts/pilot.py")


@pytest.fixture
def review(tmp_path):
    rv = tmp_path / "review"
    (rv / "search").mkdir(parents=True)
    (rv / "review.yaml").write_text("objective: t\nsearch: {query: q}\nscreening:\n"
                                    "  abstract: {inclusion: [Human, Task fMRI], exclusion: [Review]}\n"
                                    "  fulltext: {inclusion: [W]}\n")
    pmids = [str(10000000 + i) for i in range(30)]
    (rv / "search" / "records.jsonl").write_text("".join(
        json.dumps({"pmid": p, "title": f"Paper {p}", "abstract": "a"}) + "\n" for p in pmids))
    r = ledger.Review(rv)
    rows = []
    for i, p in enumerate(pmids):
        decision = "include" if i < 5 else "exclude"
        crit = {"I1": "met", "I2": "met" if decision == "include" else "not_met", "E1": "not_met"}
        rows.append({"stage": "abstract", "pmid": p, "decision": decision, "criteria": crit, "reason": f"r{i}",
                     "criteria_hash": r.criteria["abstract"]["hash"], "input_hash": r.input_hash("abstract", p),
                     "protocol_version": 1})
    (rv / "decisions").mkdir()
    (rv / "decisions" / "abstract.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    return rv


def test_the_abstract_sheet_shows_every_include_and_a_sample_of_excludes(review):
    out = pilot.cmd_sheet(ledger.Review(review), "abstract", excludes=10)
    assert out["by_decision"] == {"include": 5, "exclude": 25} and out["shown"] == 15
    sheet = Path(out["sheet"]).read_text()
    assert sheet.count("**include**") == 5 and sheet.count("**exclude** (deciding") == 10
    assert sheet.index("**exclude**") < sheet.index("**include**")          # excludes before includes
    assert "deciding: I2 not_met" in sheet and "10 of 25 excludes" in sheet and "\n\na\n" in sheet


def test_verdicts_are_recorded_beside_the_decisions_never_into_them(review):
    rv = ledger.Review(review)
    before = (review / "decisions" / "abstract.jsonl").read_text()
    pilot.cmd_record(rv, "abstract", "10000000", "agree", [], None)
    pilot.cmd_record(rv, "abstract", "10000007", "disagree", ["I2"], "the abstract says BOLD fMRI")
    pilot.cmd_record(rv, "abstract", "10000008", "disagree", ["I2"], "an fMRI study")
    assert (review / "decisions" / "abstract.jsonl").read_text() == before
    s = pilot.cmd_summary(rv)["abstract"]
    assert s["reviewed"] == 3 and s["agree"] == 1 and s["agreement"] == 0.333
    assert [d["pmid"] for d in s["disagreements_by_criterion"]["I2"]] == ["10000007", "10000008"]
    assert s["excludes_checked"] == 2 and s["excludes_wrongly_dropped"] == 2
    assert s["criteria_text"]["I2"] == "Task fMRI"
    # The latest verdict on a decision wins.
    pilot.cmd_record(rv, "abstract", "10000008", "agree", [], "on reflection, fine")
    assert pilot.cmd_summary(rv)["abstract"]["disagree"] == 1


def test_a_verdict_must_say_what_was_wrong_and_name_real_criteria(review):
    rv = ledger.Review(review)
    with pytest.raises(ledger.LedgerError, match="needs --criterion or --note"):
        pilot.cmd_record(rv, "abstract", "10000007", "disagree", [], None)
    with pytest.raises(ledger.LedgerError, match="unknown criteria"):
        pilot.cmd_record(rv, "abstract", "10000007", "disagree", ["I9"], None)
    with pytest.raises(ledger.LedgerError, match="no current abstract decision"):
        pilot.cmd_record(rv, "abstract", "99999999", "agree", [], None)


def test_feedback_on_a_reopened_decision_is_stale(review):
    rv = ledger.Review(review)
    pilot.cmd_record(rv, "abstract", "10000007", "disagree", ["I2"], "x")
    (review / "review.yaml").write_text((review / "review.yaml").read_text().replace("Task fMRI", "Task fMRI or PET"))
    s = pilot.cmd_summary(ledger.Review(review))["abstract"]
    assert s["reviewed"] == 0 and s["stale"] == 1
