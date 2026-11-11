"""check_known.py: why a known study was not found by the drafted search."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_known", ROOT / "skills/pubmed-search/scripts/check_known.py")
check_known = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_known)
pubmed_search = check_known.pubmed_search


def review(tmp_path, search: str) -> Path:
    rv = tmp_path / "rv"
    (rv / "search").mkdir(parents=True)
    (rv / "review.yaml").write_text(search)
    (rv / "search" / "records.jsonl").write_text(json.dumps({"pmid": "1"}) + "\n")
    (rv / "search" / "search_log.json").write_text(json.dumps({"missing_from_efetch": ["2"]}))
    return rv


def fake_pubmed(monkeypatch, records, matching):
    monkeypatch.setattr(pubmed_search, "fetch_records", lambda client, pmids: (
        [r for r in records if r["pmid"] in pmids], [p for p in pmids if p not in {r["pmid"] for r in records}]))
    monkeypatch.setattr(pubmed_search, "_esearch", lambda client, term, a, b, retmax=0: (len(matching), matching))


RECS = [{"pmid": "3", "title": "old", "year": 1998}, {"pmid": "4", "title": "missed", "year": 2010},
        {"pmid": "5", "title": "matched", "year": 2012}]


def test_pubmed_search_reasons(tmp_path, monkeypatch):
    rv = review(tmp_path, "search:\n  query: emotion\n  date_from: 2000/01/01\n  date_to: 2019/12/31\n")
    fake_pubmed(monkeypatch, RECS, ["5"])
    report = check_known.run(rv, ["1", "2", "3", "4", "5", "9"], pm_client=object())
    assert report["found"] == ["1", "2"]                     # a record, or a candidate without one
    reasons = {m["pmid"]: m["reason"] for m in report["missed"]}
    assert reasons == {"3": "outside_dates", "4": "query_does_not_match", "5": "unexplained", "9": "not_in_pubmed"}


class FakeNeuroStore:
    def __init__(self, bases):
        self.bases = bases

    def get(self, path, params):
        return {"results": self.bases.get(params["pmid"], [])}


def test_neurostore_search_separates_index_from_query(tmp_path, monkeypatch):
    rv = review(tmp_path, "search:\n  query: emotion\n  neurostore: {data_type: coordinate}\n")
    fake_pubmed(monkeypatch, RECS, ["4"])
    ns = FakeNeuroStore({"4": [{"pmid": "4", "versions": [{"has_coordinates": True}]}],
                         "5": [{"pmid": "5", "versions": [{"has_coordinates": False}]}]})
    report = check_known.run(rv, ["3", "4", "5"], pm_client=object(), ns_client=ns)
    got = {m["pmid"]: (m["reason"], m["pubmed_query_matches"]) for m in report["missed"]}
    assert got == {"3": ("not_in_neurostore", False), "4": ("neurostore_query_does_not_match", True),
                   "5": ("neurostore_no_coordinates", False)}
