# Temporary Workspace Cleanup

## Goal

Use the project-local `tmp/` folder for ad-hoc files (scratch YAML, one-off exports, smoke-test artifacts) and clean it safely.

## Recommended Usage

- Write temporary files under `tmp/` instead of `/tmp/` so commands are reproducible from the repo root.
- Keep source-like examples in tracked template files (for example `templates/person-template.yaml`) and only write run-specific test files into `tmp/`.

Example:

```bash
cat > tmp/demo-person.yaml <<'YAML'
person_id: "demo-person"
display_name: "Demo Person"
birth_year: 1988
sex: "female"
relationships:
  - relationship_type: "spouse"
    target_person_id: "related-person"
YAML

python scripts/import_person.py --person-file tmp/demo-person.yaml
```

## Cleanup Commands

Remove all temporary files but keep the folder itself:

```bash
find tmp -mindepth 1 ! -name '.gitkeep' -delete
```

Or run the helper script:

```bash
bash scripts/clean_tmp.sh
```

Preview what will be deleted first:

```bash
find tmp -mindepth 1 ! -name '.gitkeep' -print
```

## Safety Notes

- Do not store real sensitive exports in `tmp/` longer than needed.
- `tmp/` contents are gitignored except `tmp/.gitkeep`.
- If a command writes output you want to preserve, move it into a tracked docs or template path before cleanup.
