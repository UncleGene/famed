# How to Add a Family Member

## Goal

Add a person profile and relationships so the person appears in the vault and the local SQLite index.

## Required inputs

- `display_name` — must be unique across all persons.
- `birth_year` — required for every person.
- `sex` — required for every person.
- `person_id` — optional; auto-generated from `display_name` (lowercase, spaces become `-`). Set manually only when you need a stable custom ID or are resolving a name collision.

## Relationship direction (important)

Every relationship is directional and stored as:

`source_person_id --relationship_type--> target_person_id`

Allowed relationship types: `parent`, `child`, `spouse`, `sibling`, `grandparent`, `grandchild`, `guardian`. Common aliases auto-normalize: `mother`, `father`, `mom`, `dad` → `parent`; `son`, `daughter` → `child`; `wife`, `husband`, `partner` → `spouse`.

Examples on person `sam`:

- `spouse:alex` → `sam --spouse--> alex`
- `child:casey` → `sam --child--> casey`

On person `casey`: `parent:sam` → `casey --parent--> sam`.

## User-facing path: drop a person YAML and invoke a skill

Per ADR-015 the user-facing interface is a skill. One invocation handles validation, person creation, vault note generation, DB insertion, reciprocal relationship updates, and report refresh.

1. Copy [templates/person-template.yaml](../../templates/person-template.yaml) into `data/inbox/persons/`, e.g. `data/inbox/persons/dummy_alice.yaml`.
2. Fill in front-matter values, including any `relationships`. Relationship entries may be object-style (`relationship_type` + `target_person_id`) or compact string style (`parent:dummy_alice`).
3. In your AI CLI in this repo, ask:

   ```
   Use fm-ingest-inbox to process my inbox.
   ```

4. The skill validates the person YAML, creates the vault note in `famed-vault/Persons/<person_id>.md`, sets up record folders under `famed-vault/Records/<person_id>/`, creates the person inbox root under `data/inbox/<person_id>/`, inserts the DB row, and adds reciprocal relationships across known person notes. Unknown relationship targets are left unresolved rather than auto-created — the skill flags them for your decision.

You can drop multiple person YAML files at once and process them in a single skill invocation; each one becomes its own per-file outcome in the run summary.

## Direct vault editing (fallback only)

Manual Obsidian editing remains a fallback for review, correction, and human-authored notes:

1. Create `famed-vault/Persons/<person_id>.md` from `famed-vault/Persons/_template.md`.
2. Fill in front-matter fields and `relationships`.
3. Re-run `fm-ingest-inbox` (or its persistence step) so the change reaches the DB and reciprocal links update across known person notes.

Direct DB inspection:

```bash
sqlite3 data/famed.db "SELECT id, display_name, birth_year FROM persons ORDER BY id;"
sqlite3 data/famed.db "SELECT source_person_id, relationship_type, target_person_id FROM family_links ORDER BY source_person_id;"
```

## Notes

- `person_id` is normalized to lowercase with spaces converted to `-`.
- Re-running ingestion on the same YAML upserts the existing person row and merges relationships by default; reciprocal updates are on by default.
- Stable identity fields belong on the person profile (legal name, full DOB, MRN). Dynamic encounter fields (PCP, referring provider, address, insurance, phone) belong on the individual encounter note, not on the person profile — the skill enforces this distinction when proposing person-record updates.

## Developer / internal entrypoints

The user-facing path is the skill above. The commands below are implementation detail invoked by skills; they are not the documented user interface (ADR-015). They are listed here for development, debugging, and CI use only.

```bash
# Direct CLI flag-driven add (used by tests and scripted setup)
python scripts/add_family_member.py --display-name "Demo Person" --birth-year 1988 --sex female --relationship spouse:dummy_bob
python scripts/add_family_member.py --interactive
python scripts/add_family_member.py --replace-relationships     # replace instead of merge
python scripts/add_family_member.py --no-reciprocal             # disable reciprocal updates

# Batch import of person YAML files in data/inbox/persons/
python scripts/import_person.py

# Direct vault sync after manual note edits
python scripts/sync_vault.py
```
