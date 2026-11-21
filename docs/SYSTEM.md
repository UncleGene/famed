# famed — System Snapshot

The current architecture. This file is not history; decisions and their
rationale live in `DECISIONS.md`. Component detail lives in
`docs/references/component-detail.md`.

## Purpose

A local-first family medical vault with AI-assisted ingestion and
evidence-backed reports. Retained source files and the Obsidian vault are the
evidentiary and human-facing base; SQLite is a derived index.

## Layers

| Layer | Design |
|---|---|
| Data | Retained source files, Obsidian vault, SQLite index, rebuildable reference cache, append-only findings ledger. |
| Processing | Inbox orchestrator, category handlers, canonicalizers, report generators, vault validators (`src/famed/`). |
| AI | Business logic skills (`.agents/skills/fm-*`) executed by the live agent. No model-provider layer ships in the runtime (ADR-013). |
| Interface | Agent skills for every user workflow (ADR-015); Obsidian + Dataview for reading. The vault is also the AI's context for reports and queries (ADR-014). |

## Ingestion Pipeline

```text
Files dropped into data/inbox/ → fm-ingest-inbox
  → Source registration (new / changed / processed / unsupported)
  → Clinical Interpreter (live agent, proposal only)
  → Canonicalizer (deterministic validation, coding, units)
  → Deduplication Engine (reversible links)
  → Human Resolver (only when ambiguous)
  → Data Steward (vault notes, DB rows, retained source, audit)
  → Dependent report refresh (ADR-025)
```

- `fm-ingest-inbox` composes proposal (`fm-inbox-batch-processor` /
  `fm-inbox-freeform-processor`), in-chat verification, deterministic
  persistence (`fm-inbox-execute`), and history fact extraction
  (`fm-history-extract`).
- The orchestrator routes `data/inbox/<person_id>/` by deterministic category
  inference, defaults to a dry run, tries deterministic parsers first, and
  leaves unrecognized sources for attended interpretation (ADR-016).
  Unresolved shared files are resolved by the user (CLI arguments or a TSV
  resolution table).
- Health-summary archives fan out into labs, medications, vitals, immunizations,
  and history review notes; the ZIP is the retained source (ADR-017).
- Medication contradictions are persisted and then raised for resolution
  (ADR-042).

## Roles

| Role | Implementation | Constraints |
|---|---|---|
| Clinical Interpreter | Live agent in `fm-*` skills; `clinical_interpreter.py` contracts | Proposal only; output validated by `validate_fact_extraction` (ADR-001). |
| Canonicalizer | Category sync modules (`lab_sync`, `medication_sync`, `history_sync`, …) | Deterministic; no clinical phrase lists for prose (ADR-018). |
| Deduplication Engine | Category modules + shared `dedup_links` | No destructive merge; restoration on owner loss (ADR-006). |
| Human Resolver | The user, via skill prompts and decision bundles | Asked only on ambiguity; resolutions audited (ADR-039, ADR-042). |
| Data Steward | Persistence paths + `storage_contract` | Applies ADR-003; keeps source/vault/DB/audit consistent. |

## Storage Model

Authority: `user_correction / user_update > retained source > vault note > famed.db` (ADR-003).

| Surface | Location | Notes |
|---|---|---|
| Retained sources | `data/processed/<person_id>/<Category>/` | Untracked. Archive members referenced as `archive.zip#member.xml`. |
| Vault | `famed-vault/` | Human-readable record (ADR-009, ADR-010). |
| Index | `data/famed.db` | Derived; rebuildable from higher layers. |
| Reference cache | `data/reference/`, `famed-reference.db` | RxNorm, LOINC, CPIC, ClinVar, GenCC, targeted gnomAD (ADR-011). |
| Findings ledger | `ledger/findings.jsonl` | Append-only (ADR-027). |
| Audit | `audit_events` table | Append-only; never treated as current state (ADR-043). |

Provenance: every vault note and DB row carries the repo-relative retained
`data/processed/...` path. History refutations live in
`Records/<person_id>/History/history-corrections.md` (ADR-004). Dated user
updates live in their category folders (ADR-005).

## Vault Structure

```text
famed-vault/
  Persons/<person_id>.md
  Records/<person_id>/
    Labs/           source-evidence lab notes + index
    Medications/    source-evidence medication notes, dated updates + index
    History/        history review notes, dated updates, history-corrections.md
    Genes/          DNA source notes
    Immunizations/  canonical immunization notes + index
    Insights/
    Reports/        derived reports, doctor-visit-packets/
```

- Person data is partitioned; cross-person reads only through declared
  features (family-history context, ADR-028).
- Lab and medication source notes are evidence projections owned by one source;
  the Lab index, `lab-results.md`, and `current-medications.md` are the canonical
  current views (ADR-038).
- Generated sections regenerate in place; user-authored sections survive.

## Report Graph

`dependent_reports.py` declares producer/consumer edges and refreshes in
dependency order after ingestion, corrections, and reference-cache changes
(ADR-025). Main chains:

- **Labs:** `lab-results.md`, `lab-conflicts.md` → `flagged-for-discussion.md` → FHIR →
  medication-safety alerts → health advisory.
- **Medications:** `current-medications.md`, `medication-conflicts.md` →
  `genetics.md` → composites → FHIR.
- **DNA / reference cache:** `genetics.md` (PGx, carrier) → composites.
- **History:** `medical-history-summary.md` → composites for the person and for
  blood relatives whose family-history context reads them.
- **Review:** `pending-review.md` on medication, lab, history, and quarantine
  changes (ADR-043).

The doctor-visit packet is generated on demand and is a dated snapshot
(ADR-031). Agent-authored synthesis lives in the health advisory and is carried
over as stale on every deterministic refresh (ADR-026).

## Reports

| Report | Owner |
|---|---|
| `pending-review.md` | `pending_review.py` |
| `lab-results.md`, `lab-conflicts.md`, `flagged-for-discussion.md` | `lab_sync.py` |
| `current-medications.md`, `medication-conflicts.md` | `medication_sync.py` |
| `medical-history-summary.md` | `history_sync.py` |
| `immunizations.md` | `immunization_sync.py` |
| `genetics.md` | `genetics_report.py`, `pgx_coverage.py`, `carrier_screening.py` |
| `medication-safety-alerts.md` | `medication_safety_alerts.py` |
| `health-advisory.md` | `health_advisory.py`, `report_synthesis.py` |
| `doctor-visit-packets/` | `doctor_visit_packet.py` |
| `prs-validation.md` | `prs_validation.py` |
| FHIR bundle | `fhir_export.py` |

## Rebuild and Replay

- `fm-reprocess-source` replays one retained source through a shared handler
  registry; History replay preflights before mutation (ADR-039).
- `fm-rebuild-vault` replays the retained corpus into isolated storage and emits
  a read-only differential audit against the working store (ADR-040).

## Agent Operations

- Skills live in `.agents/skills/`; `.claude/skills` is a symlink; Gemini and
  Copilot adapters point back to the same files (ADR-012).
- Business logic skills cite capability levels from
  `docs/references/model-capability-matrix.md`.
- `.geminiignore` and `.aiexclude` keep agent indexing away from local medical
  data.

## Non-Success States

`awaiting_user_decision`, `quarantined`, `stale`, `superseded`,
`skipped_user_correction` (an audit event, not a failure).
