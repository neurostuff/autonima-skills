"""review_page.py: the local page where the user reviews a pilot's or a complete run's decisions."""

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
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
page = load("review_page", SKILLS / "cbma-review/scripts/review_page.py")

PAPER = """# A paper

## Methods
Twenty adults   viewed food pictures and neutral pictures during fMRI.

## Results
Whole-brain peaks for food > neutral are listed in Table 1.
"""


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def review(tmp_path):
    """30 records; 20 abstracts judged (a pilot); 4 full texts; 2 included studies with analyses."""
    rv = tmp_path / "review"
    rv.mkdir()
    (rv / "review.yaml").write_text("""
objective: test
search: {query: q}
screening:
  abstract: {inclusion: [Human, Task fMRI], exclusion: [Review]}
  fulltext: {inclusion: [Adults, Whole brain], exclusion: [ROI only]}
selection:
  global: {inclusion: [Whole-brain analysis]}
  targets:
    - {name: food, inclusion: [Food cue > neutral]}
    - {name: drug, inclusion: [Drug cue > neutral]}
""")
    pmids = [str(10000000 + i) for i in range(30)]
    _write(rv / "search" / "records.jsonl", [{"pmid": p, "title": f"Paper {p}", "abstract": f"Abstract of {p}.",
                                              "journal": "J", "year": 2010, "doi": f"10.1/{p}"} for p in pmids])
    r = ledger.Review(rv)
    rows = []
    for i, p in enumerate(pmids[:20]):
        decision = "include" if i < 4 else "uncertain" if i < 6 else "exclude"
        crit = {"I1": "met", "I2": {"include": "met", "uncertain": "unclear", "exclude": "not_met"}[decision],
                "E1": "not_met"}
        rows.append({"stage": "abstract", "pmid": p, "decision": decision, "criteria": crit, "reason": f"r{i}",
                     "criteria_hash": r.criteria["abstract"]["hash"], "input_hash": r.input_hash("abstract", p),
                     "protocol_version": 1, "agent": "claude-code/m/effort-low/x"})
    _write(rv / "decisions" / "abstract.jsonl", rows)
    index = []
    for p in pmids[:4]:
        d = rv / "docs" / p
        (d / "tables").mkdir(parents=True)
        (d / "text.md").write_text(PAPER)
        (d / "meta.json").write_text(json.dumps({"text_sha256": p, "source": "pmc", "complete": True}))
        (d / "tables" / "t1.json").write_text(json.dumps({
            "table_id": "t1", "label": "Table 1", "caption": "Food > neutral", "footer": "", "duplicate_of": None,
            "coordinate_candidate": True, "has_data": True, "content_sha256": p, "tsv": "x\ty\tz\n1\t2\t3",
            "grid": [["x", "y", "z"], ["1", "2", "3"]]}))
        index.append({"pmid": p, "status": "available", "source": "pmc"})
    _write(rv / "fulltext" / "index.jsonl", index)
    r = ledger.Review(rv)
    ft = [("include", {"I1": "met", "I2": "met", "E1": "not_met"}, [], "food pictures and neutral pictures"),
          ("include", {"I1": "met", "I2": "met", "E1": "not_met"}, [], "Whole-brain peaks for food > neutral"),
          ("exclude", {"I1": "met", "I2": "not_met", "E1": "met"}, [1], "these words are not in the paper at all"),
          ("incomplete", {"I1": "unclear", "I2": "unclear", "E1": "unclear"}, [], None)]
    rows = []
    for p, (decision, crit, ungrounded, quote) in zip(pmids[:4], ft):
        rows.append({"stage": "fulltext", "pmid": p, "decision": decision, "criteria": crit, "reason": "why",
                     "evidence": [{"criterion": "I2", "quote": quote}] if quote else [],
                     "ungrounded_evidence": ungrounded, "criteria_hash": r.criteria["fulltext"]["hash"],
                     "input_hash": r.input_hash("fulltext", p), "protocol_version": 1})
    _write(rv / "decisions" / "fulltext.jsonl", rows)
    for p, names in ((pmids[0], ["food > neutral", "drug > neutral", "food > neutral, obese"]),
                     (pmids[1], ["rest > task"])):
        (rv / "analyses").mkdir(exist_ok=True)
        (rv / "analyses" / f"{p}.json").write_text(json.dumps({
            "pmid": p, "tables": [], "criteria_hash": r.criteria["extraction"]["hash"],
            "input_hash": r.input_hash("extraction", p),
            "analyses": [{"analysis_id": f"{p}-{i}", "table_id": "record", "table_label": "record",
                          "table_caption": "", "name": n, "description": None, "n_points": 2,
                          "points": [{"xyz": [1, 2, 3], "space": "MNI", "values": [], "verification": "source"}] * 2}
                         for i, n in enumerate(names)]}))
    r = ledger.Review(rv)
    rows = []
    for p, picks in ((pmids[0], {0: "food", 1: "drug", 2: "food"}), (pmids[1], {})):
        for i in range(3 if p == pmids[0] else 1):
            for target in ("food", "drug"):
                include = picks.get(i) == target
                rows.append({"stage": "selection", "pmid": p, "analysis_id": f"{p}-{i}", "target": target,
                             "include": include, "criteria": {"GI1": "met", "I1": "met" if include else "not_met"},
                             "reason": "because", "criteria_hash": r.criteria["selection"]["hash"],
                             "input_hash": r.input_hash("selection", p), "protocol_version": 1})
    _write(rv / "decisions" / "selection.jsonl", rows)
    return rv


