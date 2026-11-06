"""NeuroStore as a review's search index and its source of coordinates. Standard library plus PyYAML.

    python neurostore.py search  REVIEW    # search.neurostore     -> search/neurostore_pmids.txt
    python neurostore.py records REVIEW    # extraction.neurostore -> neurostore/records/<pmid>.analyses.json

`search` runs the review's NeuroStore query (titles and abstracts, or full text), checks that
every result was retrieved, and writes the PMIDs. The query is search.neurostore.query as written,
or the review's PubMed query (search.query) translated exactly as autonima translates it
(translate.py). In a full-text search, a translated query's top-level NOT clauses are kept to
titles and abstracts, as PubMed scoped them. The PubMed script then fetches their records,
so every later stage reads the same records as a PubMed search would give it. With
`search.neurostore.snapshot`, the PMIDs come from an earlier search's log instead, which must
have run the same query: a frozen search for comparisons.

`records` takes, for each study that passed abstract screening, the analyses of one NeuroStore
version: the first, in `sources` order, that has coordinates, keeping analyses whose role is in
`roles`. They are written in the records format that `ledger.py import-analyses` reads. What each
study used is frozen in neurostore/analyses.json and reused on later runs, because NeuroStore
updates its parses in place. With `extraction.neurostore.snapshot`, records come from a frozen
snapshot (this script's, or autonima's outputs/neurostore_analyses.json) and nothing is fetched.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import translate  # noqa: E402

API = "https://neurostore.org/api"
# Compose's current parse first, then earlier extractions by how closely they recovered
# NeuroMetaBench's expert-curated peaks, then the database sources (as in autonima).
SOURCES = ("study_schema", "nuextract-v21", "llm", "neurostore", "pubget", "nuextract-v19", "neuroquery",
           "neurosynth")
ROLES = ("result", "anchor", "localization", "reference", "other")
VALUE_KINDS = {"z": "z-statistic", "t": "t-statistic", "f": "f-statistic", "r": "correlation", "p": "p-value",
               "beta": "beta"}
SNAPSHOT_SCHEMA = 1


class NeuroStoreError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Client:
    """GET-only, with retries on server errors and rate limits. A 404 is None, not an error."""

    def __init__(self, base_url: str = API, timeout: float = 60, retries: int = 4, delay: float = 2.0):
        self.base_url, self.timeout, self.retries, self.delay = base_url.rstrip("/"), timeout, retries, delay
        self.n_requests = 0

    def _open(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "autonima-skills"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        last = None
        for attempt in range(self.retries):
            self.n_requests += 1
            try:
                return json.loads(self._open(url))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code < 500 and exc.code != 429:
                    raise NeuroStoreError(f"GET {url} returned {exc.code}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            if attempt + 1 < self.retries:
                time.sleep(self.delay * (attempt + 1))
        raise NeuroStoreError(f"GET {url} failed after {self.retries} attempts: {last}")

    def search(self, query: str, full_text: bool, data_type: Optional[str], year_min: Optional[int],
               year_max: Optional[int], page_size: int = 1000) -> tuple:
        """Every matching base study, and the total NeuroStore reported. Fails rather than return a
        partial set: the total must hold while paging, and the count retrieved must equal it."""
        params = {"search": query, "search_full_text": str(bool(full_text)).lower(), "flat": "true",
                  "page_size": page_size}
        params.update({k: v for k, v in (("data_type", data_type), ("year_min", year_min),
                                         ("year_max", year_max)) if v is not None})
        results, total, page = [], None, 1
        while True:
            data = self.get("base-studies/", {**params, "page": page})
            if data is None:
                raise NeuroStoreError("the base-studies search returned 404")
            reported = (data.get("metadata") or {}).get("total_count")
            if total is None:
                total = reported
            elif reported != total:
                raise NeuroStoreError(f"the total changed while paging ({total} -> {reported}); search again")
            batch = data.get("results") or []
            results += batch
            if not batch or len(results) >= (total or 0):
                break
            page += 1
        if total is not None and len(results) != total:
            raise NeuroStoreError(f"retrieved {len(results)} base studies but NeuroStore reported {total}")
        return results, total


# --------------------------------------------------------------------------- #
# review.yaml
# --------------------------------------------------------------------------- #

def load_spec(review: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise SystemExit("neurostore.py needs PyYAML: pip install pyyaml") from exc
    return yaml.safe_load((review / "review.yaml").read_text()) or {}


def _resolve(review: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else review / path


def _year(date: Optional[str]) -> Optional[int]:
    return int(str(date)[:4]) if date else None


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def resolve_query(spec: dict) -> Tuple[str, Optional["translate.Translation"]]:
    """The NeuroStore query to send, and the translation it came from, if any: search.neurostore.query
    as written, or search.query, a PubMed query, translated."""
    search = spec.get("search") or {}
    cfg = search.get("neurostore") or {}
    if cfg.get("query"):
        return cfg["query"], None
    if not search.get("query"):
        raise NeuroStoreError("search.neurostore needs a query: search.query (PubMed, translated) or "
                              "search.neurostore.query (NeuroStore's syntax)")
    try:
        translation = translate.translate_pubmed_query(search["query"])
    except translate.QueryTranslationError as exc:
        raise NeuroStoreError(f"search.query cannot be translated for NeuroStore: {exc}") from exc
    return translation.query, translation


def search_params(spec: dict, query: str) -> dict:
    """What defines the search; a snapshot must have been made with the same."""
    search = spec.get("search") or {}
    cfg = search.get("neurostore") or {}
    return {"query": query, "search_full_text": bool(cfg.get("search_full_text", False)),
            "data_type": cfg.get("data_type", "coordinate"), "year_min": _year(search.get("date_from")),
            "year_max": _year(search.get("date_to"))}


def cmd_search(review: Path, client: Optional[Client] = None) -> dict:
    spec = load_spec(review)
    if "neurostore" not in (spec.get("search") or {}):
        raise NeuroStoreError("review.yaml sets no search.neurostore")
    cfg = spec["search"]["neurostore"] or {}
    query, translation = resolve_query(spec)
    params = search_params(spec, query)
    translated = None if translation is None else {
        "pubmed_query": spec["search"]["query"], "dropped": translation.dropped,
        "approximated": translation.approximated, "include": translation.include, "exclude": translation.exclude}
    if cfg.get("snapshot"):
        source = _resolve(review, cfg["snapshot"])
        frozen = json.loads(source.read_text())
        if frozen.get("params") != params:
            raise NeuroStoreError(f"{source} was searched with {frozen.get('params')}, not this review's "
                                  f"{params}; use a snapshot of the same search")
        log = dict(frozen, snapshot={"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                     "searched_at": frozen.get("searched_at")}, loaded_at=now())
    else:
        client = client or Client(cfg.get("base_url", API))
        filters = (params["data_type"], params["year_min"], params["year_max"])
        if params["search_full_text"] and translation is not None and translation.exclude:
            # NeuroStore applies NOT to every field it searches, so PubMed's `NOT review[tiab]` sent as
            # written would drop every paper whose text mentions a review. Search the rest in full
            # text, and remove what a title-and-abstract search of each NOT clause finds.
            results, total = client.search(translation.include, True, *filters)
            excluded = set()
            for clause in translation.exclude:
                excluded |= {r.get("id") for r in client.search(clause, False, *filters)[0]}
            translated.update(exclude_scope="title_abstract", matched_before_exclusion=len(results),
                              removed_by_exclusion=sum(r.get("id") in excluded for r in results))
            results = [r for r in results if r.get("id") not in excluded]
        else:
            results, total = client.search(params["query"], params["search_full_text"], *filters)
        pmids = sorted({str(r["pmid"]) for r in results if r.get("pmid")}, key=int)
        log = {"params": params, "translation": translated, "base_url": client.base_url, "searched_at": now(),
               "reported_total": total, "base_studies": len(results), "pmids": pmids,
               # The rest of the review is keyed on PMIDs, so these cannot be carried.
               "without_pmid": [{"id": r.get("id"), "name": r.get("name"), "doi": r.get("doi")}
                                for r in results if not r.get("pmid")],
               "requests": client.n_requests}
    out = review / "search"
    out.mkdir(parents=True, exist_ok=True)
    (out / "neurostore_search_log.json").write_text(json.dumps(log, indent=1))
    (out / "neurostore_pmids.txt").write_text("".join(f"{p}\n" for p in log["pmids"]))
    report = {"pmids": len(log["pmids"]), "without_pmid": len(log.get("without_pmid", [])),
              "frozen": bool(cfg.get("snapshot")), "written": str(out / "neurostore_pmids.txt")}
    if translation is not None:
        # The agent shows these to the user: what the PubMed query lost on the way to NeuroStore.
        report.update(neurostore_query=query, dropped=translation.dropped, approximated=translation.approximated)
    return report


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #

def record_options(spec: dict) -> dict:
    cfg = (spec.get("extraction") or {}).get("neurostore") or {}
    return {"sources": list(cfg.get("sources") or SOURCES), "roles": list(cfg.get("roles") or ["result"]),
            "snapshot": cfg.get("snapshot")}


def _space(value) -> Optional[str]:
    value = str(value or "").upper()
    return value if value in ("MNI", "TAL") else None


def record_from_version(pmid: str, base_study: dict, version: dict, roles: List[str]) -> dict:
    """One study's record: the version used and its analyses with a kept role and points.
    An analysis with no role predates roles and counts as a result."""
    analyses = []
    for a in sorted(version.get("analyses") or [], key=lambda a: (a.get("order") is None, a.get("order") or 0)):
        if (a.get("role") or "result") not in roles:
            continue
        points = [{"coordinates": [float(c) for c in p["coordinates"]], "space": _space(p.get("space")),
                   "values": [{"value": v.get("value"),
                               "kind": VALUE_KINDS.get(str(v.get("kind") or "").lower(), "other")}
                              for v in p.get("values") or [] if v.get("kind")]}
                  for p in a.get("points") or []
                  if isinstance(p.get("coordinates"), list) and len(p["coordinates"]) == 3
                  and all(isinstance(c, (int, float)) for c in p["coordinates"])]
        if not points:
            continue
        meta = a.get("metadata") if isinstance(a.get("metadata"), dict) else {}
        table = a.get("table_id") if a.get("table_id") not in (None, "None") else None
        analyses.append({"neurostore_id": a.get("id"), "name": a.get("name"), "description": a.get("description"),
                         "role": a.get("role"), "table_id": table,
                         "table_label": str(meta["table_id"]) if meta.get("table_id") else None, "points": points})
    return {"pmid": str(pmid), "base_study_id": base_study.get("id"), "version_id": version.get("id"),
            "source": version.get("source"), "version_updated_at": version.get("updated_at") or version.get("created_at"),
            "fetched_at": now(), "analyses": analyses}


def fetch_record(client: Client, pmid: str, sources: List[str], roles: List[str]) -> dict:
    """One study's analyses from the live API, or a record of why there are none."""
    listing = client.get("base-studies/", {"pmid": pmid, "info": "true"}) or {}
    matches = [r for r in listing.get("results") or [] if str(r.get("pmid")) == pmid]
    if not matches:
        return {"pmid": pmid, "unavailable": "not_in_neurostore", "fetched_at": now()}
    base = matches[0]
    versions: Dict[str, List[dict]] = {}
    for v in base.get("versions") or []:
        if isinstance(v, dict) and v.get("has_coordinates"):
            versions.setdefault(v.get("source"), []).append(v)
    tried = False
    for source in sources:
        # Several versions from one source: the most recently updated is its current one.
        for listed in sorted(versions.get(source, []), key=lambda v: v.get("updated_at") or "", reverse=True):
            version = client.get(f"studies/{listed['id']}", {"nested": "true"})
            if version:
                tried = True
                record = record_from_version(pmid, base, version, roles)
                if record["analyses"]:
                    return record
    # no_kept_analyses: a configured source has coordinates, but no analysis with a kept role has points.
    reason = ("no_kept_analyses" if tried else "no_coordinates_from_configured_sources" if versions
              else "no_coordinates")
    return {"pmid": pmid, "base_study_id": base.get("id"), "fetched_at": now(), "unavailable": reason,
            "sources_seen": sorted(s for s in versions if s)}


