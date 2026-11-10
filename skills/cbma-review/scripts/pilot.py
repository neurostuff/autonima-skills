"""Review a pilot's decisions with the user, and turn the disagreements into criteria edits.

    python pilot.py sheet   REVIEW --stage abstract|fulltext|selection [--excludes N] [--seed S]
    python pilot.py record  REVIEW --stage STAGE --pmid PMID [--analysis ID --target NAME]
                                   --verdict agree|disagree [--criterion ID ...] [--note "..."]
    python pilot.py summary REVIEW

A pilot judges a small set (`run_stage.py --limit N`) before the full run. Criteria rarely
survive first contact with real papers, and an amendment after a pilot re-opens only the
pilot's decisions, so this is where editing them is cheap.

`sheet` writes REVIEW/pilot/sheet_<stage>.md: every current decision of the stage, with its
reason, its criteria and, at full text, its quoted evidence. At the abstract stage it shows every
include and uncertain, and a random sample of excludes (10 by default): without known studies,
recall cannot be measured, so the excludes are checked for studies dropped wrongly.

`record` appends the user's verdict on one decision to REVIEW/pilot/feedback.jsonl, with the
criteria the user says were misapplied and their words. It never changes a decision: only the
judges and the ledger decide, and a disagreement becomes a protocol amendment.

`summary` counts agreement per stage and groups the disagreements by criterion, so the agent can
propose one edit per criterion. Feedback made under an earlier protocol version is reported as
stale: the decision it judged has been re-opened since.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402

STAGES = ("abstract", "fulltext", "selection")


def feedback_path(rv: "ledger.Review") -> Path:
    return rv.root / "pilot" / "feedback.jsonl"


def _states(criteria: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in (criteria or {}).items())


def _sample_excludes(rows: List[dict], n: int, seed: int) -> List[dict]:
    excludes = [r for r in rows if r["decision"] == "exclude"]
    if len(excludes) <= n:
        return excludes
    return sorted(random.Random(seed).sample(excludes, n), key=lambda r: int(r["pmid"]))


def cmd_sheet(rv: "ledger.Review", stage: str, excludes: int = 10, seed: int = 0) -> dict:
    dec = rv.valid_decisions(stage)
    out = [f"# Pilot review: {stage}", ""]
    crit = rv.criteria[stage]
    if stage == "selection":
        g = crit["payload"]["global"]
        out += ["Global criteria: " + "; ".join(f"{k}: {v}" for k, v in g.items()), ""]
        for name, t in crit["payload"]["targets"].items():
            out += [f"Target `{name}`: {t.get('description') or ''}. "
                    + "; ".join(f"{k}: {v}" for k, v in t["criteria"].items()), ""]
    else:
        out += ["Criteria: " + "; ".join(f"{k}: {v}" for k, v in crit["criteria"].items()), ""]
    shown = 0
    if stage == "selection":
        by_pmid = defaultdict(list)
        for (pmid, aid, target), r in dec.items():
            by_pmid[pmid].append(r)
        for pmid in sorted(by_pmid, key=int):
            a = rv.analyses(pmid) or {"analyses": []}
            names = {x["analysis_id"]: x.get("name") or x.get("table_label") for x in a["analyses"]}
            out += [f"## {pmid}: {rv.records.get(pmid, {}).get('title', '')}", ""]
            for r in sorted(by_pmid[pmid], key=lambda r: (r["analysis_id"], r["target"])):
                mark = "INCLUDE" if r["include"] else "exclude"
                out.append(f"- `{r['analysis_id']}` ({names.get(r['analysis_id'], '?')}) x `{r['target']}`: "
                           f"**{mark}**. {r.get('reason', '')}")
                shown += 1
            out.append("")
        counts = Counter("include" if r["include"] else "exclude" for r in dec.values())
    else:
        rows = sorted(dec.values(), key=lambda r: int(r["pmid"]))
        counts = Counter(r["decision"] for r in rows)
        if stage == "abstract":
            keep = [r for r in rows if r["decision"] != "exclude"]
            sampled = _sample_excludes(rows, excludes, seed)
            rows = sorted(keep + sampled, key=lambda r: int(r["pmid"]))
            out += [f"Showing every include and uncertain, and {len(sampled)} of {counts['exclude']} excludes "
                    f"(random sample, seed {seed}). Check the excludes for studies that belong.", ""]
        for r in rows:
            title = rv.records.get(r["pmid"], {}).get("title", "")
            out += [f"## {r['pmid']}: {title}", "",
                    f"**{r['decision']}**. {r.get('reason', '')}", "", f"Criteria: {_states(r.get('criteria'))}"]
            for e in r.get("evidence") or []:
                if isinstance(e, dict):
                    out.append(f"> {e.get('criterion')}: \"{e.get('quote')}\"")
            out.append("")
            shown += 1
    path = rv.root / "pilot" / f"sheet_{stage}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    return {"stage": stage, "decisions": sum(counts.values()), "by_decision": dict(counts), "shown": shown,
            "sheet": str(path)}


def _current(rv: "ledger.Review", stage: str, pmid: str, analysis: Optional[str], target: Optional[str]):
    key = (pmid, analysis, target) if stage == "selection" else (pmid,)
    return rv.valid_decisions(stage).get(key)


def cmd_record(rv: "ledger.Review", stage: str, pmid: str, verdict: str, criteria: List[str],
               note: Optional[str], analysis: Optional[str] = None, target: Optional[str] = None) -> dict:
    if stage == "selection" and not (analysis and target):
        raise ledger.LedgerError("a selection verdict names the --analysis and the --target")
    rec = _current(rv, stage, pmid, analysis, target)
    if rec is None:
        raise ledger.LedgerError(f"no current {stage} decision for {pmid}"
                                 + (f" {analysis} x {target}" if stage == "selection" else ""))
    known = set(rec.get("criteria") or {})
    unknown = [c for c in criteria if c not in known]
    if unknown:
        raise ledger.LedgerError(f"unknown criteria {unknown}; this decision has {sorted(known)}")
    if verdict == "disagree" and not (criteria or note):
        raise ledger.LedgerError("a disagreement needs --criterion or --note: what the judge got wrong")
    row = {"stage": stage, "pmid": pmid, "analysis_id": analysis, "target": target, "verdict": verdict,
           "criteria": criteria, "note": note,
           "judged": rec["decision"] if stage != "selection" else ("include" if rec["include"] else "exclude"),
           "criteria_hash": rec["criteria_hash"], "protocol_version": rec.get("protocol_version"),
           "recorded_at": ledger.now()}
    ledger.append_jsonl(feedback_path(rv), [row])
    return row


def _criteria_text(rv: "ledger.Review", stage: str) -> dict:
    if stage != "selection":
        return rv.criteria[stage]["criteria"]
    payload = rv.criteria["selection"]["payload"]
    return {"global": payload["global"], "targets": {n: t["criteria"] for n, t in payload["targets"].items()}}


def cmd_summary(rv: "ledger.Review") -> dict:
    latest = {}
    for r in ledger.read_jsonl(feedback_path(rv)):
        latest[(r["stage"], r["pmid"], r.get("analysis_id"), r.get("target"))] = r
    report = {}
    for stage in STAGES:
        current = rv.accepted_criteria_hashes(stage)
        rows = [r for k, r in latest.items() if k[0] == stage]
        live = [r for r in rows if r["criteria_hash"] in current]
        stale = [r for r in rows if r["criteria_hash"] not in current]
        if not rows:
            continue
        agree = sum(r["verdict"] == "agree" for r in live)
        by_criterion = defaultdict(list)
        for r in live:
            if r["verdict"] == "disagree":
                for c in r["criteria"] or ["unspecified"]:
                    by_criterion[c].append({k: r.get(k) for k in ("pmid", "analysis_id", "target", "judged", "note")})
        entry = {"reviewed": len(live), "agree": agree, "disagree": len(live) - agree,
                 "agreement": round(agree / len(live), 3) if live else None,
                 "disagreements_by_criterion": dict(sorted(by_criterion.items())),
                 "criteria_text": _criteria_text(rv, stage)}
        if stage == "abstract":
            entry["excludes_checked"] = sum(r["judged"] == "exclude" for r in live)
            entry["excludes_wrongly_dropped"] = sum(r["judged"] == "exclude" and r["verdict"] == "disagree"
                                                    for r in live)
        if stale:
            entry["stale"] = len(stale)
            entry["stale_note"] = "made under an earlier protocol version; those decisions were re-opened"
        report[stage] = entry
    return report


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("review_dir", type=Path)
    s.add_argument("--stage", choices=STAGES, required=True)
    s.add_argument("--excludes", type=int, default=10, help="abstract excludes to sample (default 10)")
    s.add_argument("--seed", type=int, default=0)
    r = sub.add_parser("record")
    r.add_argument("review_dir", type=Path)
    r.add_argument("--stage", choices=STAGES, required=True)
    r.add_argument("--pmid", required=True)
    r.add_argument("--analysis")
    r.add_argument("--target")
    r.add_argument("--verdict", choices=["agree", "disagree"], required=True)
    r.add_argument("--criterion", action="append", default=[], help="a criterion ID the judge misapplied")
    r.add_argument("--note")
    m = sub.add_parser("summary")
    m.add_argument("review_dir", type=Path)
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        rv = ledger.Review(args.review_dir)
        if args.cmd == "sheet":
            out = cmd_sheet(rv, args.stage, args.excludes, args.seed)
        elif args.cmd == "record":
            out = cmd_record(rv, args.stage, str(args.pmid), args.verdict, args.criterion, args.note,
                             args.analysis, args.target)
        else:
            out = cmd_summary(rv)
    except ledger.LedgerError as exc:
        print(f"PILOT ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