def test_deciding_criteria_are_what_stands_in_the_way():
    crit = {"I1": "met", "I2": "not_met", "I3": "unclear", "E1": "met", "E2": "unclear", "GI1": "not_met", "GE1": "met"}
    assert page.deciding(crit, excluded=True) == ["I2", "E1", "GI1", "GE1"]          # the hard failures
    assert page.deciding({"I1": "met", "I2": "unclear"}, excluded=False) == ["I2"]    # an uncertain abstract
    assert page.deciding({"I1": "met", "E1": "not_met"}, excluded=False) == []


def test_a_quote_is_found_in_its_paragraph_despite_spacing_and_case():
    norm = page._normalize(PAPER)
    hit = page.find_context(PAPER, norm, "twenty adults viewed food pictures")
    assert hit["found"] and hit["match"] == "Twenty adults   viewed food pictures" and "## Methods" in hit["before"]
    joined = page.find_context(PAPER, norm, "Twenty adults ... Whole-brain peaks for food > neutral are listed")
    assert joined["found"] and joined["partial"] and joined["match"].startswith("Whole-brain peaks")
    assert page.find_context(PAPER, norm, "no such sentence in this paper")["found"] is False


def test_the_overview_knows_a_pilot_and_groups_decisions_into_lanes(review):
    snap = page.Snapshot(review)
    ov = snap.overview()
    assert ov["pilot"] and ov["judged"] == {"abstract": 20, "fulltext": 4, "selection": 2} and ov["records"] == 30
    lanes = {s: {ln["id"]: ln["size"] for ln in ov["lanes"][s]} for s in ov["lanes"]}
    assert lanes["abstract"] == {"known": 0, "uncertain": 2, "close": 0, "excluded": 14, "included": 4}
    assert lanes["fulltext"] == {"known": 0, "check": 1, "close": 0, "excluded": 0, "included": 2, "incomplete": 1}
    assert lanes["selection"] == {"known": 0, "close": 0, "none_selected": 1, "selected": 1}
    assert ov["in_map"] == 1 and ov["in_no_map"] == 1 and ov["known"] == [] and ov["checks"] == []
    by_id = {c["id"]: c for c in ov["criteria"]["abstract"]}
    assert by_id["I2"]["excluded"] == 14 and by_id["I2"]["uncertain"] == 2 and by_id["I1"]["excluded"] == 0
    # Uncertain first, then excluded, then included; an incomplete text names no deciding criterion.
    assert [it["lane"] for it in snap.items("abstract")][:3] == ["uncertain", "uncertain", "excluded"]
    assert [it for it in snap.items("fulltext") if it["decision"] == "incomplete"][0]["deciding"] == []


