"""A local page where the user reviews a review's decisions: a pilot's, or a complete run's.

    python review_page.py REVIEW [--port N] [--no-open] [--idle-minutes 180] [--seed 0]

It serves one page on 127.0.0.1 and opens it in the user's browser. The page shows where studies
and analyses were lost, stage by stage and criterion by criterion, and lets the user read the
decisions one at a time with the evidence beside them: the abstract, the quotes in their
paragraphs, the tables, the analyses each target collects. The user marks agree or disagree, names
the criterion that was misapplied, and can note what a criterion should say.

What it writes, all under REVIEW/pilot/ (nothing else in the review changes):
- feedback.jsonl          one verdict per line, marked `via: page` (pilot.py's format);
- criterion_notes.jsonl   the user's notes on criteria;
- findings.md             a readable summary, written when the user clicks "Finish review".

A verdict never changes a decision: only judges and the ledger decide. Disagreements become a
protocol amendment, which the user makes (see cbma-review, Registration).

Which decisions it puts first, at every stage: the studies the user named as belonging in the
review (search/known_check.json, written by check_known.py), then
- abstracts: uncertain ones, close calls, then excludes (an abstract exclude is final; an include
  gets a second look at full text), then includes;
- full texts: decisions whose quote was not found in the paper, close calls, then excludes, then
  includes, then texts judged incomplete;
- analyses: close calls, included studies from which nothing was selected, then the rest.
A close call is a decision one reading could turn: a study included with a criterion left unclear,
or excluded on a single criterion with another left unclear; an analysis selected on an unclear
criterion, or left out although no criterion failed.
The Overview also says where each named study ended up and, for a complete run, lists the patterns
worth a look (`Snapshot.checks`): only the ones that fire.
Within each group the order is a seeded shuffle, so in a complete run the first N of a group are a
random sample of it, and agreement on them estimates the group's.

Run it in the background: it prints one JSON line when it starts, and the findings as JSON when
the user finishes (or after --idle-minutes without a request). The page's token is in the URL
fragment the browser is opened with and is never printed, unless --no-open asks for the full URL
(for a harness that shows the page in its own pane).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import secrets
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
import pilot  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "review_page"
ASSET_TYPES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
               "app.css": "text/css; charset=utf-8",
               **{f"fonts/Roboto-{w}.woff2": "font/woff2" for w in ("Regular", "Medium", "Bold", "Italic")}}
MAX_TEXT = 600_000          # characters of a paper's text sent to the page
MAX_TABLE_ROWS = 80

KNOWN_LANE = ("known", "Studies you named", "You said these belong in the review. Check what the judges did with them.")
LANES = {
    "abstract": [KNOWN_LANE, ("uncertain", "Uncertain", "The abstract did not settle a criterion. These show where a criterion is ambiguous."),
                 ("close", "Close calls", "Excluded on a single criterion, with another left unclear."),
                 ("excluded", "Excluded", "Final at this stage: check for studies that belong."),
                 ("included", "Included", "These go on to full text, where they are judged again.")],
    "fulltext": [KNOWN_LANE, ("check", "Needs a look", "A quote the judge relied on was not found in the paper."),
                 ("close", "Close calls", "Included with a criterion left unclear, or excluded on a single criterion with another left unclear."),
                 ("excluded", "Excluded", "Final: an excluded study contributes nothing to any map."),
                 ("included", "Included", "Their analyses go on to selection."),
                 ("incomplete", "Text incomplete", "The file was not the whole article, so nothing was decided. "
                                                   "These go back to retrieval; supply the paper if you can.")],
    "selection": [KNOWN_LANE,
                  ("close", "Close calls", "An analysis was selected on a criterion marked unclear, or left out "
                                           "although no criterion failed."),
                  ("none_selected", "Nothing selected", "Included studies that contribute no analysis to any target."),
                  ("selected", "Selected", "Studies with at least one analysis in a target.")],
}


def deciding(criteria: Optional[dict], excluded: bool) -> List[str]:
    """The criteria that stand in the way of inclusion: inclusion criteria not met, exclusion
    criteria met; for a decision that is not an exclusion, also the inclusion criteria left unclear."""
    hard, soft = [], []
    for k, v in (criteria or {}).items():
        kind = k.lstrip("G")[:1]
        if (kind == "I" and v == "not_met") or (kind == "E" and v == "met"):
            hard.append(k)
        elif kind == "I" and v == "unclear":
            soft.append(k)
    return hard if (excluded and hard) else hard + soft


def unclear(criteria: Optional[dict]) -> List[str]:
    return [k for k, v in (criteria or {}).items() if v == "unclear"]


def close_cell(rec: dict) -> bool:
    """A selection decision that could go the other way: selected on a criterion marked unclear,
    or left out although no criterion failed."""
    if rec["include"]:
        return bool(unclear(rec.get("criteria")))
    return not [c for c in deciding(rec.get("criteria"), True) if rec["criteria"].get(c) != "unclear"]


# Words that say nothing about what an analysis contrasts.
STOP = set("""the of in and to a an for vs versus with during on at by from than is or as minus contrast contrasts
effect effects main group groups condition conditions activation activations activity brain region regions areas
area table response responses greater showing significant analysis analyses whole task all between across compared
positive negative increased decreased more less correlation cluster clusters results non not""".split())

MISSED = {"not_in_pubmed": "PubMed has no record of it", "outside_dates": "published outside the search dates",
          "query_does_not_match": "the query does not match it", "not_in_neurostore": "NeuroStore does not index it",
          "neurostore_no_coordinates": "NeuroStore has no coordinates for it",
          "neurostore_query_does_not_match": "the query does not match it"}
FEW_STUDIES = 3             # a cluster holding peaks from this many studies or fewer is reported as fragile


def _normalize(text: str) -> Tuple[str, List[int]]:
    """Lower-cased text with whitespace runs collapsed and typographic quotes and dashes
    straightened, and for each character its index in the original."""
    table = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-",
             " ": " "}
    out, index, space = [], [], True
    for i, ch in enumerate(text):
        ch = table.get(ch, ch)
        if ch.isspace():
            if not space:
                out.append(" ")
                index.append(i)
            space = True
        else:
            out.append(ch.lower())
            index.append(i)
            space = False
    return "".join(out), index


def find_context(text: str, norm: Tuple[str, List[int]], quote: str, pad: int = 320) -> dict:
    """Where a judge's quote sits in the paper, with the rest of its paragraph around it (at most
    `pad` characters each side). A quote joined with an ellipsis is located by its longest part."""
    hay, index = norm
    parts = [p for p in _normalize(quote)[0].replace("…", "...").split("...") if len(p.strip()) >= 12]
    needle = max(parts, key=len).strip() if parts else _normalize(quote)[0].strip()
    at = hay.find(needle) if needle else -1
    if at < 0:
        return {"found": False}
    start, end = index[at], index[at + len(needle) - 1] + 1
    before, after = text[max(0, start - pad):start], text[end:end + pad]
    cut_before, cut_after = start > pad and "\n\n" not in before, len(text) > end + pad and "\n\n" not in after
    return {"found": True, "before": before.rsplit("\n\n", 1)[-1], "match": text[start:end],
            "after": after.split("\n\n", 1)[0], "cut_before": cut_before, "cut_after": cut_after,
            "partial": len(parts) > 1}


class Snapshot:
    """The review as the page shows it. Built once, and again on reload; verdicts are kept live."""

    def __init__(self, root: Path, seed: int = 0):
        self.root, self.seed = Path(root).resolve(), seed
        self.load()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        rv = self.rv = ledger.Review(self.root)
        self.status = ledger.cmd_status(rv)
        self.decisions = {s: rv.valid_decisions(s) for s in pilot.STAGES}
        self.criteria = {s: rv.criteria[s] for s in pilot.STAGES}
        self.targets = list(self.criteria["selection"]["payload"]["targets"])
        self.known = ledger.known_studies(self.root)
        self._checks: Optional[List[dict]] = None
        self.verdicts: Dict[tuple, dict] = {}
        for r in ledger.read_jsonl(pilot.feedback_path(rv)):
            if r["criteria_hash"] in rv.accepted_criteria_hashes(r["stage"]):
                self.verdicts[(r["stage"], r["pmid"], r.get("analysis_id"), r.get("target"))] = r
        self._items: Dict[str, List[dict]] = {}
        self._analyses: Dict[str, Optional[dict]] = {}
        self._norm: Dict[str, tuple] = {}

    def analyses(self, pmid: str) -> Optional[dict]:
        if pmid not in self._analyses:
            self._analyses[pmid] = self.rv.analyses(pmid)
        return self._analyses[pmid]

    def _order(self, stage: str, items: List[dict]) -> List[dict]:
        """Lane by lane, each in a seeded shuffle: the first N of a lane are a random sample of it."""
        rank = {lane: i for i, (lane, _, _) in enumerate(LANES[stage])}
        by_lane = defaultdict(list)
        for it in sorted(items, key=lambda it: int(it["pmid"])):
            by_lane[it["lane"]].append(it)
        out = []
        for lane in sorted(by_lane, key=rank.get):
            random.Random(f"{self.seed}:{stage}:{lane}").shuffle(by_lane[lane])
            out += by_lane[lane]
        return out

    def _analysis_name(self, pmid: str, analysis_id: Optional[str]) -> Optional[str]:
        for x in (self.analyses(pmid) or {}).get("analyses", []) if analysis_id else []:
            if x["analysis_id"] == analysis_id:
                return x.get("name") or x.get("table_label")
        return None

    def _shuffled(self, name: str, rows: list) -> list:
        random.Random(f"{self.seed}:{name}").shuffle(rows)
        return rows

    def _study(self, pmid: str) -> dict:
        r = self.rv.records.get(pmid, {})
        return {"pmid": pmid, "title": r.get("title") or "", "journal": r.get("journal"), "year": r.get("year"),
                "doi": r.get("doi")}

    # ----------------------------------------------------------------- items
    def items(self, stage: str) -> List[dict]:
        if stage in self._items:
            return self._items[stage]
        out = []
        if stage in ("abstract", "fulltext"):
            for (pmid,), r in self.decisions[stage].items():
                excluded = r["decision"] == "exclude"
                # An incomplete text decides nothing, so no criterion is "deciding" for it.
                it = dict(self._study(pmid), decision=r["decision"], criteria=r.get("criteria") or {},
                          deciding=[] if r["decision"] == "incomplete" else deciding(r.get("criteria"), excluded),
                          reason=r.get("reason") or "", flags=[],
                          close=[])
                # A close call: included with a criterion left unclear, or excluded on a single
                # criterion with another left unclear. Either could go the other way on one reading.
                if r["decision"] == "include" or (excluded and len(it["deciding"]) == 1):
                    it["close"] = unclear(r.get("criteria"))
                if stage == "abstract":
                    it["abstract"] = self.rv.records.get(pmid, {}).get("abstract") or ""
                    it["lane"] = "uncertain" if r["decision"] == "uncertain" else "close" if it["close"] else \
                        ("excluded" if excluded else "included")
                else:
                    it["evidence"] = [e for e in r.get("evidence") or [] if isinstance(e, dict)]
                    it["source"] = self.rv.fulltext_index.get(pmid, {}).get("source")
                    if r.get("ungrounded_evidence"):
                        it["flags"].append("a quote was not found in the paper")
                    if r.get("text_view") == "trimmed":
                        it["flags"].append("judged on the trimmed view")
                    it["lane"] = "incomplete" if r["decision"] == "incomplete" else \
                        "check" if r.get("ungrounded_evidence") else "close" if it["close"] else \
                        ("excluded" if excluded else "included")
                if pmid in self.known:
                    it["lane"] = "known"
                out.append(it)
        else:
            cells = defaultdict(list)
            for (pmid, aid, target), r in self.decisions["selection"].items():
                cells[pmid].append(r)
            shared = self.criteria["selection"]["payload"]["global"]
            for pmid, rows in cells.items():
                selected = Counter(r["target"] for r in rows if r["include"])
                out_by = Counter(c for r in rows if not r["include"]
                                 for c in deciding(r.get("criteria"), True) if c in shared)
                out.append(dict(self._study(pmid), n_analyses=len({r["analysis_id"] for r in rows}),
                                n_selected=len({r["analysis_id"] for r in rows if r["include"]}),
                                n_cells=len(rows), selected={t: selected.get(t, 0) for t in self.targets},
                                out_by=[c for c, _ in out_by.most_common(3)],
                                close=sum(close_cell(r) for r in rows),
                                lane="known" if pmid in self.known else "close" if any(close_cell(r) for r in rows)
                                else "selected" if selected else "none_selected"))
        self._items[stage] = self._order(stage, out)
        return self._items[stage]

    def with_verdicts(self, stage: str) -> List[dict]:
        out = []
        for it in self.items(stage):
            if stage == "selection":
                rows = [v for k, v in self.verdicts.items() if k[0] == "selection" and k[1] == it["pmid"]]
                out.append(dict(it, reviewed=len(rows), disagree=sum(v["verdict"] == "disagree" for v in rows)))
            else:
                v = self.verdicts.get((stage, it["pmid"], None, None))
                out.append(dict(it, verdict=_verdict(v)))
        return out

    # ---------------------------------------------------------------- detail
    def fulltext_detail(self, pmid: str) -> dict:
        d = self.rv.doc_dir(pmid)
        if not (d / "text.md").exists():
            raise ledger.LedgerError(f"no text for {pmid}")
        text = ledger.evidence_text(d)
        if pmid not in self._norm:
            self._norm = {pmid: _normalize(text)}          # one paper cached at a time
        rec = self.decisions["fulltext"].get((pmid,)) or {}
        contexts = [dict(find_context(text, self._norm[pmid], e.get("quote") or ""), criterion=e.get("criterion"),
                         quote=e.get("quote")) for e in rec.get("evidence") or [] if isinstance(e, dict)]
        tables = []
        for p in sorted((d / "tables").glob("*.json")):
            t = json.loads(p.read_text())
            if t.get("duplicate_of"):
                continue
            grid = t.get("grid") or []
            tables.append({"label": t.get("label"), "caption": t.get("caption"), "footer": t.get("footer"),
                           "coordinates": bool(t.get("coordinate_candidate")), "rows": len(grid),
                           "grid": grid[:MAX_TABLE_ROWS]})
        return {"pmid": pmid, "abstract": self.rv.records.get(pmid, {}).get("abstract") or "",
                "contexts": contexts, "tables": tables}

    def text(self, pmid: str) -> dict:
        path = self.rv.doc_dir(pmid) / "text.md"
        if not path.exists():
            raise ledger.LedgerError(f"no text for {pmid}")
        text = path.read_text()
        return {"pmid": pmid, "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT}

    def selection_detail(self, pmid: str) -> dict:
        a = self.analyses(pmid) or {"analyses": []}
        cells: Dict[str, dict] = defaultdict(dict)
        for (p, aid, target), r in self.decisions["selection"].items():
            if p == pmid:
                cells[aid][target] = {"include": bool(r["include"]), "criteria": r.get("criteria") or {},
                                      "deciding": deciding(r.get("criteria"), not r["include"]),
                                      "reason": r.get("reason") or "", "close": close_cell(r),
                                      "verdict": _verdict(self.verdicts.get(("selection", pmid, aid, target)))}
        ft = self.decisions["fulltext"].get((pmid,)) or {}
        return {"pmid": pmid, "fulltext_reason": ft.get("reason"), "has_text": (self.rv.doc_dir(pmid) / "text.md").exists(),
                "analyses": [{"analysis_id": x["analysis_id"], "name": x.get("name"),
                              "description": x.get("description"), "table": x.get("table_label"),
                              "caption": x.get("table_caption"), "n_points": x.get("n_points")}
                             for x in a["analyses"] if x["analysis_id"] in cells],
                "cells": cells}

    # --------------------------------------------------------------- targets
    def target_counts(self) -> List[dict]:
        """Per target: what it collected, and what kept the other analyses out. Every analysis is
        in exactly one of `analyses` (selected), `out_shared` (a criterion all targets share stood
        in the way) and `out_own` (only the target's own criteria did, or none)."""
        payload = self.criteria["selection"]["payload"]
        shared = payload["global"]
        kept, studies, considered = Counter(), defaultdict(set), Counter()
        out_shared, unnamed = Counter(), Counter()
        failed: Dict[str, Counter] = {t: Counter() for t in self.targets}
        own: Dict[str, Counter] = {t: Counter() for t in self.targets}
        for (pmid, aid, target), r in self.decisions["selection"].items():
            considered[target] += 1
            if r["include"]:
                kept[target] += 1
                studies[target].add(pmid)
                continue
            ids = deciding(r.get("criteria"), True)
            failed[target].update(ids)
            unnamed[target] += not ids          # left out although no criterion stood in the way
            if any(c in shared for c in ids):
                out_shared[target] += 1
            else:
                own[target].update(ids)
        out = []
        for t in self.targets:
            texts = dict(shared, **payload["targets"][t]["criteria"])
            listed = lambda counts: [{"id": c, "text": texts.get(c), "n": n} for c, n in counts.most_common()]
            out.append({"name": t, "studies": len(studies[t]), "analyses": kept[t], "considered": considered[t],
                        "left_out": considered[t] - kept[t], "out_shared": out_shared[t],
                        "out_own": considered[t] - kept[t] - out_shared[t], "no_criterion": unnamed[t],
                        "own_by": listed(own[t]), "left_out_by": listed(failed[t])})
        return out

    def shared_counts(self) -> dict:
        """The criteria every target shares: how many analyses each kept out of every target."""
        shared = self.criteria["selection"]["payload"]["global"]
        cells: Dict[tuple, list] = defaultdict(list)
        for (pmid, aid, target), r in self.decisions["selection"].items():
            cells[(pmid, aid)].append(set(deciding(r.get("criteria"), True)) if not r["include"] else set())
        counts = Counter(c for row in cells.values() for c in set.intersection(*row) if c in shared)
        return {"analyses": len(cells), "left_out_by": [{"id": c, "text": shared.get(c), "n": n}
                                                         for c, n in counts.most_common()]}

    def targets_view(self) -> dict:
        """Each target with the analyses it collected, study by study, and each one's decision."""
        payload = self.criteria["selection"]["payload"]
        names = {}
        by_target: Dict[str, Dict[str, list]] = {t: defaultdict(list) for t in self.targets}
        for (pmid, aid, target), r in self.decisions["selection"].items():
            if not r["include"]:
                continue
            if pmid not in names:
                names[pmid] = {x["analysis_id"]: x for x in (self.analyses(pmid) or {"analyses": []})["analyses"]}
            x = names[pmid].get(aid, {})
            by_target[target][pmid].append({
                "analysis_id": aid, "name": x.get("name") or x.get("table_label"), "n_points": x.get("n_points"),
                "reason": r.get("reason") or "", "criteria": r.get("criteria") or {},
                "verdict": _verdict(self.verdicts.get(("selection", pmid, aid, target)))})
        meta_path = self.root / "results" / "meta" / "summary.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        out = []
        for row in self.target_counts():
            t = row["name"]
            spec = payload["targets"][t]
            skip = STOP | set(re.findall(r"[a-z]+", t.lower()))
            words = Counter(w for xs in by_target[t].values() for x in xs
                            for w in set(re.findall(r"[a-z]{3,}", str(x["name"] or "").lower())) - skip)
            out.append(dict(row, description=spec.get("description"), criteria=spec["criteria"],
                            words=[{"word": w, "n": n} for w, n in words.most_common(8) if n > 2],
                            top_study=max((len(xs) for xs in by_target[t].values()), default=0),
                            by_study=self._shuffled(f"target:{t}", [
                                dict(self._study(p), analyses=sorted(xs, key=lambda x: str(x["name"])))
                                for p, xs in sorted(by_target[t].items(), key=lambda kv: int(kv[0]))]),
                            map=self._map(t, meta.get(t))))
        return {"global": payload["global"], "targets": out,
                "without_coordinates": [self._study(p) for p in self.rv.fulltext_included()
                                        if not (self.analyses(p) or {}).get("analyses")]}

    def _map(self, target: str, summary: Optional[dict]) -> Optional[dict]:
        if not summary:
            return None
        out = {k: summary.get(k) for k in ("analyses", "studies", "estimator", "corrector")}
        if summary.get("analyses") != sum(1 for k, r in self.decisions["selection"].items()
                                          if k[2] == target and r["include"]):
            out["stale"] = True                 # the map was fitted before the current selection
        space = summary.get("unknown_space") or {}
        out["unknown_space_points"] = space.get("points_with_unknown_space")
        # NiMARE's FocusCounter table: one row per analysis ("pmid-pmid-analysis"), one column per
        # cluster, the number of the analysis's peaks inside it.
        behind: Dict[str, set] = defaultdict(set)
        for path in (self.root / "results" / "meta" / target).glob("*diag-FocusCounter*.tsv"):
            with path.open() as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    study = (row.get("id") or "").split("-")[0]
                    for name, n in row.items():
                        if name != "id" and float(n or 0) > 0:
                            behind[name].add(study)
        clusters = []
        for path in (self.root / "results" / "meta" / target).glob("*tab-clust.tsv"):
            with path.open() as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    if (row.get("Cluster Size (mm3)") or "").strip():
                        clusters.append({"x": row.get("X"), "y": row.get("Y"), "z": row.get("Z"),
                                         "peak": row.get("Peak Stat"), "mm3": row.get("Cluster Size (mm3)"),
                                         "studies": len(behind[row["Cluster ID"]]) if behind else None,
                                         "from": sorted(behind[row["Cluster ID"]], key=int) if behind else []})
        out["clusters"] = len(clusters)
        thin = [c for c in clusters if c["studies"] is not None and c["studies"] <= FEW_STUDIES]
        out["fragile"] = len(thin)
        out["thin"] = [dict(c, **{"from": [self._study(p) for p in c["from"]]})
                       for c in sorted(thin, key=lambda c: -float(c["mm3"]))]
        for c in clusters:
            del c["from"]
        out["largest"] = sorted(clusters, key=lambda c: -float(c["mm3"]))[:6]
        return out

    # -------------------------------------------------------------- overview
    def overview(self) -> dict:
        rv, st = self.rv, self.status
        records = len(rv.records)
        judged = {s: len(self.items(s)) for s in ("abstract", "fulltext")}
        eligible = {"abstract": records, "fulltext": len(rv.with_text())}
        sel_eligible = [p for p in rv.fulltext_included() if (self.analyses(p) or {}).get("analyses")]
        judged["selection"], eligible["selection"] = len(self.items("selection")), len(sel_eligible)
        pilot_run = judged["abstract"] < records
        crit = {}
        for stage in ("abstract", "fulltext"):
            counts = {"excluded": Counter(), "uncertain": Counter(), "alone": Counter()}
            for it in self.items(stage):
                if it["decision"] == "exclude":
                    counts["excluded"].update(it["deciding"])
                    if len(it["deciding"]) == 1:        # the studies that return if this criterion is wrong
                        counts["alone"].update(it["deciding"])
                elif it["decision"] == "uncertain":
                    counts["uncertain"].update(it["deciding"])
            crit[stage] = [{"id": c, "text": text, "excluded": counts["excluded"][c],
                            "alone": counts["alone"][c], "uncertain": counts["uncertain"][c]}
                           for c, text in self.criteria[stage]["criteria"].items()]
        passed_as = {k[0]: r["decision"] for k, r in self.decisions["abstract"].items()}
        unavailable = [dict(self._study(p), status=rv.fulltext_index.get(p, {}).get("status", "not_gathered"),
                            abstract=passed_as.get(p))
                       for p in rv.abstract_passed()
                       if rv.fulltext_index.get(p, {}).get("status") not in ("available", "incomplete")]
        unavailable.sort(key=lambda it: it["abstract"] != "include")       # the likelier ones first
        selection = self.items("selection")
        # Decisions an amendment or a changed text re-opened: studies still in the review whose
        # earlier decision is no longer current, and which the judges have not decided again.
        superseded = {}
        for stage in pilot.STAGES:
            before = {r["pmid"] for r in ledger.read_jsonl(rv.decisions_path(stage))}
            superseded[stage] = len(before & set(rv.pending(stage)))
        agents = {s: Counter(r.get("agent") or "?" for r in self.decisions[s].values()).most_common(2)
                  for s in pilot.STAGES}
        return {"name": rv.spec.get("name") or self.root.name, "objective": rv.spec.get("objective"),
                "pilot": pilot_run, "judged": judged, "eligible": eligible, "records": records,
                "status": {k: st[k] for k in ("abstract_screening", "fulltext_retrieval", "fulltext_screening",
                                              "extraction", "selection") if k in st},
                "protocol": {k: st.get("protocol", {}).get(k) for k in ("version", "amendments", "registered",
                                                                        "matches_live")},
                "criteria": crit, "targets": self.targets, "target_counts": self.target_counts(),
                "shared_counts": self.shared_counts(),
                "unavailable": unavailable, "judges": agents,
                "in_map": sum(1 for it in selection if it["n_selected"]),
                "in_no_map": sum(1 for it in selection if not it["n_selected"]),
                "close": {s: sum(1 for it in self.items(s) if it["close"]) for s in pilot.STAGES},
                "known": self.known_view(), "checks": [] if pilot_run else self.checks(),
                "lanes": {s: self.lane_counts(s) for s in pilot.STAGES}, "superseded": superseded}

    def known_view(self) -> List[dict]:
        """Where each study the user named ended up, the ones that are not in a map first."""
        out = []
        ft_included = set(self.rv.fulltext_included())
        cells = defaultdict(list)
        for (pmid, aid, target), r in self.decisions["selection"].items():
            if pmid in self.known:
                cells[pmid].append(r)
        for pmid, missed in self.known.items():
            row = dict(self._study(pmid), tone="wait", view=None, deciding=[], reason="")
            ab = self.decisions["abstract"].get((pmid,))
            ft = self.decisions["fulltext"].get((pmid,))
            status = self.rv.fulltext_index.get(pmid, {}).get("status")
            if missed is not None:
                row.update(title=missed.get("title") or "", year=missed.get("year"), journal=missed.get("journal"),
                           outcome="Not found by the search", tone="bad",
                           reason=MISSED.get(missed.get("reason"), missed.get("reason") or ""))
            elif ab is None:
                row.update(outcome="Abstract not judged yet")
            elif ab["decision"] == "exclude":
                row.update(outcome="Excluded at abstract", tone="bad", view="abstract",
                           deciding=deciding(ab.get("criteria"), True), reason=ab.get("reason") or "")
            elif ft is None and status not in (None, "available", "incomplete"):
                row.update(outcome="No full text found", tone="warn")
            elif ft is None:
                row.update(outcome=f"Abstract {'uncertain' if ab['decision'] == 'uncertain' else 'included'}; "
                                   "full text not judged yet", view="abstract")
            elif ft["decision"] == "incomplete":
                row.update(outcome="Full text incomplete", tone="warn", view="fulltext", reason=ft.get("reason") or "")
            elif ft["decision"] == "exclude":
                row.update(outcome="Excluded at full text", tone="bad", view="fulltext",
                           deciding=deciding(ft.get("criteria"), True), reason=ft.get("reason") or "")
            elif pmid not in cells:
                extracted = self.analyses(pmid)
                if extracted is not None and not extracted.get("analyses") and pmid in ft_included:
                    row.update(outcome="Included, but no coordinates were extracted", tone="warn", view="fulltext")
                else:
                    row.update(outcome="Included; analyses not selected yet", view="fulltext")
            else:
                targets = sorted({r["target"] for r in cells[pmid] if r["include"]}, key=self.targets.index)
                if targets:
                    row.update(outcome="In " + ", ".join(targets), tone="ok", view="selection")
                else:
                    row.update(outcome="Included, but no analysis was selected", tone="warn", view="selection")
            out.append(row)
        rank = {"bad": 0, "warn": 1, "wait": 2, "ok": 3}
        return sorted(out, key=lambda r: rank[r["tone"]])

    def checks(self) -> List[dict]:
        """Patterns across a complete run that are worth a look. Only the ones that fire are
        returned; each says where its studies are."""
        if self._checks is not None:
            return self._checks
        rv, out = self.rv, []
        ab = {k[0]: r["decision"] for k, r in self.decisions["abstract"].items()}
        ft = {k[0]: r for k, r in self.decisions["fulltext"].items()}
        included = sum(r["decision"] == "include" for r in ft.values())

        # Studies that passed abstract screening and never got a usable text. How many of them
        # would have been included is estimated from the studies of the same abstract decision
        # that were judged at full text.
        judged = defaultdict(list)
        for p, r in ft.items():
            if r["decision"] in ("include", "exclude"):
                judged[ab.get(p)].append(r["decision"] == "include")
        everyone = [x for xs in judged.values() for x in xs]
        rate = lambda cls: (sum(judged[cls]) / len(judged[cls]) if len(judged[cls]) >= 10
                            else sum(everyone) / len(everyone) if everyone else 0)
        missing = [p for p in rv.abstract_passed()
                   if rv.fulltext_index.get(p, {}).get("status") not in ("available", "incomplete")]
        incomplete = [p for p, r in ft.items() if r["decision"] == "incomplete"]
        expected = sum(rate(ab.get(p)) for p in missing + incomplete)
        if expected >= 3 and expected >= 0.05 * included:
            out.append({"id": "retrieval",
                        "text": f"About {round(expected)} studies that belong are probably missing for want of a full text",
                        "detail": f"{len(missing)} had no text and {len(incomplete)} an incomplete one, and "
                                  f"{included} studies were included. Estimated from this run's inclusion rates.",
                        "go": {"anchor": "unavailable"}})

        # Excluded for reporting no coordinates, when no coordinate table was parsed from the
        # file: a table that is an image or a supplement is invisible to the judge.
        about = [c for c, text in self.criteria["fulltext"]["criteria"].items()
                 if re.search(r"coordinat|foci|peak", text, re.I)]
        blind = []
        for it in self.items("fulltext"):
            if it["decision"] == "exclude" and set(it["deciding"]) & set(about):
                tables = (rv.doc_dir(it["pmid"]) / "tables").glob("*.json")
                if not any(json.loads(t.read_text()).get("coordinate_candidate") for t in tables):
                    blind.append(it["pmid"])
        if len(blind) >= 5:
            out.append({"id": "tables",
                        "text": f"{len(blind)} studies were excluded for reporting no coordinates, and their "
                                "file had no coordinate table",
                        "detail": "A table that is an image or a supplement never reaches the judge. "
                                  "Check a few against the papers.",
                        "go": {"view": "fulltext", "pmids": blind, "label": "No coordinate table in the file"}})

        fragile = []
        for t in self.targets_view()["targets"]:
            if t["top_study"] >= 5 and t["top_study"] >= 0.1 * t["analyses"]:
                top = max(t["by_study"], key=lambda s: len(s["analyses"]))
                out.append({"id": "concentration:" + t["name"],
                            "text": f"One study supplies {t['top_study']} of the {t['analyses']} analyses in {t['name']}",
                            "detail": "Each analysis enters the map as its own experiment.",
                            "go": {"view": "selection", "pmid": top["pmid"]}})
            m = t["map"]
            if m and m["clusters"] and m["fragile"] >= 0.25 * m["clusters"]:
                fragile.append((m["fragile"], m["clusters"], t["name"]))
        if fragile:
            n, of, name = max(fragile)
            others = [f"{x} of {y} in {t}" for x, y, t in sorted(fragile, reverse=True)[1:]]
            out.append({"id": "fragile",
                        "text": f"{n} of the {of} clusters in {name} hold peaks from {FEW_STUDIES} or fewer studies",
                        "detail": ("Also " + ", ".join(others) + ". " if others else "")
                                  + "Such a cluster can come or go with one selection decision.",
                        "go": {"view": "targets", "target": name}})
        self._checks = out
        return out

    def lane_counts(self, stage: str) -> List[dict]:
        out = []
        items = self.with_verdicts(stage)
        for lane, label, hint in LANES[stage]:
            rows = [it for it in items if it["lane"] == lane]
            if stage == "selection":
                reviewed = sum(1 for it in rows if it["reviewed"] >= it["n_cells"])
            else:
                reviewed = sum(1 for it in rows if it["verdict"])
            out.append({"id": lane, "label": label, "hint": hint, "size": len(rows), "reviewed": reviewed})
        return out

    # -------------------------------------------------------------- verdicts
    def record(self, body: dict) -> dict:
        stage, pmid = body.get("stage"), str(body.get("pmid") or "")
        if stage not in pilot.STAGES:
            raise ledger.LedgerError("unknown stage")
        analysis, target = body.get("analysis_id"), body.get("target")
        key = (pmid, analysis, target) if stage == "selection" else (pmid,)
        row = pilot.verdict_row(self.decisions[stage].get(key), stage, pmid, body.get("verdict"),
                                [str(c) for c in body.get("criteria") or []], (body.get("note") or "").strip() or None,
                                analysis, target, via="page")
        ledger.append_jsonl(pilot.feedback_path(self.rv), [row])
        self.verdicts[(stage, pmid, row["analysis_id"], row["target"])] = row
        return row

    def agree_rest(self, pmid: str) -> int:
        """Agree with every selection decision of a study the user has not judged yet."""
        rows = []
        for (p, aid, target), rec in self.decisions["selection"].items():
            if p == pmid and ("selection", p, aid, target) not in self.verdicts:
                rows.append(pilot.verdict_row(rec, "selection", p, "agree", [], None, aid, target, via="page"))
        ledger.append_jsonl(pilot.feedback_path(self.rv), rows)
        for row in rows:
            self.verdicts[("selection", pmid, row["analysis_id"], row["target"])] = row
        return len(rows)

    def note(self, body: dict) -> dict:
        stage, criterion = body.get("stage"), str(body.get("criterion") or "")
        if stage not in pilot.STAGES or criterion not in self.criterion_texts(stage):
            raise ledger.LedgerError("unknown stage or criterion")
        row = {"stage": stage, "criterion": criterion, "note": (body.get("note") or "").strip() or None,
               "criteria_hash": self.criteria[stage]["hash"], "recorded_at": ledger.now(), "via": "page"}
        ledger.append_jsonl(pilot.notes_path(self.rv), [row])
        return row

    def criterion_texts(self, stage: str) -> Dict[str, str]:
        if stage != "selection":
            return dict(self.criteria[stage]["criteria"])
        payload = self.criteria["selection"]["payload"]
        out = dict(payload["global"])
        for name, t in payload["targets"].items():
            for c, text in t["criteria"].items():
                out[f"{name}:{c}"] = text
        return out

    # -------------------------------------------------------------- findings
    def findings(self) -> dict:
        notes = pilot.criterion_notes(self.rv)
        out = {}
        for stage in pilot.STAGES:
            texts = self.criterion_texts(stage)
            rows = [(k, v) for k, v in self.verdicts.items() if k[0] == stage]
            lanes = []
            if stage == "selection":
                population = Counter("selected" if r["include"] else "not selected"
                                     for r in self.decisions["selection"].values())
                groups = Counter(("selected" if v["judged"] == "include" else "not selected", v["verdict"])
                                 for _, v in rows)
                for lane in ("selected", "not selected"):
                    lanes.append({"label": f"Analysis × target, {lane}", "size": population[lane],
                                  "reviewed": groups[(lane, "agree")] + groups[(lane, "disagree")],
                                  "agree": groups[(lane, "agree")]})
            else:
                lane_of = {it["pmid"]: it["lane"] for it in self.items(stage)}
                for lane, label, _ in LANES[stage]:
                    mine = [v for k, v in rows if lane_of.get(k[1]) == lane]
                    lanes.append({"label": label, "size": sum(1 for x in lane_of.values() if x == lane),
                                  "reviewed": len(mine), "agree": sum(v["verdict"] == "agree" for v in mine)})
            by_criterion: Dict[str, list] = defaultdict(list)
            for k, v in rows:
                if v["verdict"] != "disagree":
                    continue
                for c in v["criteria"] or ["unspecified"]:
                    # A target's own criterion is named with its target, since I1 differs between targets.
                    cid = f"{v['target']}:{c}" if stage == "selection" and not c.startswith("G") and c != "unspecified" else c
                    by_criterion[cid].append(dict(self._study(v["pmid"]), analysis_id=v.get("analysis_id"),
                                                  analysis=self._analysis_name(v["pmid"], v.get("analysis_id")),
                                                  target=v.get("target"), judged=v["judged"], note=v.get("note")))
            ids = sorted(set(by_criterion) | set(notes.get(stage, {})))
            reviewed = len(rows)
            agree = sum(v["verdict"] == "agree" for _, v in rows)
            out[stage] = {"decisions": len(self.decisions[stage]), "reviewed": reviewed, "agree": agree,
                          "disagree": reviewed - agree, "lanes": lanes,
                          "criteria": [{"id": c, "text": texts.get(c), "items": by_criterion.get(c, []),
                                        "note": notes.get(stage, {}).get(c)} for c in ids],
                          "all_criteria": texts}
        return out

    def findings_markdown(self) -> str:
        f, ov = self.findings(), self.overview()
        label = {"abstract": "Abstract screening", "fulltext": "Full-text screening", "selection": "Analysis selection"}
        out = [f"# Review findings: {ov['name']}", "",
               ("Pilot" if ov["pilot"] else "Complete run") + f", protocol v{ov['protocol']['version']}. "
               f"Written {ledger.now()} from the review page.", ""]
        for stage in pilot.STAGES:
            s = f[stage]
            if not s["reviewed"] and not s["criteria"]:
                continue
            out += [f"## {label[stage]}", "",
                    f"Reviewed {s['reviewed']} of {s['decisions']} decisions: {s['agree']} agreed, "
                    f"{s['disagree']} disagreed.", ""]
            out += [f"- {ln['label']}: reviewed {ln['reviewed']} of {ln['size']}, agreed with {ln['agree']}."
                    for ln in s["lanes"] if ln["size"]]
            for c in s["criteria"]:
                out += ["", f"### {c['id']}: {c['text'] or 'no criterion named'}", ""]
                if c["note"]:
                    out += [f"The user's note on this criterion: {c['note']}", ""]
                for it in c["items"]:
                    where = (f" ({it.get('analysis') or it['analysis_id']} × {it['target']})"
                             if it.get("analysis_id") else "")
                    out.append(f"- {it['pmid']}{where}, judged {it['judged']}: {it['note'] or 'no note'}. "
                               f"{it['title']}")
            out += ["", f"Changing these criteria is an amendment that re-opens all {s['decisions']} "
                        f"{stage} decisions.", ""]
        return "\n".join(out) + "\n"


def _verdict(row: Optional[dict]) -> Optional[dict]:
    return None if row is None else {k: row.get(k) for k in ("verdict", "criteria", "note", "via")}


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "cbma-review-page"
    timeout = 30                                    # a browser's idle preconnect must not hold a thread

    def log_message(self, *args) -> None:           # the review's contents never reach stderr
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _allowed(self, api: bool) -> bool:
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json({"error": "wrong host"}, 403)             # DNS rebinding
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Review-Token") or "", self.server.token):
            self._json({"error": "this page's session has ended; run review_page.py again"}, 403)
            return False
        return True

    def do_GET(self) -> None:
        self.server.last_request = time.time()
        path = self.path.split("?", 1)[0]
        name = "index.html" if path == "/" else path.lstrip("/")
        if name in ASSET_TYPES:
            if self._allowed(api=False):
                self._send(200, (ASSETS / name).read_bytes(), ASSET_TYPES[name])
            return
        if not path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._allowed(api=True):
            return
        snap: Snapshot = self.server.snapshot
        parts = path.split("/")[2:]
        try:
            with self.server.lock:
                if parts == ["overview"]:
                    self._json(snap.overview())
                elif parts[0] == "stage" and len(parts) == 2 and parts[1] in pilot.STAGES:
                    self._json({"items": snap.with_verdicts(parts[1]), "lanes": snap.lane_counts(parts[1]),
                                "criteria": snap.criterion_texts(parts[1]), "targets": snap.targets})
                elif parts[0] == "detail" and len(parts) == 3 and parts[1] == "fulltext":
                    self._json(snap.fulltext_detail(parts[2]))
                elif parts[0] == "detail" and len(parts) == 3 and parts[1] == "selection":
                    self._json(snap.selection_detail(parts[2]))
                elif parts[0] == "text" and len(parts) == 2:
                    self._json(snap.text(parts[1]))
                elif parts == ["targets"]:
                    self._json(snap.targets_view())
                elif parts == ["findings"]:
                    self._json({"stages": snap.findings(), "markdown": snap.findings_markdown()})
                else:
                    self._json({"error": "not found"}, 404)
        except ledger.LedgerError as exc:
            self._json({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        self.server.last_request = time.time()
        if not self._allowed(api=True):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if 0 < length < 100_000 else {}
            with self.server.lock:
                self._post(self.server.snapshot, body if isinstance(body, dict) else {})
        except (ledger.LedgerError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)

    def _post(self, snap: "Snapshot", body: dict) -> None:
        if self.path == "/api/verdict":
            self._json(_verdict(snap.record(body)))
        elif self.path == "/api/agree-rest":
            self._json({"recorded": snap.agree_rest(str(body.get("pmid") or ""))})
        elif self.path == "/api/criterion-note":
            self._json(snap.note(body))
        elif self.path == "/api/reload":
            snap.load()
            self._json({"reloaded": True})
        elif self.path == "/api/done":
            path = pilot.feedback_path(snap.rv).parent / "findings.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snap.findings_markdown())
            self.server.finished = True
            self._json({"findings": str(path)})
        else:
            self._json({"error": "not found"}, 404)


def make_server(review: Path, port: int = 0, seed: int = 0) -> HTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.lock = threading.Lock()
    server.snapshot = Snapshot(review, seed)
    server.token = secrets.token_urlsafe(24)
    server.finished = False
    server.last_request = time.time()
    return server


def report(snap: Snapshot, finished: bool) -> dict:
    """What the agent reads when the page closes: agreement per stage and the disagreements by
    criterion, without the page's contents."""
    f = snap.findings()
    return {"finished_by_user": finished,
            "stages": {s: {"decisions": v["decisions"], "reviewed": v["reviewed"], "agree": v["agree"],
                           "disagree": v["disagree"], "lanes": v["lanes"],
                           "disagreements_by_criterion": {c["id"]: {"text": c["text"], "note": c["note"],
                                                                     "items": [{k: it.get(k) for k in
                                                                                ("pmid", "analysis_id", "target",
                                                                                 "judged", "note")}
                                                                               for it in c["items"]]}
                                                          for c in v["criteria"]}}
                       for s, v in f.items()},
            "findings_file": str(pilot.feedback_path(snap.rv).parent / "findings.md") if finished else None}


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review_dir", type=Path)
    ap.add_argument("--port", type=int, default=0, help="default: any free port")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open a browser; print the full URL, token included, to open elsewhere")
    ap.add_argument("--idle-minutes", type=float, default=180, help="stop after this long without a request")
    ap.add_argument("--seed", type=int, default=0, help="the seed of each group's order (default 0)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        server = make_server(args.review_dir, args.port, args.seed)
    except ledger.LedgerError as exc:
        print(f"REVIEW PAGE ERROR: {exc}", file=sys.stderr)
        return 1
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    url = f"{base}#t={server.token}"
    opened = False if args.no_open else webbrowser.open(url)
    started = {"serving": base, "opened_browser": bool(opened),
               "note": "the user reviews in the page; this process prints the findings when they finish"}
    if not opened:
        # Nothing could open it here: the user, or the harness's own browser pane, needs the link.
        started["open_this"] = url
    print(json.dumps(started), flush=True)
    server.timeout = 1
    try:
        while not server.finished and time.time() - server.last_request < args.idle_minutes * 60:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(json.dumps(report(server.snapshot, server.finished), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
