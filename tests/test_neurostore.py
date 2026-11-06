"""The neurostore skill: NeuroStore search and NeuroStore coordinates, against a fake API."""

import contextlib
import importlib.util
import io
import json
import sys
import urllib.parse
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
ns = load("neurostore", SKILLS / "neurostore/scripts/neurostore.py")

PMIDS = ["11111111", "22222222", "33333333"]          # the PubMed fixture's records

REVIEW_YAML = """\
objective: test
search:
  date_from: 2000/01/01
  date_to: 2019/12/31
  neurostore:
    query: '"emotion regulation" AND fMRI'
screening:
  abstract: {inclusion: [Human participants]}
  fulltext: {inclusion: [Whole brain]}
extraction:
  neurostore: {}
"""


def version(vid, source, analyses):
    return {"id": vid, "source": source, "updated_at": "2026-11-01", "analyses": analyses}


def analysis(aid, role="result", points=((1, 2, 3),), order=0, table=None):
    return {"id": aid, "name": f"contrast {aid}", "description": f"about {aid}", "role": role, "order": order,
            "metadata": {"table_id": table} if table else {},
            "points": [{"coordinates": list(c), "space": "MNI", "values": [{"kind": "z", "value": 3.1}]}
                       for c in points]}


class FakeAPI:
    """NeuroStore's base-studies search and per-PMID lookups, and the studies they point to."""

    def __init__(self):
        self.calls = []
        self.search_results = [{"id": f"bs{p}", "pmid": p} for p in PMIDS] + [{"id": "bsx", "pmid": None,
                                                                             "name": "no pmid"}]
        self.total = len(self.search_results)
        # 11111111: an llm parse and a Compose parse, which wins; 22222222: only non-result
        # analyses; 33333333: not in NeuroStore.
        self.base = {
            "11111111": {"id": "bs1", "pmid": "11111111", "versions": [
                {"id": "v-llm", "source": "llm", "has_coordinates": True, "updated_at": "2026-10-01"},
                {"id": "v-ss", "source": "study_schema", "has_coordinates": True, "updated_at": "2026-11-01"}]},
            "22222222": {"id": "bs2", "pmid": "22222222", "versions": [
                {"id": "v-anchor", "source": "study_schema", "has_coordinates": True}]},
        }
        self.studies = {
            "v-ss": version("v-ss", "study_schema", [
                analysis("a2", order=2, table="T2"), analysis("a1", order=1, points=((1, 2, 3), (4, 5, 6))),
                analysis("seed", role="anchor")]),
            "v-llm": version("v-llm", "llm", [analysis("old")]),
            "v-anchor": version("v-anchor", "study_schema", [analysis("s", role="anchor")]),
        }

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, q))
        if parsed.path.endswith("/base-studies/") and "search" in q:
            size, page = int(q["page_size"]), int(q["page"])
            rows = self.search_results[(page - 1) * size: page * size]
            return json.dumps({"metadata": {"total_count": self.total}, "results": rows}).encode()
        if parsed.path.endswith("/base-studies/"):
            hit = self.base.get(q["pmid"])
            return json.dumps({"results": [hit] if hit else []}).encode()
        vid = parsed.path.rsplit("/", 1)[-1]
        return json.dumps(self.studies[vid]).encode()


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(ns.Client, "_open", lambda self, url: fake(url))
    return fake


@pytest.fixture
def review(tmp_path):
    rv = tmp_path / "review"
    rv.mkdir()
    (rv / "review.yaml").write_text(REVIEW_YAML)
    return rv


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


def test_search_writes_every_pmid_and_logs_the_rest(review, api):
    out = ns.cmd_search(review, ns.Client())
    assert out == {"pmids": 3, "without_pmid": 1, "frozen": False,
                   "written": str(review / "search" / "neurostore_pmids.txt")}
    assert (review / "search" / "neurostore_pmids.txt").read_text().split() == PMIDS
    log = json.loads((review / "search" / "neurostore_search_log.json").read_text())
    assert log["params"] == {"query": '"emotion regulation" AND fMRI', "search_full_text": False,
                             "data_type": "coordinate", "year_min": 2000, "year_max": 2019}
    assert log["reported_total"] == 4 and log["without_pmid"][0]["id"] == "bsx"
    [(path, q)] = api.calls
    assert q["year_min"] == "2000" and q["year_max"] == "2019" and q["search_full_text"] == "false"