def test_each_lane_is_in_a_seeded_random_order(review):
    first = [it["pmid"] for it in page.Snapshot(review, seed=0).items("abstract") if it["lane"] == "excluded"]
    again = [it["pmid"] for it in page.Snapshot(review, seed=0).items("abstract") if it["lane"] == "excluded"]
    other = [it["pmid"] for it in page.Snapshot(review, seed=1).items("abstract") if it["lane"] == "excluded"]
    assert first == again and sorted(first) == sorted(other) and first != other
    assert first != sorted(first, key=int)                   # not the lowest PMIDs, which are the oldest papers


def test_the_fulltext_detail_shows_quotes_in_context_and_tables(review):
    snap = page.Snapshot(review)
    ok = snap.fulltext_detail("10000000")
    assert ok["contexts"][0]["found"] and ok["contexts"][0]["match"] == "food pictures and neutral pictures"
    assert ok["tables"][0]["caption"] == "Food > neutral" and ok["tables"][0]["grid"][1] == ["1", "2", "3"]
    assert snap.fulltext_detail("10000002")["contexts"][0]["found"] is False
    assert snap.text("10000000")["text"] == PAPER


def test_targets_show_what_each_collects_and_what_contributes_nothing(review):
    view = page.Snapshot(review).targets_view()
    food = next(t for t in view["targets"] if t["name"] == "food")
    assert food["analyses"] == 2 and food["studies"] == 1
    assert [a["name"] for a in food["by_study"][0]["analyses"]] == ["food > neutral", "food > neutral, obese"]
    assert food["by_study"][0]["analyses"][0]["reason"] and food["by_study"][0]["analyses"][0]["verdict"] is None
    assert food["considered"] == 4 and food["left_out"] == 2 and food["no_criterion"] == 0
    assert food["left_out_by"] == [{"id": "I1", "text": "Food cue > neutral", "n": 2}]
    counts = page.Snapshot(review).overview()["target_counts"]
    assert [(t["name"], t["studies"], t["analyses"]) for t in counts] == [("food", 1, 2), ("drug", 1, 1)]
    assert food["map"] is None and view["without_coordinates"] == []
    detail = page.Snapshot(review).selection_detail("10000001")
    assert [a["name"] for a in detail["analyses"]] == ["rest > task"]
    assert detail["cells"]["10000001-0"]["food"]["deciding"] == ["I1"]


@pytest.fixture
def server(review):
    srv = page.make_server(review)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, path, body=None, token=True, host=None):
    port = srv.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-Review-Token", srv.token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if res.headers["Content-Type"].startswith("application/json") else raw), res.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), exc.headers


def test_the_api_needs_the_pages_token_and_its_own_host(server):
    assert call(server, "/api/overview")[0] == 200
    assert call(server, "/api/overview", token=False)[0] == 403
    assert call(server, "/api/overview", host="evil.example:80")[0] == 403           # DNS rebinding
    assert call(server, "/api/verdict", {"stage": "abstract", "pmid": "10000000", "verdict": "agree"}, token=False)[0] == 403
    status, body, headers = call(server, "/", token=False)                           # the shell holds no data
    assert status == 200 and b"app.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
    assert call(server, "/../ledger.py", token=False)[0] == 404


def test_verdicts_from_the_page_land_in_the_feedback_file_and_never_in_the_decisions(server, review):
    before = (review / "decisions" / "abstract.jsonl").read_text()
    assert call(server, "/api/verdict", {"stage": "abstract", "pmid": "10000000", "verdict": "agree"})[0] == 200
    status, body, _ = call(server, "/api/verdict", {"stage": "abstract", "pmid": "10000007", "verdict": "disagree",
                                                    "criteria": ["I2"], "note": "it says BOLD fMRI"})
    assert status == 200 and body == {"verdict": "disagree", "criteria": ["I2"], "note": "it says BOLD fMRI", "via": "page"}
    assert call(server, "/api/verdict", {"stage": "abstract", "pmid": "10000007", "verdict": "disagree"})[0] == 400
    assert call(server, "/api/verdict", {"stage": "abstract", "pmid": "10000025", "verdict": "agree"})[0] == 400   # not judged
    rows = [json.loads(line) for line in (review / "pilot" / "feedback.jsonl").read_text().splitlines()]
    assert [(r["pmid"], r["verdict"], r["via"]) for r in rows] == [("10000000", "agree", "page"),
                                                                    ("10000007", "disagree", "page")]
    assert (review / "decisions" / "abstract.jsonl").read_text() == before
    items = {it["pmid"]: it for it in call(server, "/api/stage/abstract")[1]["items"]}
    assert items["10000007"]["verdict"]["verdict"] == "disagree" and items["10000008"]["verdict"] is None
    # pilot.py reads the same file.
    assert pilot.cmd_summary(ledger.Review(review))["abstract"]["excludes_wrongly_dropped"] == 1


