"""Check a drafted search against studies the user already knows belong in the review.

    python check_known.py REVIEW --pmids 12345678 23456789 ... [--file known.txt]

Run it after the search and before registering, while revising the query is still free. For
each known study the search did not find, it says why, and gives the study's title, keywords
and MeSH terms so you can propose the term that would catch it:

- not_in_pubmed:            the PMID has no PubMed record;
- outside_dates:            published outside search.date_from / date_to;
- query_does_not_match:     the PubMed query does not match the study's PubMed record;
- not_in_neurostore:        (NeuroStore search) NeuroStore does not index the study at all;
- neurostore_no_coordinates: (NeuroStore search, data_type: coordinate) indexed, but without
                            coordinates, so the coordinate filter drops it;
- neurostore_query_does_not_match: (NeuroStore search) indexed with coordinates, but the
                            NeuroStore query does not match its title or abstract.

With a NeuroStore search it also reports whether the PubMed form of the query matches, which
separates a query problem from an index problem.

Known studies are a handful the user knows from their own reading, five to ten. They are not a
published review's full list of included studies: the check tunes the query on them, so in a
benchmark that would put the answer into the search.

Writes REVIEW/search/known_check.json and prints it. The pilot judges these studies first, and the
review page reports where each one ended up, so give the whole list each time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "neurostore" / "scripts"))
import pubmed_search  # noqa: E402


def _year(value) -> Optional[int]:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


def found_pmids(review: Path) -> set:
    """What the search found: every record it wrote, plus candidates PubMed had no record for."""
    found = set()
    records = review / "search" / "records.jsonl"
    if records.exists():
        found |= {str(json.loads(line)["pmid"]) for line in records.read_text().splitlines() if line.strip()}
    log = review / "search" / "search_log.json"
    if log.exists():
        found |= {str(p) for p in json.loads(log.read_text()).get("missing_from_efetch") or []}
    return found


def run(review: Path, known: List[str], pm_client=None, ns_client=None) -> dict:
    spec = pubmed_search.load_search_spec(review)
    known = sorted({pubmed_search.normalize_pmid(p) for p in known}, key=int)
    found = found_pmids(review)
    if not found:
        raise SystemExit("no search results yet: run the search first")
    missing = [p for p in known if p not in found]
    report = {"known": len(known), "found": [p for p in known if p in found], "missed": [],
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if not missing:
        return report

    pm_client = pm_client or pubmed_search.Client(email=os.environ.get("NCBI_EMAIL"),
                                                  api_key=os.environ.get("NCBI_API_KEY"))
    records, not_found = pubmed_search.fetch_records(pm_client, missing)
    by_pmid = {r["pmid"]: r for r in records}
    query = spec.get("query")
    matches = set()
    if query and by_pmid:
        term = f"({query}) AND (" + " OR ".join(f"{p}[uid]" for p in by_pmid) + ")"
        _, ids = pubmed_search._esearch(pm_client, term, None, None, retmax=len(by_pmid))
        matches = set(ids)

    ns_spec = spec.get("neurostore")
    if ns_spec is not None and ns_client is None:
        import neurostore  # noqa: E402
        ns_client = neurostore.Client(**({"base_url": ns_spec["base_url"]} if ns_spec.get("base_url") else {}))
    lo, hi = _year(spec.get("date_from")), _year(spec.get("date_to"))

    for pmid in missing:
        rec = by_pmid.get(pmid)
        row = {"pmid": pmid}
        if rec is None:
            report["missed"].append({**row, "reason": "not_in_pubmed"})
            continue
        row.update({k: rec.get(k) for k in ("title", "year", "journal", "keywords", "mesh")})
        if query:
            row["pubmed_query_matches"] = pmid in matches
        year = rec.get("year")
        if year and ((lo and year < lo) or (hi and year > hi)):
            row["reason"] = "outside_dates"
        elif ns_spec is not None:
            listing = ns_client.get("base-studies/", {"pmid": pmid, "info": "true"}) or {}
            bases = [b for b in listing.get("results") or [] if str(b.get("pmid")) == pmid]
            with_coords = any(isinstance(v, dict) and v.get("has_coordinates")
                              for b in bases for v in b.get("versions") or [])
            if not bases:
                row["reason"] = "not_in_neurostore"
            elif not with_coords and ns_spec.get("data_type", "coordinate") == "coordinate":
                row["reason"] = "neurostore_no_coordinates"
            else:
                row["reason"] = "neurostore_query_does_not_match"
        else:
            row["reason"] = "query_does_not_match" if pmid not in matches else "unexplained"
        report["missed"].append(row)
    report["not_in_pubmed"] = not_found
    return report


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review_dir", type=Path)
    ap.add_argument("--pmids", nargs="*", default=[])
    ap.add_argument("--file", type=Path, help="one PMID per line")
    args = ap.parse_args(list(argv) if argv is not None else None)
    known = list(args.pmids) + (pubmed_search.read_pmids(args.file) if args.file else [])
    if not known:
        ap.error("give the known studies with --pmids or --file")
    report = run(args.review_dir, known)
    out = args.review_dir / "search" / "known_check.json"
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    print(f"{len(report['found'])} of {report['known']} known studies found; wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
