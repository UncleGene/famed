# How to Delete an Erroneous Record Safely

## Goal

Remove a bad imported fact without bypassing provenance, audit, or report refresh rules.

## Current supported safe-delete paths

### Imported fact came from a retained source file

1. Edit or replace the retained source under:

   ```text
   data/processed/<person_id>/<Category>/
   ```

2. Ask your AI CLI:

   ```text
   Use fm-reprocess-source on data/processed/<person_id>/<Category>/<filename>.
   ```

3. The system re-parses the retained source, replaces the rows derived from that source, and refreshes affected reports.

### Wrong user correction should be removed

Ask your AI CLI:

```text
Use fm-correct-fact to revoke my correction on dummy_alice's Glucose on 2024-01-15.
```

This restores source authority instead of deleting rows blindly.

## What not to do

- Do not delete rows directly from `data/famed.db`.
- Do not delete generated vault notes as a substitute for correcting the authoritative source or correction row.
- Do not treat report edits as data deletion.

## Notes

There is no separate end-user delete skill today. Safe deletion currently means reprocessing the authoritative retained source or revoking a user correction so the canonical state can be rebuilt deterministically.
