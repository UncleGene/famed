---
person_id: "replace-with-person-id"
display_name: ""
legal_name: ""            # optional: full legal name as it appears on medical records
birth_year: 1980          # required; used when full birth_date is unavailable
birth_date: ""            # optional: full date YYYY-MM-DD; preferred over birth_year when known
sex: "female"             # required
identifiers: []           # optional: list of {system: "<hospital/lab/network>", id: "<MRN or patient ID>"}
relationships:
  - relationship_type: ""
    target_person_id: ""
---

# Person Profile

## Notes

- Add context about this family member here.
- Relationship direction is this person -> target_person_id.
- Recommended relationship_type values: parent, child, spouse, sibling, grandparent, grandchild, guardian.
- `identifiers` holds stable system patient IDs (MRNs). Dynamic fields (PCP, address, insurance) belong in individual encounter records, not here.
