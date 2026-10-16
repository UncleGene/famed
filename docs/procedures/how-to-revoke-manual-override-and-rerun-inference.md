# How to Revoke a Manual Override and Re-Run Inference

## Goal

Remove a `user_correction` override so source authority applies again, then refresh the downstream outputs.

## Revoke the override

Ask your AI CLI:

```text
Use fm-correct-fact to revoke my correction on dummy_alice's Glucose on 2024-01-15 because the original source was correct.
```

The skill should confirm the existing correction before revoking it.

## Re-run source-derived state when needed

If you also edited the retained source file or want the source-derived rows rebuilt immediately, follow with:

```text
Use fm-reprocess-source on data/processed/<person_id>/<Category>/<filename>.
```

## Notes

- Revocation restores source authority; it does not make generated reports authoritative on its own.
- If the original source remains wrong, do not revoke the correction unless you are also fixing the source.
