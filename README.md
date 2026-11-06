# autonima-skills

Run a coordinate-based neuroimaging meta-analysis as a systematic review, from a
PubMed query to NiMARE maps, inside the coding agent you already use (Claude Code or
Codex). The agent's own judge sessions make every judgment:
- abstract screening;
- full-text screening;
- splitting coordinate tables into analyses;
- selecting analyses for each target.

No LLM client and no API key: judges run on your Claude or Codex subscription. Small
standard-library scripts do the bookkeeping that an agent should not do from memory:
- searching PubMed completely;
- normalizing full texts;
- validating and recording every decision;
- checking extracted coordinates against their tables;
- exporting NiMADS.

This is the skills-based implementation of
[autonima](https://github.com/neurostuff/autonima). It does not import autonima. Its
benchmark against NeuroMetaBench, and autonima's own runs, are in
[autonima-results](https://github.com/neurostuff/autonima-results).

## Install

The skills are installed as a set, because the orchestrator refers to the others by
relative path. From the folder where you will keep your reviews (the workspace):

```bash
git clone https://github.com/neurostuff/autonima-skills
autonima-skills/install.sh .
pip install pyyaml            # every stage; add nimare for the meta-analysis stage
```

`install.sh` does three things:
- **Skills:** links them into `.claude/skills` (Claude Code) and `.agents/skills` (Codex).
- **Agent definitions:** copies them into `.claude/agents`.
- **Lock:** records every installed file in `.autonima-skills.lock`. `ledger.py status`
  compares against it and names any skill, script or judge setting edited since.

**Network:** outbound access to `eutils.ncbi.nlm.nih.gov` for search and PMC, and to
`neurostore.org` when a review uses NeuroStore.

## Use

In the agent, from the workspace:

> Run the cbma-review skill on ./wm_schizophrenia. Pilot abstract screening on 50 records first.

The `cbma-review` skill walks the stages. If `review.yaml` does not exist yet, it writes
it with you first; `skills/cbma-review/references/review-spec.md` is the format.

Once you confirm the criteria, the agent registers them as protocol v1. From the first
judgment on, the agent cannot change them. You amend them yourself, in a terminal, with
a reason:

```bash
python autonima-skills/skills/cbma-review/scripts/ledger.py amend REVIEW --reason "..."
```

Every version is kept, and the results carry the whole record.

Every judged stage runs through `skills/cbma-review/scripts/run_stage.py`:
- **One judge per batch:** a fresh, tool-free session of your harness's CLI, `claude -p`
  or `codex exec`.
- **Inline inputs:** the stage skill and the batch's complete inputs go in, and one
  JSON response comes back.
- **Then the ledger:** it validates the response and records accepted decisions.
- **Retries:** rejected items stay pending and are retried.

In Claude Code, a `cbma-stage-runner` subagent runs each stage and returns a short
report, so the main session stays small. In Codex, the orchestrator runs the script
with `--harness codex`.

| Skill | Role |
|---|---|
| [`cbma-review`](skills/cbma-review/SKILL.md) | orchestrator: stage order, rules, audit checklist; holds the ledger and the stage runner |
| [`pubmed-search`](skills/pubmed-search/SKILL.md) | complete, de-duplicated PubMed search via E-utilities |
| [`neurostore`](skills/neurostore/SKILL.md) | NeuroStore as the search index and as the source of extracted coordinates |
| [`fulltext-sources`](skills/fulltext-sources/SKILL.md) | PMC open access plus any local folders of pre-downloaded articles |
| [`screen-studies`](skills/screen-studies/SKILL.md) | abstract and full-text screening against numbered criteria |
| [`screen-and-select`](skills/screen-and-select/SKILL.md) | full-text screening and analysis selection in one pass (records mode) |
| [`extract-coordinates`](skills/extract-coordinates/SKILL.md) | table → analyses → points, using autonima's analysis-boundary rules |
| [`select-analyses`](skills/select-analyses/SKILL.md) | analysis × target inclusion decisions |
| [`cbma-nimare`](skills/cbma-nimare/SKILL.md) | NiMADS export and NiMARE meta-analysis |

| Agent | Role | Effort |
|---|---|---|
| [`cbma-stage-runner`](agents/cbma-stage-runner.md) | runs one judged stage and reports | medium |
| [`cbma-abstract-screener`](agents/cbma-abstract-screener.md) | abstract judges | low |
| [`cbma-fulltext-screener`](agents/cbma-fulltext-screener.md) | full-text judges | medium |
| [`cbma-extractor`](agents/cbma-extractor.md) | extraction judges | low |
| [`cbma-selector`](agents/cbma-selector.md) | selection judges | medium |

Every decision records the harness, model, effort and transport that made it, for
example `claude-code/claude-opus-5-5/effort-low/single-response-v2`.

## What the scripts enforce

These are the lessons from autonima's issue tracker and code review, made
mechanical:

| Rule | Autonima failure it prevents |
|---|---|
| esearch windows split past 9,999; retrieved must equal reported | silent truncation at 10k (#53) |
| every abstract section, full titles, the article's own DOI, string PMIDs | truncated abstracts and titles, wrong DOIs, integer PMIDs giving 0 studies (#56) |
| failures are never decisions; rejected output stays pending and is retried | failed screenings cached forever |
| unavailable ≠ excluded, reported separately in PRISMA | retrieval failures counted as exclusions |
| a missing selection decision is never exported as `false` | missing annotations exported as "exclude" |
| every extracted xyz must appear in its table row, or the point is dropped | sign errors and invented foci reaching the meta-analysis |
| duplicate tables flagged at normalization | double-weighted studies (#60) |
| reference lists removed from full text | bibliography sent to the model (#67) |
| unknown `review.yaml` keys are errors | typos silently ignored (#58) |
| criteria IDs numbered per stage; decisions keyed on hashes of criteria, skill text and input | renumbered IDs reusing stale decisions; hand-bumped prompt versions |
| full-text evidence quotes checked against the text | ungrounded reasons (#38) |
| installed files locked; status names any later edit | skills and judge settings changed mid-review without a record |
| nothing is judged under an unregistered or silently changed protocol; after the first judgment only the user, at a terminal, can amend it, with a reason | criteria revised after seeing results, with no record of when or why |

Issue numbers are [autonima's](https://github.com/neurostuff/autonima/issues).

## Tests

```bash
pip install pytest pyyaml
pytest tests
```

The tests run offline against fixtures and fake judge CLIs. They cover:
- PubMed parsing, date-window splitting and retries;
- JATS and HTML normalization;
- point verification;
- the stage runner on both harnesses;
- the package lock;
- one review through every stage.

## Status

Under active development. Next steps:
- **Lower token use per stage.**
- **Criteria drafting:** a skill that helps you write `review.yaml`.
- **A review page** for checking decisions.
- **Neurosynth Compose mode:** NeuroStore search and coordinates work today (the
  `neurostore` skill); next is reading Compose projects and pushing results back.
