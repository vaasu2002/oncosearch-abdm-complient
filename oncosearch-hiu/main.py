"""
OncoSearch HIU (Health Information User)
The clinical decision support application.

Responsibilities:
  - Trigger consent requests to the ABDM Gateway
  - Receive FHIR R4 Bundles from the HIP via webhook
  - Parse and print oncology-specific summaries to the console

Data retention / cleanup is handled by a separate oncosearch-retention-worker
container so that this service can scale horizontally without spawning
duplicate scheduler processes.
"""

import uuid
import logging
import httpx
import os
import json
import psycopg2
import psycopg2.extras
import psycopg2.pool
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HIU/OncoSearch] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(title="OncoSearch HIU", version="1.0.0")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://mock-gateway:8000")

HIU_CALLBACK_URL = os.getenv(
    "HIU_CALLBACK_URL",
    "http://oncosearch-hiu:8002/v0.5/health-information/transfer"
)

DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", 31))

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://oncosearch_user:oncosearch_pass@oncosearch-postgres:5432/oncosearch_db"
)

# Connection pool — shared across all requests in this process.
# min=1 keeps one idle connection warm; max=10 caps DB connections per replica.
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _init_pool() -> None:
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@contextmanager
def get_db_conn():
    """Borrow a connection from the pool, return it when done."""
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ------------------------------------------------------------------ #
# Schemas
# ------------------------------------------------------------------ #

class ConsentTriggerRequest(BaseModel):
    abha_address: str
    hip_id: str = "mock-hip-001"
    hiu_id: str = "oncosearch-hiu-001"
    purpose_code: str = "CAREMGT"
    purpose_text: str = "Care Management — Oncology Clinical Decision Support"


# ------------------------------------------------------------------ #
# FHIR parsing helpers
# ------------------------------------------------------------------ #

def _resources_of_type(bundle: dict, resource_type: str) -> list[dict]:
    return [
        e["resource"]
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == resource_type
    ]


def _text(coding_or_cc: dict | None) -> str:
    if not coding_or_cc:
        return "Unknown"
    if "text" in coding_or_cc:
        return coding_or_cc["text"]
    codings = coding_or_cc.get("coding", [])
    if codings:
        return codings[0].get("display", "Unknown")
    return "Unknown"


def print_oncology_summary(bundle: dict, transaction_id: str) -> None:
    separator = "=" * 70

    patients   = _resources_of_type(bundle, "Patient")
    conditions = _resources_of_type(bundle, "Condition")
    obs        = _resources_of_type(bundle, "Observation")
    meds       = _resources_of_type(bundle, "MedicationStatement")

    patient_name = "Unknown Patient"
    abha_address = ""
    if patients:
        p = patients[0]
        names = p.get("name", [])
        patient_name = names[0].get("text", "Unknown") if names else "Unknown"
        for ident in p.get("identifier", []):
            if "healthid.ndhm.gov.in" in ident.get("system", ""):
                abha_address = ident.get("value", "")
                break

    log.info("\n%s", separator)
    log.info("  ONCOSEARCH — PATIENT DATA RECEIVED")
    log.info("%s", separator)
    log.info("  Transaction ID : %s", transaction_id)
    log.info("  Patient        : %s", patient_name)
    log.info("  ABHA Address   : %s", abha_address or "N/A")
    log.info("  Bundle Total   : %d resources", bundle.get("total", len(bundle.get("entry", []))))
    log.info("%s", separator)

    if conditions:
        log.info("  DIAGNOSES (%d)", len(conditions))
        for c in conditions:
            code_text = _text(c.get("code"))
            status    = c.get("clinicalStatus", {})
            status_text = _text(status) if isinstance(status, dict) else str(status)
            onset     = c.get("onsetDateTime", "N/A")
            codings   = c.get("code", {}).get("coding", [{}])
            icd_code  = codings[0].get("code", "") if codings else ""
            log.info("    [%s] %s | Status: %s | Onset: %s",
                     icd_code, code_text, status_text, onset)

    if obs:
        log.info("  GENOMIC & LAB RESULTS (%d)", len(obs))
        for o in obs:
            display      = _text(o.get("code"))
            value_str    = o.get("valueString", "N/A")
            interp_list  = o.get("interpretation", [{}])
            interp_text  = _text(interp_list[0]) if interp_list else "N/A"
            effective    = o.get("effectiveDateTime", "N/A")
            marker = "+" if "positive" in interp_text.lower() or interp_text == "POS" \
                     else "-" if "negative" in interp_text.lower() or interp_text == "NEG" \
                     else "~"
            log.info("    [%s] %s", marker, display)
            log.info("        Result : %s", value_str)
            log.info("        Date   : %s", effective)

    if meds:
        log.info("  MEDICATIONS / TREATMENTS (%d)", len(meds))
        for m in meds:
            med_name = _text(m.get("medicationCodeableConcept"))
            status   = m.get("status", "unknown")
            dosage   = ""
            dosages  = m.get("dosage", [])
            if dosages:
                dosage = dosages[0].get("text", "")
            effective_start = (
                m.get("effectiveDateTime")
                or m.get("effectivePeriod", {}).get("start", "N/A")
            )
            log.info("    %-30s | Status: %-10s | Start: %s",
                     med_name, status, effective_start)
            if dosage:
                log.info("        Dosage : %s", dosage)

    log.info("%s\n", separator)


