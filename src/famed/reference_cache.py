"""External reference data cache manager.

Downloads non-credentialed medical terminology datasets and detects manually
placed datasets in the untracked ``data/reference/`` cache directory.
Maintains a version manifest for idempotent downloads and stale detection.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from famed.storage_contract import configured_storage_roots

MANIFEST_FILENAME = "manifest.json"
HTTP_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 2
RXNAV_PAGE_SIZE = 1000
RXNAV_PAGE_DELAY = 0.1
LOINC_PARSED_FILENAME = "loinc_concepts.jsonl"

STATUS_OK = "OK"
STATUS_MISSING = "MISSING"
STATUS_STALE = "STALE"
STATUS_UNAVAILABLE = "UNAVAILABLE"

SUPPORTED_DATASETS: dict[str, dict[str, Any]] = {
    "rxnorm": {
        "display_name": "RxNorm",
        "description": "Medication concept vocabulary for RxNorm CUI mapping",
        "downloadable": True,
        "version_url": "https://rxnav.nlm.nih.gov/REST/version?output=json",
        "tty_list": [
            {"tty": "IN", "label": "ingredients", "file": "rxnorm_ingredients.jsonl"},
            {"tty": "SCD", "label": "clinical_drugs", "file": "rxnorm_clinical_drugs.jsonl"},
            {"tty": "SBD", "label": "branded_drugs", "file": "rxnorm_branded_drugs.jsonl"},
        ],
    },
    "loinc": {
        "display_name": "LOINC",
        "description": "Laboratory observation codes for LOINC mapping",
        "downloadable": False,
        "version_url": None,
    },
    "cpic": {
        "display_name": "CPIC",
        "description": (
            "Pharmacogenomics guidelines (gene-drug pairs, stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
    "cpic_alleles": {
        "display_name": "CPIC Alleles",
        "description": (
            "Star allele SNP definitions for pharmacogenes (stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
    "cpic_pairs": {
        "display_name": "CPIC Pairs",
        "description": (
            "CPIC A/B drug-gene pairs from pair_view, including guidelines without "
            "per-phenotype recommendations (e.g. warfarin) (stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
    "cpic_diplotypes": {
        "display_name": "CPIC Diplotypes",
        "description": (
            "Exact CPIC diplotype-to-gene-result translations (stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
    "clinvar": {
        "display_name": "ClinVar",
        "description": (
            "Variant pathogenicity classifications from NCBI (stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
    "gnomad": {
        "display_name": "gnomAD",
        "description": (
            "Targeted global population frequencies for the public ClinVar variant set "
            "(stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
    "gencc": {
        "display_name": "GenCC (ClinGen inheritance)",
        "description": (
            "ClinGen-origin gene-disease inheritance assertions from GenCC "
            "(stored in famed-reference.db)"
        ),
        "downloadable": True,
        "version_url": None,
        "annotation_db": True,
    },
}


def _cache_root(repo_root: Path | None = None) -> Path:
    if repo_root is None:
        root = _find_repo_root()
    else:
        root = repo_root
    return configured_storage_roots(root).reference


def _find_repo_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists():
        return candidate
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return cwd


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / MANIFEST_FILENAME


def read_manifest(cache_dir: Path) -> dict[str, Any]:
    path = _manifest_path(cache_dir)
    if not path.exists():
        return {"datasets": {}}
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


def write_manifest(cache_dir: Path, manifest: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(cache_dir)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _http_request(url: str, headers: dict[str, str] | None = None) -> bytes:
    req_headers: dict[str, str] = {
        "User-Agent": "famed-reference-cache/0.1",
    }
    if headers:
        req_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts: {last_error}")


def _fetch_json(url: str, headers: dict[str, str] | None = None) -> Any:
    json_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        json_headers.update(headers)
    body = _http_request(url, headers=json_headers).decode("utf-8")
    return json.loads(body)


def _compute_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rxnorm_version() -> str | None:
    try:
        data = _fetch_json("https://rxnav.nlm.nih.gov/REST/version?output=json")
        return str(data.get("version", ""))
    except Exception:
        return None


def _rxnorm_fetch_tty_page(tty: str, page_num: int) -> dict[str, Any]:
    url = (
        f"https://rxnav.nlm.nih.gov/REST/allconcepts?tty={tty}"
        f"&output=json&pageSize={RXNAV_PAGE_SIZE}&pageNum={page_num}"
    )
    return _fetch_json(url)


def _rxnorm_page_concepts(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Return RxNorm concept rows from either supported API response shape."""
    group = page.get("minConceptGroup", {})
    if group:
        return group.get("minConcept", [])
    group = page.get("allConcepts", {}).get("allConceptGroup", {})
    return group.get("allConceptProperties", [])


def _rxnorm_last_page_reached(page: dict[str, Any], page_num: int) -> bool:
    page_info = page.get("allConcepts", {}).get("pageInfo", {}) or page.get("pageInfo", {})
    try:
        total_pages = int(page_info["totalPages"])
    except (KeyError, TypeError, ValueError):
        return False
    return page_num >= total_pages


def download_rxnorm(cache_dir: Path) -> str:
    dataset_dir = cache_dir / "rxnorm"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    version = _rxnorm_version()
    if not version:
        version = datetime.now(UTC).strftime("%Y%m%d")

    total_concepts: list[dict[str, Any]] = []
    for tty_entry in SUPPORTED_DATASETS["rxnorm"]["tty_list"]:
        tty = tty_entry["tty"]
        page_num = 1
        while True:
            page = _rxnorm_fetch_tty_page(tty, page_num)
            concepts = _rxnorm_page_concepts(page)
            if not concepts:
                break
            total_concepts.extend(concepts)
            if _rxnorm_last_page_reached(page, page_num):
                break
            page_num += 1
            time.sleep(RXNAV_PAGE_DELAY)

    lines = [
        json.dumps({"rxcui": c.get("rxcui"), "name": c.get("name"), "tty": c.get("tty")})
        for c in total_concepts
    ]
    rxnorm_file = dataset_dir / "rxnorm_concepts.jsonl"
    rxnorm_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return version


def _find_loinc_csv(cache_dir: Path) -> Path | None:
    dataset_dir = cache_dir / "loinc"
    if not dataset_dir.is_dir():
        return None
    candidates = sorted(
        path
        for path in dataset_dir.rglob("*")
        if path.is_file() and path.name.lower() == "loinc.csv"
    )
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.parent.name.lower() == "loinctable":
            return candidate
    return candidates[0]


