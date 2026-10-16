# famed

**A local-first medical record for your family, kept in Obsidian and maintained
by your AI coding agent.**

famed turns the scattered paperwork of family health care (lab PDFs, portal
exports, medication lists, visit summaries, consumer DNA files) into one
organized, cross-linked record per person on your own computer. You drop files
into a folder and ask your AI agent (Claude Code, Codex, Gemini CLI, and
similar) to process them. The agent reads and interprets the documents; famed's
deterministic code validates every fact before anything is saved; and you
browse the result in [Obsidian](https://obsidian.md).

> [!WARNING]
> famed is a personal organization tool, not medical software. It does not
> diagnose, recommend treatment, or suggest doses, and it is not a substitute for
> a clinician, pharmacist, or genetic counselor. Always confirm anything
> important with a healthcare professional.

## Goals

- **Keep everything in one place you control.** Source files, notes, and the
  index all stay on your machine. Nothing is uploaded unless you choose to.
- **Make the record useful to a caregiver.** Current medications, lab trends,
  history, and a printable summary for the next doctor visit, without digging
  through portals.
- **Never let AI silently write medical truth.** AI proposes; validated rules
  decide what is saved; you are asked whenever something is ambiguous or
  conflicting.
- **Stay traceable.** Every fact links back to the document it came from, and
  your own corrections are preserved across re-imports.

## What You Get

For each family member, famed maintains:

| Area | What you see in the vault |
|---|---|
| Labs | Every result, coded with LOINC; one lab report grouped by body system with trend charts; a short list of results worth discussing (out of range, trending toward a limit, recently back to normal) |
| Medications | A current list grouped into prescriptions, supplements, and as-needed items; refill renewal reminders; a report of sources that disagree about a dose or schedule |
| History | Diagnoses, procedures, follow-ups, vitals, allergies, and immunizations extracted from visit notes and summaries, with a short Digest of chronic conditions at the top |
| Genetics | From a consumer DNA file: a gene-by-gene pharmacogenomics table you can print as a wallet card, and carrier screening when a carrier is found |
| Doctor-visit packet | A two-page printable summary: allergies, problems, medications, labs to discuss, vitals, PGx results, immunizations, and family history |
| Health advisory | Non-authoritative conversation starters: genetic risk flags, screening reminders, notable labs, blood-relative family history |
| Review queue | Everything that is waiting for your decision, per person |
| FHIR export | A standard FHIR R4 bundle you can hand to other health software |

Supported inputs:

- **Labs:** PDF reports (Quest, LabCorp, MyChart and others), CSV/TSV exports,
  C-CDA health-summary archives (the "download my record" ZIP from Epic/MyChart
  portals).
- **Medications:** portal lists (text, PDF, HTML, screenshots), vendor CSV
  exports, typed lists of supplements.
- **History:** visit summaries, discharge notes, imaging and procedure reports
  (PDF, text, scanned TIF via OCR).
- **DNA:** 23andMe, AncestryDNA, MyHeritage, Genotek, Nebula Genomics, and VCF
  files.

## How It Works

```text
data/inbox/<person>/  ──►  your AI agent runs fm-ingest-inbox
                              │  reads the files, proposes facts with evidence
                              ▼
                         famed validates, codes, de-duplicates
                              │  asks you about anything ambiguous
                              ▼
     famed-vault/ (Obsidian)  +  data/famed.db (index)  +  data/processed/ (originals)
                              │
                              ▼
                     reports refresh automatically
```

- Workflows are **skills**: plain-language instructions under `.agents/skills/`
  that your agent follows. You ask for an outcome ("process my inbox", "prepare
  a doctor-visit packet for Mom"); the agent runs the right steps.
- The agent you already use is the AI. famed does not call any model service
  itself and needs no API keys.
- Original documents are kept unchanged in `data/processed/`. The vault is the
  readable record; the SQLite database is a rebuildable index.

## Requirements

- macOS (recommended) or Linux, Python 3.11+.
- [Obsidian](https://obsidian.md) with the **Dataview** community plugin.
- An AI coding agent that can read repository skills, for example
  [Claude Code](https://claude.com/claude-code), Codex CLI, or Gemini CLI.
- Optional: [Tesseract](https://github.com/tesseract-ocr/tesseract) for scanned
  TIF documents (`brew install tesseract`).

> [!IMPORTANT]
> Your AI agent sends the content of the documents it reads to its model
> provider. Use an agent and account whose data-handling terms you are
> comfortable with for medical records.

## Installation

```bash
git clone https://github.com/UncleGene/famed.git
cd famed
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .            # add ".[ocr]" for scanned documents
python scripts/setup.py               # creates the vault, database, and folders
```

`setup.py` creates:

| Path | Purpose |
|---|---|
| `famed-vault/` | The Obsidian vault |
| `data/inbox/` | Where you drop new files |
| `data/processed/` | Original documents, kept after import |
| `data/quarantine/` | Files that need your attention |
| `data/famed.db` | Local search index |

All of these are git-ignored. Re-running setup only adds what is missing. On
Linux, or to skip the macOS Keychain step, use
`python scripts/setup.py --skip-keychain`. To check dependencies at any time:
`python scripts/setup.py --check`.

Then open Obsidian, choose **Open folder as vault**, select `famed-vault/`, and
enable the Dataview plugin.

### Reference data (recommended)

Medical coding uses public reference data cached under `data/reference/`. Ask
your agent:

```text
Use fm-refresh-reference-cache to download the reference data.
```

This fetches RxNorm (medications), CPIC and ClinVar (genetics), and related
datasets. **LOINC** (lab codes) requires a free account: download the current
LOINC zip from <https://loinc.org/downloads/>, extract it under
`data/reference/loinc/`, then ask the agent to refresh the LOINC dataset. Without
LOINC, labs still import; codes are just left unmapped.

### Connect your agent

Start your agent from the repository root so it picks up `AGENTS.md` and the
skills:

- **Claude Code** reads `.claude/skills/` (a link to `.agents/skills/`) and
  `CLAUDE.md` automatically.
- **Codex CLI** and other `AGENTS.md`-aware agents read `.agents/skills/`.
- **Gemini CLI** loads `GEMINI.md`; `.gemini/commands/` provides slash commands.

## Getting Started

### 1. Add your family

Copy `templates/person-template.yaml` into `data/inbox/persons/` once per
person, fill in a display name, birth year, sex, and relationships (for
example `parent:dummy_alice`), then ask:

```text
Use fm-ingest-inbox to process my inbox.
```

Each person gets a note in `famed-vault/Persons/`, a record folder in
`famed-vault/Records/<person_id>/`, and a drop folder `data/inbox/<person_id>/`.
Relationships are mirrored automatically. Details:
[how-to-add-a-family-member.md](docs/procedures/how-to-add-a-family-member.md).

### 2. Import documents

Download what you have from patient portals (the full health-summary ZIP is the
richest source), put each person's files in `data/inbox/<person_id>/`, and ask
again:

```text
Use fm-ingest-inbox to process my inbox.
```

The agent processes everything it can, asks you one question at a time about
anything unclear (which person, which value is right), and moves originals to
`data/processed/`. Files it cannot handle safely go to `data/quarantine/` with a
reason.

### 3. Read the record

In Obsidian, open a person's note and follow the links to Labs, Medications,
History, Genes, and Reports.

## Everyday Workflows

| You want to… | Ask your agent | More |
|---|---|---|
| Add new documents after a visit | `Use fm-ingest-inbox to process my inbox.` | [how-to-add-medical-history](docs/procedures/how-to-add-medical-history.md) |
| Prepare for an appointment | `Use fm-prepare-doctor-visit for dummy_alice.` Print or share the packet. | — |
| Record something that isn't in any document ("started vitamin D 2000 IU today", a home blood-pressure reading) | `Use fm-record-user-update for dummy_alice: …` | [how-to-add-medication](docs/procedures/how-to-add-medication.md) |
| See what needs your decision | `Use fm-review-pending.` | [how-to-review-pending-items](docs/procedures/how-to-review-pending-items.md) |
| Fix a misread value | `Use fm-correct-fact to correct dummy_alice's glucose on 2024-01-15 to 88 mg/dL.` | [how-to-correct-auto-imported-value](docs/procedures/how-to-correct-auto-imported-value.md) |
| Remove a diagnosis a clinic summary got wrong | `Use fm-correct-fact to refute dummy_alice's "<diagnosis>" dated <date>.` | same |
| Undo a correction | `Use fm-correct-fact to revoke my correction on …` | [how-to-revoke-manual-override…](docs/procedures/how-to-revoke-manual-override-and-rerun-inference.md) |
| Ask a question | `Use fm-query to answer: when was dummy_alice's last TSH and was it normal?` | — |
| Import a DNA file and get the PGx wallet card | Drop the file in the person's inbox, run `fm-ingest-inbox`, then open `genetics.md`. | [how-to-import-dna-data](docs/procedures/how-to-import-dna-data.md) |
| Check medications for review prompts | `Use fm-generate-medication-safety-alerts for dummy_alice.` | — |
| Get conversation starters for a check-up | `Use fm-generate-health-advisory for dummy_alice.` | — |
| Send records to another app | `Use fm-export-fhir for dummy_alice.` | [how-to-export-fhir-r4-bundle](docs/procedures/how-to-export-fhir-r4-bundle.md) |
| Double-check medical coding | `Use fm-correctness-audit.` (sends only generic code lookups to the web) | — |
| Re-run a document after a fix | `Use fm-reprocess-source on data/processed/<path>.` | [how-to-recalculate-after-data-correction](docs/procedures/how-to-recalculate-after-data-correction.md) |
| Back up | Copy `famed-vault/` and `data/` together. | [backup-and-restore-vault-db](docs/procedures/backup-and-restore-vault-db.md) |

### Tips

- **Health-summary archives first.** A portal's full record export usually
  contains labs, medications, immunizations, and problems in structured form;
  import it before individual PDFs.
- **Disagreeing sources are expected.** When two documents disagree about a
  medication, famed keeps both and asks you. Your answer is saved and survives
  future imports.
- **Corrections beat imports.** Anything you correct or record yourself outranks
  imported documents until you revoke it.
- **Reports keep themselves current.** Every import or correction refreshes the
  reports that depend on it. The doctor-visit packet is a dated snapshot;
  generate a new one before each visit.
- **"Not on chip" is not "negative."** Consumer DNA covers a fraction of the
  genome; famed says when it cannot tell.

## Privacy

- All personal data (`famed-vault/`, `data/`, `ledger/`) is git-ignored and
  stays local.
- famed itself makes network requests only when you ask for reference-data
  downloads or a correctness audit, and those requests never contain personal
  data.
- Your AI agent sees the documents it processes; see the note under
  Requirements.

## Troubleshooting

| Symptom | What to do |
|---|---|
| A file was not imported | Check `data/quarantine/` and the reason in `fm-review-pending`. |
| A value is wrong | Use `fm-correct-fact`; never edit the database directly. |
| A report looks stale | Run the report's skill again, or `fm-reprocess-source` on the source. |
| Lab codes are missing | Install the LOINC files (see Reference data). |
| Setup check fails | Run `python scripts/setup.py --check` and follow its remediation. |

All caregiver procedures are in [docs/procedures/](docs/procedures/).

## Contributing

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the architecture, developer
setup, quality gates, and documentation conventions.

## License

MIT. See [LICENSE](LICENSE).