# ------------------------------------------------------------------ #
# Lifecycle
# ------------------------------------------------------------------ #

@app.on_event("startup")
async def startup_event():
    """Initialize the connection pool and verify DB connectivity."""
    _init_pool()
    with get_db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
    log.info("Database connection pool initialised (min=1, max=10)")


@app.on_event("shutdown")
async def shutdown_event():
    if _pool:
        _pool.closeall()
    log.info("Database connection pool closed")


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@app.get("/health")
async def health():
    return {"status": "ok", "service": "oncosearch-hiu"}


@app.post("/consent/trigger")
async def trigger_consent(req: ConsentTriggerRequest):
    """
    Entry point. The clinician (or curl) calls this with an ABHA address.
    We send a consent request to the ABDM Gateway which auto-approves
    and instructs the HIP to push FHIR data back to us.
    """
    request_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    data_erase_at = (datetime.now(timezone.utc) + timedelta(days=DATA_RETENTION_DAYS)).isoformat()

    payload = {
        "requestId": request_id,
        "timestamp": now,
        "hiuCallbackUrl": HIU_CALLBACK_URL,
        "consent": {
            "purpose": {
                "code": req.purpose_code,
                "text": req.purpose_text,
            },
            "patient": {"id": req.abha_address},
            "hip": {"id": req.hip_id},
            "hiu": {"id": req.hiu_id},
            "requester": {
                "name": "OncoSearch Clinical Decision Support System",
                "identifier": {
                    "type": "REGNO",
                    "value": "ONCOSEARCH-001",
                    "system": "https://oncosearch.example.com",
                }
            },
            "hiTypes": [
                "OPConsultation",
                "DiagnosticReport",
                "Prescription",
                "DischargeSummary",
            ],
            "permission": {
                "accessMode": "VIEW",
                "dateRange": {"from": "2020-01-01T00:00:00Z", "to": now},
                "dataEraseAt": data_erase_at,
                "frequency": {"unit": "HOUR", "value": 1, "repeats": 0},
            }
        }
    }

    log.info("Sending consent request to Gateway | requestId=%s patient=%s",
             request_id, req.abha_address)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{GATEWAY_URL}/v0.5/consent-requests/init",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        log.error("Gateway returned error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Gateway error: {exc.response.text}")
    except Exception as exc:
        log.error("Could not reach Gateway: %s", exc)
        raise HTTPException(status_code=503, detail=f"Gateway unreachable: {exc}")

    consent_id     = data.get("consentId")
    transaction_id = data.get("transactionId")

    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO consent_transactions
                    (transaction_id, consent_id, request_id, abha_address, purpose_code, data_erase_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (transaction_id, consent_id, request_id, req.abha_address, req.purpose_code, data_erase_at))
            cur.execute("""
                INSERT INTO data_access_log (abha_address, transaction_id, action)
                VALUES (%s, %s, %s)
            """, (req.abha_address, transaction_id, 'CONSENT_TRIGGERED'))
    except Exception as e:
        log.error("Failed to persist consent transaction: %s", e)
        # Continue — consent was already accepted by gateway

    log.info("Consent GRANTED by Gateway | consentId=%s txn=%s", consent_id, transaction_id)
    log.info("Waiting for HIP to push FHIR data...")

    return {
        "message": "Consent request sent. Awaiting FHIR data from HIP.",
        "consentId": consent_id,
        "transactionId": transaction_id,
        "abhaAddress": req.abha_address,
    }


