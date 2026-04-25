"""
Mock ABDM HIP (Health Information Provider)
Simulates a hospital system. When instructed by the gateway, it:
  1. Queries Postgres for the patient's oncology record
  2. Assembles a FHIR R4 Bundle
  3. POSTs it to the HIU callback URL
"""

import uuid
import logging
import httpx
import psycopg2
import psycopg2.extras
import os
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HIP] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Mock ABDM HIP", version="0.5.0")

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://abdm_user:abdm_pass@postgres-db:5432/abdm_mock"
)


# ------------------------------------------------------------------ #
# DB helpers
# ------------------------------------------------------------------ #

def get_conn():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_patient_record(abha_address: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM patients WHERE abha_address = %s", (abha_address,)
            )
            patient = cur.fetchone()
            if not patient:
                return None
            pid = patient["id"]

            cur.execute("SELECT * FROM conditions WHERE patient_id = %s", (pid,))
            conditions = cur.fetchall()

            cur.execute("SELECT * FROM observations WHERE patient_id = %s", (pid,))
            observations = cur.fetchall()

            cur.execute(
                "SELECT * FROM medication_statements WHERE patient_id = %s", (pid,)
            )
            medications = cur.fetchall()

    return {
        "patient": dict(patient),
        "conditions": [dict(r) for r in conditions],
        "observations": [dict(r) for r in observations],
        "medications": [dict(r) for r in medications],
    }


# ------------------------------------------------------------------ #
# FHIR R4 Bundle builder
# ------------------------------------------------------------------ #

def _date(d) -> str:
    if d is None:
        return datetime.now(timezone.utc).date().isoformat()
    return str(d)


def build_fhir_bundle(record: dict, transaction_id: str) -> dict:
    p = record["patient"]
    patient_fhir_id = f"Patient/{p['abha_address'].replace('@', '-')}"

    entries = []

    # --- Patient resource ---
    entries.append({
        "fullUrl": f"urn:uuid:{uuid.uuid4()}",
        "resource": {
            "resourceType": "Patient",
            "id": patient_fhir_id,
            "identifier": [
                {
                    "system": "https://healthid.ndhm.gov.in",
                    "value": p["abha_address"],
                    "type": {
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                            "code": "MR",
                            "display": "ABHA Address"
                        }]
                    }
                }
            ],
            "name": [{"use": "official", "text": p["name"]}],
            "gender": p["gender"],
            "birthDate": _date(p["birth_date"]),
            "telecom": [{"system": "phone", "value": p.get("phone", "")}],
        }
    })

    # --- Condition resources ---
    for cond in record["conditions"]:
        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "Condition",
                "id": f"condition-{cond['id']}",
                "clinicalStatus": {
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": cond["clinical_status"],
                        "display": cond["clinical_status"].capitalize()
                    }]
                },
                "code": {
                    "coding": [{
                        "system": "http://hl7.org/fhir/sid/icd-10",
                        "code": cond["icd10_code"],
                        "display": cond["display"]
                    }],
                    "text": cond["display"]
                },
                "subject": {"reference": patient_fhir_id},
                "onsetDateTime": _date(cond.get("onset_date")),
                "recordedDate": _date(cond.get("recorded_date")),
            }
        })

    # --- Observation resources (genomic lab results) ---
    for obs in record["observations"]:
        interp_code = "POS" if obs["interpretation"] == "positive" else \
                      "NEG" if obs["interpretation"] == "negative" else "N"
        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "Observation",
                "id": f"observation-{obs['id']}",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory",
                        "display": "Laboratory"
                    }]
                }],
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": obs.get("loinc_code", "unknown"),
                        "display": obs["display"]
                    }],
                    "text": obs["display"]
                },
                "subject": {"reference": patient_fhir_id},
                "effectiveDateTime": _date(obs.get("effective_date")),
                "valueString": obs.get("value_string", ""),
                "interpretation": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                        "code": interp_code,
                        "display": obs["interpretation"].capitalize() if obs.get("interpretation") else "Unknown"
                    }]
                }],
            }
        })

    # --- MedicationStatement resources ---
    for med in record["medications"]:
        effective: dict = {}
        if med.get("effective_start") and med.get("effective_end"):
            effective = {
                "effectivePeriod": {
                    "start": _date(med["effective_start"]),
                    "end": _date(med["effective_end"]),
                }
            }
        elif med.get("effective_start"):
            effective = {"effectiveDateTime": _date(med["effective_start"])}

        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "MedicationStatement",
                "id": f"medstmt-{med['id']}",
                "status": med["status"],
                "medicationCodeableConcept": {
                    "coding": [{
                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": med.get("rxnorm_code", ""),
                        "display": med["medication_name"]
                    }],
                    "text": med["medication_name"]
                },
                "subject": {"reference": patient_fhir_id},
                "dosage": [{"text": med.get("dosage_text", "")}],
                **effective,
            }
        })

    bundle = {
        "resourceType": "Bundle",
        "id": f"bundle-{transaction_id}",
        "type": "collection",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(entries),
        "entry": entries,
    }
    return bundle


# ------------------------------------------------------------------ #
# Background: push FHIR bundle to HIU
# ------------------------------------------------------------------ #

async def push_data_to_hiu(
    transaction_id: str,
    consent_id: str,
    abha_address: str,
    data_push_url: str,
) -> None:
    log.info("Fetching DB record for patient=%s", abha_address)
    record = fetch_patient_record(abha_address)
    if not record:
        log.error("Patient not found in DB: %s", abha_address)
        return

    bundle = build_fhir_bundle(record, transaction_id)
    payload = {
        "pageNumber": 1,
        "pageCount": 1,
        "transactionId": transaction_id,
        "entries": [
            {
                "content": bundle,
                "media": "application/fhir+json",
                "checksum": "",
                "careContextReference": abha_address,
            }
        ],
        "keyMaterial": {
            "cryptoAlg": "ECDH",
            "curve": "Curve25519",
            "dhPublicKey": {"expiry": "", "parameters": "Curve25519/32byte random key", "keyValue": "MOCK_KEY"},
            "nonce": "MOCK_NONCE"
        }
    }

    log.info("Pushing FHIR Bundle to HIU | url=%s txn=%s", data_push_url, transaction_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(data_push_url, json=payload)
            log.info("HIU acknowledged with HTTP %s", resp.status_code)
    except Exception as exc:
        log.error("Failed to push data to HIU: %s", exc)


# ------------------------------------------------------------------ #
# Schemas
# ------------------------------------------------------------------ #

class DataRequest(BaseModel):
    requestId: str
    timestamp: str
    transactionId: str
    consentId: str
    abhaAddress: str
    dataPushUrl: str


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-abdm-hip"}


@app.post("/v0.5/health-information/request")
async def health_info_request(req: DataRequest, background_tasks: BackgroundTasks):
    """
    Called by the gateway. Instructs the HIP to push FHIR data to the HIU.
    """
    log.info(
        "Received data request | txn=%s patient=%s pushUrl=%s",
        req.transactionId, req.abhaAddress, req.dataPushUrl
    )
    background_tasks.add_task(
        push_data_to_hiu,
        req.transactionId,
        req.consentId,
        req.abhaAddress,
        req.dataPushUrl,
    )
    return {
        "requestId": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hiRequest": {
            "transactionId": req.transactionId,
            "sessionStatus": "ACKNOWLEDGED",
        }
    }
