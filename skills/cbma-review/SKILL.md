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
`review.yaml`. `PYTHON` means the workspace's interpreter (see Setup and planning), and
`LEDGER` means `PYTHON SKILLS/cbma-review/scripts/ledger.py`.

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
   `claude-code/claude-sonnet-5-5/effort-low/single-response-v2`. In the fallback,
   pass the same to `ingest --agent`, with your real identifiers.
7. **Keep state on disk, not in your context.** After any interruption or context
   compaction, run `LEDGER status REVIEW` and continue from what it reports.
8. **Never edit the package.** Change nothing under `SKILLS/` or `.claude/agents/`,
   with any tool, shell commands included. They are links into the autonima-skills
   package, so an edit changes every review that uses it; `install.sh` also denies
   these edits. When a script fails, stop, quote the error to the user, and say it
   needs a fix in the package. Do not work around it with a patched copy.
9. **No improvised analysis code.** Do not write scripts that change what is judged,
   selected, merged or fitted. If the skills cannot do what the user wants, say so and
   stop: the change belongs in the package, where it is tested and registered.
   Read-only checks, such as counting, comparing or reading a paper, are fine.
10. **One decision per question.** See "Asking the user" below.

## Asking the user

There are two kinds of question, and they are asked differently.

**Planning: few questions, then a draft.** Before `review.yaml` exists, ask only the
questions that define the review:
- **the route:** where the studies, their evidence and their coordinates come from (step 1
  below);
- **the judges' model** (step 2);
- **the review question and population, the targets, and any neighbouring scope** (step 3).

That is at most about eight questions. Use AskUserQuestion, which takes up to four
related questions per call. Everything else goes into the draft with its recommended
default. Show those defaults as one short list the user can change in a single reply.
Don't ask about each one. The list always states:
- **the publication years:** "all years" unless the user named a range;
- **the coordinate source:** NeuroStore's analyses on the NeuroStore route;
- **the text the full-text judges read:** the full text (trimmed is an option, below);
- **full-text search:** off, with the record count it would give (NeuroStore route);
- **the estimator and the corrector;**
- **the pilot:** recommended (step 7).

Other choices, such as cue modality, age limits or imaging methods, go in the same list
when they apply.

**Decisions during the review: one per question.** A problem a stage runner raises, a
change to the protocol, or anything that changes which studies, analyses or maps
result:
- **One decision per question.** In Claude Code, ask with AskUserQuestion; otherwise
  ask one numbered question per message. Never put several such decisions in one
  message to be answered in a line.
- **Each option states its scope in numbers:** how many studies and analyses it
  touches. It also says whether it is recorded in the protocol (an amendment) or not,
  and names any conflict with the protocol's own instructions.
- **Recommendation first,** with the reason.
- **Say it back before acting** when the chosen option reaches beyond the problem
  raised. Give one line on what will happen, for example "this merges the analyses of
  9 studies, including 2 with separate patient and control groups", and wait for a
  yes.
- **Settled defaults are not decisions.** Don't ask about them. For example, points
  with no stated coordinate space are fitted as MNI (`cbma-nimare`); report the
  count instead.

## Setup and planning

**Python.** Run every script with the workspace's interpreter, `PYTHON` below: the
`python` named in the workspace's `.autonima-skills.lock`. `install.sh` builds that
environment (`.venv`) from the package's lockfile. Never install packages into the
system Python, and never run `pip install` without asking the user.

If `REVIEW/review.yaml` exists, run `LEDGER init REVIEW` and continue from
`LEDGER status REVIEW`. Otherwise plan the review with the user, in this order:

