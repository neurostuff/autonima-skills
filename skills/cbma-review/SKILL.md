---
name: cbma-review
description: Run a coordinate-based neuroimaging meta-analysis as a systematic review, from a PubMed query to NiMARE maps, with the agent itself doing the abstract screening, full-text screening, coordinate-table extraction and analysis selection. Use when the user asks to run, resume, pilot or check the status of a neuroimaging meta-analysis or systematic review (fMRI/PET activation coordinates, PRISMA, NiMADS, NiMARE), or points at a folder containing a review.yaml.
---

# CBMA review (orchestrator)

You are running a systematic review whose output is a coordinate-based
meta-analysis (CBMA). The judgments are made by fresh judge sessions of your own
harness, one per batch; small bundled scripts do the bookkeeping. No LLM API keys
are involved.

This skill is the entry point. It tells you the order of stages and the rules. Each
stage has its own skill with the detailed instructions:

| Stage | Skill | Who does it |
|---|---|---|
| 1. Search | `pubmed-search` (with `neurostore` first, when NeuroStore is the index) | scripts |
| 2. Abstract screening | `screen-studies` | judges, in batches |
| 3. Full-text retrieval | `fulltext-sources` | a script (PMC, then any local folders) |
| 4. Full-text screening | `screen-studies` | judges, in batches |
| 5. Coordinate extraction | `extract-coordinates`, or `neurostore` records | judges, one study per batch; or a script |
| 6. Analysis selection | `select-analyses` | judges, one study per batch |
| 7. Meta-analysis | `cbma-nimare` | a script (NiMARE) |