def test_selection_verdicts_per_cell_and_agreeing_with_the_rest(server, review):
    cell = {"stage": "selection", "pmid": "10000000", "analysis_id": "10000000-1", "target": "food"}
    assert call(server, "/api/verdict", dict(cell, verdict="disagree", criteria=["I1"], note="it is a food cue"))[0] == 200
    assert call(server, "/api/agree-rest", {"pmid": "10000000"})[1] == {"recorded": 5}
    assert call(server, "/api/agree-rest", {"pmid": "10000000"})[1] == {"recorded": 0}
    study = next(it for it in call(server, "/api/stage/selection")[1]["items"] if it["pmid"] == "10000000")
    assert study["reviewed"] == study["n_cells"] == 6 and study["disagree"] == 1
    sel = call(server, "/api/findings")[1]["stages"]["selection"]
    assert [c["id"] for c in sel["criteria"]] == ["food:I1"] and sel["criteria"][0]["text"] == "Food cue > neutral"
    assert {ln["label"]: (ln["size"], ln["reviewed"], ln["agree"]) for ln in sel["lanes"]} == {
        "Analysis × target, selected": (3, 3, 3), "Analysis × target, not selected": (5, 3, 2)}


def test_target_counts_add_up_and_shared_criteria_are_counted_once_per_analysis(review):
    ov = page.Snapshot(review).overview()
    for t in ov["target_counts"]:
        assert t["analyses"] + t["out_shared"] + t["out_own"] == t["considered"] == ov["shared_counts"]["analyses"]
    assert all(c["id"].startswith("G") for c in ov["shared_counts"]["left_out_by"])
    for stage in ("abstract", "fulltext"):
        assert all(c["alone"] <= c["excluded"] for c in ov["criteria"][stage])
    selection = {it["pmid"]: it for it in page.Snapshot(review).with_verdicts("selection")}
    assert selection["10000000"]["n_selected"] == 3 and selection["10000001"]["n_selected"] == 0


def test_findings_group_disagreements_by_criterion_and_finishing_writes_them(server, review):
    call(server, "/api/verdict", {"stage": "abstract", "pmid": "10000007", "verdict": "disagree", "criteria": ["I2"],
                                  "note": "BOLD"})
    assert call(server, "/api/criterion-note", {"stage": "abstract", "criterion": "I2",
                                                "note": "Count BOLD as fMRI"})[0] == 200
    assert call(server, "/api/criterion-note", {"stage": "abstract", "criterion": "I9", "note": "x"})[0] == 400
    out = call(server, "/api/findings")[1]
    crit = out["stages"]["abstract"]["criteria"]
    assert [(c["id"], c["note"], [i["pmid"] for i in c["items"]]) for c in crit] == [
        ("I2", "Count BOLD as fMRI", ["10000007"])]
    excluded = next(ln for ln in out["stages"]["abstract"]["lanes"] if ln["label"] == "Excluded")
    assert (excluded["size"], excluded["reviewed"], excluded["agree"]) == (14, 1, 0)
    assert "re-opens all 20 abstract decisions" in out["markdown"]
    status, body, _ = call(server, "/api/done", {})
    assert status == 200 and server.finished
    text = Path(body["findings"]).read_text()
    assert "### I2: Task fMRI" in text and "Count BOLD as fMRI" in text and "10000007, judged exclude: BOLD" in text
    report = page.report(server.snapshot, True)
    assert report["stages"]["abstract"]["disagreements_by_criterion"]["I2"]["note"] == "Count BOLD as fMRI"