1. **The route.** Ask one question: which route the review takes. The route sets where
   studies are found, what screening reads, where coordinates come from, and most of the
   cost, so it comes first.
   - **NeuroStore route (recommended).** NeuroStore finds the studies
     (`search.neurostore`), and its pre-extracted analyses supply the coordinates
     (`extraction.neurostore`), so there is no extraction stage to pay for. On the
     benchmark's queries NeuroStore returned about 2.7 times fewer records than PubMed,
     with recall of the benchmark's included studies of 0.71 against PubMed's 0.85, and
     its candidates mostly have coordinates already.
   - **PubMed route.** PubMed finds the studies; every candidate's full text is fetched;
     judges screen it and extract the coordinates from its tables. It covers the whole
     literature, and it depends on how much full text the user can get. Always offer it;
     never hide it.
   - **Hybrids,** offered as variants in the same question, not as questions of their own:
     - **PubMed search with NeuroStore's coordinates where a study has them:**
       `extraction.neurostore` with `fallback: judged`. Judges extract only the studies
       NeuroStore has no analyses for. It can reach studies a NeuroStore search cannot:
       ones NeuroStore does not index, or indexes without coordinates. In the
       cue-reactivity pilot those were 13 of the 38 benchmark studies the NeuroStore
       search missed; whether a PubMed search finds them depends on its query, and has
       not been measured.
     - **NeuroStore search with judged extraction from the papers' tables:** NeuroStore
       search and no `extraction.neurostore`.
   - **Don't ask about the coordinate source on the NeuroStore route.** It is NeuroStore's
     analyses; state it in the draft's defaults.
   - **Full text** (`fulltext.sources`). On every route, full-text screening and selection
     read each paper's text, so ask once whether the user has folders of downloaded
     publisher HTML or XML to add after PMC open access.
     - **Say plainly what PMC alone covers,** which is often a minority of candidates. In
       the emotion regulation pilot, PMC gave usable text for 148 of 298 candidates. Of
       the 64 benchmark-included studies the search found, 45 had no usable PMC text.
     - A study with no text is reported as unavailable, never excluded.