@app.post("/v0.5/health-information/transfer")
async def receive_health_data(payload: dict[str, Any]):
    """
    Webhook called by the HIP with the FHIR R4 Bundle.
    Persists the bundle to cache, updates transaction status, and prints summary.
    """
    transaction_id = payload.get("transactionId", "unknown")
    entries        = payload.get("entries", [])

    log.info("FHIR data received | txn=%s entries=%d", transaction_id, len(entries))

    try:
        with get_db_conn() as conn:
            cur = conn.cursor()

            cur.execute(
                "SELECT data_erase_at, abha_address FROM consent_transactions WHERE transaction_id = %s",
                (transaction_id,)
            )
            txn_row = cur.fetchone()

            if txn_row:
                data_erase_at = txn_row['data_erase_at']
                abha_address = txn_row['abha_address']
            else:
                data_erase_at = datetime.now(timezone.utc) + timedelta(days=DATA_RETENTION_DAYS)
                abha_address = "unknown"

            for entry in entries:
                bundle = entry.get("content", {})
                if bundle.get("resourceType") == "Bundle":
                    patients = _resources_of_type(bundle, "Patient")
                    if patients:
                        for ident in patients[0].get("identifier", []):
                            if "healthid.ndhm.gov.in" in ident.get("system", ""):
                                abha_address = ident.get("value", abha_address)
                                break

                    cur.execute("""
                        INSERT INTO health_data_cache
                            (transaction_id, abha_address, fhir_bundle, entry_count, expires_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        transaction_id, abha_address,
                        json.dumps(bundle),
                        bundle.get("total", len(bundle.get("entry", []))),
                        data_erase_at,
                    ))

            cur.execute("""
                UPDATE consent_transactions
                SET status = %s, data_received_at = NOW()
                WHERE transaction_id = %s
            """, ('DATA_RECEIVED', transaction_id))

            cur.execute("""
                INSERT INTO data_access_log (abha_address, transaction_id, action)
                VALUES (%s, %s, %s)
            """, (abha_address, transaction_id, 'DATA_RECEIVED'))

        log.info("FHIR bundle(s) cached | txn=%s patient=%s", transaction_id, abha_address)
    except Exception as e:
        log.error("Failed to cache FHIR bundle: %s", e)

    for entry in entries:
        bundle = entry.get("content", {})
        if bundle.get("resourceType") == "Bundle":
            print_oncology_summary(bundle, transaction_id)
        else:
            log.warning("Entry did not contain a FHIR Bundle: %s", list(bundle.keys()))

    return {
        "requestId": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hiRequest": {
            "transactionId": transaction_id,
            "sessionStatus": "TRANSFER_COMPLETE",
        }
    }


@app.get("/patient/{abha_address}/data")
async def get_cached_patient_data(abha_address: str):
    """
    Retrieve cached FHIR health data for a patient.
    Only returns data that is NOT expired and NOT soft-deleted.
    """
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO data_access_log (abha_address, action) VALUES (%s, %s)
            """, (abha_address, 'DATA_ACCESSED'))

            cur.execute("""
                SELECT transaction_id, stored_at, expires_at, entry_count, fhir_bundle
                FROM health_data_cache
                WHERE abha_address = %s AND is_deleted = FALSE AND expires_at > NOW()
                ORDER BY stored_at DESC
            """, (abha_address,))
            rows = cur.fetchall()
    except Exception as e:
        log.error("Error retrieving cached patient data: %s", e)
        raise HTTPException(status_code=500, detail="Error retrieving cached data")

    if not rows:
        raise HTTPException(status_code=404, detail="No cached health data found for this patient")

    records = []
    for row in rows:
        records.append({
            "transaction_id": row['transaction_id'],
            "stored_at": row['stored_at'].isoformat() if row['stored_at'] else None,
            "expires_at": row['expires_at'].isoformat() if row['expires_at'] else None,
            "entry_count": row['entry_count'],
            "fhir_bundle": json.loads(row['fhir_bundle']) if isinstance(row['fhir_bundle'], str) else row['fhir_bundle'],
        })

    return {"abha_address": abha_address, "records": records}


@app.get("/transactions")
async def list_transactions():
    """
    Debug endpoint — see all consent transactions from the database.
    """
    try:
        with get_db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, transaction_id, consent_id, request_id, abha_address, status,
                       purpose_code, data_erase_at, initiated_at, data_received_at
                FROM consent_transactions
                ORDER BY initiated_at DESC
            """)
            rows = cur.fetchall()
    except Exception as e:
        log.error("Error listing transactions: %s", e)
        raise HTTPException(status_code=500, detail="Error listing transactions")

    return {
        row['transaction_id']: {
            "consentId": row['consent_id'],
            "abhaAddress": row['abha_address'],
            "requestId": row['request_id'],
            "status": row['status'],
            "purposeCode": row['purpose_code'],
            "dataEraseAt": row['data_erase_at'].isoformat() if row['data_erase_at'] else None,
            "initiatedAt": row['initiated_at'].isoformat() if row['initiated_at'] else None,
            "receivedAt": row['data_received_at'].isoformat() if row['data_received_at'] else None,
        }
        for row in rows
    }
