from __future__ import annotations

import builtins
import io
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from famed import inbox_orchestrator
from famed.clinical_interpreter import (
    FACT_EXTRACTION_SCHEMA_VERSION,
)
from famed.immunization_sync import persist_immunization_rows
from famed.inbox_orchestrator import (
    _extract_text_for_person_resolution,
    append_inbox_state_ledger,
    execute_routes,
    format_batch_summary,
    inspect_inbox,
    inspect_inbox_file,
    inspect_inbox_file_with_resolution,
    load_resolution_file,
    resolve_person_interactive,
)
from famed.setup import SCHEMA_SQL


def test_inspect_inbox_skips_hidden_metadata_files_and_dirs(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    hidden_file = inbox_root / ".DS_Store"
    nested_hidden_file = inbox_root / "dummy_alice" / "labs" / ".sync-metadata"
    hidden_dir_source = inbox_root / ".metadata" / "labs" / "panel.csv"
    for path in [source_path, hidden_file, nested_hidden_file, hidden_dir_source]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")

    routes = inspect_inbox(inbox_root)

    assert [route.relative_path for route in routes] == [Path("dummy_alice/labs/panel.csv")]


def _ccda_visit_summary_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <title>Summarization of Episode Note</title>
  <effectiveTime value="20230427191511-0400"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Example</family></name>
        <birthTime value="19900115"/>
      </patient>
    </patientRole>
  </recordTarget>
  <componentOf>
    <encompassingEncounter>
      <effectiveTime><low value="20230203140000-0800"/></effectiveTime>
    </encompassingEncounter>
  </componentOf>
  <component>
    <structuredBody>
      <component>
        <section>
          <title>Assessments</title>
          <text>
            <table>
              <tbody>
                <tr><td>GERD (ICD-10 - K21.9)</td><td>Continue current plan.</td></tr>
              </tbody>
            </table>
          </text>
        </section>
      </component>
      <component>
        <section>
          <title>Plan Of Treatment</title>
          <text><content>Follow Up: PRN</content></text>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
"""


def _ccda_mixed_summary_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Continuity of Care Document</title>
  <effectiveTime value="20230427191511-0400"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Example</family></name>
        <birthTime value="19900115"/>
      </patient>
    </patientRole>
  </recordTarget>
  <componentOf>
    <encompassingEncounter>
      <effectiveTime><low value="20230203140000-0800"/></effectiveTime>
    </encompassingEncounter>
  </componentOf>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <text>
            <table>
              <tbody>
                <tr><td ID="result1">Hemoglobin A1c</td><td>5.8</td></tr>
              </tbody>
            </table>
          </text>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="4548-4" codeSystem="2.16.840.1.113883.6.1" displayName="Hemoglobin A1c">
                <originalText><reference value="#result1"/></originalText>
              </code>
              <statusCode code="completed"/>
              <effectiveTime value="20230203"/>
              <value xsi:type="PQ" value="5.8" unit="%"/>
              <referenceRange>
                <observationRange>
                  <value xsi:type="IVL_PQ">
                    <low value="4.0"/>
                    <high value="5.6"/>
                  </value>
                </observationRange>
              </referenceRange>
            </observation>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <code code="10160-0" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Medications</title>
          <text>
            <table>
              <tbody>
                <tr><td ID="med1">Omeprazole 20 mg delayed release capsule</td>
                <td ID="sig1">1 capsule daily</td></tr>
              </tbody>
            </table>
          </text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="EVN">
              <text><reference value="#med1"/></text>
              <statusCode code="active"/>
              <effectiveTime xsi:type="IVL_TS">
                <low value="20230203"/>
              </effectiveTime>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code code="7646" codeSystem="2.16.840.1.113883.6.88"
                          displayName="Omeprazole 20 MG Delayed Release Oral Capsule">
                      <originalText><reference value="#med1"/></originalText>
                    </code>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
              <entryRelationship typeCode="COMP">
                <substanceAdministration classCode="SBADM" moodCode="EVN">
                  <text><reference value="#sig1"/></text>
                </substanceAdministration>
              </entryRelationship>
            </substanceAdministration>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <code code="8716-3" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Vital Signs</title>
          <entry>
            <organizer classCode="CLUSTER" moodCode="EVN">
              <component>
                <observation classCode="OBS" moodCode="EVN">
                  <code code="8310-5" codeSystem="2.16.840.1.113883.6.1"
                        displayName="Body temperature"/>
                  <statusCode code="completed"/>
                  <effectiveTime value="20230203"/>
                  <value xsi:type="PQ" value="98.6" unit="F"/>
                </observation>
              </component>
              <component>
                <observation classCode="OBS" moodCode="EVN">
                  <code code="8867-4" codeSystem="2.16.840.1.113883.6.1" displayName="Heart rate"/>
                  <statusCode code="completed"/>
                  <effectiveTime value="20230203"/>
                  <value xsi:type="PQ" value="72" unit="/min"/>
                </observation>
              </component>
            </organizer>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <code code="11369-6" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Immunizations</title>
          <text><table><tbody><tr><td ID="imm1">Influenza vaccine</td></tr></tbody></table></text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="EVN">
              <text><reference value="#imm1"/></text>
              <statusCode code="completed"/>
              <effectiveTime value="20230115"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code code="140" codeSystem="2.16.840.1.113883.12.292"
                          displayName="Influenza, seasonal, injectable"/>
                    <lotNumberText>FLU-2026</lotNumberText>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
            </substanceAdministration>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <title>Assessments</title>
          <text>
            <table>
              <tbody>
                <tr><td>GERD (ICD-10 - K21.9)</td><td>Continue current plan.</td></tr>
              </tbody>
            </table>
          </text>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
"""


def _write_health_summary_archive(path: Path, *members: tuple[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for member_name, xml_text in members:
            archive.writestr(member_name, xml_text)


def _history_visit_pdf_text() -> str:
    return (
        "Patient: Example, Dummy Alice DOB: 01/15/1990 DOS: 02/03/2023 "
        "Reason for Appointment 1. GERD History of Present Illness Dummy Alice reports reflux."
    )


def _lab_pdf_text() -> str:
    return (
        "Name: Dummy Alice | DOB: 01/15/1990 | MRN: 123 "
        "COMPLETE BLOOD COUNT Collected on Feb 03, 2023 Value 4.9"
    )


def _write_person_note(vault_root: Path, person_id: str = "dummy_alice") -> None:
    person_dir = vault_root / "Persons"
    person_dir.mkdir(parents=True, exist_ok=True)
    (person_dir / f"{person_id}.md").write_text(
        "---\n"
        f"person_id: {person_id}\n"
        "record_type: person\n"
        "display_name: Dummy Alice\n"
        "birth_year: 1990\n"
        "sex: f\n"
        "relationships: []\n"
        "---\n\n"
        "# Dummy Alice\n",
        encoding="utf-8",
    )


def test_inspect_inbox_routes_person_scoped_lab_csv(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_labs"
    assert route.person_id == "dummy_alice"
    assert route.category == "labs"
    assert route.relative_path == Path("dummy_alice/labs/panel.csv")


def test_inspect_inbox_routes_person_yaml(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "persons" / "dummy_alice.yaml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("person_id: dummy_alice\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_person"
    assert route.category == "persons"
    assert route.person_id is None


def test_inspect_inbox_marks_shared_file_awaiting_decision(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "labs" / "panel.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("pdf fixture", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.handler is None
    assert route.person_id is None
    assert "requires person/category resolution" in route.reason


def test_inspect_inbox_marks_root_ccda_visit_summary_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "230217 VisitSummary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_visit_summary_xml(), encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "history"
    assert route.handler is None
    assert "history review document requires person resolution" in route.reason


def test_inspect_inbox_marks_root_visit_pdf_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "230217 visitNote.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "history"
    assert route.handler is None
    assert "history review document requires person resolution" in route.reason


def test_inspect_inbox_marks_root_imaging_pdf_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "231124 MRI Knee Right  wo Contrast .pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "history"
    assert route.handler is None
    assert "history review document requires person resolution" in route.reason


def test_inspect_inbox_marks_root_lab_pdf_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "260212 COMPLETE BLOOD COUNT.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "labs"
    assert route.handler is None
    assert "lab PDF requires person resolution" in route.reason


def test_inspect_inbox_marks_root_supplement_list_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "Dummy Alice Supplements"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Dummy Alice Supplements\n\nD3 2000 IU\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "medications"
    assert route.handler is None
    assert "supplement list requires person resolution" in route.reason


def test_inspect_inbox_marks_root_prescription_list_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "Dummy Alice Medications.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Dummy Alice medications\n\nOmeprazole 20 mg daily\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "medications"
    assert route.handler is None
    assert "medication list requires person resolution" in route.reason


def test_inspect_inbox_marks_root_history_tif_awaiting_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "230109 COLONOSCOPY - SCANNED - Jan 9, 2020.TIF"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"tif fixture")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "history"
    assert route.handler is None
    assert "history review document requires person resolution" in route.reason


def test_inspect_inbox_routes_person_root_health_summary_archive(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "N HealthSummary_Jun_01_2026.zip"
    _write_health_summary_archive(source_path, ("DOC0001.XML", _ccda_mixed_summary_xml()))

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_history_archive"
    assert route.person_id == "dummy_alice"
    assert route.category == "history"
    assert route.reason == "person-scoped health summary archive route"


def test_inspect_inbox_routes_person_root_lab_csv(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_labs"
    assert route.person_id == "dummy_alice"
    assert route.category == "labs"
    assert route.reason == "person-root inferred lab route"


def test_inspect_inbox_routes_person_root_free_t4_pdf(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "260624 T4 FREE.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_labs"
    assert route.person_id == "dummy_alice"
    assert route.category == "labs"
    assert route.reason == "person-root inferred lab route"


def test_inspect_inbox_routes_person_root_medication_text(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "current-meds.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Omeprazole 20 mg daily\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "medication_fact_extract"
    assert route.person_id == "dummy_alice"
    assert route.category == "medications"
    assert route.reason == "person-root inferred medication route"


def test_inspect_inbox_routes_person_root_medication_csv_by_header(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "pharmacy-export.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Drug Name,Strength,Sig\nOmeprazole,20 mg,1 daily\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_medications_csv"
    assert route.person_id == "dummy_alice"
    assert route.category == "medications"
    assert route.reason == "person-root inferred medication route"


def test_inspect_inbox_routes_person_root_history_xml(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "visit-summary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_visit_summary_xml(), encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "history_review"
    assert route.person_id == "dummy_alice"
    assert route.category == "history"
    assert route.reason == "person-root inferred history route"


def test_inspect_inbox_routes_person_root_vaccine_pdf_to_history(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "Vaccine History.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "history_review"
    assert route.person_id == "dummy_alice"
    assert route.category == "history"
    assert route.reason == "person-root inferred history route"


def test_inspect_inbox_resolves_shared_file_to_person_category(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="labs",
    )

    assert route.status == "routed"
    assert route.scope == "shared"
    assert route.handler == "import_labs"
    assert route.person_id == "dummy_alice"
    assert route.category == "labs"
    assert route.reason == "shared inbox item resolved by user"


def test_inspect_inbox_resolves_shared_ccda_history_to_review_handler(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "history" / "visit-summary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_visit_summary_xml(), encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history",
    )

    assert route.status == "routed"
    assert route.handler == "history_review"
    assert route.person_id == "dummy_alice"


def test_inspect_inbox_resolution_routes_supported_placeholder_category(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "medications" / "list.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("medication,dose\nOmeprazole,20 mg\n", encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="medications",
    )

    assert route.status == "routed"
    assert route.handler == "import_medications_csv"
    assert route.reason == "shared inbox item resolved by user"


def test_inspect_inbox_resolution_routes_medication_text_to_fact_extraction(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "medications" / "med-list.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Omeprazole 20 mg daily\n", encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="medications",
    )

    assert route.status == "routed"
    assert route.handler == "medication_fact_extract"
    assert route.person_id == "dummy_alice"
    assert route.reason == "shared inbox item resolved by user"


def test_inspect_inbox_resolution_routes_multi_category_placeholder(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "visits" / "summary.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("visit summary\n", encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history,medications",
    )

    assert route.status == "routed"
    assert route.handler == "multi_category_placeholder"
    assert route.category == "history,medications"
    assert route.reason == "shared inbox item resolved to multiple placeholder categories"


def test_inspect_inbox_resolution_keeps_unknown_category_quarantined(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "unknown" / "note.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("unknown content\n", encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="unknown",
    )

    assert route.status == "quarantined"
    assert route.handler is None
    assert route.reason == "unsupported person-scoped inbox category"


def test_inspect_inbox_routes_supported_placeholder_category(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "list.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("medication,dose\nOmeprazole,20 mg\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "import_medications_csv"
    assert route.reason == "person-scoped handler route"


def test_inspect_inbox_routes_person_scoped_medication_text_to_fact_extraction(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "current-meds.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Omeprazole 20 mg daily\n", encoding="utf-8")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "medication_fact_extract"
    assert route.person_id == "dummy_alice"
    assert route.reason == "person-scoped medication fact extraction route"


def test_inspect_inbox_routes_person_scoped_medication_html_to_fact_extraction(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "medications-export.html"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "<html><body><p>Omeprazole 20 mg daily</p></body></html>", encoding="utf-8"
    )

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "medication_fact_extract"
    assert route.person_id == "dummy_alice"


def test_inspect_inbox_routes_shared_medication_html_requires_person_resolution(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "medications-export.html"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "<html><body><p>Omeprazole 20 mg daily</p></body></html>", encoding="utf-8"
    )

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "awaiting_user_decision"
    assert route.category == "medications"


def test_inspect_inbox_routes_person_scoped_medication_image_to_fact_extraction(
    tmp_path: Path,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "medications-screenshot.png"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    route = inspect_inbox_file(source_path, inbox_root)

    assert route.status == "routed"
    assert route.handler == "medication_fact_extract"
    assert route.person_id == "dummy_alice"


def test_inspect_inbox_returns_sorted_file_routes(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    for relative_path in (
        "dummy_alice/labs/b.csv",
        "persons/dummy_alice.yaml",
        "dummy_alice/labs/a.csv",
    ):
        source_path = inbox_root / relative_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("fixture\n", encoding="utf-8")

    routes = inspect_inbox(inbox_root)

    assert [route.relative_path for route in routes] == [
        Path("dummy_alice/labs/a.csv"),
        Path("dummy_alice/labs/b.csv"),
        Path("persons/dummy_alice.yaml"),
    ]


def test_inspect_inbox_uses_per_file_resolutions(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    first_path = inbox_root / "shared" / "visits" / "summary.txt"
    second_path = inbox_root / "shared" / "medications" / "list.txt"
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    first_path.write_text("visit summary\n", encoding="utf-8")
    second_path.write_text("med list\n", encoding="utf-8")
    resolution_path = tmp_path / "resolutions.tsv"
    resolution_path.write_text(
        "relative_path\tperson_id\tcategory\n"
        "shared/visits/summary.txt\tdummy_alice\thistory,medications\n"
        "shared/medications/list.txt\tdummy_bob\tmedications\n",
        encoding="utf-8",
    )

    routes = inspect_inbox(inbox_root, resolutions=load_resolution_file(resolution_path))

    assert [(route.person_id, route.category, route.handler) for route in routes] == [
        ("dummy_bob", "medications", "medication_fact_extract"),
        ("dummy_alice", "history,medications", "multi_category_placeholder"),
    ]


def test_load_resolution_file_rejects_incomplete_rows(tmp_path: Path) -> None:
    resolution_path = tmp_path / "resolutions.tsv"
    resolution_path.write_text(
        "relative_path\tperson_id\tcategory\nshared/visits/summary.txt\tdummy_alice\t\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="row 2 is incomplete"):
        load_resolution_file(resolution_path)


def test_inbox_orchestrator_cli_prints_routes(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")

    monkeypatch.setattr(inbox_orchestrator, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["inbox_orchestrator"])

    assert inbox_orchestrator.main() == 0

    captured = capsys.readouterr()
    assert (
        "routed\tdry_run\timport_labs\tdummy_alice\tlabs\tdummy_alice/labs/panel.csv"
        in captured.out
    )


def test_execute_routes_invokes_person_import_handler(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "persons" / "dummy_alice.yaml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "person_id: dummy_alice\ndisplay_name: Dummy Alice\nbirth_year: 1990\nsex: female\n",
        encoding="utf-8",
    )
    routes = [inspect_inbox_file(source_path, inbox_root)]

    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=True)

    person_note = tmp_path / "famed-vault" / "Persons" / "dummy_alice.md"
    processed_source = data_dir / "processed" / "persons" / "dummy_alice.yaml"
    assert executions[0].action == "executed"
    assert executions[0].exit_code == 0
    assert person_note.exists()
    assert processed_source.exists()
    assert not source_path.exists()
    assert "record_type: person" in person_note.read_text(encoding="utf-8")


def test_execute_routes_processes_lab_route_through_orchestrator_lifecycle(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )
    routes = [inspect_inbox_file(source_path, inbox_root)]

    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-panel.csv"
    note_path = (
        tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Labs" / "2023-04-20-panel-panel.md"
    )
    assert executions[0].action == "executed"
    assert executions[0].exit_code == 0
    assert processed_path.exists()
    assert not source_path.exists()
    assert note_path.exists()
    reports_dir = tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Reports"
    for filename in (
        "lab-results.md",
        "lab-conflicts.md",
        "flagged-for-discussion.md",
    ):
        assert (reports_dir / filename).exists()
    flagged = (reports_dir / "flagged-for-discussion.md").read_text(encoding="utf-8")
    assert "Hemoglobin A1c" in flagged
    assert "2023-04-20" in flagged


def test_execute_routes_root_lab_pdf_requires_approval(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "260212 COMPLETE BLOOD COUNT.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    assert executions[0].final_state == "skipped"
    assert "requires --approve-pdf" in executions[0].message
    assert source_path.exists()


def test_execute_routes_matches_root_lab_pdf_to_known_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "260212 COMPLETE BLOOD COUNT.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        inbox_orchestrator.import_labs,
        "extract_pdf_text",
        lambda _path: _lab_pdf_text(),
    )
    monkeypatch.setattr(
        inbox_orchestrator.import_labs,
        "parse_labs_from_pdf_text",
        lambda _text, person_id, _source_label: [
            inbox_orchestrator.import_labs.lab_payload_from_mapping(
                {
                    "person_id": person_id,
                    "test_name": "White Blood Cells",
                    "observed_at": "2023-02-03",
                    "value": "4.9",
                    "unit": "10E3/uL",
                    "source_type": "pdf:mychart",
                },
                "pdf",
            )
        ],
    )
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes(
        [route],
        root=tmp_path,
        inbox_root=inbox_root,
        execute=True,
        approve_pdf=True,
    )

    processed_path = (
        data_dir / "processed" / "dummy_alice" / "Labs" / ("2023-02-03-COMPLETE BLOOD COUNT.pdf")
    )
    assert executions[0].final_state == "processed"
    assert processed_path.exists()
    assert not source_path.exists()


def test_execute_routes_resolved_shared_lab_uses_orchestrator_lifecycle(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )
    routes = [
        inspect_inbox_file_with_resolution(
            source_path,
            inbox_root,
            resolve_person="dummy_alice",
            resolve_category="labs",
        )
    ]

    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-panel.csv"
    note_path = (
        tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Labs" / "2023-04-20-panel-panel.md"
    )
    assert executions[0].final_state == "processed"
    assert "orchestrator retained source" in executions[0].message
    assert processed_path.exists()
    assert not source_path.exists()
    assert note_path.exists()


def test_execute_routes_quarantines_invalid_lab_source(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("not,a,lab\n", encoding="utf-8")
    routes = [inspect_inbox_file(source_path, inbox_root)]

    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=True)

    quarantine_path = data_dir / "quarantine" / "dummy_alice" / "Labs" / "undated-panel.csv"
    assert executions[0].final_state == "quarantined"
    assert executions[0].exit_code == 0
    assert quarantine_path.exists()
    assert not source_path.exists()
    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute(
            "SELECT source_path, status FROM ingestion_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit_row = conn.execute(
            "SELECT source_path, status FROM audit_events ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
    assert log_row == ("data/quarantine/dummy_alice/Labs/undated-panel.csv", "quarantined")
    assert audit_row == ("data/quarantine/dummy_alice/Labs/undated-panel.csv", "quarantined")


def test_execute_routes_quarantines_unresolved_shared_route(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "labs" / "panel.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("ambiguous lab report\n", encoding="utf-8")
    routes = [inspect_inbox_file(source_path, inbox_root)]

    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=True)
    ledger_path = append_inbox_state_ledger(tmp_path, executions)

    quarantine_path = data_dir / "quarantine" / "_unresolved" / "labs" / "undated-panel.pdf"
    assert executions[0].action == "executed"
    assert executions[0].exit_code == 0
    assert executions[0].final_state == "quarantined"
    assert "unresolved route quarantined" in executions[0].message
    assert quarantine_path.exists()
    assert not source_path.exists()
    ledger_row = ledger_path.read_text(encoding="utf-8").splitlines()[1].split("\t")
    assert ledger_row[2:10] == [
        "shared/labs/panel.pdf",
        "shared",
        "",
        "labs",
        "awaiting_user_decision",
        "",
        "executed",
        "quarantined",
    ]
    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute(
            "SELECT source_path, status, message FROM ingestion_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT event_type, subject_type, source_path, status, message
            FROM audit_events
            ORDER BY occurred_at DESC LIMIT 1
            """
        ).fetchone()
    assert log_row == (
        "data/quarantine/_unresolved/labs/undated-panel.pdf",
        "quarantined",
        "non-interactive ambiguity quarantined: shared inbox item requires "
        "person/category resolution",
    )
    assert audit_row == (
        "inbox_route_quarantined",
        "inbox_route",
        "data/quarantine/_unresolved/labs/undated-panel.pdf",
        "quarantined",
        "non-interactive ambiguity quarantined: shared inbox item requires "
        "person/category resolution",
    )


def test_execute_routes_creates_reviewed_placeholder_for_unhandled_category(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "history" / "visit-summary.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Visit summary narrative.\n", encoding="utf-8")
    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history",
    )

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = (
        data_dir / "processed" / "dummy_alice" / "History" / "undated-visit-summary.txt"
    )
    note_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "History"
        / "undated-visit-summary-placeholder.md"
    )
    index_path = tmp_path / "famed-vault" / "Records" / "dummy_alice" / "History" / "_index.md"
    assert executions[0].final_state == "processed"
    assert processed_path.exists()
    assert not source_path.exists()
    note_content = note_path.read_text(encoding="utf-8")
    assert "record_type: category_placeholder" in note_content
    assert "review_status: needs_review" in note_content
    assert (
        "source_path: data/processed/dummy_alice/History/undated-visit-summary.txt" in note_content
    )
    assert "[[undated-visit-summary-placeholder]]" in index_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute(
            "SELECT source_path, status, message FROM ingestion_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT event_type, source_path, status
            FROM audit_events
            ORDER BY occurred_at DESC LIMIT 1
            """
        ).fetchone()
    assert log_row == (
        "data/processed/dummy_alice/History/undated-visit-summary.txt",
        "processed",
        "placeholder note: Records/dummy_alice/History/undated-visit-summary-placeholder.md",
    )
    assert audit_row == (
        "placeholder_import_accepted",
        "data/processed/dummy_alice/History/undated-visit-summary.txt",
        "processed",
    )


def test_execute_routes_processes_resolved_ccda_visit_summary_as_review_note(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "history" / "visit-summary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_visit_summary_xml(), encoding="utf-8")
    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history",
    )

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = (
        data_dir / "processed" / "dummy_alice" / "History" / "2023-02-03-visit-summary.xml"
    )
    note_path = (
        vault_root
        / "Records"
        / "dummy_alice"
        / "History"
        / "2023-02-03-visit-summary-history-review.md"
    )
    assert executions[0].final_state == "processed"
    assert "history review note" in executions[0].message
    assert processed_path.exists()
    assert not source_path.exists()
    note_content = note_path.read_text(encoding="utf-8")
    assert "record_type: history" in note_content
    assert "verification_status: needs_review" in note_content
    assert (
        "source_path: data/processed/dummy_alice/History/2023-02-03-visit-summary.xml"
        in note_content
    )
    assert "fm-history-extract" in note_content
    assert "[[2023-02-03-visit-summary-history-review]]" in (
        note_path.parent / "_index.md"
    ).read_text(encoding="utf-8")
    with sqlite3.connect(data_dir / "famed.db") as conn:
        audit_row = conn.execute(
            "SELECT event_type, subject_type, source_path FROM audit_events"
        ).fetchone()
    assert audit_row == (
        "history_import_needs_review",
        "history",
        "data/processed/dummy_alice/History/2023-02-03-visit-summary.xml",
    )


def test_execute_routes_fans_out_mixed_ccda_into_labs_medications_vitals_and_immunizations(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "history" / "mixed-summary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_mixed_summary_xml(), encoding="utf-8")
    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history",
    )

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_source = "data/processed/dummy_alice/History/2023-02-03-mixed-summary.xml"
    assert executions[0].final_state == "processed"
    assert (
        data_dir / "processed" / "dummy_alice" / "History" / "2023-02-03-mixed-summary.xml"
    ).exists()
    assert (
        vault_root
        / "Records"
        / "dummy_alice"
        / "History"
        / "2023-02-03-mixed-summary-history-review.md"
    ).exists()
    assert (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Medications"
        / "2023-02-03-mixed-summary-medications.md"
    ).exists()
    assert (
        vault_root / "Records" / "dummy_alice" / "Labs" / "2023-02-03-mixed-summary-panel.md"
    ).exists()
    vital_note = (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Vitals"
        / "2023-02-03-mixed-summary-vitals-review.md"
    )
    assert vital_note.exists()
    vital_text = vital_note.read_text(encoding="utf-8")
    assert "temperature" in vital_text
    assert "pulse" in vital_text
    assert f"source_path: {processed_source}" in vital_text
    immunization_note = (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Immunizations"
        / "2023-02-03-mixed-summary-immunizations.md"
    )
    assert immunization_note.exists()
    immunization_text = immunization_note.read_text(encoding="utf-8")
    assert "record_type: immunization" in immunization_text
    assert "Influenza vaccine" in immunization_text
    assert "140" in immunization_text
    assert "2023-01-15" in immunization_text
    assert "FLU-2026" in immunization_text
    assert f"source_path: {processed_source}" in immunization_text
    with sqlite3.connect(data_dir / "famed.db") as conn:
        lab_rows = conn.execute(
            "SELECT test_name, value, unit, source_path FROM lab_results ORDER BY test_name"
        ).fetchall()
        medication_rows = conn.execute(
            "SELECT medication_name, rxnorm_cui, start_date, source_path FROM medications"
        ).fetchall()
    assert lab_rows == [("Hemoglobin A1c", 5.8, "%", processed_source)]
    assert medication_rows == [
        (
            "Omeprazole 20 mg delayed release capsule",
            "7646",
            "2023-02-03",
            processed_source,
        )
    ]


def test_execute_routes_quarantines_same_event_immunization_status_conflict(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
        persist_immunization_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("existing-summary.xml"),
            processed_source_path=(
                "data/processed/dummy_alice/History/2023-01-20-existing-summary.xml"
            ),
            checksum="existing123",
            source_type="ccda_xml",
            rows=[
                {
                    "person_id": "dummy_alice",
                    "vaccine_name": "Influenza vaccine",
                    "cvx_code": "140",
                    "administered_date": "2023-01-15",
                    "status": "not-done",
                    "lot_number": "FLU-2026",
                    "source_type": "source_import",
                    "evidence_snippet": "Influenza vaccine not given",
                }
            ],
            document_date="2023-01-20",
        )
        conn.commit()

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "history" / "mixed-summary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_mixed_summary_xml(), encoding="utf-8")
    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history",
    )

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    assert executions[0].final_state == "quarantined"
    assert "immunization conflict" in executions[0].message
    assert not source_path.exists()
    assert not (
        data_dir / "processed" / "dummy_alice" / "History" / "2023-02-03-mixed-summary.xml"
    ).exists()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        assert conn.execute(
            "SELECT status FROM immunizations WHERE person_id = 'dummy_alice'"
        ).fetchall() == [("not-done",)]


def test_execute_routes_processes_person_root_health_summary_archive(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root, person_id="dummy_alice")
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "dummy_alice" / "N HealthSummary_Jun_01_2026.zip"
    _write_health_summary_archive(
        source_path,
        ("DOC0001.XML", _ccda_mixed_summary_xml().replace("Dummy Alice", "Test Person")),
    )
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    archive_source = "data/processed/dummy_alice/HealthSummary/N HealthSummary_Jun_01_2026.zip"
    member_source = f"{archive_source}#DOC0001.XML"
    assert executions[0].final_state == "processed"
    assert "history archive processed" in executions[0].message
    assert "notes auto-accepted" in executions[0].message
    assert (
        data_dir / "processed" / "dummy_alice" / "HealthSummary" / "N HealthSummary_Jun_01_2026.zip"
    ).exists()
    assert not source_path.exists()
    assert not (data_dir / "processed" / "dummy_alice" / "History").exists()
    history_note = (
        vault_root / "Records" / "dummy_alice" / "History" / "2023-02-03-doc0001-history-review.md"
    )
    assert history_note.exists()
    assert (
        vault_root / "Records" / "dummy_alice" / "Medications" / "2023-02-03-doc0001-medications.md"
    ).exists()
    assert (
        vault_root / "Records" / "dummy_alice" / "Labs" / "2023-02-03-doc0001-panel.md"
    ).exists()
    immunization_note = (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Immunizations"
        / "2023-02-03-doc0001-immunizations.md"
    )
    assert immunization_note.exists()
    vital_note = (
        vault_root / "Records" / "dummy_alice" / "Vitals" / "2023-02-03-doc0001-vitals-review.md"
    )
    assert vital_note.exists()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        lab_rows = conn.execute(
            "SELECT test_name, source_path FROM lab_results WHERE person_id = 'dummy_alice'"
        ).fetchall()
        medication_rows = conn.execute(
            "SELECT medication_name, source_path FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchall()
        immunization_rows = conn.execute(
            "SELECT vaccine_name, source_path FROM immunizations WHERE person_id = 'dummy_alice'"
        ).fetchall()
    assert lab_rows == [("Hemoglobin A1c", member_source)]
    assert medication_rows == [("Omeprazole 20 mg delayed release capsule", member_source)]
    assert immunization_rows == [("Influenza vaccine", member_source)]
    immunization_text = immunization_note.read_text(encoding="utf-8")
    assert "2023-01-15" in immunization_text
    history_text = history_note.read_text(encoding="utf-8")
    assert "verification_status: needs_review" in history_text


def test_execute_routes_matches_root_ccda_visit_summary_to_known_person(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "230217 VisitSummary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_visit_summary_xml(), encoding="utf-8")
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    assert executions[0].final_state == "processed"
    assert (
        data_dir / "processed" / "dummy_alice" / "History" / "2023-02-03-VisitSummary.xml"
    ).exists()


def test_execute_routes_matches_root_visit_pdf_to_known_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "230217 visitNote.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        inbox_orchestrator.import_labs,
        "extract_pdf_text",
        lambda _path: _history_visit_pdf_text(),
    )
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    note_path = (
        vault_root
        / "Records"
        / "dummy_alice"
        / "History"
        / "2023-02-03-visitnote-history-review.md"
    )
    assert executions[0].final_state == "processed"
    processed_path = (
        data_dir / "processed" / "dummy_alice" / "History" / ("2023-02-03-visitNote.pdf")
    )
    assert processed_path.exists()
    note_content = note_path.read_text(encoding="utf-8")
    assert (
        "source_path: data/processed/dummy_alice/History/2023-02-03-visitNote.pdf" in note_content
    )
    assert "fm-history-extract" in note_content


def test_execute_routes_quarantines_ccda_visit_summary_without_person_match(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "230217 VisitSummary.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_ccda_visit_summary_xml(), encoding="utf-8")
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    quarantine_path = (
        data_dir / "quarantine" / "_unresolved" / "history" / ("undated-230217 VisitSummary.xml")
    )
    assert executions[0].final_state == "quarantined"
    assert quarantine_path.exists()
    assert not source_path.exists()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        audit_message = conn.execute("SELECT message FROM audit_events").fetchone()[0]
    assert "did not match exactly one known person" in audit_message


def test_execute_routes_quarantines_history_tif_when_ocr_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OCR is not available, TIF files are quarantined with a clear message."""
    from famed import ocr as ocr_module

    monkeypatch.setattr(ocr_module, "is_ocr_available", lambda: False)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "230109 COLONOSCOPY - SCANNED - Jan 9, 2020.TIF"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"tif fixture")
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    quarantine_path = (
        data_dir
        / "quarantine"
        / "_unresolved"
        / "history"
        / ("undated-230109 COLONOSCOPY - SCANNED - Jan 9, 2020.tif")
    )
    assert executions[0].final_state == "quarantined"
    assert quarantine_path.exists()
    assert not source_path.exists()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        audit_message = conn.execute("SELECT message FROM audit_events").fetchone()[0]
    assert "OCR support is not available" in audit_message


def test_execute_routes_quarantines_history_pdf_without_extractable_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "230109 Colonoscopy.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        inbox_orchestrator.import_labs,
        "extract_pdf_text",
        lambda _path: (_ for _ in ()).throw(
            inbox_orchestrator.import_labs.LabSyncError("OCR dependencies are missing")
        ),
    )
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    quarantine_path = (
        data_dir / "quarantine" / "_unresolved" / "history" / ("undated-230109 Colonoscopy.pdf")
    )
    assert executions[0].final_state == "quarantined"
    assert quarantine_path.exists()
    assert not source_path.exists()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        audit_message = conn.execute("SELECT message FROM audit_events").fetchone()[0]
    assert "OCR dependencies are missing" in audit_message


def test_execute_routes_persists_canonical_medications_for_parseable_supplement_list(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_alice', 'Dummy Alice')")
        conn.commit()
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "Dummy Alice Supplements"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "Dummy Alice Supplements\n\nCurrent:\n\nD3 2000 IU\nB12 1000 mcg\nSaw Palmetto\n\n"
        "Paused 04/27/26:\n\nL-Carnitine 250 mg\n",
        encoding="utf-8",
    )
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = (
        data_dir / "processed" / "dummy_alice" / "Medications" / "undated-Dummy Alice Supplements"
    )
    assert executions[0].final_state == "processed"
    assert processed_path.exists()
    assert not source_path.exists()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT medication_name, medication_type, status FROM medications "
            "WHERE person_id = 'dummy_alice' ORDER BY medication_name"
        ).fetchall()
        ingestion_source_types = {
            row[0] for row in conn.execute("SELECT source_type FROM ingestion_log")
        }
    assert ("B12", "supplement", "active") in rows
    assert ("D3", "supplement", "active") in rows
    assert ("L-Carnitine", "supplement", "discontinued") in rows
    assert ("Saw Palmetto", "supplement", "active") in rows
    assert "supplement_list" in ingestion_source_types
    report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
    assert report_path.exists()
    report_content = report_path.read_text(encoding="utf-8")
    # Report is now a flat alphabetical table (no category grouping)
    assert "# Current Medications" in report_content
    assert "D3" in report_content
    # Discontinued items must not appear in the active report
    assert "L-Carnitine" not in report_content


def test_execute_routes_falls_back_to_placeholder_when_supplement_unparseable(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "Dummy Alice Supplements"
    source_path.parent.mkdir(parents=True)
    # No dose patterns → parser yields zero facts → placeholder fallback
    source_path.write_text("Dummy Alice Supplements\n\nFree-text musings only.\n", encoding="utf-8")
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = (
        data_dir / "processed" / "dummy_alice" / "Medications" / "undated-Dummy Alice Supplements"
    )
    note_path = (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Medications"
        / "undated-Dummy Alice Supplements-placeholder.md"
    )
    assert executions[0].final_state == "processed"
    assert processed_path.exists()
    note_content = note_path.read_text(encoding="utf-8")
    assert "record_type: category_placeholder" in note_content


def test_execute_routes_creates_multi_category_placeholders_for_one_source(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "visits" / "visit-summary.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Visit changed medication plan.\n", encoding="utf-8")
    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="history,medications",
    )

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)
    ledger_path = append_inbox_state_ledger(tmp_path, executions)

    processed_path = data_dir / "processed" / "dummy_alice" / "Mixed" / "undated-visit-summary.txt"
    history_note = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "History"
        / "undated-visit-summary-placeholder.md"
    )
    medication_note = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Medications"
        / "undated-visit-summary-placeholder.md"
    )
    assert executions[0].final_state == "processed"
    assert processed_path.exists()
    assert not source_path.exists()
    for note_path in (history_note, medication_note):
        note_content = note_path.read_text(encoding="utf-8")
        assert "record_type: category_placeholder" in note_content
        assert (
            "source_path: data/processed/dummy_alice/Mixed/undated-visit-summary.txt"
            in note_content
        )
    assert "[[undated-visit-summary-placeholder]]" in (history_note.parent / "_index.md").read_text(
        encoding="utf-8"
    )
    assert "[[undated-visit-summary-placeholder]]" in (
        medication_note.parent / "_index.md"
    ).read_text(encoding="utf-8")
    ledger_row = ledger_path.read_text(encoding="utf-8").splitlines()[1].split("\t")
    assert ledger_row[2:10] == [
        "shared/visits/visit-summary.txt",
        "shared",
        "dummy_alice",
        "history,medications",
        "routed",
        "multi_category_placeholder",
        "executed",
        "processed",
    ]
    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute(
            "SELECT source_path, status, message FROM ingestion_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT event_type, subject_type, source_path, status
            FROM audit_events
            ORDER BY occurred_at DESC LIMIT 1
            """
        ).fetchone()
    assert log_row[0:2] == (
        "data/processed/dummy_alice/Mixed/undated-visit-summary.txt",
        "processed",
    )
    assert "Records/dummy_alice/History/undated-visit-summary-placeholder.md" in log_row[2]
    assert "Records/dummy_alice/Medications/undated-visit-summary-placeholder.md" in log_row[2]
    assert audit_row == (
        "placeholder_import_accepted",
        "multi_category_placeholder",
        "data/processed/dummy_alice/Mixed/undated-visit-summary.txt",
        "processed",
    )


def test_append_inbox_state_ledger_records_route_outcomes(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    routed_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    shared_path = inbox_root / "shared" / "labs" / "panel.pdf"
    routed_path.parent.mkdir(parents=True)
    shared_path.parent.mkdir(parents=True)
    routed_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")
    shared_path.write_text("pdf fixture", encoding="utf-8")
    routes = [
        inspect_inbox_file(routed_path, inbox_root),
        inspect_inbox_file(shared_path, inbox_root),
    ]
    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=False)

    ledger_path = append_inbox_state_ledger(tmp_path, executions)

    ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert ledger_lines[0].split("\t") == [
        "recorded_at",
        "source_path",
        "relative_path",
        "scope",
        "person_id",
        "category",
        "route_status",
        "handler",
        "action",
        "final_state",
        "exit_code",
        "reason",
        "message",
    ]
    assert len(ledger_lines) == 3
    routed_row = ledger_lines[1].split("\t")
    shared_row = ledger_lines[2].split("\t")
    assert routed_row[2:10] == [
        "dummy_alice/labs/panel.csv",
        "person",
        "dummy_alice",
        "labs",
        "routed",
        "import_labs",
        "dry_run",
        "dry_run",
    ]
    assert shared_row[2:10] == [
        "shared/labs/panel.pdf",
        "shared",
        "",
        "labs",
        "awaiting_user_decision",
        "",
        "skipped",
        "awaiting_user_decision",
    ]


def test_format_batch_summary_counts_states_and_attention_items(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    routed_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    shared_path = inbox_root / "shared" / "labs" / "panel.pdf"
    routed_path.parent.mkdir(parents=True)
    shared_path.parent.mkdir(parents=True)
    routed_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")
    shared_path.write_text("pdf fixture", encoding="utf-8")
    routes = [
        inspect_inbox_file(routed_path, inbox_root),
        inspect_inbox_file(shared_path, inbox_root),
    ]
    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=False)

    summary = format_batch_summary(executions)

    assert summary == [
        "",
        "Batch summary:",
        "- awaiting_user_decision: 1",
        "- dry_run: 1",
        "Attention needed:",
        "- awaiting_user_decision: shared/labs/panel.pdf - "
        "shared inbox item requires person/category resolution",
    ]


def test_inbox_orchestrator_cli_writes_ledger(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")

    monkeypatch.setattr(inbox_orchestrator, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["inbox_orchestrator"])

    assert inbox_orchestrator.main() == 0

    ledger_path = tmp_path / "data" / "inbox_state.tsv"
    assert ledger_path.exists()
    assert "dummy_alice/labs/panel.csv" in ledger_path.read_text(encoding="utf-8")


def test_inbox_orchestrator_cli_resolves_shared_lab_route(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "shared" / "labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("test_name,observed_at\nA1c,2023-04-20\n", encoding="utf-8")

    monkeypatch.setattr(inbox_orchestrator, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inbox_orchestrator",
            "--resolve-person",
            "dummy_alice",
            "--resolve-category",
            "labs",
        ],
    )

    assert inbox_orchestrator.main() == 0

    captured = capsys.readouterr()
    assert "routed\tdry_run\timport_labs\tdummy_alice\tlabs\tshared/labs/panel.csv" in captured.out
    assert "Batch summary:\n- dry_run: 1" in captured.out


def test_inbox_orchestrator_cli_uses_resolution_file(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    first_path = inbox_root / "shared" / "visits" / "summary.txt"
    second_path = inbox_root / "shared" / "medications" / "list.txt"
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    first_path.write_text("visit summary\n", encoding="utf-8")
    second_path.write_text("med list\n", encoding="utf-8")
    resolution_path = tmp_path / "resolutions.tsv"
    resolution_path.write_text(
        "relative_path\tperson_id\tcategory\n"
        "shared/visits/summary.txt\tdummy_alice\thistory,medications\n"
        "shared/medications/list.txt\tdummy_bob\tmedications\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(inbox_orchestrator, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inbox_orchestrator",
            "--resolution-file",
            str(resolution_path),
        ],
    )

    assert inbox_orchestrator.main() == 0

    captured = capsys.readouterr()
    assert (
        "routed\tdry_run\tmedication_fact_extract\tdummy_bob\tmedications\tshared/medications/list.txt"
    ) in captured.out
    assert (
        "routed\tdry_run\tmulti_category_placeholder\tdummy_alice\thistory,medications\t"
        "shared/visits/summary.txt"
    ) in captured.out
    assert "Batch summary:\n- dry_run: 2" in captured.out


def test_execute_routes_processes_person_scoped_medication_csv(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "list.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "medication_name,dose,frequency,start_date,medication_type\n"
        "Omeprazole,20 mg,daily,2023-04-01,prescription\n",
        encoding="utf-8",
    )

    route = inspect_inbox_file(source_path, inbox_root)
    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = data_dir / "processed" / "dummy_alice" / "Medications" / "2023-04-01-list.csv"
    report_path = (
        tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
    )
    assert executions[0].final_state == "processed"
    assert executions[0].exit_code == 0
    assert "medication CSV note" in executions[0].message
    assert processed_path.exists()
    assert report_path.exists()
    assert not source_path.exists()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT medication_name, medication_type, dose, frequency, start_date, source_path
            FROM medications
            WHERE person_id = 'dummy_alice'
            """
        ).fetchall()
    assert rows == [
        (
            "Omeprazole",
            "prescription",
            "20 mg",
            "daily",
            "2023-04-01",
            "data/processed/dummy_alice/Medications/2023-04-01-list.csv",
        )
    ]


def test_execute_routes_processes_resolved_shared_medication_csv(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "shared" / "medications" / "list.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("medication,dose\nOmeprazole,20 mg\n", encoding="utf-8")

    route = inspect_inbox_file_with_resolution(
        source_path,
        inbox_root,
        resolve_person="dummy_alice",
        resolve_category="medications",
    )
    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    processed_path = data_dir / "processed" / "dummy_alice" / "Medications" / "undated-list.csv"
    assert executions[0].final_state == "processed"
    assert executions[0].exit_code == 0
    assert processed_path.exists()
    assert not source_path.exists()


def test_execute_routes_quarantines_medication_csv_with_invalid_start_date(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "list.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "medication_name,start_date\nOmeprazole,April 1/2026\n",
        encoding="utf-8",
    )

    route = inspect_inbox_file(source_path, inbox_root)
    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    quarantine_path = data_dir / "quarantine" / "dummy_alice" / "Medications" / "undated-list.csv"
    assert executions[0].final_state == "quarantined"
    assert executions[0].exit_code == 0
    assert quarantine_path.exists()
    assert not source_path.exists()


def test_execute_routes_skips_pdf_without_noninteractive_approval(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"
    source_path = inbox_root / "dummy_alice" / "labs" / "panel.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("pdf fixture", encoding="utf-8")
    routes = [inspect_inbox_file(source_path, inbox_root)]

    executions = execute_routes(routes, root=tmp_path, inbox_root=inbox_root, execute=True)

    assert executions[0].action == "skipped"
    assert executions[0].exit_code is None
    assert "--approve-pdf" in executions[0].message
    assert source_path.exists()


# ── Fact extraction orchestrator wiring ─────────────────────────────────────


def _fact_extraction_payload(
    *,
    person_id: str = "dummy_alice",
    confidence: float = 0.92,
    test_name: str = "Glucose",
    value: str = "102",
    unit: str = "mg/dL",
    observed_at: str = "2023-04-01",
) -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": confidence},
        "category": "labs",
        "category_confidence": confidence,
        "facts": [
            {
                "test_name": test_name,
                "value": value,
                "unit": unit,
                "observed_at": observed_at,
                "confidence": confidence,
            }
        ],
        "overall_confidence": confidence,
        "evidence": [{"field": "patient_name", "snippet": "Test Person", "confidence": 0.95}],
        "document_date": observed_at,
    }


def _medication_fact_extraction_payload(
    *,
    person_id: str = "dummy_alice",
    confidence: float = 0.92,
    medication_type: str | None = None,
    facts: list[dict[str, Any]] | None = None,
    document_date: str = "2023-04-01",
) -> dict[str, Any]:
    fact_rows = facts
    if fact_rows is None:
        fact: dict[str, Any] = {
            "name": "Omeprazole",
            "dose": "20 mg",
            "frequency": "daily",
            "status": "active",
            "start_date": "2023-04-01",
            "confidence": confidence,
            "evidence_snippet": "started omeprazole 20 mg daily",
        }
        if medication_type is not None:
            fact["medication_type"] = medication_type
        fact_rows = [fact]
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": confidence},
        "category": "medications",
        "category_confidence": confidence,
        "facts": fact_rows,
        "overall_confidence": confidence,
        "evidence": [{"field": "medication", "snippet": "omeprazole", "confidence": 0.95}],
        "document_date": document_date,
    }


def _history_fact_extraction_payload(
    *,
    person_id: str = "dummy_alice",
    confidence: float = 0.92,
    facts: list[dict[str, Any]] | None = None,
    document_date: str = "2023-04-01",
) -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": confidence},
        "category": "history",
        "category_confidence": confidence,
        "facts": facts
        or [
            {
                "description": "GERD",
                "category": "diagnosis",
                "icd_10_code": "K21.9",
                "recorded_date": document_date,
                "confidence": confidence,
                "evidence_snippet": "Assessment: GERD - K21.9",
            }
        ],
        "overall_confidence": confidence,
        "evidence": [{"field": "assessment", "snippet": "GERD", "confidence": 0.95}],
        "document_date": document_date,
    }


def _vitals_fact_extraction_payload(
    *,
    person_id: str = "dummy_alice",
    confidence: float = 0.92,
) -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": confidence},
        "category": "vitals",
        "category_confidence": confidence,
        "facts": [
            {
                "vital_name": "temperature",
                "value": "98.6",
                "unit": "F",
                "observed_at": "2023-04-01",
                "confidence": confidence,
                "evidence_snippet": "Temperature: 98.6 F",
            },
            {
                "vital_name": "pulse",
                "value": "72",
                "unit": "bpm",
                "observed_at": "2023-04-01",
                "confidence": confidence,
                "evidence_snippet": "Pulse: 72 bpm",
            },
        ],
        "overall_confidence": confidence,
        "evidence": [
            {"field": "temperature", "snippet": "Temperature: 98.6 F", "confidence": 0.95}
        ],
        "document_date": "2023-04-01",
    }


def test_execute_routes_creates_placeholder_for_prescription_text_without_fact_extractor(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "famed.db") as conn:
        conn.executescript(SCHEMA_SQL)
    inbox_root = data_dir / "inbox"
    source_path = inbox_root / "dummy_alice" / "medications" / "current-meds.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Omeprazole 20 mg daily\n", encoding="utf-8")
    route = inspect_inbox_file(source_path, inbox_root)

    executions = execute_routes([route], root=tmp_path, inbox_root=inbox_root, execute=True)

    placeholder_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Medications"
        / "undated-current-meds-placeholder.md"
    )
    assert executions[0].final_state == "processed"
    assert "placeholder note" in executions[0].message
    note_content = placeholder_path.read_text(encoding="utf-8")
    assert "record_type: category_placeholder" in note_content
    assert (
        "source_path: data/processed/dummy_alice/Medications/undated-current-meds.txt"
        in note_content
    )


def _make_person_note(vault_root: Path, person_id: str, display_name: str) -> None:
    persons_dir = vault_root / "Persons"
    persons_dir.mkdir(parents=True, exist_ok=True)
    note = persons_dir / f"{person_id}.md"
    note.write_text(
        f"---\nperson_id: {person_id}\ndisplay_name: {display_name}\n"
        f"birth_year: 1990\nsex: female\nrelationships: []\n---\n\n"
        f"# {display_name}\n\n## Records\n\n## Notes\n",
        encoding="utf-8",
    )


def _patch_input(monkeypatch: pytest.MonkeyPatch, lines: str) -> None:
    stream = io.StringIO(lines)

    def _readline(*_args: object, **_kwargs: object) -> str:
        result = stream.readline()
        if not result:
            raise EOFError
        return result.rstrip("\n")

    monkeypatch.setattr(builtins, "input", _readline)


class TestResolvePersonInteractive:
    def test_cold_start_creates_new_person(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault_root = tmp_path / "vault"
        inbox_root = tmp_path / "inbox"
        db_path = tmp_path / "data" / "famed.db"
        db_path.parent.mkdir(parents=True)

        from famed.setup import SCHEMA_SQL as schema

        conn = sqlite3.connect(str(db_path))
        conn.executescript(schema)
        conn.close()

        source_file = tmp_path / "test.txt"
        source_file.write_text("Patient: John Doe", encoding="utf-8")

        _patch_input(
            monkeypatch,
            "y\n"  # "Create new person?"
            "John Doe\n"  # display name
            "1990\n"  # birth year
            "male\n",  # sex
        )

        result = resolve_person_interactive(
            source_file,
            "Patient: John Doe",
            root=tmp_path,
            vault_root=vault_root,
            inbox_root=inbox_root,
        )

        assert result == "john-doe"
        note_path = vault_root / "Persons" / "john-doe.md"
        assert note_path.exists()
        assert "display_name: John Doe" in note_path.read_text()

    def test_cold_start_cancel_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault_root = tmp_path / "vault"
        inbox_root = tmp_path / "inbox"
        source_file = tmp_path / "test.txt"
        source_file.write_text("test", encoding="utf-8")

        _patch_input(monkeypatch, "n\n")

        result = resolve_person_interactive(
            source_file,
            "test",
            root=tmp_path,
            vault_root=vault_root,
            inbox_root=inbox_root,
        )

        assert result is None

    def test_select_existing_person(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        vault_root = tmp_path / "vault"
        inbox_root = tmp_path / "inbox"
        _make_person_note(vault_root, "dummy_alice", "Dummy Alice Smith")
        _make_person_note(vault_root, "dummy_bob", "Dummy Bob Jones")

        source_file = tmp_path / "test.txt"
        source_file.write_text("Report for Dummy Alice", encoding="utf-8")

        _patch_input(monkeypatch, "1\n")

        result = resolve_person_interactive(
            source_file,
            "Report for Dummy Alice",
            root=tmp_path,
            vault_root=vault_root,
            inbox_root=inbox_root,
        )

        assert result == "dummy_alice"

    def test_create_new_person_with_relationship(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault_root = tmp_path / "vault"
        inbox_root = tmp_path / "inbox"
        db_path = tmp_path / "data" / "famed.db"
        db_path.parent.mkdir(parents=True)

        from famed.setup import SCHEMA_SQL as schema

        conn = sqlite3.connect(str(db_path))
        conn.executescript(schema)
        conn.close()

        _make_person_note(vault_root, "dummy_alice", "Dummy Alice Smith")

        source_file = tmp_path / "test.txt"
        source_file.write_text("Report for Dummy Charlie", encoding="utf-8")

        _patch_input(
            monkeypatch,
            "2\n"  # "Create new person"
            "Dummy Charlie Brown\n"  # display name
            "1992\n"  # birth year
            "male\n"  # sex
            "spouse\n",  # relationship to Dummy Alice
        )

        result = resolve_person_interactive(
            source_file,
            "Report for Dummy Charlie",
            root=tmp_path,
            vault_root=vault_root,
            inbox_root=inbox_root,
        )

        assert result == "dummy-charlie-brown"
        note_path = vault_root / "Persons" / "dummy-charlie-brown.md"
        assert note_path.exists()
        content = note_path.read_text()
        assert "display_name: Dummy Charlie Brown" in content

        dummy_alice_note = vault_root / "Persons" / "dummy_alice.md"
        dummy_alice_content = dummy_alice_note.read_text()
        assert "dummy-charlie-brown" in dummy_alice_content

    def test_cancel_existing_person_menu(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault_root = tmp_path / "vault"
        inbox_root = tmp_path / "inbox"
        _make_person_note(vault_root, "dummy_alice", "Dummy Alice")

        source_file = tmp_path / "test.txt"
        source_file.write_text("test", encoding="utf-8")

        _patch_input(monkeypatch, "3\n")  # Cancel

        result = resolve_person_interactive(
            source_file,
            "test",
            root=tmp_path,
            vault_root=vault_root,
            inbox_root=inbox_root,
        )

        assert result is None


class TestExtractTextForPersonResolution:
    def test_extracts_pdf_text(self, tmp_path: Path) -> None:
        import importlib.util

        if importlib.util.find_spec("pypdf") is None:
            pytest.skip("pypdf not installed")
        try:
            from pypdf import PdfWriter
        except ImportError:
            pytest.skip("pypdf not installed")

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        pdf_path = tmp_path / "test.pdf"
        with pdf_path.open("wb") as f:
            writer.write(f)
        result = _extract_text_for_person_resolution(pdf_path)
        assert not result.startswith("%PDF")

    def test_extracts_txt_text(self, tmp_path: Path) -> None:
        path = tmp_path / "test.txt"
        path.write_text("Hello World", encoding="utf-8")
        result = _extract_text_for_person_resolution(path)
        assert result == "Hello World"

    def test_extracts_html_text(self, tmp_path: Path) -> None:
        path = tmp_path / "test.html"
        path.write_text("<html><body>Patient: John Doe</body></html>", encoding="utf-8")
        result = _extract_text_for_person_resolution(path)
        assert "Patient: John Doe" in result

    def test_image_without_ocr_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import famed.ocr as ocr_module

        monkeypatch.setattr(ocr_module, "is_ocr_available", lambda: False)
        path = tmp_path / "test.png"
        path.write_bytes(b"\x89PNG\r\n")
        result = _extract_text_for_person_resolution(path)
        assert result == ""
