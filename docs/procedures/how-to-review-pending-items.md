# How to Review Pending Items

## Goal

See what currently needs a caregiver decision without treating old audit events
as an active queue.

## Recommended path

Ask your AI CLI:

```text
Use fm-review-pending to show what currently needs review.
```

The skill refreshes every configured person's report and then summarizes:

- Retained History notes whose structured extraction still needs review
- Rejected retained Lab observations without canonical coverage, missing
  accepted results, quarantine decisions, and inaccessible Lab evidence
- Current person-scoped medication contradictions
- Non-hidden files still present under `data/inbox/` or `data/quarantine/`
- Unassigned files that cannot yet be placed in a person's vault
- Historical audit classes that are coverage gaps, not confirmed current work

The skill's `SKILL.md` is the authoritative workflow.

## Vault path

Each configured person's persistent report is:

```text
famed-vault/Records/<person_id>/Reports/pending-review.md
```

Open that report from the person's Reports index. Unassigned files are shown
only in the skill output and are not written to a cross-person vault note.

## Resolve an item

Each item names its source, reason, and resolving skill:

- History source → `fm-history-extract`
- Lab coverage → `fm-reprocess-source` with exact reviewed decisions;
  reconcile inaccessible source/decision metadata first
- Medication contradiction → `fm-correct-fact`
- Inbox, quarantine, or unassigned source → `fm-ingest-inbox`

Follow the named skill's confirmation steps. Do not edit
`pending-review.md` directly; it is a generated projection.

## Verify the result

Run `fm-review-pending` again. An accepted or reviewed-no-facts History source,
a resolved medication contradiction, or a source moved out of `data/inbox/` or
`data/quarantine/` should leave the current report while its append-only audit
history remains.
