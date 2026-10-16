# Family Members

```dataview
TABLE display_name AS "Name", birth_year AS "Birth Year", sex AS "Sex"
FROM "Persons"
WHERE person_id
SORT display_name ASC
```

## Family Relationships

```dataview
TABLE WITHOUT ID
    file.link AS "Person",
    rel.relationship_type AS "Relationship",
    link(rel.target_person_id) AS "Related Person"
FROM "Persons"
FLATTEN relationships AS rel
WHERE rel
SORT display_name ASC
```

## Latest Labs By Person

```dataview
TABLE WITHOUT ID
    link(person_id) AS "Person",
    split(key, "|")[1] AS "Test",
    rows[0].value AS "Value",
    rows[0].unit AS "Unit",
    rows[0].observed_at AS "Observed",
    rows[0].loinc_code AS "LOINC"
FROM "Records"
WHERE record_type = "lab_result" AND person_id AND test_name
SORT observed_at DESC
GROUP BY person_id + "|" + test_name AS key
```

## Out-of-Range Lab Flags

```dataview
TABLE WITHOUT ID
    link(person_id) AS "Person",
    test_name AS "Test",
    value AS "Value",
    unit AS "Unit",
    reference_range_low AS "Low",
    reference_range_high AS "High",
    observed_at AS "Observed"
FROM "Records"
WHERE record_type = "lab_result" AND person_id AND test_name AND value != null
WHERE (reference_range_low != null AND value < reference_range_low) OR (reference_range_high != null AND value > reference_range_high)
SORT observed_at DESC
```