def _loinc_version_from_path(path: Path) -> str | None:
    for part in path.parts:
        match = re.fullmatch(r"Loinc_(\d+(?:\.\d+)*)", part, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_loinc_csv(cache_dir: Path) -> str | None:
    loinc_csv = _find_loinc_csv(cache_dir)
    if loinc_csv is None:
        raise FileNotFoundError(
            "LOINC CSV not found. Download LOINC from https://loinc.org/downloads/ "
            "and place Loinc.csv under data/reference/loinc/"
        )

    dataset_dir = cache_dir / "loinc"
    parsed_file = dataset_dir / LOINC_PARSED_FILENAME
    version = _loinc_version_from_path(loinc_csv)

    row_count = 0
    with loinc_csv.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "LOINC_NUM" not in reader.fieldnames:
            raise ValueError(f"{loinc_csv} does not look like a LOINC table CSV")

        with parsed_file.open("w", encoding="utf-8") as target:
            for row in reader:
                loinc_num = (row.get("LOINC_NUM") or "").strip()
                if not loinc_num:
                    continue
                record = {
                    "loinc_num": loinc_num,
                    "component": (row.get("COMPONENT") or "").strip(),
                    "long_common_name": (row.get("LONG_COMMON_NAME") or "").strip(),
                    "class": (row.get("CLASS") or "").strip(),
                    "system": (row.get("SYSTEM") or "").strip(),
                    "scale": (row.get("SCALE_TYP") or "").strip(),
                    "property": (row.get("PROPERTY") or "").strip(),
                    "method": (row.get("METHOD_TYP") or "").strip(),
                    "status": (row.get("STATUS") or "").strip(),
                }
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
                row_count += 1

    if row_count == 0:
        raise ValueError(f"{loinc_csv} contained no LOINC rows")

    manifest = read_manifest(cache_dir)
    checksum = _compute_sha256_bytes(parsed_file.read_bytes())
    source_file = str(loinc_csv.relative_to(cache_dir))
    manifest.setdefault("datasets", {})["loinc"] = {
        "version": version,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "source_url": "manual:https://loinc.org/downloads/",
        "checksum_sha256": checksum,
        "files": [source_file, f"loinc/{LOINC_PARSED_FILENAME}"],
        "row_count": row_count,
    }
    write_manifest(cache_dir, manifest)
    return version


_NORMALIZE_RE = re.compile(r"[\s\-_/,.;:()\[\]%]+")
_REVIEWED_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PARENTHETICAL_RE = re.compile(r"\([^)]*\)")


def _normalize_reference_key(name: str) -> str:
    without_parens = _PARENTHETICAL_RE.sub(" ", name)
    return _NORMALIZE_RE.sub(" ", without_parens).strip().lower()


# Words stripped from start/end of test names during multi-pass LOINC lookup.
# Handles common PDF-extracted variants like "Auto WBC" → "wbc", "Hemoglobin, POC" → "hemoglobin".
_COMMON_AFFIX_WORDS: frozenset[str] = frozenset(
    {
        "abs",
        "afr",
        "am",
        "auto",
        "automated",
        "manual",
        "calc",
        "calculated",
        "cal",
        "cardiac",
        "confirmatory",
        "count",
        "cv",
        "direct",
        "fasting",
        "if",
        "lc",
        "metabolites",
        "nih",
        "poc",
        "random",
        "screen",
        "sd",
        "sgpt",
        "sgot",
        "total",
    }
)


def _strip_common_affixes(key: str) -> str:
    words = key.split()
    while words and words[0] in _COMMON_AFFIX_WORDS:
        words.pop(0)
    while words and words[-1] in _COMMON_AFFIX_WORDS:
        words.pop()
    return " ".join(words)


# Maps common clinical abbreviations to their normalized LOINC component names.
# Entries are injected into the LOINC index at load time when the target key is
# present; aliases never overwrite existing exact-name entries.
_LOINC_SYNONYMS: dict[str, str] = {
    "25 hydroxy vitamin d": "calcidiol",
    "25 oh vitamin d": "calcidiol",
    "a1c": "hemoglobin a1c",
    "a g ratio": "albumin globulin",
    "albumin globulin ratio": "albumin globulin",
    "ana ifa": "nuclear ab presence in serum by immunofluorescence",
    "ana screen": "nuclear ab presence in serum by immunofluorescence",
    "ana screen ifa": "nuclear ab presence in serum by immunofluorescence",
    "alk phosphatase": "alkaline phosphatase",
    "alt": "alanine aminotransferase",
    "amphetamines screen urine": "amphetamines presence in urine by screen method",
    "ast": "aspartate aminotransferase",
    "barbiturates screen urine": "barbiturates presence in urine by screen method",
    "baso abs": "basophils # volume in blood by automated count",
    "basophils absolute": "basophils # volume in blood by automated count",
    "absolute basophils": "basophils # volume in blood by automated count",
    "basos": "basophils",
    "benzodiazepines screen urine": "benzodiazepines panel presence urine by screen method",
    "buprenorphine urine": "buprenorphine presence in urine by screen method",
    "bili total": "bilirubin total",
    "bun": "urea nitrogen",
    "bun creat ratio": "urea nitrogen creatinine mass ratio in serum or plasma",
    "bun creatinine ratio": "urea nitrogen creatinine mass ratio in serum or plasma",
    "cannabinoid screen urine": "cannabinoids presence in urine by screen method",
    "carboxy thc confirmatory urine": (
        "carboxy tetrahydrocannabinol mass volume in urine by confirmatory method"
    ),
    "ch hd": "cholesterol total cholesterol in hdl",
    "co2": "carbon dioxide",
    "cocaine metabolites screen urine": (
        "cocaine+benzoylecgonine presence in urine by screen method"
    ),
    "creat": "creatinine",
    "crp": "c reactive protein",
    "dheas": "dhea s",
    "eag": "estimated average glucose",
    "egfr": "glomerular filtration rate",
    "eos": "eosinophils",
    "eos abs": "eosinophils # volume in blood by automated count",
    "eosinophils absolute": "eosinophils # volume in blood by automated count",
    "absolute eosinophils": "eosinophils # volume in blood by automated count",
    "fecal occult blood": "hemoglobin presence in stool from gastrointestinal",
    "fentanyl urine": "fentanyl presence in urine by screen method",
    "free psa": "prostate specific ag free",
    "free t3": "triiodothyronine free",
    "free t4": "thyroxine free",
    "free testosterone": "testosterone free",
    "gfr": "glomerular filtration rate",
    "glucose random": "glucose",
    "hb": "hemoglobin",
    "hba1c": "hemoglobin a1c",
    "hct": "hematocrit volume fraction of blood by automated count",
    "hdl": "cholesterol in hdl",
    "hdl c": "cholesterol in hdl",
    "hematocrit": "hematocrit volume fraction of blood by automated count",
    "hepatitis c ab": "hepatitis c virus ab",
    "hepatitis c antibody": "hepatitis c virus ab",
    "hgb": "hemoglobin",
    "hgba1c": "hemoglobin a1c",
    "immature granulocytes": "immature granulocytes # volume in blood by automated count",
    "immature granulocytes absolute": "immature granulocytes # volume in blood by automated count",
    "ionized calcium": "calcium ionized",
    "iron total": "iron",
    "ldl": "cholesterol in ldl",
    "ldl c": "cholesterol in ldl",
    "ldl calculated": "cholesterol in ldl",
    "lipase": "triacylglycerol lipase",
    "lymphocytes absolute": "lymphocytes # volume in blood by automated count",
    "absolute lymphocytes": "lymphocytes # volume in blood by automated count",
    "lymphs": "lymphocytes",
    "lymphs abs": "lymphocytes # volume in blood by automated count",
    "mch": "mch entitic mass by automated count",
    "mchc": "mchc entitic mass volume in red blood cells by automated count",
    "mcv": "mcv entitic mean volume in red blood cells by automated count",
    "meperidine screen urine": "meperidine presence in urine by screen method",
    "methadone screen urine": "methadone presence in urine by screen method",
    "monocytes absolute": "monocytes # volume in blood by automated count",
    "absolute monocytes": "monocytes # volume in blood by automated count",
    "monocytesabs": "monocytes # volume in blood by automated count",
    "mpv": "platelet mean volume",
    "neutrophils absolute": "neutrophils # volume in blood by automated count",
    "absolute neutrophils": "neutrophils # volume in blood by automated count",
    "non hdl chol": "cholesterol non hdl",
    "nrbc": "nucleated erythrocytes leukocytes ratio in blood by automated count",
    "nrbc#": "nucleated erythrocytes # volume in blood by automated count",
    "nrbc auto": "nucleated erythrocytes leukocytes ratio in blood by automated count",
    "nrbc count": "nucleated erythrocytes # volume in blood by automated count",
    "nrbc percent": "nucleated erythrocytes leukocytes ratio in blood by automated count",
    "nt probrain natriuretic peptide": "natriuretic peptide b prohormone n terminal",
    "opiate screen urine": "opiates presence in urine by screen method",
    "opiates screen urine": "opiates presence in urine by screen method",
    "oxycodone oxymorphone screen urine": (
        "oxycodone+oxymorphone presence in urine by screen method"
    ),
    "phencyclidine screen urine": "phencyclidine presence in urine by screen method",
    "phosphorus": "phosphate",
    "platelet count": "platelets",
    "plt": "platelets",
    # POCT urinalysis: MyChart three-line format stores "POCT <Test>, UA" as the
    # test name. Targets use urine-specimen long_common_name keys to avoid
    # resolving to serum-specimen equivalents.
    "poct bilirubin ua": "bilirubin total mass volume in urine",
    "poct blood ua": "hemoglobin presence in urine by test strip",
    "poct clarity ua": "clarity of urine",
    "poct color ua": "color of urine",
    "poct glucose ua": "glucose mass volume in urine",
    "poct ketones ua": "ketones mass volume in urine",
    "poct leukocytes ua": "leukocyte esterase presence in urine by test strip",
    "poct leukocyte esterase ua": "leukocyte esterase presence in urine by test strip",
    "poct nitrite ua": "nitrite presence in urine",
    "poct ph ua": "ph of urine",
    "poct protein ua": "protein mass volume in urine",
    "poct spec grav ua": "specific gravity of urine",
    "poct urobilinogen ua": "urobilinogen mass volume in urine",
    "protein total": "protein",
    "propoxyphene screen urine": "propoxyphene presence in urine by screen method",
    "prostate specific antigen": "prostate specific ag",
    "prostate specific ag serum": "prostate specific ag mass volume in serum or plasma",
    "psa": "prostate specific ag",
    "psa free": "prostate specific ag free mass volume in serum or plasma",
    "rbc": "erythrocytes",
    "rdw": "erythrocyte distribution width",
    "red blood cell count": "erythrocytes",
    "serum iron": "iron",
    "t3 free": "triiodothyronine free",
    "t3 total": "triiodothyronine",
    "t4 free": "thyroxine free",
    "t4 total": "thyroxine",
    "testosterone total": "testosterone",
    "tibc": "iron binding capacity",
    "total bilirubin": "bilirubin total",
    "total protein": "protein",
    "tpo ab": "thyroperoxidase ab",
    "thyroid peroxidase ab": "thyroperoxidase ab",
    "thyroid peroxidase antibody": "thyroperoxidase ab",
    "thyroglobulin antibody": "thyroglobulin ab",
    "tramadol screen urine": "tramadol presence in urine by screen method",
    "troponin i": "troponin i cardiac",
    "troponin i poc": (
        "troponin i cardiac mass volume in serum plasma or blood by rapid immunoassay"
    ),
    "transferrin saturation": "iron saturation",
    "triglycerides": "triglyceride",
    "tsh": "thyrotropin",
    "tsh 3rd generation": "thyrotropin",
    "tsh ultrasensitive": "thyrotropin",
    "uric acid": "urate",
    "vitamin b12": "cobalamins",
    "vitamin d 25 hydroxy": "calcidiol",
    "vitamin d total": "calcidiol",
    "vldl cholesterol": "cholesterol in vldl",
    "wbc": "leukocytes",
    "white blood cell count": "leukocytes",
    "ldl p": "lipoprotein beta subparticle moles volume in serum or plasma",
    "hdl p": "lipoprotein alpha moles volume in serum or plasma",
    "small ldl p": "lipoprotein beta subparticle small moles volume in serum or plasma",
    "ldl size": "lipoprotein beta subparticle entitic length in serum or plasma",
}

_CBC_DIFFERENTIAL_PERCENT_CODE_BY_KEY: dict[str, str] = {
    "basophils": "706-2",
    "basos": "706-2",
    "eos": "713-8",
    "eosinophils": "713-8",
    "lymphocytes": "736-9",
    "lymphs": "736-9",
    "monocytes": "5905-5",
    "neutrophils": "770-8",
    "immature granulocytes": "71695-1",
    "nrbc auto": "58413-6",
    "nrbc per 100 cells": "19048-8",
}

_CBC_DIFFERENTIAL_COUNT_CODE_BY_KEY: dict[str, str] = {
    "absolute basophils": "704-7",
    "absolute eosinophils": "711-2",
    "absolute lymphocytes": "731-0",
    "absolute monocytes": "742-7",
    "absolute neutrophils": "751-8",
    "baso abs": "704-7",
    "basophils absolute": "704-7",
    "eos abs": "711-2",
    "eosinophils absolute": "711-2",
    "lymphocytes absolute": "731-0",
    "lymphs abs": "731-0",
    "monocytes absolute": "742-7",
    "monocytesabs": "742-7",
    "neutrophils absolute": "751-8",
    "neutrophils abs": "751-8",
    "immature granulocytes": "53115-2",
    "nrbc#": "771-6",
    "nrbc auto": "771-6",
}

_CBC_DIFFERENTIAL_ANALYTE_ALIASES: tuple[tuple[str, str], ...] = (
    ("immature granulocytes", "immature granulocytes"),
    ("nrbc", "nrbc"),
    ("neutrophils", "neutrophils"),
    ("lymphocytes", "lymphocytes"),
    ("lymphs", "lymphocytes"),
    ("monocytes", "monocytes"),
    ("eosinophils", "eosinophils"),
    ("eos", "eosinophils"),
    ("basophils", "basophils"),
    ("basos", "basophils"),
    ("baso", "basophils"),
)

_CBC_DIFFERENTIAL_PERCENT_CODE_BY_ANALYTE: dict[str, str] = {
    "basophils": "706-2",
    "eosinophils": "713-8",
    "lymphocytes": "736-9",
    "monocytes": "5905-5",
    "neutrophils": "770-8",
    "immature granulocytes": "71695-1",
}

_CBC_DIFFERENTIAL_COUNT_CODE_BY_ANALYTE: dict[str, str] = {
    "basophils": "704-7",
    "eosinophils": "711-2",
    "lymphocytes": "731-0",
    "monocytes": "742-7",
    "neutrophils": "751-8",
    "immature granulocytes": "53115-2",
    "nrbc": "771-6",
}

_UNIT_DISAMBIGUATED_LOINC_CODES: dict[tuple[str, str], str] = {
    ("albumin", "g/dl"): "1751-7",
    ("cholesterol", "mg/dl"): "2093-3",
    ("free psa", "%"): "12841-3",
    ("free t4", "ng/dl"): "3024-7",
    ("hdl c", "mg/dl"): "2085-9",
    ("hepatitis c ab", "s/co ratio"): "104741-4",
    ("ldl calculated", "mg/dl"): "13457-7",
    # LOINC explicitly maps deprecated duplicate 32711-4 to 32623-1 (ADR-035).
    ("mpv", "fl"): "32623-1",
    ("platelet mean volume", "fl"): "32623-1",
    ("platelet count", "k/ul"): "777-3",
    ("platelet count", "10*3/ul"): "777-3",
    ("platelet count", "thousand/ul"): "777-3",
    ("platelets", "k/ul"): "777-3",
    ("platelets", "10*3/ul"): "777-3",
    ("platelets", "thousand/ul"): "777-3",
    ("prostate specific antigen", "ng/ml"): "2857-1",
    ("psa", "ng/ml"): "2857-1",
    ("rdw", "%"): "788-0",
    ("rdw", "fl"): "21000-5",
    ("rdw cv", "%"): "788-0",
    ("rdw sd", "fl"): "21000-5",
    ("triglycerides", "mg/dl"): "2571-8",
    ("tsh", "uiu/ml"): "3016-3",
}


def _cbc_differential_analyte(key: str) -> str:
    for alias, analyte in _CBC_DIFFERENTIAL_ANALYTE_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", key):
            return analyte
    return ""


def _lookup_cbc_differential_loinc_code(test_name: str, unit: str) -> str:
    raw_name = str(test_name or "").strip()
    if not raw_name:
        return ""
    key = _normalize_reference_key(raw_name)
    if not key:
        return ""

    raw_has_absolute = bool(re.search(r"\babsolute\b|\babs\b", raw_name, re.IGNORECASE))
    unit_key = str(unit or "").strip().casefold()
    analyte = _cbc_differential_analyte(key)
    if raw_has_absolute or unit_key in {"k/ul", "10*3/ul", "thousand/ul"}:
        count_code = _CBC_DIFFERENTIAL_COUNT_CODE_BY_KEY.get(key, "")
        if count_code:
            return count_code
        return _CBC_DIFFERENTIAL_COUNT_CODE_BY_ANALYTE.get(analyte, "")

    if analyte == "nrbc" and "auto" in raw_name.casefold():
        return "58413-6"

    raw_has_percent = "%" in raw_name or "percent" in key or key.startswith("n ")
    if raw_has_percent or key in _CBC_DIFFERENTIAL_PERCENT_CODE_BY_KEY:
        percent_code = _CBC_DIFFERENTIAL_PERCENT_CODE_BY_KEY.get(key, "")
        if percent_code:
            return percent_code
        return _CBC_DIFFERENTIAL_PERCENT_CODE_BY_ANALYTE.get(analyte, "")
    return ""


# Serum/plasma-family specimen systems, most-canonical first, used to break ties
# when several LOINC concepts share a normalized name (ADR-034). A bare analyte
# name on a chemistry panel means the serum/plasma form; hematology analytes have
# no Ser/Plas form, so whole-blood ("Bld") wins among their candidates instead.
_LOINC_SYSTEM_PREFERENCE: dict[str, int] = {
    "ser/plas": 4,
    "ser": 3,
    "plas": 2,
    "bld": 1,
}

# Reviewed, authoritative canonical-LOINC overrides keyed by normalized component
# name (ADR-034). The highest-priority selector: a listed analyte resolves to its
# reviewed code regardless of cache ordering, the ranking heuristic, or synonym
# injection. Bounded to confirmed analytes (ADR-018 spirit); do not grow this into
# a broad keyword list.
#   - ``sodium`` is locked as a regression guard even though the heuristic already
#     resolves it to ``2951-2``.
#   - ``albumin`` and ``cholesterol`` are pinned to the same conventional-units
#     codes the unit-disambiguation lane already treats as canonical
#     (``_UNIT_DISAMBIGUATED_LOINC_CODES``), so the bare-name index fallback cannot
#     disagree with that reviewed path; the heuristic otherwise picks molar-unit
#     serum variants.
_CANONICAL_LOINC_OVERRIDES: dict[str, str] = {
    "sodium": "2951-2",
    "albumin": "1751-7",
    "cholesterol": "2093-3",
    # Every "Glomerular filtration rate" concept is ACTIVE,
    # serum/plasma/blood and quantitative, so they tie on every field
    # `_loinc_canonical_rank` can see -- they differ only by `method`, the
    # estimating equation. Insertion order then won, and the winner was
    # 102097-3: the CKiD equation, which is pediatric and combines creatinine,
    # cystatin C and urea, and is wrong for adults. A source that states only
    # "eGFR" names no equation, so the honest target is 69405-9 ("based on
    # 1.73 sq M", equation-unspecified).
    # Ranking by `method` was measured and rejected: it changes 1,820 unrelated
    # keys across the index and still does not disambiguate this family, since
    # every GFR concept carries a method. This is exactly the reviewed-override
    # escape hatch ADR-034 specifies for what the heuristic cannot decide.
    "glomerular filtration rate": "69405-9",
    # Thyroid antibody concepts tie on every ranked
    # field and differ only by method, so a method-specific variant (RIA 5382-7,
    # IA 56536-6) won. Lab reports name no method; pin the general codes, which
    # also match what source-coded exports already use (8099-4).
    "thyroperoxidase ab": "8099-4",
    "thyroglobulin ab": "8098-6",
    # Correctness audit (ADR-034). Method ties: the general
    # mass-concentration code lost to a method-specific variant (dialysis,
    # immunoassay, LC/MS/MS) that the lab report does not state.
    "triiodothyronine free": "3051-0",
    "ferritin": "2276-4",
    "dehydroepiandrosterone sulfate": "2191-5",
    "testosterone": "2986-8",
    "testosterone total": "2986-8",
    # Name-key misses: the only concept under the key was wrong -- a urinary
    # stone analysis code for serum total bilirubin, and the mass/volume A1c
    # code for results reported as a percentage of total hemoglobin.
    "bilirubin total": "1975-2",
    "hemoglobin a1c": "4548-4",
    # "25-hydroxy vitamin D" names the D2+D3 total; the synonym target
    # "calcidiol" is the D3-only concept.
    "vitamin d 25 hydroxy": "62292-8",
    "25 hydroxy vitamin d": "62292-8",
    "25 oh vitamin d": "62292-8",
    "vitamin d total": "62292-8",
}

# Unit dimension -> LOINC PROPERTY (ADR-034). Only the mass/molar split: these
# are the concept pairs that share a name and differ only by property, and a
# result's unit settles which one it is. Other unit kinds are not mapped.
_MASS_CONCENTRATION_UNITS: frozenset[str] = frozenset(
    {
        "g/dl",
        "mg/dl",
        "ug/dl",
        "mcg/dl",
        "ng/dl",
        "g/l",
        "mg/l",
        "ug/l",
        "mcg/l",
        "ng/ml",
        "pg/ml",
        "ug/ml",
        "mcg/ml",
    }
)
_SUBSTANCE_CONCENTRATION_UNITS: frozenset[str] = frozenset(
    {"mol/l", "mmol/l", "umol/l", "nmol/l", "pmol/l"}
)
_OPPOSITE_PROPERTY: dict[str, str] = {"MCnc": "SCnc", "SCnc": "MCnc"}


def _unit_property(unit: str) -> str:
    compact = "".join(str(unit or "").casefold().split()).strip(",")
    if compact in _MASS_CONCENTRATION_UNITS:
        return "MCnc"
    if compact in _SUBSTANCE_CONCENTRATION_UNITS:
        return "SCnc"
    return ""


def _loinc_canonical_rank(record: dict[str, Any]) -> tuple[int, int, int]:
    """Return a higher-is-more-canonical rank for collision resolution (ADR-034).

    Ranks by the fields the parsed cache carries, in the order: ``STATUS=ACTIVE``,
    then a serum/plasma-family ``SYSTEM``, then quantitative ``SCALE``. The parsed
    cache does not carry LOINC's official ``COMMON_TEST_RANK``/``CLASSTYPE``, so a
    reviewed override (applied separately) is the escape hatch where this loses.
    """
    status = str(record.get("status") or "").strip().upper()
    system = str(record.get("system") or "").strip().lower()
    scale = str(record.get("scale") or "").strip().lower()
    is_active = 1 if status == "ACTIVE" else 0
    system_rank = _LOINC_SYSTEM_PREFERENCE.get(system, 0)
    is_quantitative = 1 if scale == "qn" else 0
    return (is_active, system_rank, is_quantitative)


@functools.lru_cache(maxsize=4)
def load_loinc_classes(cache_dir: Path) -> dict[str, tuple[str, str, str]]:
    """Return LOINC code -> (CLASS, COMPONENT, SYSTEM) from the parsed cache; {} when absent.

    These official LOINC fields drive caregiver report sections (ADR-031).
    """
    parsed = cache_dir / "loinc" / LOINC_PARSED_FILENAME
    if not parsed.is_file():
        return {}
    attributes: dict[str, tuple[str, str, str]] = {}
    try:
        with parsed.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                code = record.get("loinc_num")
                loinc_class = record.get("class")
                if code and loinc_class:
                    attributes[str(code)] = (
                        str(loinc_class),
                        str(record.get("component") or ""),
                        str(record.get("system") or ""),
                    )
    except (OSError, json.JSONDecodeError):
        return {}
    return attributes


def load_loinc_index(cache_dir: Path) -> dict[str, str]:
    """Return a normalized-name → LOINC code mapping from the parsed LOINC cache.

    Keyed by both ``component`` and ``long_common_name`` (normalized), plus
    common clinical abbreviations from ``_LOINC_SYNONYMS``. When several concepts
    share a normalized key, the most canonical is chosen by ``_loinc_canonical_rank``
    rather than file order (ADR-034), and ``_CANONICAL_LOINC_OVERRIDES`` wins
    outright. Returns an empty dict when the parsed file is absent; never raises.
    """
    parsed = cache_dir / "loinc" / LOINC_PARSED_FILENAME
    if not parsed.is_file():
        return {}
    index: dict[str, str] = {}
    best_rank: dict[str, tuple[int, int, int]] = {}
    try:
        for line in parsed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            loinc_num = record.get("loinc_num", "")
            if not loinc_num:
                continue
            rank = _loinc_canonical_rank(record)
            prop = str(record.get("property") or "")
            for field in ("component", "long_common_name"):
                raw = record.get(field, "")
                if not raw:
                    continue
                key = _normalize_reference_key(raw)
                if not key:
                    continue
                # Canonical-preference selection (ADR-034): keep the highest-ranked
                # concept for a shared key; strict ``>`` means the first record seen
                # wins ties, preserving determinism. Mass and molar concepts are
                # also indexed under a property-qualified key (ADR-034).
                keys = [key, f"{key}|{prop}"] if prop in _OPPOSITE_PROPERTY else [key]
                for indexed_key in keys:
                    if indexed_key not in index or rank > best_rank[indexed_key]:
                        index[indexed_key] = loinc_num
                        best_rank[indexed_key] = rank
    except Exception:
        return {}
    # Reviewed overrides are authoritative (ADR-034). Apply them before synonym
    # injection so any alias that resolves to an overridden analyte inherits the
    # reviewed code rather than the heuristic pick, and so an alias that is itself
    # an override key is left untouched.
    for key, code in _CANONICAL_LOINC_OVERRIDES.items():
        index[key] = code
        for prop in _OPPOSITE_PROPERTY:
            index.pop(f"{key}|{prop}", None)
    for alias, target in _LOINC_SYNONYMS.items():
        if alias not in index and target in index:
            index[alias] = index[target]
        for prop in _OPPOSITE_PROPERTY:
            qualified_target = f"{target}|{prop}"
            if f"{alias}|{prop}" not in index and qualified_target in index:
                index[f"{alias}|{prop}"] = index[qualified_target]
    return index


# Reviewed exact-name codes, checked before normalization (ADR-034 escape hatch).
# Normalization drops the parenthetical, so
# "Q-T INTERVAL (CORRECTED)" (QTc) resolved to 8634-8, the uncorrected QT
# interval, and the FHIR export labeled QTc values as QT.
_REVIEWED_EXACT_NAME_LOINC_CODES: dict[str, str] = {
    "q-t interval (corrected)": "8636-3",
    "qt interval (corrected)": "8636-3",
}

# Normalized labels that name no analyte. "Comment" matches only non-laboratory
# comment concepts (eyeglasses, refraction), so inference attached an eyeglasses
# code to a lipid-panel comment. Leave these uncoded.
_NON_ANALYTE_LOINC_KEYS: frozenset[str] = frozenset({"comment", "comments"})


def lookup_loinc_code(test_name: str, loinc_index: dict[str, str], unit: str = "") -> str:
    """Return the LOINC code for *test_name* from *loinc_index*, or ``""`` if not found.

    Tries progressively broader matches:
    1. CBC differential unit/name disambiguation for percentage vs absolute-count rows.
    2. Reviewed name/unit disambiguation for confirmed result-code collisions.
    3. Exact normalized key (includes synonym aliases injected at index load).
    4. Key with ``#`` characters stripped.
    5. Key with common redundant suffix words removed (e.g. *absolute*, *auto*).
    6. Key with both ``#`` and suffix words stripped.
    """
    if not loinc_index or not test_name:
        return ""
    reviewed = _REVIEWED_EXACT_NAME_LOINC_CODES.get(" ".join(test_name.casefold().split()))
    if reviewed is not None:
        return reviewed
    if _normalize_reference_key(test_name) in _NON_ANALYTE_LOINC_KEYS:
        return ""
    cbc_code = _lookup_cbc_differential_loinc_code(test_name, unit)
    if cbc_code:
        return cbc_code
    key = _normalize_reference_key(test_name)
    unit_key = str(unit or "").strip().casefold()

    unit_disambiguated = _UNIT_DISAMBIGUATED_LOINC_CODES.get((key, unit_key), "")
    if unit_disambiguated:
        return unit_disambiguated

    unit_property = _unit_property(unit)

    def _try(variant: str) -> str:
        # Reviewed overrides outrank the unit-qualified keys (ADR-034).
        override = _CANONICAL_LOINC_OVERRIDES.get(variant)
        if override:
            return override
        if unit_property:
            qualified = loinc_index.get(f"{variant}|{unit_property}", "")
            if qualified:
                return qualified
        code = loinc_index.get(variant, "")
        # The best concept for the name contradicts the unit (a molar code for a
        # mg/dL result): leave the row uncoded rather than misstate it.
        opposite = _OPPOSITE_PROPERTY.get(unit_property, "")
        if code and opposite and loinc_index.get(f"{variant}|{opposite}") == code:
            return ""
        return code

    code = _try(key)
    if code:
        return code

    no_hash = key.replace("#", " ").strip()
    no_hash = _NORMALIZE_RE.sub(" ", no_hash).strip()
    if no_hash != key:
        code = _try(no_hash)
        if code:
            return code

    stripped = _strip_common_affixes(key)
    if stripped != key:
        code = _try(stripped)
        if code:
            return code

    stripped_no_hash = _strip_common_affixes(no_hash)
    if stripped_no_hash not in (key, stripped):
        code = _try(stripped_no_hash)
        if code:
            return code

    return ""


_RXNORM_TTY_PRIORITY = ("IN", "SCD", "SBD")
_RXNORM_REVIEW_OVERRIDES_FILENAME = "rxnorm_review_overrides.jsonl"

# Dose-strength, dosage-form, and route tokens stripped from the end of a
# normalized medication name when an exact RxNorm key lookup misses. Handles
# display names like "Omeprazole 20 mg. delay release capsule" matching a
# reviewed override for the bare ingredient "omeprazole" (ADR-018 bounded
# normalization).
_RXNORM_DOSE_FORM_WORDS: frozenset[str] = frozenset(
    {
        "mg",
        "mcg",
        "g",
        "ml",
        "iu",
        "tablet",
        "tablets",
        "tab",
        "tabs",
        "capsule",
        "capsules",
        "cap",
        "caps",
        "delay",
        "delayed",
        "release",
        "oral",
        "po",
        "powder",
        "sugar",
        "free",
    }
)
_RXNORM_NUMERIC_TOKEN_RE = re.compile(r"\d+(\.\d+)?")


def _strip_rxnorm_dose_form(key: str) -> str:
    """Strip trailing dose-strength/dosage-form/route tokens from *key*."""
    words = key.split()
    while words and (
        words[-1] in _RXNORM_DOSE_FORM_WORDS or _RXNORM_NUMERIC_TOKEN_RE.fullmatch(words[-1])
    ):
        words.pop()
    return " ".join(words)


def _iter_valid_reviewed_rxnorm_override_records(cache_dir: Path) -> list[dict[str, Any]]:
    """Return valid reviewed local RxNorm override records.

    The optional overrides file lives at
    ``data/reference/rxnorm/rxnorm_review_overrides.jsonl`` and must contain one
    JSON object per line with at least ``name``, ``rxcui``, ``reviewed_by``, and
    ``reviewed_at`` (``YYYY-MM-DD``). Malformed lines are ignored.
    """
    overrides_file = cache_dir / "rxnorm" / _RXNORM_REVIEW_OVERRIDES_FILENAME
    if not overrides_file.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in overrides_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            name = str(record.get("name") or "").strip()
            rxcui = str(record.get("rxcui") or "").strip()
            reviewed_by = str(record.get("reviewed_by") or "").strip()
            reviewed_at = str(record.get("reviewed_at") or "").strip()
            if not (name and rxcui and reviewed_by and _REVIEWED_DATE_RE.fullmatch(reviewed_at)):
                continue
            records.append(record)
    except Exception:
        return []
    return records


def _load_reviewed_rxnorm_overrides(cache_dir: Path) -> dict[str, str]:
    """Return reviewed local RxNorm CUI overrides keyed by normalized display name.

    Valid reviewed overrides take precedence over the downloaded RxNorm cache.
    """
    overrides: dict[str, str] = {}
    for record in _iter_valid_reviewed_rxnorm_override_records(cache_dir):
        name = str(record.get("name") or "").strip()
        rxcui = str(record.get("rxcui") or "").strip()
        key = _normalize_reference_key(name)
        if key:
            overrides[key] = rxcui
    return overrides


def load_reviewed_rxnorm_cpic_aliases(cache_dir: Path) -> dict[str, str]:
    """Return reviewed medication display-name → CPIC drug aliases.

    The optional ``cpic_drug`` field in ``rxnorm_review_overrides.jsonl`` lets a
    reviewed medication display such as a brand name match a generic CPIC drug
    row without changing the stored RxNorm CUI.
    """
    aliases: dict[str, str] = {}
    for record in _iter_valid_reviewed_rxnorm_override_records(cache_dir):
        name = str(record.get("name") or "").strip()
        cpic_drug = " ".join(str(record.get("cpic_drug") or "").split())
        key = _normalize_reference_key(name)
        if key and cpic_drug:
            aliases[key] = cpic_drug
    return aliases


def lookup_reviewed_rxnorm_cpic_alias(
    name: str,
    cpic_aliases: dict[str, str],
) -> str:
    """Return the reviewed CPIC drug alias for *name*, or ``""`` if absent."""
    if not name or not cpic_aliases:
        return ""
    return cpic_aliases.get(_normalize_reference_key(name), "")


def load_rxnorm_index(cache_dir: Path) -> dict[str, str]:
    """Return a normalized-name → RxNorm CUI mapping from the downloaded RxNorm cache.

    Ingredient (IN) entries take priority over clinical drug (SCD) and branded drug
    (SBD) entries. The first downloaded match for a normalized name wins. Optional
    reviewed local overrides from ``rxnorm_review_overrides.jsonl`` then replace
    downloaded entries for the reviewed names. Reviewed overrides still load
    when the downloaded concepts file is absent; never raises.
    """
    overrides = _load_reviewed_rxnorm_overrides(cache_dir)
    rxnorm_file = cache_dir / "rxnorm" / "rxnorm_concepts.jsonl"
    if not rxnorm_file.is_file():
        return overrides
    by_tty: dict[str, list[tuple[str, str]]] = {tty: [] for tty in _RXNORM_TTY_PRIORITY}
    try:
        for line in rxnorm_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            rxcui = record.get("rxcui", "")
            name = record.get("name", "")
            tty = record.get("tty", "")
            if not rxcui or not name or tty not in by_tty:
                continue
            by_tty[tty].append((name, rxcui))
    except Exception:
        return overrides
    index: dict[str, str] = {}
    for tty in _RXNORM_TTY_PRIORITY:
        for name, rxcui in by_tty[tty]:
            key = _normalize_reference_key(name)
            if key and key not in index:
                index[key] = rxcui
    index.update(overrides)
    return index


def lookup_rxnorm_cui(name: str, rxnorm_index: dict[str, str]) -> str:
    """Return the RxNorm CUI for *name* from *rxnorm_index*, or ``""`` if not found.

    Falls back to a bounded dose-form/route-stripped key (see
    :func:`_strip_rxnorm_dose_form`) when the exact normalized name has no entry.
    """
    if not rxnorm_index or not name:
        return ""
    key = _normalize_reference_key(name)
    if key in rxnorm_index:
        return rxnorm_index[key]
    stripped_key = _strip_rxnorm_dose_form(key)
    if stripped_key != key:
        return rxnorm_index.get(stripped_key, "")
    return ""


def _dataset_has_files(cache_dir: Path, dataset_id: str) -> bool:
    dataset_dir = cache_dir / dataset_id
    if not dataset_dir.is_dir():
        return False
    if dataset_id == "loinc":
        return (
            _find_loinc_csv(cache_dir) is not None
            or (dataset_dir / LOINC_PARSED_FILENAME).is_file()
        )
    return any(dataset_dir.iterdir())


def _annotation_dataset_status(
    cache_dir: Path, dataset_id: str, display_name: str
) -> dict[str, Any]:
    from famed.annotation_cache import _reference_db_path, get_annotation_status

    ref_db = _reference_db_path(cache_dir)
    all_status = get_annotation_status(ref_db)
    info = all_status.get(dataset_id, {"status": "MISSING", "row_count": 0, "refreshed_at": None})
    annotation_status = info["status"]
    status = {
        "OK": STATUS_OK,
        "STALE": STATUS_STALE,
        "UNAVAILABLE": STATUS_UNAVAILABLE,
    }.get(annotation_status, STATUS_MISSING)
    return {
        "status": status,
        "version": info.get("source_version") or info.get("refreshed_at"),
        "display_name": display_name,
        "downloaded_at": info.get("refreshed_at"),
        "row_count": info.get("row_count", 0),
        "source_url": info.get("source_url"),
        "last_attempted_at": info.get("last_attempted_at"),
        "last_attempt_status": info.get("last_attempt_status"),
        "last_error": info.get("last_error"),
    }


def cache_status(cache_dir: Path) -> dict[str, dict[str, Any]]:
    manifest = read_manifest(cache_dir)
    datasets = manifest.get("datasets", {})
    result: dict[str, dict[str, Any]] = {}

    for dataset_id, config in SUPPORTED_DATASETS.items():
        if config.get("annotation_db"):
            result[dataset_id] = _annotation_dataset_status(
                cache_dir, dataset_id, config["display_name"]
            )
            continue

        cached = datasets.get(dataset_id)
        if cached is None:
            if _dataset_has_files(cache_dir, dataset_id):
                result[dataset_id] = {
                    "status": STATUS_OK,
                    "version": None,
                    "display_name": config["display_name"],
                }
            else:
                result[dataset_id] = {
                    "status": STATUS_MISSING,
                    "version": None,
                    "display_name": config["display_name"],
                }
            continue

        cached_version = cached.get("version")
        try:
            is_stale = check_stale(dataset_id, manifest)
        except Exception:
            is_stale = False

        result[dataset_id] = {
            "status": STATUS_STALE if is_stale else STATUS_OK,
            "version": cached_version,
            "display_name": config["display_name"],
            "downloaded_at": cached.get("downloaded_at"),
            "source_url": cached.get("source_url"),
        }

    return result


def check_stale(dataset_id: str, manifest: dict[str, Any]) -> bool:
    if dataset_id not in SUPPORTED_DATASETS:
        return False
    config = SUPPORTED_DATASETS[dataset_id]
    if not config["version_url"]:
        return False
    if dataset_id == "rxnorm":
        current_version = _rxnorm_version()
        if current_version is None:
            return False
        cached = manifest.get("datasets", {}).get(dataset_id, {})
        cached_version = cached.get("version")
        return cached_version is not None and cached_version != current_version
    return False


def download_dataset(
    cache_dir: Path,
    dataset_id: str,
) -> bool:
    if dataset_id not in SUPPORTED_DATASETS:
        raise ValueError(f"Unknown dataset: {dataset_id}")

    if SUPPORTED_DATASETS[dataset_id].get("annotation_db"):
        from famed.annotation_cache import _reference_db_path, refresh_annotation_dataset

        ref_db = _reference_db_path(cache_dir)
        refresh_annotation_dataset(ref_db, dataset_id)
        return True

    manifest = read_manifest(cache_dir)

    if dataset_id == "rxnorm":
        version = download_rxnorm(cache_dir)
        checksum = _compute_sha256_bytes(
            (cache_dir / "rxnorm" / "rxnorm_concepts.jsonl").read_bytes()
        )
        manifest.setdefault("datasets", {})[dataset_id] = {
            "version": version,
            "downloaded_at": datetime.now(UTC).isoformat(),
            "source_url": "https://rxnav.nlm.nih.gov/REST/allconcepts?tty=IN+SCD+SBD",
            "checksum_sha256": checksum,
            "files": ["rxnorm/rxnorm_concepts.jsonl"],
        }
        write_manifest(cache_dir, manifest)
        return True

    if dataset_id == "loinc":
        raise ValueError(
            "LOINC is not downloaded by famed. Download it manually from "
            "https://loinc.org/downloads/ and place files under data/reference/loinc/"
        )

    return False


def refresh_cache(
    cache_dir: Path,
    dataset_id: str | None = None,
) -> dict[str, str]:
    results: dict[str, str] = {}

    ids = [dataset_id] if dataset_id else list(SUPPORTED_DATASETS)
    for ds_id in ids:
        if ds_id not in SUPPORTED_DATASETS:
            results[ds_id] = f"Unknown dataset: {ds_id}"
            continue
        config = SUPPORTED_DATASETS[ds_id]
        if not config["downloadable"]:
            if ds_id == "loinc":
                try:
                    parse_loinc_csv(cache_dir)
                    results[ds_id] = STATUS_OK
                except FileNotFoundError:
                    results[ds_id] = STATUS_MISSING + " (manual download required)"
                except Exception as exc:
                    results[ds_id] = f"{STATUS_UNAVAILABLE}: {exc}"
            else:
                results[ds_id] = (
                    STATUS_OK
                    if _dataset_has_files(cache_dir, ds_id)
                    else (STATUS_MISSING + " (manual download required)")
                )
            continue
        try:
            download_dataset(cache_dir, ds_id)
            results[ds_id] = STATUS_OK
        except Exception as exc:
            results[ds_id] = f"{STATUS_UNAVAILABLE}: {exc}"

    return results


def _refresh_derived_outputs_after_cache_change(root: Path, cache_dir: Path) -> int:
    """Refresh household derived outputs after reference data changes.

    A cache refresh is a dependency change for derived reports under ADR-025.
    Fresh checkouts and cache-only test fixtures may not have a vault/DB yet; in
    that case there is no configured report surface to refresh.
    """
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    if not vault_root.exists() or not db_path.exists():
        return 0

    from famed.annotation_cache import REFERENCE_DB_FILENAME
    from famed.dependent_reports import refresh_derived_reports_for_household
    from famed.findings_ledger import FINDINGS_LEDGER_PATH

    ref_db_path = cache_dir / REFERENCE_DB_FILENAME
    findings_ledger_path = root / FINDINGS_LEDGER_PATH
    with sqlite3.connect(db_path) as conn:
        refreshed = refresh_derived_reports_for_household(
            conn,
            vault_root,
            ref_db_path=ref_db_path,
            findings_ledger_path=findings_ledger_path,
        )
    return len(refreshed)


def _successful_cache_change(results: dict[str, str]) -> bool:
    return any(result == STATUS_OK for result in results.values())


def _print_derived_refresh_after_successful_cache_change(
    results: dict[str, str], root: Path, cache_dir: Path
) -> None:
    if not _successful_cache_change(results):
        return
    refreshed_count = _refresh_derived_outputs_after_cache_change(root, cache_dir)
    print(f"derived reports refreshed for {refreshed_count} person(s)")


def main() -> int:
    import argparse

    dataset_display_names = ", ".join(
        config["display_name"] for config in SUPPORTED_DATASETS.values()
    )
    dataset_ids = ", ".join(SUPPORTED_DATASETS)
    parser = argparse.ArgumentParser(
        description=f"Manage famed reference data cache ({dataset_display_names})."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("download", help="Download supported non-credentialed reference datasets")
    sub.add_parser("status", help="Report cache status for each dataset")
    sub.add_parser("refresh", help="Refresh downloadable datasets and report manual datasets")
    sub.add_parser("clean", help="Remove all cached reference data")

    parser.add_argument(
        "--dataset",
        help=f"Operate on a single dataset ({dataset_ids})",
    )

    args = parser.parse_args()
    root = _find_repo_root()
    cache_dir = _cache_root(root)

    if args.command == "download":
        results: dict[str, str] = {}
        ids = [args.dataset] if args.dataset else list(SUPPORTED_DATASETS)
        for ds_id in ids:
            if ds_id not in SUPPORTED_DATASETS:
                print(f"Unknown dataset: {ds_id}")
                results[ds_id] = f"Unknown dataset: {ds_id}"
                continue
            config = SUPPORTED_DATASETS[ds_id]
            if not config["downloadable"]:
                print(
                    f"{ds_id}: manual download required; place files under data/reference/{ds_id}/"
                )
                results[ds_id] = STATUS_MISSING + " (manual download required)"
                continue
            try:
                download_dataset(cache_dir, ds_id)
                print(f"{ds_id}: downloaded")
                results[ds_id] = STATUS_OK
            except Exception as exc:
                print(f"{ds_id}: failed — {exc}")
                results[ds_id] = f"{STATUS_UNAVAILABLE}: {exc}"
        _print_derived_refresh_after_successful_cache_change(results, root, cache_dir)

    elif args.command == "status":
        status = cache_status(cache_dir)
        for ds_id, info in status.items():
            ver = info["version"] or "-"
            print(f"{info['status']:<8} {ds_id}  version={ver}  ({info['display_name']})")
            if info.get("last_attempt_status") == "failed":
                print(
                    f"         last refresh failed at {info['last_attempted_at']}: "
                    f"{info['last_error']}"
                )

    elif args.command == "refresh":
        results = refresh_cache(cache_dir, dataset_id=args.dataset)
        for ds_id, r in results.items():
            print(f"{ds_id}: {r}")
        _print_derived_refresh_after_successful_cache_change(results, root, cache_dir)

    elif args.command == "clean":
        import shutil

        from famed.annotation_cache import REFERENCE_DB_FILENAME

        manifest = _manifest_path(cache_dir)
        if manifest.exists():
            manifest.unlink()
        ref_db = cache_dir / REFERENCE_DB_FILENAME
        if ref_db.exists():
            ref_db.unlink()
        for child in cache_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
        print("Reference cache cleared.")

    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
