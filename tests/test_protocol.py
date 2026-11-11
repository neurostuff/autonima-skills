"""Protocol registration: nothing is judged under an unregistered or silently edited protocol."""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
FIX = Path(__file__).parent / "fixtures"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pubmed = load("pubmed_search", SKILLS / "pubmed-search/scripts/pubmed_search.py")
ledger = load("ledger", SKILLS / "cbma-review/scripts/ledger.py")

REVIEW_YAML = """\
objective: test
search: {query: q}
screening:
  abstract: {inclusion: [Human participants]}
  fulltext: {inclusion: [Whole brain]}
"""


@pytest.fixture
def rv(tmp_path):
    ws = tmp_path / "ws"
    review = ws / "review"
    (review / "search").mkdir(parents=True)
    (review / "review.yaml").write_text(REVIEW_YAML)
    recs = pubmed.parse_pubmed_xml((FIX / "pubmed_efetch.xml").read_bytes())
    (review / "search" / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    shutil.copytree(ROOT / "agents", ws / ".claude" / "agents")
    return review


def run(*args):
    """The ledger CLI: exit code, printed JSON (if any) and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ledger.main([str(a) for a in args])
    return code, last_json(out.getvalue()), err.getvalue()


def last_json(text):
    """The last JSON document printed; amend prints its diff first."""
    decoder, pos, last = json.JSONDecoder(), 0, None
    while (start := text.find("{", pos)) != -1:
        try:
            last, pos = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
    return last


def judge_abstracts(review, n=1):
    """Batch the abstract stage and record n include decisions."""
    assert run("batches", review, "--stage", "abstract", "--size", "10")[0] == 0
    batch = json.loads((review / "work/abstract/batch_0001.json").read_text())
    rows = [{"pmid": i["pmid"], "decision": "include", "criteria": {"I1": "met"}, "reason": "x"}
            for i in batch["items"][:n]]
    Path(batch["output"]).write_text("".join(json.dumps(r) + "\n" for r in rows))
    run("ingest", review, "--stage", "abstract", "--agent", "test/fake")


def typed(monkeypatch, answer):
    """A person at a terminal typing `answer` at the confirmation prompt."""
    monkeypatch.setattr(ledger.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)


def test_nothing_is_judged_before_registration(rv):
    code, _, err = run("batches", rv, "--stage", "abstract")
    assert code == 1 and "no registered protocol" in err and "ledger.py register" in err
    code, out, _ = run("register", rv)
    assert code == 0 and out["version"] == 1 and out["kind"] == "registration"
    record = json.loads((rv / "protocol" / "v001.json").read_text())
    assert record["review_yaml"] == REVIEW_YAML and record["previous_sha256"] is None
    assert record["judge_settings"]["efforts"] == {"abstract": "low", "fulltext": "medium",
                                                   "extraction": "low", "selection": "medium"}
    judge_abstracts(rv)
    [decision] = ledger.read_jsonl(rv / "decisions" / "abstract.jsonl")
    assert decision["protocol_version"] == 1


def test_revisions_before_any_judgment_are_recorded_freely(rv):
    run("register", rv)
    (rv / "review.yaml").write_text(REVIEW_YAML.replace("Human participants", "Adult human participants"))
    code, _, err = run("batches", rv, "--stage", "abstract")
    assert code == 1 and "Nothing has been judged yet" in err
    code, out, _ = run("register", rv, "--reason", "adults only")
    assert out["version"] == 2 and out["kind"] == "revision" and out["reason"] == "adults only"
    assert run("register", rv)[1]["unchanged"]                        # registering again is a no-op
    assert run("batches", rv, "--stage", "abstract")[0] == 0


def test_after_a_judgment_only_a_person_can_amend(rv, monkeypatch):
    run("register", rv)
    judge_abstracts(rv)
    (rv / "review.yaml").write_text(REVIEW_YAML.replace("Human participants", "Adult human participants"))
    code, _, err = run("batches", rv, "--stage", "abstract")
    assert code == 1 and "Only a person can amend" in err and "Never run amend yourself" in err
    assert run("ingest", rv, "--stage", "abstract", "--agent", "x")[0] == 1
    assert run("export", rv, "--allow-pending")[0] == 1
    code, _, err = run("register", rv)
    assert code == 1 and "amendment" in err
    # An agent's shell has no terminal.
    code, _, err = run("amend", rv, "--reason", "adults only")
    assert code == 1 and "person at a terminal" in err
    assert not (rv / "protocol" / "v002.json").exists()
    typed(monkeypatch, "yes")                                           # not the confirmation asked for
    assert run("amend", rv, "--reason", "adults only")[0] == 1
    typed(monkeypatch, "amend v002")
    code, out, _ = run("amend", rv, "--reason", "adults only")
    assert code == 0 and out["version"] == 2 and out["kind"] == "amendment"
    record = json.loads((rv / "protocol" / "v002.json").read_text())
    assert record["judgments_recorded_before"] == {"abstract": 1}
    assert record["previous_sha256"] == json.loads((rv / "protocol" / "v001.json").read_text())["sha256"]
    # The criteria changed, so the decision is judged again, under v2.
    assert ledger.Review(rv).pending("abstract") and run("batches", rv, "--stage", "abstract")[0] == 0


def test_an_amendment_needs_a_reason(rv, monkeypatch):
    run("register", rv)
    judge_abstracts(rv)
    (rv / "review.yaml").write_text(REVIEW_YAML.replace("Whole brain", "Whole-brain coverage"))
    typed(monkeypatch, "amend v002")
    code, _, err = run("amend", rv, "--reason", "  ")
    assert code == 1 and "needs --reason" in err


def test_judge_settings_are_part_of_the_protocol(rv, monkeypatch):
    run("register", rv)
    judge_abstracts(rv)
    selector = rv.parent / ".claude" / "agents" / "cbma-selector.md"
    selector.write_text(selector.read_text().replace("effort: medium", "effort: high"))
    code, _, err = run("batches", rv, "--stage", "abstract")
    assert code == 1 and "judge setting efforts" in err
    selector.write_text(selector.read_text().replace("effort: high", "effort: medium"))     # reverting works
    (rv.parent / ".claude" / "agents" / "judge_input_views.json").write_text('{"extraction": "full"}')
    code, _, err = run("batches", rv, "--stage", "abstract")
    assert code == 1 and "judge setting judge_input_views" in err


def test_a_comment_is_not_a_protocol_change(rv):
    run("register", rv)
    judge_abstracts(rv)
    (rv / "review.yaml").write_text("# reviewed with the team on Tuesday\n" + REVIEW_YAML)
    assert run("batches", rv, "--stage", "abstract")[0] == 0


def test_edited_or_removed_versions_are_detected(rv):
    run("register", rv)
    judge_abstracts(rv)
    path = rv / "protocol" / "v001.json"
    original = path.read_text()
    path.write_text(original.replace("Human participants", "Adult human participants"))
    code, _, err = run("batches", rv, "--stage", "abstract")
    assert code == 1 and "edited after it was registered" in err
    code, status, _ = run("status", rv)
    assert status["protocol"]["chain"] == "broken"
    # Starting over hides nothing: the decisions name a version that is gone.
    shutil.rmtree(rv / "protocol")
    code, _, err = run("register", rv)
    assert code == 1 and "protocol/ was altered" in err


def test_registering_a_review_already_under_way_is_reported(rv):
    """A review judged before registration existed can be registered, and says so."""
    (rv / "decisions").mkdir()
    (rv / "decisions" / "abstract.jsonl").write_text(json.dumps(
        {"stage": "abstract", "pmid": "11111111", "decision": "include", "criteria_hash": "x"}) + "\n")
    code, out, _ = run("register", rv)
    assert code == 0 and "registered after 1 judgments" in out["warning"]
    code, status, _ = run("status", rv)
    assert status["protocol"]["judgments_without_version"] == 1


def test_status_reports_the_chain(rv, monkeypatch):
    run("register", rv)
    judge_abstracts(rv, n=2)
    (rv / "review.yaml").write_text(REVIEW_YAML.replace("Whole brain", "Whole-brain coverage"))
    code, status, err = run("status", rv)
    assert not status["protocol"]["matches_live"] and "WARNING: protocol" in err
    typed(monkeypatch, "amend v002")
    run("amend", rv, "--reason", "clarify coverage")
    code, status, _ = run("status", rv)
    p = status["protocol"]
    assert p["version"] == 2 and p["amendments"] == 1 and p["matches_live"]
    assert [(v["version"], v["kind"], v["judgments"]) for v in p["versions"]] == [(1, "registration", 2),
                                                                                  (2, "amendment", 0)]
