---
name: neurostore
description: Use NeuroStore (neurostore.org, the index behind Neurosynth Compose) as a review's search index and as the source of each study's activation coordinates, instead of searching all of PubMed and extracting coordinates from tables with judges. Use when review.yaml sets search.neurostore or extraction.neurostore, when the user wants to search NeuroStore or use its extracted coordinates, or to run the search or records step of such a review.
---

# NeuroStore: search index and coordinates

NeuroStore indexes neuroimaging studies with the coordinates already extracted from
them. A review can use it in two places, separately or together:

| Setting in `review.yaml` | Replaces | Script |
|---|---|---|
| `search.neurostore` | the PubMed query | `neurostore.py search REVIEW` |
| `extraction.neurostore` | judged coordinate extraction | `neurostore.py records REVIEW` |

Full text is still needed for full-text screening and selection, because NeuroStore does
not share it: retrieval is unchanged (`fulltext-sources`, including local folders).

```bash
python SKILLS/neurostore/scripts/neurostore.py search  REVIEW
python SKILLS/neurostore/scripts/neurostore.py records REVIEW
```

Both scripts need network access to `neurostore.org`, unless the setting names a
snapshot.

## Search

```yaml
search:
  query: '("emotion regulation"[tiab] OR reappraisal*[tiab]) AND fMRI[tiab]'   # PubMed, translated
  date_from: 2000/01/01          # NeuroStore filters by year
  date_to: 2019/12/31
  neurostore:
    # query: '...'               # instead of search.query: a query in NeuroStore's own syntax
    search_full_text: false      # true: match the article text too, not only title and abstract
    data_type: coordinate        # coordinate (default) | image | both | null for every study
    snapshot: null               # an earlier search log to reuse instead of searching
```

Give one query, not both:

- **`search.query`, a PubMed query.** The script translates it exactly as autonima does
  (`scripts/translate.py` is autonima's translator, unchanged), so both send NeuroStore
  the same query for the same review. What PubMed expresses and NeuroStore cannot is
  reported, never dropped silently:
  - **text tags** (`[tiab]`) are removed;
  - **MeSH** inside an `OR` becomes the heading as a phrase; MeSH `AND`-ed on
    (`AND Humans[mh]`) is dropped;
  - **publication type, language and date tags** are dropped (dates come from
    `date_from` and `date_to`);
  - **truncation** (`reactiv*`) becomes the prefix with common endings.

  `search` prints the query it sent, and what was `dropped` and `approximated`. Show
  them to the user.
- **`search.neurostore.query`, NeuroStore's own syntax.** It is sent as written:
  - whole words after stemming (`regulation` also finds `regulating`);
  - quoted phrases, `AND`, `OR`, `NOT`, `-` and parentheses;
  - no field tags, no MeSH and no truncation: list the word forms you need.

**`NOT` in a full-text search.** NeuroStore applies `NOT` to every field it searches,
so `NOT review` would drop every paper whose text mentions a review. For a translated
PubMed query, the script keeps the top-level `NOT` clauses to titles and abstracts, as
PubMed scoped them: it searches the rest in full text and removes what a
title-and-abstract search of each `NOT` clause finds. A native query is sent as
written, so keep `NOT` out of a native full-text query.

`search.pmids` and `pmids_file` still add seed studies.

**Then** run `pubmed-search` as usual. It reads `search/neurostore_pmids.txt` instead
of running a PubMed query, and fetches each candidate's PubMed record, so the later
stages see the same records a PubMed search would give them. Its log tags these
records `found_by: neurostore`.

**What `search` guarantees:**
- **No partial result sets:** the total NeuroStore reports must hold while paging, and
  the count retrieved must equal it, or the script fails.
- **A full log:** `search/neurostore_search_log.json` records the query and filters, the
  total, and the base studies without a PMID, which the review cannot carry because it
  is keyed on PMIDs.

## Coordinates (records)

```yaml
extraction:
  neurostore:                    # {} for the defaults below
    sources: [study_schema, nuextract-v21, llm, neurostore, pubget, nuextract-v19, neuroquery, neurosynth]
    roles: [result]
    snapshot: null               # a frozen analyses snapshot to read instead of the API
```

Run `records` after abstract screening; it covers every study that passed it. Then run
`ledger.py import-analyses REVIEW` in place of the extraction stage. In combined mode,
do both after retrieval and before full-text screening.

**Which analyses each study gets:**
- **One version per study:** a NeuroStore study has one version per extraction source.
  It uses the first source, in `sources` order, that has coordinates, so a study never
  mixes two extractions.
- **Results only:** it keeps the analyses whose role is in `roles`. NeuroStore marks a
  paper's own findings `result`; seed regions (`anchor`), coordinates cited from other
  work (`reference`) and region definitions (`localization`) are left out. An analysis
  with no role counts as a result.
- **Studies with no usable analyses** are listed in `neurostore/unavailable.csv`, with
  the reason:
  - `not_in_neurostore`;
  - `no_coordinates`;
  - `no_coordinates_from_configured_sources`;
  - `no_kept_analyses`: the configured sources had coordinates, but not in a kept role;
  - `not_in_snapshot`.

  They stay pending at extraction, and are reported.

**Frozen once fetched.** NeuroStore updates its parses in place, so what each study used
is kept in `neurostore/analyses.json` and reused on later runs. Changing `sources` or
`roles` fetches again. The imported analyses keep NeuroStore's analysis IDs
(`<pmid>-<NeuroStore analysis id>`), and their points are marked `source`, since there
is no table to verify them against.

## Snapshots, for comparisons

A comparison must use exactly the inputs of the run it is compared with.
- **`search.neurostore.snapshot`:** an earlier `neurostore_search_log.json` (this
  script's). It must record the same query and filters, or the script refuses.
- **`extraction.neurostore.snapshot`:** an earlier `neurostore/analyses.json`, or
  autonima's `outputs/neurostore_analyses.json`. Nothing is fetched; a study missing
  from it is `not_in_snapshot`.

Either way the settings are part of `review.yaml`, so the registered protocol records
which snapshot a review used.