2. **The judges' model.** Ask which model the judges use, and recommend
   `claude-sonnet-5-5` for every stage in Claude Code (`run_stage.py`'s default).
   - **Never default to your own model.** A judge reads one batch and returns one JSON
     response; a larger model mostly adds cost.
   - **Keep the answer for the whole review.** It is the model in `AGENT` for every
     stage. Decisions record their model, so a review that switches midway mixes
     judges.
   - **With Codex,** ask for a model id; there is no default.
3. **Scope:** the question, the population, and the meta-analytic targets.
   - **Ask once about neighbouring scope.** When an adjacent paradigm shares the design of
     the one the user named, ask whether it belongs, as its own target, pooled, or out of
     scope. For example, "drug cue reactivity" neighbours natural-reward cues (food,
     sexual), which use the same cue > neutral design. This question is cheap in
     planning and costly after registration.
   - **Give examples for each target:** one or two concrete contrasts it would include,
     and one near-miss it would not, named explicitly. For a natural-reward cue target, a
     near-miss is the monetary incentive delay task's anticipation cues: a cue paradigm
     by some definitions, but not a depiction of the reward. Users correct definitions
     more reliably on examples than on words.
   - Settle the remaining choices as defaults in the draft (see Asking the user).
4. **Draft `review.yaml`.** The format is in `references/review-spec.md`, and
   `references/example-review.yaml` is a starting point.
   - **Dates:** no `date_from` or `date_to` unless the user asked for a range. Most
     reviews want every study; a cutoff matters mainly when reproducing or updating a
     published review. The draft's defaults say "all years".
   - **On the NeuroStore route, leave imaging-method terms out of the query:** no fMRI,
     PET, MRI or neuroimaging clause. Tell the user why: NeuroStore indexes neuroimaging
     studies only, and the coordinate filter already requires coordinates, so such a
     clause mostly drops studies whose abstract names the method differently ("BOLD",
     "magnetic resonance imaging") or not at all. The query carries only the topic's
     terms. A PubMed query keeps its imaging clause.
   - **Full-text search** (`search.neurostore.search_full_text: true`) is an option, not
     the default. Run `PYTHON SKILLS/neurostore/scripts/neurostore.py count REVIEW` and
     state its cost in records. In the cue-reactivity pilot it gave 4 times the records,
     and found 3% more of the benchmark's studies than a query without the imaging clause.
   - **Text view:** full-text judges read the full text (`screening.fulltext.text: full`,
     the default). The trimmed view (`text: trimmed`) is an option that roughly halves
     what they read. Its effect on screening accuracy has not been measured, so offer it
     only as a cost saving, and never present it as the default.
   - Run `LEDGER init REVIEW`. It rejects unknown keys, so a typo fails loudly.
   - Run `LEDGER check REVIEW`. Fix what it reports before going on, for example a
     missing package, a judge CLI that is not logged in (the user runs `claude auth
     login` in a terminal), or imaging terms in a NeuroStore query.
5. **Search.** Run the search (stage 1 below), and report the count.
   - **Known studies are optional.** Say, in the same message as the count: "If you know
     studies that belong in this review, give me their PMIDs and I'll check the search
     finds them." If the user has none, move on; the pilot (step 7) is the default check.
   - **If they give some,** run:

     ```bash
     PYTHON SKILLS/pubmed-search/scripts/check_known.py REVIEW --pmids <pmid> ...
     ```

     For each study the search missed, it gives the reason and the study's title,
     keywords and MeSH terms. Show the misses, and for each propose the query change that
     would catch it, or say why none is reasonable, for example when NeuroStore does not
     index the study. Re-run the search on the user's answer: nothing is judged yet, so
     revising is free.
   - **Use studies the user names.** Do not take the full included list of a published
     review they are trying to reproduce: that puts the answer into the search.
6. **Show the numbered criteria** that `init` prints, and register them
   (Registration, below).
7. **Recommend a pilot,** and run it unless the user declines. Give the reason: criteria
   rarely survive first contact with real papers, and an amendment after a pilot re-opens
   only the pilot's decisions, so editing then is cheap. See "Pilot" below.

## Pilot

A pilot runs every stage on a small sample, then reviews the decisions with the user.

1. **Run the stages with `--limit N --random`:** a seeded random sample, not the lowest
   PMIDs, which are the oldest papers. The same seed gives the same sample, so after an
   amendment the pilot re-judges the same studies.
   - about 40 abstracts (`--limit 40 --random`);
   - retrieval for the ones that pass;
   - about 8 full texts (`--limit 8 --random`);
   - extraction, or the NeuroStore records and `import-analyses`;
   - selection for the included studies.
2. **Open the review page.** The user reads and judges the decisions there, not in chat.

   ```bash
   PYTHON SKILLS/cbma-review/scripts/review_page.py REVIEW
   ```

   - **Run it in the background.** It serves one page on this machine, opens it in the
     user's browser, and prints one JSON line. Then tell the user the page is open, and
     wait: do not ask about decisions in chat meanwhile.
   - **If `opened_browser` is false,** no browser could be opened from here. Give the
     user the `open_this` link, or open it in your harness's own browser pane.
   - **What the user does there.** They read each decision with its evidence: the full
     abstract, the judge's quotes in their paragraphs, the tables, the analyses each
     target collects. They mark agree or disagree, name the criterion that was
     misapplied, and can note what a criterion should say. See "The review page" below.
   - **A verdict never changes a decision.**
   - **When the user clicks "Finish review",** the process exits and prints the findings
     as JSON: agreement per stage and group, and the disagreements and notes by
     criterion. They are also in `REVIEW/pilot/findings.md`.
3. **Turn the findings into edits.** Propose one edit per criterion the user disagreed
   on or noted, in the criterion's own words, starting from their note when they left
   one. Show which pilot decisions each edit would change.
   - **Read a paper yourself** only to draft an edit, never to overrule a judge.
   - **No browser at all** (a remote shell): fall back to `pilot.py sheet`, `record` and
     `summary`, which do the same in files and chat.
4. **Amend, or continue.**
   - **The user has edits:** the pilot judged items, so this is an amendment. Give them
     the `amend` command (Registration, below) with the edits in `review.yaml`, then
     re-run the pilot's re-opened decisions and review those again.
   - **No edits:** the full run continues under the same protocol version, and the
     pilot's decisions stand.

## The review page

`review_page.py` is how the user checks the judges, in a pilot and after a complete run.
It is the same page for both. Its tabs follow the pipeline, and within a tab the
decisions that are final, or that silently drop a study, come first.

| Tab | What it shows | What the user is checking |
|---|---|---|
| Overview | one tile per stage with what it kept and dropped; which criterion excluded how many studies; what each target collected and which criteria kept analyses out of it | that the losses are the ones they intended |
| Abstracts | uncertain decisions, then excludes, then includes | studies dropped wrongly: an abstract exclude is final, an include gets a second look |
| Full texts | decisions whose quote was not found, then excludes, then includes, then incomplete texts | exclusions, with the quote each rests on shown in the paper |
| Analyses | included studies from which nothing was selected, then the rest; a table of each analysis × target, with the reason in the row when an analysis is out of every target | studies that silently contribute to no map |
| Targets | one target at a time: the analyses it selected, by name and study; why the others were left out; the map's clusters | that each map is built from the contrasts they meant |
| Findings | agreement per group; disagreements and notes by criterion | what to change |

- **Pilot:** the sample is small, so the user can read everything.
- **Complete run:** each group lists a random sample first (a seeded shuffle), so the
  share the user agrees with estimates the whole group. They can filter by the
  criterion that decided, and ask for more of any group.
- **The page reads the review; it writes only `REVIEW/pilot/`:** the verdicts
  (`feedback.jsonl`, marked `via: page`), notes on criteria, and `findings.md`.
- **It stops** when the user finishes, or after three hours without a request. If the
  judges run a stage while it is open, the user selects Reload.
- **Do not open the page's link yourself to enter verdicts.** The verdicts are the
  user's. To record something they told you in chat, use `pilot.py record`, which marks
  it `via: agent`.

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
  user the change, and ask them to run, in their own terminal, with the real paths
  filled in so they can paste it:

  ```bash
  PYTHON SKILLS/cbma-review/scripts/ledger.py amend REVIEW --reason "<why, in a sentence>"
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
> AGENT=`<harness>/<model id>`. [Batch size `<n>`.] [Pilot: `--limit <N> --random`.]

`<model id>` is the judges' model chosen in planning: `claude-sonnet-5-5` unless the
user chose another. It is never your own model by default.

The runner runs the script, audits the stage, and returns a short report, so your
context grows by one report per stage.

**Codex, or no subagents: run the script yourself.**

```bash
PYTHON SKILLS/cbma-review/scripts/run_stage.py REVIEW --stage <stage> --model <model id> \
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
retries never expand into unscreened records. With `--random` (and `--seed`, 0 by default)
the N are a random sample of the pending items instead of the lowest PMIDs.

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
   (`extraction.records` or `extraction.neurostore` is set in `review.yaml`) the records
   are the extractor: with `extraction.neurostore`, run the `neurostore` skill's
   `records`; then run `LEDGER import-analyses REVIEW` instead of the stage, and report
   its counts, including studies with no record.
   - **With `extraction.neurostore.fallback: judged`** (a hybrid), then run the `extraction`
     stage: judges extract exactly the studies NeuroStore had no analyses for.
   - **Without it,** those studies stay pending, and are reported.
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

**Then offer the review page** (see "The review page"), and say what it is for: checking
what each map was built from, and auditing a sample of the judges' decisions.
- **Point the user at the Overview's three "What decided" blocks, and at Targets.** A
  criterion that excluded far more than they expected, or a target that collected the
  wrong contrasts, matters more than any single decision.
- **Report the audit with its denominators:** for each group, how many decisions it
  has, how many the user reviewed, and how many they agreed with. A sample of 10
  excludes says little about 400; say so.
- **A disagreement is not a correction.** If the user wants a decision changed, the way
  is a criterion edit: an amendment, which re-opens every decision of that stage. Give
  them that count before they choose. If they choose not to amend, the disagreements
  stay in the record as the audit's result.

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
pilot/                      the user's review: feedback.jsonl, criterion_notes.jsonl, findings.md
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