def to_records_file(record: dict) -> dict:
    """The records format `ledger.py import-analyses` reads. The key is NeuroStore's analysis id, so
    every imported analysis can be traced, and later pushed, back to it."""
    return {"neurostore": {k: record.get(k) for k in ("base_study_id", "version_id", "source", "version_updated_at")},
            "analyses": [{"key": a.get("neurostore_id") or f"a{i + 1}", "name": a.get("name"),
                          "description": a.get("description"),
                          "table_id": a.get("table_label") or a.get("table_id"), "points": a["points"]}
                         for i, a in enumerate(record.get("analyses") or [])]}


def needed_pmids(review: Path) -> List[str]:
    """Studies that passed abstract screening: the ones full-text screening and extraction reach."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cbma-review" / "scripts"))
    import ledger  # noqa: E402
    return ledger.Review(review).abstract_passed()


def cmd_records(review: Path, client: Optional[Client] = None, pmids: Optional[Iterable[str]] = None,
                workers: int = 8) -> dict:
    spec = load_spec(review)
    if (spec.get("extraction") or {}).get("neurostore") is None:
        raise NeuroStoreError("review.yaml sets no extraction.neurostore")
    opts = record_options(spec)
    pmids = [str(p) for p in (pmids if pmids is not None else needed_pmids(review))]
    folder = review / "neurostore"
    snap_path = folder / "analyses.json"
    options = {"sources": opts["sources"], "roles": opts["roles"], "snapshot": opts["snapshot"]}
    studies: Dict[str, dict] = {}
    if snap_path.exists():
        data = json.loads(snap_path.read_text())
        if data.get("options") == options:
            studies = data.get("studies") or {}        # frozen: what this review already used
    missing = [p for p in pmids if p not in studies]
    if opts["snapshot"]:
        frozen = json.loads(_resolve(review, opts["snapshot"]).read_text())
        frozen_studies = frozen.get("studies") or {}
        for p in missing:
            studies[p] = frozen_studies.get(p) or {"pmid": p, "unavailable": "not_in_snapshot"}
    elif missing:
        client = client or Client()
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_record, client, p, opts["sources"], opts["roles"]): p for p in missing}
            failed = []
            for fut in cf.as_completed(futures):
                try:
                    studies[futures[fut]] = fut.result()
                except NeuroStoreError as exc:
                    failed.append(f"{futures[fut]}: {exc}")    # not frozen: fetched again next run
        if failed:
            print(f"WARNING: {len(failed)} studies could not be fetched and stay without records; run again. "
                  f"First: {failed[0]}", file=sys.stderr)
    folder.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps({"schema": SNAPSHOT_SCHEMA, "options": options, "studies": studies}, indent=1))
    records = folder / "records"
    records.mkdir(exist_ok=True)
    by_source, unavailable = Counter(), Counter()
    with (folder / "unavailable.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["pmid", "reason", "sources_seen"])
        for p in pmids:
            rec = studies.get(p)
            target = records / f"{p}.analyses.json"
            if rec and rec.get("analyses"):
                target.write_text(json.dumps(to_records_file(rec), indent=1))
                by_source[rec.get("source")] += 1
            else:
                target.unlink(missing_ok=True)
                reason = (rec or {}).get("unavailable", "fetch_failed")
                unavailable[reason] += 1
                writer.writerow([p, reason, ";".join((rec or {}).get("sources_seen") or [])])
    return {"studies": len(pmids), "with_analyses": sum(by_source.values()), "by_source": dict(by_source),
            "unavailable": dict(unavailable), "frozen_from": opts["snapshot"], "records": str(records)}


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["search", "records"])
    ap.add_argument("review_dir", type=Path)
    args = ap.parse_args(list(argv) if argv is not None else None)
    review = args.review_dir.resolve()
    try:
        report = cmd_search(review) if args.command == "search" else cmd_records(review)
    except NeuroStoreError as exc:
        print(f"NEUROSTORE ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
