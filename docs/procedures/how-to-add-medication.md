# Add Medications

Use this procedure when you have a prescription list, supplement list, medication portal export, or medication CSV that belongs in the vault.

## Recommended path

1. Put the source file in the person's inbox:

   ```text
   data/inbox/<person_id>/
   ```

   Examples:

   ```text
   data/inbox/dummy_alice/current-meds.txt
   data/inbox/dummy_alice/portal-medications.pdf
   data/inbox/dummy_alice/pharmacy-export.csv
   ```

   Legacy category subfolders such as `data/inbox/<person_id>/medications/` still route,
   but the preferred layout is a single per-person drop folder with automatic category inference.

2. Invoke the inbox skill:

   ```text
   Use fm-ingest-inbox to process my inbox.
   ```

3. Review any question the skill asks. It should only ask when the source is ambiguous, low confidence, conflicting, or invalid.
   When two retained sources disagree about dose or frequency, the skill shows
   both source-stated candidates and their source paths. You may choose one,
   provide a value, or defer; the agent does not recommend a medical value.

## What the pipeline does

- Free-form prescription text/list files use AI-assisted fact extraction, then deterministic validation before persistence.
- Supplement text lists use deterministic parsing when possible.
- Medication CSV is accepted when it is already an export/support format; do not create CSV by hand just to satisfy the tool.
- Each accepted retained source gets a labeled source-evidence note under
  `Records/<person_id>/Medications/`. It preserves what that source reported;
  use the linked Medication index to review evidence.
- Accepted rows refresh `Records/<person_id>/Reports/current-medications.md` and `Records/<person_id>/Reports/medication-conflicts.md`.
- `current-medications.md` is the deduplicated, authority-aware current view;
  source-evidence notes are not a substitute for that report.

## Safety outcomes

- Person mismatch, invalid dates, invalid schema, or low-confidence medication type inference quarantines the source before persistence.
- Existing user corrections remain authoritative during re-import.
- A cross-source dose/frequency contradiction does not quarantine the whole
  document. Both source facts remain available, `medication-conflicts.md` stays
  visibly pending, and an attended inbox run routes any confirmed resolution
  through `fm-correct-fact`.
- Source files move to `data/processed/<person_id>/Medications/` after successful persistence.

## Manual fallback

If the source cannot be interpreted yet, keep the original source in the inbox or quarantine and ask for parser/fixture refinement. Do not rewrite the source into a structured format unless it is already available as an export.
