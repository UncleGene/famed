---
name: fm-review-pending
description: Refresh person-scoped pending-review reports and summarize current history, lab-rejection coverage, medication, inbox, and quarantine review work.
metadata:
  reasoning_class: low
  category: business-logic
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  status: active
  links: REQ-005, REQ-006, REQ-016, REQ-018, ADR-015, ADR-025, ADR-043, ADR-039
---

# When to use

Use this skill when a caregiver asks what currently needs review, wants the
person-scoped review reports refreshed, or needs to find unresolved inbox or
quarantine files. Do not query historical `audit_events` statuses as though
they were a current queue.

# Inputs

- Repository root containing `data/famed.db`, `data/inbox`,
  `data/quarantine`, retained HealthSummary archives, and `famed-vault`
- Optional person ID when the user wants only one person's results

# Procedure

1. Read `docs/REQUIREMENTS.md` (REQ-005, REQ-006, REQ-016, REQ-018) and
   `docs/DECISIONS.md` (ADR-043, ADR-039) from source.
2. From the repository root, run `python scripts/review_pending.py`.
3. Read the JSON output. Treat `people` and `unresolved` as the current queue.
   Treat `unsupported_event_counts` only as a coverage warning; never convert
   those historical event counts into current items.
4. For each person requested, verify
   `Records/<person_id>/Reports/pending-review.md` exists and that its
   `pending_review_count` matches the corresponding `people` entry's `items`
   count. Use that entry's `display_name` in human-facing output; keep
   `person_id` only for paths and internal lookup.
5. Summarize each item with its person or `unresolved`, source, subject,
   reason, and named resolving skill/action. Link a dedicated report when the
   item supplies one instead of copying its detailed body.
6. Ask which item the user wants to resolve. Invoke the named skill only after
   following that skill's own confirmation and safety rules.

# Skeptic pass

- Does every reported item still have category-owned current evidence: a
  current History note at `needs_review`, a retained rejected Lab observation
  lacking canonical coverage or remaining quarantined, an inaccessible retained
  Lab source/decision, a current medication contradiction,
  or a non-hidden inbox/quarantine file on disk?
- Did any hidden metadata file such as `.DS_Store` enter the result? If so,
  stop and report a scanner defect.
- Is an unassigned item being persisted into a person or household note? If
  so, stop; unassigned items are invocation-only.
- Are unsupported audit classes clearly labeled as coverage gaps rather than
  pending work?
- Does a report duplicate a dedicated sub-report body instead of linking it?
  If so, stop and fix the projection.

# Output

Report:

```text
Current review:
  <display_name>: <count>
    - <subject> — <reason>
      Source: <source>
      Resolve: <skill> — <action>
  Unresolved: <count>

Coverage gaps:
  <event_type>: <historical event count>
```

Omit the coverage-gaps block when the helper returns none.

# Hand-off

Hand off History notes to `fm-history-extract`, medication contradictions to
`fm-correct-fact`, Lab coverage items to `fm-reprocess-source` with their exact
reviewed decisions (reconcile inaccessible evidence first), and inbox,
quarantine, or unresolved files to
`fm-ingest-inbox`. After resolution, rerun this skill to verify the item drops
out while historical audit evidence remains.