def test_reopened_decisions_are_reported_not_shown(review):
    (review / "review.yaml").write_text((review / "review.yaml").read_text().replace("Task fMRI", "Task fMRI or PET"))
    ov = page.Snapshot(review).overview()
    assert ov["judged"]["abstract"] == 0 and ov["superseded"]["abstract"] == 20


def _rewrite(path, change):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    _write(path, [change(r) or r for r in rows])


def test_close_calls_are_decisions_that_one_unclear_criterion_could_turn(review):
    def abstract(r):
        if r["pmid"] == "10000007":                     # excluded on I2 alone, with E1 left unclear
            r["criteria"]["E1"] = "unclear"
        if r["pmid"] == "10000008":                     # excluded on two criteria: not close
            r["criteria"].update(I1="not_met", E1="unclear")
    _rewrite(review / "decisions" / "abstract.jsonl", abstract)

    def selection(r):
        if r["analysis_id"] == "10000001-0" and r["target"] == "food":
            r["criteria"]["I1"] = "met"                 # left out although no criterion failed
    _rewrite(review / "decisions" / "selection.jsonl", selection)
    snap = page.Snapshot(review)
    close = [it for it in snap.items("abstract") if it["lane"] == "close"]
    assert [(it["pmid"], it["close"]) for it in close] == [("10000007", ["E1"])]
    assert snap.overview()["close"] == {"abstract": 1, "fulltext": 0, "selection": 1}
    study = [it for it in snap.items("selection") if it["pmid"] == "10000001"][0]
    assert study["lane"] == "close" and study["close"] == 1
    cells = snap.selection_detail("10000001")["cells"]["10000001-0"]
    assert cells["food"]["close"] and not cells["drug"]["close"]
    # The counts on the Overview no longer follow the lanes.
    assert snap.overview()["in_map"] == 1 and snap.overview()["in_no_map"] == 1


def test_studies_the_user_named_get_their_own_group_and_say_where_they_ended(review):
    (review / "search" / "known_check.json").write_text(json.dumps({
        "known": 6, "found": ["10000000", "10000001", "10000002", "10000010", "10000025"],
        "missed": [{"pmid": "99", "title": "Unseen", "reason": "query_does_not_match"}]}))
    snap = page.Snapshot(review)
    rows = {k["pmid"]: k for k in snap.overview()["known"]}
    assert [k["tone"] for k in snap.overview()["known"]] == ["bad", "bad", "bad", "warn", "wait", "ok"]
    assert rows["99"]["outcome"] == "Not found by the search" and rows["99"]["reason"] == "the query does not match it"
    assert rows["10000010"]["outcome"] == "Excluded at abstract" and rows["10000010"]["deciding"] == ["I2"]
    assert rows["10000002"]["outcome"] == "Excluded at full text" and rows["10000002"]["view"] == "fulltext"
    assert rows["10000001"]["outcome"] == "Included, but no analysis was selected"
    assert rows["10000025"]["outcome"] == "Abstract not judged yet"
    assert rows["10000000"]["outcome"] == "In food, drug" and rows["10000000"]["view"] == "selection"
    # Named studies lead each stage, whatever was decided.
    assert [it["pmid"] for it in snap.items("abstract") if it["lane"] == "known"] and \
        {it["pmid"] for it in snap.items("abstract") if it["lane"] == "known"} == {"10000000", "10000001", "10000002", "10000010"}
    assert snap.items("fulltext")[0]["lane"] == "known" and snap.items("selection")[0]["lane"] == "known"


def test_a_complete_run_reports_only_the_checks_that_fire(review):
    snap = page.Snapshot(review)
    assert [c["id"] for c in snap.checks()] == []
    # One study now supplies all six analyses selected for a target.
    rows = [json.loads(line) for line in (review / "decisions" / "selection.jsonl").read_text().splitlines()]
    extra = [dict(rows[0], analysis_id=f"10000000-{i}", include=True) for i in range(3, 7)]
    snap = page.Snapshot(review)
    for r in extra:
        snap.decisions["selection"][(r["pmid"], r["analysis_id"], r["target"])] = r
    checks = {c["id"]: c for c in snap.checks()}
    assert list(checks) == ["concentration:food"]
    assert checks["concentration:food"]["go"] == {"view": "selection", "pmid": "10000000"}
    assert "6 of the 6" in checks["concentration:food"]["text"]