def test_search_refuses_a_partial_result_set(review, api):
    api.total = 10                                         # reports more than it returns
    with pytest.raises(ns.NeuroStoreError, match="retrieved 4 base studies but NeuroStore reported 10"):
        ns.cmd_search(review, ns.Client())


def test_a_frozen_search_must_be_the_same_search(review, api):
    ns.cmd_search(review, ns.Client())
    frozen = review.parent / "frozen_search.json"
    (review / "search" / "neurostore_search_log.json").rename(frozen)
    (review / "review.yaml").write_text(REVIEW_YAML.replace("    query:", "    snapshot: ../frozen_search.json\n    query:"))
    api.calls.clear()
    out = ns.cmd_search(review)
    assert out["frozen"] and out["pmids"] == 3 and not api.calls                    # nothing fetched
    (review / "review.yaml").write_text(REVIEW_YAML.replace("    query:", "    snapshot: ../frozen_search.json\n"
                                                            "    search_full_text: true\n    query:"))
    with pytest.raises(ns.NeuroStoreError, match="was searched with"):
        ns.cmd_search(review)


def test_the_pubmed_script_fetches_records_for_neurostores_candidates(review, api, monkeypatch):
    ns.cmd_search(review, ns.Client())
    seen = {}

    def fake_run(review_dir, query, pmids, date_from, date_to, email, client=None, neurostore_pmids=None):
        seen.update(query=query, pmids=pmids, neurostore_pmids=neurostore_pmids)
        return {"missing_from_efetch": []}
    monkeypatch.setattr(pubmed, "run", fake_run)
    assert quiet(pubmed.main, [str(review)]) == 0
    assert seen == {"query": None, "pmids": [], "neurostore_pmids": PMIDS}


def test_records_take_one_version_per_study_and_only_its_results(review, api):
    out = ns.cmd_records(review, ns.Client(), pmids=PMIDS)
    assert out["with_analyses"] == 1 and out["by_source"] == {"study_schema": 1}
    assert out["unavailable"] == {"no_kept_analyses": 1, "not_in_neurostore": 1}
    rec = json.loads((review / "neurostore" / "records" / "11111111.analyses.json").read_text())
    assert rec["neurostore"]["source"] == "study_schema" and rec["neurostore"]["version_id"] == "v-ss"
    # Compose's parse, in the paper's order; the anchor (a seed region) is not a finding.
    assert [a["key"] for a in rec["analyses"]] == ["a1", "a2"]
    assert rec["analyses"][1]["table_id"] == "T2"
    assert rec["analyses"][0]["points"][0] == {"coordinates": [1.0, 2.0, 3.0], "space": "MNI",
                                              "values": [{"value": 3.1, "kind": "z-statistic"}]}
    assert not (review / "neurostore" / "records" / "22222222.analyses.json").exists()
    rows = (review / "neurostore" / "unavailable.csv").read_text().splitlines()
    assert rows[1:] == ["22222222,no_kept_analyses,study_schema",
                        "33333333,not_in_neurostore,"]


def test_records_are_frozen_once_fetched(review, api):
    ns.cmd_records(review, ns.Client(), pmids=PMIDS)
    api.studies["v-ss"]["analyses"].append(analysis("a3", order=3))                # NeuroStore re-parses
    api.calls.clear()
    ns.cmd_records(review, ns.Client(), pmids=PMIDS)
    assert not api.calls                                                          # read from the snapshot
    rec = json.loads((review / "neurostore" / "records" / "11111111.analyses.json").read_text())
    assert [a["key"] for a in rec["analyses"]] == ["a1", "a2"]


