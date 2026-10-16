# Manually Test Free-Form Text/PDF Inbox Processing

How to exercise free-form text/PDF interpretation in an AI CLI/IDE without persisting anything, and how to keep good results as local regression fixtures.

Behavior, output schema, and stop conditions are defined in the business logic skill: `.agents/skills/fm-inbox-freeform-processor/SKILL.md`. This procedure is the operator-side glue.

## Before you start

- Use a local file under `data/`. Do not copy or rename it; do not require non-private samples.
- Your AI CLI/IDE sends file contents to its model provider; use one you trust with medical data.
- The executing agent needs at least C2 capability per `docs/references/model-capability-matrix.md`.

## Choose a test file

Provide an exact path, filename, glob, or filename substring under `data/`, e.g.:

```text
data/tmp/visit-summary.pdf
data/inbox/shared/2024-01-15.pdf
data/inbox/240115 VisitSummary.xml
data/manual-testing/report.txt
sample CBC W:AUTO DIFFERENTIAL
*CBC*AUTO*DIFFERENTIAL*.pdf
```

The skill resolves the identifier under `data/`. If it matches no files or multiple plausible files, the skill stops and reports candidates rather than guessing. Use a structured inbox path like `data/inbox/shared/` only when testing inbox routing or source lifecycle behavior.

## Run manual verification in the AI CLI/IDE

Prompt:

```text
Use fm-inbox-freeform-processor on <path, filename, glob, or substring>. Produce only the structured manual verification output. Include all identified data, fact confidence, and overall interpretation evidence. Do not persist anything. Do not include intermediate chatter.
```

Review the AI CLI/IDE output before any persistence workflow accepts it.

## Deterministic Visit Review Notes

C-CDA/HL7 v3 XML visit summaries, extractable visit PDFs, and recognized history/imaging PDFs have a review-note path in the inbox orchestrator. When the source patient name and birth year match exactly one existing person note, execute mode retains the source under `data/processed/<person_id>/History/` and writes a linked `history` note with `verification_status: needs_review`. For mixed C-CDA/IHE XML, deterministic structured lab, medication, vital-sign, and immunization sections are also fanned out into their canonical stores from that same retained XML source path. Immunizations persist into dedicated `Records/<person_id>/Immunizations/` notes plus `Records/<person_id>/Reports/immunizations.md`, and successful canonical persistence removes any legacy immunization placeholder for that source. The history note remains the review surface for narrative, diagnoses, procedures, and follow-up context. TIF/TIFF history images are routed explicitly but require OCR/parser support before a review note can be generated.

If person matching is ambiguous or missing, the file remains a human-resolution case or is quarantined in non-interactive execution.

## Record fixture feedback

When an output is good enough to keep as a regression example, record it in your own `data/manual-testing/freeform-inbox-fixtures.yaml` (private and untracked; the repository ships no real fixtures). Validate fixtures with:

```bash
python scripts/check_freeform_fixtures.py data/manual-testing/freeform-inbox-fixtures.yaml
```

The checker verifies source resolution, candidate facts include confidence and evidence, lab facts include units and reference values, medication facts include `medication_type` when AI-assisted extraction is used, vitals are split into separate measurement facts, and non-high-confidence outcomes carry a manual verification request with concrete user options.

If validation fails because a retained source path no longer exists, update the local fixture entry under `data/manual-testing/` to the current retained source path before treating the audit as failed. The fixture file is private local state, not a tracked repository artifact.

To audit the current inbox/quarantine backlog without persisting medical data, add `--audit-current-inbox`:

```bash
python scripts/check_freeform_fixtures.py data/manual-testing/freeform-inbox-fixtures.yaml --audit-current-inbox
```

The audit prints counts by outcome, one proposed outcome per current source, fixture coverage gaps by file family, and legacy quarantine files needing re-evaluation.

To find repeated low-confidence or unknown-confidence patterns from recorded
inbox-run feedback, run:

```bash
python scripts/query_inbox_feedback.py
```

The summary includes fixture-gap candidates when multiple unresolved records
share a category and source family. Use those candidates to choose the next
manual fixture examples to record under `data/manual-testing/`.

To emit the unresolved low-confidence or unknown-confidence files from one run
as a re-evaluation batch, run:

```bash
python scripts/query_inbox_feedback.py --run-id <run_id> --reprocess-list
```

This prints source paths only; it does not reprocess files or write canonical
medical data. To act on that list, ask the AI CLI/IDE to run `fm-ingest-inbox`
as a targeted-subset run naming exactly those paths; it reprocesses only the
listed files through the normal proposal → verification → persistence path,
the same as any other run.

## Tuning feedback to retain per fixture

- file name, document type, expected person/category
- accepted / edited (with corrected value) / rejected (with reason) candidates
- missing facts; bundled vitals that should have been separate measurements
- missing or incorrect reference intervals/values
- calculated or population-qualified results extracted incorrectly
- unsafe hallucinations or unsupported inferences
- quarantine reason when applicable

## Next step after verification

Only after manual verification examples pass do accepted candidates enter the normal pipeline:

```text
Clinical Interpreter proposal → Canonicalizer → Deduplication Engine → Human Resolver if needed → Data Steward
```

The Data Steward is the only role that may write vault notes, DB rows, processed source paths, quarantine paths, or audit events.
