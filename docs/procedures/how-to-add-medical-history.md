# How to Add Medical History

## Goal

Add visit summaries, retained narrative history, and structured history facts to one person's record without writing canonical truth directly.

## Recommended path

1. Put the source file in the person's inbox:

   ```text
   data/inbox/<person_id>/
   ```

   Supported sources include visit summaries, C-CDA/HL7 v3 XML, extractable PDFs, and recognized history images.

2. In your AI CLI in this repo, ask:

   ```text
   Use fm-ingest-inbox to process my inbox.
   ```

3. Review any questions the skill asks. Ambiguous person matches, low-confidence interpretation, and unsupported sections stop for human resolution instead of persisting silently.

4. After the retained history note lands in the vault, ask:

   ```text
   Use fm-history-extract for <person_id>.
   ```

5. Review the proposed structured facts. Accepted facts refresh
   `Records/<person_id>/Reports/medical-history-summary.md` and, when the
   person's FHIR surface exists, `Records/<person_id>/Reports/fhir-export.json`.

## What the pipeline does

- Retains the original source under `data/processed/<person_id>/History/`.
- Writes a linked review note under `famed-vault/Records/<person_id>/History/`.
- Keeps `verification_status: needs_review` until structured facts are accepted.
- Fans out mixed C-CDA/IHE sections into their canonical stores when supported, including labs, medications, vitals, and immunizations.

## Manual fallback

- If the source cannot yet be interpreted safely, leave it in inbox/quarantine and refine the parser or fixture coverage.
- Do not rewrite a raw history source into canonical facts by hand as a substitute for the review flow.
- If a separately reviewed remediation changes the path of an accepted History
  note, regenerate the History index, medical-history summary, and FHIR export,
  then validate the export before treating the remediation as complete.

## Related procedures

- `docs/procedures/manually-test-freeform-inbox-processing.md`
- `docs/procedures/how-to-recalculate-after-data-correction.md`