def test_records_from_autonimas_snapshot_fetch_nothing(review, api):
    """A frozen comparison: the same coordinates autonima's NeuroStore run used."""
    snap = {"schema": 1, "options": {"sources": list(ns.SOURCES), "roles": ["result"], "base_url": ns.API},
            "studies": {"11111111": {"pmid": "11111111", "base_study_id": "bs1", "version_id": "v9",
                                     "source": "nuextract-v21", "analyses": [
                                         {"neurostore_id": "z1", "name": "n", "description": None, "role": "result",
                                          "table_id": None, "table_label": None,
                                          "points": [{"coordinates": [1.0, 2.0, 3.0], "space": "TAL",
                                                      "values": [{"value": 4.0, "kind": "t-statistic"}]}]}]},
                        "22222222": {"pmid": "22222222", "unavailable": "no_coordinates"}}}
    (review.parent / "autonima_snapshot.json").write_text(json.dumps(snap))
    (review / "review.yaml").write_text(REVIEW_YAML.replace("  neurostore: {}", "  neurostore:\n"
                                                            "    snapshot: ../autonima_snapshot.json"))
    out = ns.cmd_records(review, pmids=PMIDS)
    assert not api.calls and out["by_source"] == {"nuextract-v21": 1}
    assert out["unavailable"] == {"no_coordinates": 1, "not_in_snapshot": 1}


def test_the_ledger_imports_neurostore_records(review, api):
    """End to end: search, abstract decisions, records, import; analyses keep NeuroStore's ids."""
    (review / "search").mkdir()
    recs = pubmed.parse_pubmed_xml((FIX / "pubmed_efetch.xml").read_bytes())
    (review / "search" / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    assert quiet(ledger.main, ["register", str(review)]) == 0
    rv = ledger.Review(review)
    assert rv.criteria["extraction"]["payload"]["neurostore"] == {}
    (review / "decisions").mkdir()
    rows = [{"stage": "abstract", "pmid": p, "decision": "include", "criteria": {"I1": "met"}, "reason": "x",
             "criteria_hash": rv.criteria["abstract"]["hash"], "input_hash": rv.input_hash("abstract", p),
             "protocol_version": 1} for p in PMIDS]
    (review / "decisions" / "abstract.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert ns.cmd_records(review, ns.Client())["studies"] == 3          # the studies that passed abstracts
    rows = [{"stage": "fulltext", "pmid": "11111111", "decision": "include", "criteria": {"I1": "met"},
             "reason": "x", "criteria_hash": rv.criteria["fulltext"]["hash"],
             "input_hash": rv.input_hash("fulltext", "11111111"), "protocol_version": 1}]
    (review / "decisions" / "fulltext.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    report = ledger.cmd_import_analyses(ledger.Review(review), "neurostore")
    assert report["imported"] == 1 and report["analyses"] == 2 and report["points"] == 3
    a = json.loads((review / "analyses" / "11111111.json").read_text())
    assert [x["analysis_id"] for x in a["analyses"]] == ["11111111-a1", "11111111-a2"]
    assert a["agent"] == "neurostore" and a["protocol_version"] == 1
    assert {p["verification"] for x in a["analyses"] for p in x["points"]} == {"source"}


@pytest.mark.parametrize("change, message", [
    ("search:\n  query: q\n  neurostore: {query: x}", "search.query is a PubMed query"),
    ("search:\n  neurostore: {}", "needs a query"),
    ("search:\n  neurostore: {query: x, full_text: true}", "unknown key"),
    ("search: {query: q}\nextraction:\n  records: r\n  neurostore: {}", "keep one"),
    ("search: {query: q}\nextraction:\n  neurostore: {sources: [study_schema, pubmed]}", "unknown ['pubmed']"),
])
def test_the_ledger_rejects_unclear_neurostore_settings(tmp_path, change, message):
    rv = tmp_path / "review"
    rv.mkdir()
    (rv / "review.yaml").write_text("objective: t\n" + change + "\nscreening:\n  abstract: {inclusion: [H]}\n"
                                    "  fulltext: {inclusion: [W]}\n")
    with pytest.raises(ledger.LedgerError, match=message.replace("[", r"\[").replace("]", r"\]")):
        ledger.Review(rv)