`SKILLS` below means the directory that holds these skill folders (the parent of
this file's directory). `REVIEW` means the review folder, the one with
`review.yaml`. `LEDGER` means `python SKILLS/cbma-review/scripts/ledger.py`.

## Non-negotiable rules

1. **The ledger is the only writer of decisions.** Judges' raw output is validated
   and recorded by `ledger.py ingest`. Never edit anything under `REVIEW/decisions/`
   or `REVIEW/analyses/` by hand, and never judge an item in your own context beyond
   a pilot of one or two.
2. **A failure is not a decision.** If a judge errors, times out, returns malformed
   output, or `ingest` rejects an item, that item stays pending and is retried. Never
   mark it excluded to make progress.
3. **Unavailable is not excluded.** A study with no retrievable full text is reported
   as unavailable. It is never counted as a full-text exclusion.
4. **Criteria come from the registered protocol only.** If screening shows a criterion
   is ambiguous, stop and propose the edit to the user. Do not reinterpret it silently.
   Once a change is registered, the ledger re-opens exactly the decisions it affects.
5. **Never amend the protocol yourself.** After the first judgment, a change to
   `review.yaml`, a stage skill or a judge setting is an amendment, and only the user
   can make one, in their own terminal (see Registration). Never run `amend`, never
   simulate a terminal for it, and never edit or delete `REVIEW/protocol/`.
6. **Record who judged.** The stage runner records every decision with its harness,
   model, effort and transport, for example
   `claude-code/claude-opus-5-5/effort-low/single-response-v2`. In the fallback,
   pass the same to `ingest --agent`, with your real identifiers.
7. **Keep state on disk, not in your context.** After any interruption or context
   compaction, run `LEDGER status REVIEW` and continue from what it reports.

## Setup

```bash
pip install pyyaml            # the only dependency until the meta-analysis stage
LEDGER init REVIEW
```

If `REVIEW/review.yaml` does not exist, write it with the user first. The format is
in `references/review-spec.md`, and `references/example-review.yaml` is a starting
point. `init` rejects unknown keys, so a typo fails loudly. Show the user the
numbered criteria that `init` prints before spending any effort on screening.

## Registration

Nothing is judged under an unregistered protocol. When the user confirms the numbered
criteria, register them:

```bash
LEDGER register REVIEW
```

A protocol version (`REVIEW/protocol/v001.json`, then `v002.json`, ...) records
`review.yaml`, the stage criteria and skills, and the judges' effort and settings, each
version chained to the one before by hash. Batching, ingesting and exporting refuse to
run while any of these differ from the latest version. The refusal says what to do:

- **Nothing judged yet:** revising the protocol is free. When the user confirms the
  change, run `LEDGER register REVIEW` again; it records the next version.
- **Anything judged, a pilot included:** the change is an amendment. Stop, show the
  user the change, and ask them to run, in their own terminal:

  ```bash
  python SKILLS/cbma-review/scripts/ledger.py amend REVIEW --reason "<why, in a sentence>"
  ```

  It shows them the difference and the decisions it will re-open, and asks them to
  confirm by typing. It refuses to run without a terminal, so it cannot run from your
  shell. Or revert the change.

`LEDGER status REVIEW` reports the version in force, its amendments and what each
version judged, and the export carries the same record.

## Running a judged stage

Every judged stage (abstract, fulltext, extraction, selection) is run by
`scripts/run_stage.py`. For each batch it starts a fresh, tool-free judge session of
the harness's CLI (`claude -p`, or `codex exec`), supplies the stage skill and the
complete inputs inline, and takes back one JSON response, which the ledger
validates. It then ingests, retries what was rejected, and prints one JSON summary.

**Claude Code: hand each stage to a stage runner.** If the workspace has
`.claude/agents/cbma-stage-runner.md` (`install.sh` puts it there), give each judged
stage to one `cbma-stage-runner` subagent, with this prompt, values filled in:

> Run the `<stage>` stage. REVIEW=`<abs path>` SKILLS=`<abs path>` PYTHON=`<python>`
> AGENT=`<harness>/<model id>`. [Batch size `<n>`.] [Pilot: `--limit <N>`.]

The runner runs the script, audits the stage, and returns a short report, so your
context grows by one report per stage.

**Codex, or no subagents: run the script yourself.**

```bash
python SKILLS/cbma-review/scripts/run_stage.py REVIEW --stage <stage> --model <model id> \
    --harness codex [--size <n>] [--limit N]
```

Then do the stage's audit (below) yourself.

**What the script's exit codes and summary mean:**
- **0:** nothing the stage can batch is pending.
- **3:** work remains. Run the same command again: each call stops at a time budget
  (9 minutes by default) so that it fits one shell command.
- **4:** another live process holds the stage, usually an earlier runner's script.
  Do not start a second loop; report the `stage_busy` record and stop.
- **1 with a `ledger_error` about the protocol:** the protocol is not registered, or
  has changed since. Follow Registration.
- **`usage_limited` true:** the provider refused a call (usage, session or rate
  limit). Dispatch stops; report it, and run again after the limit resets. A refused
  call does not use up an item's attempts.
- **`retry_exhausted`:** items that failed `--max-attempts` times (3 by default,
  counted across calls for unchanged inputs). They stay pending. This is an execution
  problem, never a scientific exclusion. Report them; raising `--max-attempts`
  explicitly permits more attempts.
- **`usage`:** the stage's cumulative token use, each attempt counted once. Usage a
  CLI did not report is counted as unknown, never as zero.

**Judges' effort** is the `effort:` line of each judge's definition: the workspace's
`.claude/agents/<judge>.md` if it has one, otherwise the package's `agents/`. Efforts
and the `judge_*.json` settings files in `.claude/agents` are part of the protocol.

| Stage | Judge | Effort | Batch size |
|---|---|---|---|
| abstract | `cbma-abstract-screener` | low | 25 |
| fulltext | `cbma-fulltext-screener` | medium | 2 |
| extraction | `cbma-extractor` | low | 1 |
| selection | `cbma-selector` | medium | 1 |

Changing an effort is a protocol change (see Registration). Every decision records
the effort it was made with.

**Pilots.** `--limit N` batches at most N items, and the pilot keeps that scope: its
retries never expand into unscreened records.

**Audit trail.** Every attempt keeps its inputs' hashes, the raw CLI output, the
response and what was saved, under `REVIEW/work/<stage>/judge_responses/`.

**End-to-end runs.** If the user says to run the review end to end, do every stage in
order without asking between them:
- search;
- abstract screening;
- retrieval;
- full-text screening;
- extraction;
- selection;
- export and meta-analysis.

Stop only when a stage runner reports PROBLEM, when a scripted stage fails, or at a
pilot the user asked for. Otherwise, after each stage:
- **verdict OK:** go on to the next stage if the user asked for an end-to-end run;
  otherwise report and ask.
- **verdict PROBLEM:** stop, show the user the report, and decide with them.

## Stage by stage

1. **Search:** follow `pubmed-search`. When it finishes, run `LEDGER status REVIEW` and
   report the record count. It must equal PubMed's reported count, and the script
   fails if it does not. **With `search.neurostore`,** first run the `neurostore`
   skill's `search`, which finds the candidates; `pubmed-search` then fetches their
   records. Report NeuroStore's count and any base studies without a PMID.
2. **Abstract screening:** run the `abstract` stage. Records without an abstract are
   screened on title and metadata; the `screen-studies` skill covers this.
3. **Full-text retrieval:** first run `LEDGER needs-fulltext REVIEW`, then follow
   `fulltext-sources`. Report available, incomplete and unavailable counts by source.
4. **Full-text screening:** run the `fulltext` stage. Items whose text is
   unavailable are never batched.
   **Combined mode** (`screening.fulltext.select_analyses: true`, records mode only):
   run `LEDGER import-analyses` *before* this stage (with `extraction.neurostore`,
   run the `neurostore` skill's `records` first). The full-text judges then decide
   screening and selection together, following the `screen-and-select` skill. An
   included study with no eligible analysis is recorded as excluded. The selection
   stage afterwards has nothing pending.
5. **Extraction:** run the `extraction` stage, one study per batch. Judges receive
   every parsed table (captions, headers and footnotes included) and the paper's
   Methods and coordinate-space paragraphs. A judge that reports `context_needed`
   gets the full paper once, within the same retry limit. In records mode
   (`extraction.records` or `extraction.neurostore` is set in `review.yaml`) there is
   nothing to judge: with `extraction.neurostore`, run the `neurostore` skill's
   `records`; then run `LEDGER import-analyses REVIEW` instead of the stage, and report
   its counts, including studies with no record, which stay pending.
6. **Selection:** run the `selection` stage. Judges receive the full article and the
   extracted analyses, and must decide every analysis × target pair. Skip the stage
   if `review.yaml` defines no targets.
7. **Export and meta-analysis:** `LEDGER export REVIEW`, then follow `cbma-nimare`.

## Audit checklist (between stages; it is part of the method)

A stage runner does these and reports them. Without one, do them yourself.

- **`LEDGER status REVIEW`: pending must be 0** before the next stage, unless
  the user agreed to leave items pending.
- **Abstract screening:** read 10 random excludes and 10 random includes from
  `decisions/abstract.jsonl`. If you disagree with more than one, show the user
  the cases before continuing.
- **Full-text screening:** quotes are checked against the text in three levels.
  `decisions_with_inexact_evidence` counts decisions whose quotes are close but not
  verbatim (ellipses, joined sentences, reformatted tables). That is informational:
  never stop, re-batch or re-judge for it. `decisions_with_ungrounded_evidence` counts
  decisions with a quote that has no close match in the paper. Open up to five of
  those, check the deciding criterion against the paper, and report what you find;
  stop only if a decision itself is wrong.
- **Extraction:**
  - `points_unverified` should be near 0. Unverified points are dropped at export.
  - Open two extracted studies and compare them with their tables by eye.
- **Selection:** confirm each target has enough studies for a CBMA (usually at least
  17 for ALE and 10 as an absolute floor). Say so if a target falls short.

## Finishing

Report the final `results/prisma.json` counts to the user. Name every stage where
items remain pending or unavailable, and list the files under `results/`.

## Layout of REVIEW (for orientation; the scripts create it)

```
review.yaml                 the protocol (criteria, sources, targets)
protocol/v001.json ...      registered protocol versions (append-only; the ledger writes these)
search/records.jsonl        one record per PMID; search/search_log.json
fulltext/index.jsonl        retrieval outcome per PMID; fulltext/raw/ cached downloads
docs/<pmid>/                normalized text.md, tables/<id>.json, meta.json
work/<stage>/batch_*.json   batches waiting for judges; judge_responses/ per attempt
decisions/<stage>.jsonl     accepted decisions (append-only; the ledger writes these)
analyses/<pmid>.json        accepted, verified coordinate extractions
results/                    prisma.json, criteria.json, nimads/, meta/
```

## Appendix: fallback without a judge CLI

Use this only when neither `claude` nor `codex` can be run from a shell. Judges then
read their inputs from files, which costs more tokens than the script's inline inputs.

1. **Make batches.** `LEDGER batches REVIEW --stage <stage> --size <n>`. Only pending
   items are batched, so re-running this after an ingest picks up exactly the
   retries. Add `--limit N` for a pilot.
2. **Dispatch one subagent per batch file,** at most about 8 at a time, with exactly
   this prompt, paths filled in (`<skill>` is the batch's `skill` field):

   > Read `SKILLS/<skill>/SKILL.md` and follow it exactly. Your batch file is
   > `<batch path>`. Process every item in it and write your output to the path in
   > the batch's `output` field. Do not read or write any other part of the review
   > folder except the files the batch names. When done, reply with only the number
   > of items you wrote.

   Without in-session subagents, `scripts/run_batches.sh` runs each batch in a fresh
   headless session of any agent CLI that can read and write files.
3. **Ingest.** `LEDGER ingest REVIEW --stage <stage> --agent "<harness>/<model>/effort-<level>"`.
   It prints accepted and rejected counts and every rejection reason; exit code 2
   means at least one item was rejected.
4. **Retry** from step 1. If the same item fails twice for the same reason, read the
   rejection and the item yourself. If the problem is the input (a broken table, a
   garbled text), tell the user and leave the item pending.
5. **Audit** with the checklist above.
