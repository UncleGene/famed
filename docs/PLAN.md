# famed — Plan

Forward-looking work only. Completed work is described by the current docs and
code, not recorded here. Remove an item in the commit that completes it.

## Planning Rules

Project rules (authority order, AI non-authority, completion gates, Pyrefly,
confirmation before implementation) live in `AGENTS.md`. Rules for every new
slice:

- An importer is complete only when repeated runs are idempotent and equivalent
  facts from different sources converge without duplicates.
- A note type is complete only when it is reachable from vault navigation.
- Public reference data stays in the untracked cache.
- Deterministic parsing stays bounded (ADR-018). Semantic gaps go to the Clinical
  Interpreter plus deterministic validation, not to new phrase lists.
- User-facing vertical slices come before platform generalization.
- Format support is demand-driven: add a format when real files or
  representative fixtures need it.
- Structured caregiver-prepared inputs (CSV/YAML) are valid as exports,
  migration aids, or fixtures; they do not displace free-form text/PDF intake.
- AI-dependent behavior lands as skills, schemas, fixtures, and manual
  verification flows (ADR-013).
- Every user-facing workflow is a skill (ADR-015). Scripts are internal helpers.
- New report types need explicit approval.

## Failure Modes Registry

Guardrails every importer and report must honor.

| Area | Failure mode | Guardrail |
|---|---|---|
| Medication import | Weak person match accepted | Deterministic person resolution or explicit confirmation before persistence |
| Medication import | Unparseable document date persisted | Quarantine invalid or ambiguous dates |
| Medication import | RxNorm null with no review path | Reviewed RxNorm lane + visible `unresolved` state |
| Medication import | Refill window stored as therapy end | `authorized_through` separate from `end_date` (ADR-041) |
| Medication import | Export date taken as list currency | Judge a list by its drugs' start dates, never by archive or import date (ADR-041) |
| Legacy migration | A dated user update relabeled as a source import | Relabel only the exact row whose note already declares retained-source provenance |
| History summary | Same-day screening aliases shown twice | Collapse narrow same-event aliases at render time; keep accepted facts |
| Lab import | An individual-panel note superseded by a later comprehensive export | `superseded_by` only under full single-note coverage (ADR-038) |
| Test isolation | A test writes fixtures into the live processed tree | Writers resolve storage from an injected root; tests run from an unrelated CWD |
| Cross-source matching | The same draw dated differently across sources (local date vs UTC timestamp) | Match on analyte plus value with a one-day tolerance anchored on the value |
| Review surfaces | "Nothing found" read as "checked, nothing found" | Report only absence that was established; verify suppression rules against real data (ADR-042) |
| Fact comparison | A missing value compared as a value | Compare per axis; exclude silent rows from that axis (ADR-042) |
| C-CDA dates | UTC timestamp truncated to the next calendar day | Re-express in the document's declared offsets; fail closed on disagreement (ADR-041) |

## Release Gates

- Re-running an ingest is idempotent.
- Equivalent facts from different sources converge without duplicates.
- Generated notes are linked into the vault graph.
- Retained sources keep their meaningful original filenames and stable paths.
- Reference data is rebuildable from the untracked cache.
- Tests cover normalization and duplicate detection for touched categories.
- No AI-assisted path bypasses deterministic validation.
- Corrections and deletions refresh or mark affected outputs.
- Docs and traceability are current.
- Simplification and review passes are done; tests and Pyrefly pass.

## Not In Scope

- Dosing recommendations, clinical-grade PGx interpretation, diagnosis, or
  treatment plans.
- TTS, voice, chat UI, or genome-browser surfaces.
- New report types without explicit approval.

## Possible Future Work

Not scheduled. Each item needs a concrete trigger and its own decision before
work starts.

| Item | Trigger |
|---|---|
| HL7 v2 lab messages | A real source or representative fixture |
| Local web UI / API (localhost only) | Explicit request; must not create conflicting editable state |
| Public demo fixtures ("drop documents, get a packet") | Explicit request |
| Wearable summaries (Apple Health, Oura) | Explicit request; daily summaries only, evidence capped at E3 |
| PRS score rendering | A model-specific decision pinning score models and reporting rules (ADR-024) |
| GWAS Catalog ingestion | A concrete PRS need |
| Interaction severity scoring | A source-backed non-normal phenotype × medication pair and an approved non-dosing contract |
| External FHIR profile validation | A concrete exchange target |
| Per-person export for sharing | A sharing requirement (ADR-010) |

## Open Findings

| Finding | State | Handling |
|---|---|---|
| Only the medication C-CDA extractor resolves UTC timestamps to the document's local date; lab, vital, immunization, and document-date paths still truncate. | Adopting `local_offsets` is one change per extractor, but labs already rely on the ±1-day matching guard. | Adopt per extractor when a concrete defect appears; verify against retained sources. |
| Frequency normalization treats a titration instruction ("start ½ tablet daily, then increase…") as a frequency, and parses a tablet count as a dose. | Latent: both are excluded from contradiction comparison. | Fix with other frequency-normalization work. Do not rewrite provider text. |
| `_medication_conflict_name_key` groups by the first parenthetical brand alias, so a brand-labeled and a generic-labeled entry for the same drug are compared separately. | No contradiction is lost today. | Decide between brand alias and ingredient as the key; check dedup impact first. |
| `lab_sync.py` and `history_sync.py` are very large modules. | No decomposition planned. | Revisit when one fix touches more than three unrelated concerns, or a bug is hard to isolate. |
