"""
OncoSearch HIU (Health Information User)
The clinical decision support application.

Responsibilities:
  - Trigger consent requests to the ABDM Gateway
  - Receive FHIR R4 Bundles from the HIP via webhook
  - Parse and print oncology-specific summaries to the console
"""

import uuid
import logging
import httpx
import os
import json
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HIU/OncoSearch] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(title="OncoSearch HIU", version="1.0.0")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://mock-gateway:8000")

# URL where ABDM Gateway will send the consent status and data flow notification
HIU_CALLBACK_URL = os.getenv(
    "HIU_CALLBACK_URL",
    "http://oncosearch-hiu:8002/v0.5/health-information/transfer"
)

DATA_RETENTION_DAYS = int(os.getenv(
    "DATA_RETENTION_DAYS",
    31
))

# ---- Database connection ----
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://oncosearch_user:oncosearch_pass@oncosearch-postgres:5432/oncosearch_db"
)

def get_db_conn():
    """Get a synchronous PostgreSQL connection."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

# Global scheduler for background cleanup tasks
scheduler = BackgroundScheduler()

# In-memory transaction log (kept for backward compatibility during transition)
active_transactions: dict[str, dict] = {}


# ------------------------------------------------------------------ #
# Schemas
# ------------------------------------------------------------------ #

class ConsentTriggerRequest(BaseModel):
    abha_address: str             # e.g. "vaasu.bisht@abdm"
    hip_id: str = "mock-hip-001"
    hiu_id: str = "oncosearch-hiu-001"
    purpose_code: str = "CAREMGT" # Care Management (https://terminology.hl7.org/3.1.0/CodeSystem-v3-ActReason.html#v3-ActReason-CAREMGT)
    purpose_text: str = "Care Management — Oncology Clinical Decision Support"
    # frequency: str  # ONE_TIME / RECURRING
    # data_types: List[str]  # e.g. ["DiagnosticReport", "Observation"]
    # date_range_from: datetime
    # date_range_to: datetime
    # consent_valid_from: datetime
    # consent_valid_to: datetime


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
# Database and Retention Management
# ------------------------------------------------------------------ #

def cleanup_expired_data():
    """
    Phase 1: Soft-delete expired health data (hourly).
    Marks rows as deleted and writes audit trail.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Soft-delete expired rows
        cur.execute("""
            UPDATE health_data_cache
            SET is_deleted = TRUE, deleted_at = NOW()
            WHERE expires_at < NOW() AND is_deleted = FALSE
            RETURNING transaction_id, abha_address
        """)
        expired_rows = cur.fetchall()

        # Write audit log for each expired record
        for row in expired_rows:
            cur.execute(
                "INSERT INTO data_access_log (abha_address, transaction_id, action) VALUES (%s, %s, %s)",
                (row['abha_address'], row['transaction_id'], 'DATA_EXPIRED')
            )

        conn.commit()
        conn.close()
        log.info("Cleanup (soft-delete): Marked %d expired health data records for deletion", len(expired_rows))
    except Exception as e:
        log.error("Error in cleanup_expired_data: %s", e)


def hard_delete_expired_data():
    """
    Phase 2: Hard-delete soft-deleted data (daily at 2:00 AM UTC).
    Also marks consent transactions as EXPIRED.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Hard-delete soft-deleted rows that have been marked for >24 hours
        cur.execute("""
            DELETE FROM health_data_cache
            WHERE is_deleted = TRUE AND deleted_at < NOW() - INTERVAL '24 hours'
        """)
        deleted_count = cur.rowcount

        # Mark consent transactions as EXPIRED
        cur.execute("""
            UPDATE consent_transactions
            SET status = 'EXPIRED'
            WHERE data_erase_at < NOW() AND status NOT IN ('EXPIRED', 'ERROR')
        """)
        expired_txn_count = cur.rowcount

        conn.commit()
        conn.close()
        log.info("Cleanup (hard-delete): Deleted %d health data records, marked %d transactions EXPIRED",
                 deleted_count, expired_txn_count)
    except Exception as e:
        log.error("Error in hard_delete_expired_data: %s", e)


@app.on_event("startup")
async def startup_event():
    """Initialize database connection and start retention scheduler."""
    # Test database connectivity
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
        log.info("✓ Database connection successful")
    except Exception as e:
        log.error("✗ Database connection failed: %s", e)
        raise

    # Start APScheduler for background cleanup tasks
    scheduler.add_job(cleanup_expired_data, IntervalTrigger(hours=1), id="cleanup_expired_data")
    scheduler.add_job(hard_delete_expired_data, CronTrigger(hour=2, minute=0), id="hard_delete_expired_data")
    scheduler.start()
    log.info("✓ Retention scheduler started (soft-delete every hour, hard-delete daily at 2:00 AM UTC)")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the retention scheduler on shutdown."""
    scheduler.shutdown()
    log.info("Retention scheduler stopped")


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

    payload = {
        "requestId": request_id,
        "timestamp": now,
        "hiuCallbackUrl": HIU_CALLBACK_URL,
        "consent": {
            "purpose": {
                # Why is data being requested (required for legal and patient clarity)
                "code": req.purpose_code, # CAREMGT
                "text": req.purpose_text,
            },
            "patient": {"id": req.abha_address},
            "hip": {"id": req.hip_id},
            "hiu": {"id": req.hiu_id},
            "requester": {
                # This describes who is requesting the data (human/system context)
                "name": "OncoSearch Clinical Decision Support System",
                "identifier": {
                    # Describes the category of identifier
                    # (REGNO, Registration Number) (LICENSE) (ORGID)
                    "type": "REGNO",
                    # The requester (OncoSearch system) is officially identified by registration number ONCOSEARCH-001, issued by the OncoSearch system itself.
                    "value": "ONCOSEARCH-001",
                    "system": "https://oncosearch.example.com", # namespace
                }
            },
            # Types of health information requested
            # HI (Health Information) Types are human-friendly + policy-friendly abstractions.
            # | HI Type          | Meaning          | FHIR Resource     |
            # | ---------------- | ---------------- | ----------------- |
            # | OPConsultation   | Doctor visit     | Encounter         |
            # | DiagnosticReport | Lab/report       | DiagnosticReport  |
            # | Prescription     | Medicines        | MedicationRequest |
            # | DischargeSummary | Hospital summary | Composition       |
            # ABDM HI Types  →  mapped to  →  HL7 FHIR Resources
            # HI types are defined by ABDM (India) specific, standardized by the National Health Authority
            # Source: https://sandboxcms.abdm.gov.in/uploads/FAQ_19_08_2025_05de71fac8.pdf
            "hiTypes": [
                "OPConsultation",
                "DiagnosticReport",
                "Prescription",
                "DischargeSummary",
            ],
            "permission": {
                # Access mode defines what operations are allowed on the data.
                # "VIEW" = read-only access. No modification, storage by HIP, or write-back allowed.
                # Suitable for analytics / CDS systems like OncoSearch.
                "accessMode": "VIEW",

                # Defines the time window of health data being requested.
                # HIP will only share records that fall within this range.
                # Helps enforce data minimization and prevents over-fetching unnecessary history.
                "dateRange": {"from": "2020-01-01T00:00:00Z", "to": now},

                # Mandatory compliance field (ABDM requirement).
                # Specifies the deadline by which the HIU must delete the data.
                "dataEraseAt": (datetime.now(timezone.utc) + timedelta(days=DATA_RETENTION_DAYS)).isoformat(),

                # Controls how often data access is allowed under this consent.
                # This configuration represents a one-time data access:
                # - unit: HOUR → time granularity
                # - value: 1 → interval (every 1 hour)
                # - repeats: 0 → no repetition (single use only)
                # Useful to prevent unintended continuous data pulls or subscriptions.
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

    active_transactions[transaction_id] = {
        "consentId": consent_id,
        "abhaAddress": req.abha_address,
        "requestId": request_id,
        "status": "AWAITING_DATA",
        "initiatedAt": now,
    }

    # Persist consent transaction to HIU database
    data_erase_at = (datetime.now(timezone.utc) + timedelta(days=DATA_RETENTION_DAYS)).isoformat()
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consent_transactions (transaction_id, consent_id, request_id, abha_address, purpose_code, data_erase_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (transaction_id, consent_id, request_id, req.abha_address, req.purpose_code, data_erase_at))

        # Audit log: consent triggered
        cur.execute("""
            INSERT INTO data_access_log (abha_address, transaction_id, action) VALUES (%s, %s, %s)
        """, (req.abha_address, transaction_id, 'CONSENT_TRIGGERED'))

        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Failed to persist consent transaction: %s", e)
        # Continue anyway — consent was already sent to gateway

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

    if transaction_id in active_transactions:
        active_transactions[transaction_id]["status"] = "DATA_RECEIVED"
        active_transactions[transaction_id]["receivedAt"] = datetime.now(timezone.utc).isoformat()

    # Persist FHIR bundle(s) to cache and update transaction status in DB
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Fetch data_erase_at from the consent transaction
        cur.execute("SELECT data_erase_at, abha_address FROM consent_transactions WHERE transaction_id = %s",
                    (transaction_id,))
        txn_row = cur.fetchone()

        if txn_row:
            data_erase_at = txn_row['data_erase_at']
            abha_address = txn_row['abha_address']
        else:
            # Fallback if transaction row doesn't exist (shouldn't happen)
            data_erase_at = datetime.now(timezone.utc) + timedelta(days=DATA_RETENTION_DAYS)
            abha_address = "unknown"

        # Process each FHIR bundle entry
        for entry in entries:
            bundle = entry.get("content", {})
            if bundle.get("resourceType") == "Bundle":
                # Extract ABHA address from bundle if possible
                patients = _resources_of_type(bundle, "Patient")
                if patients:
                    for ident in patients[0].get("identifier", []):
                        if "healthid.ndhm.gov.in" in ident.get("system", ""):
                            abha_address = ident.get("value", abha_address)
                            break

                # Store FHIR bundle in cache
                cur.execute("""
                    INSERT INTO health_data_cache (transaction_id, abha_address, fhir_bundle, entry_count, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (transaction_id, abha_address, json.dumps(bundle), bundle.get("total", len(bundle.get("entry", []))), data_erase_at))

        # Update transaction status to DATA_RECEIVED
        cur.execute("""
            UPDATE consent_transactions
            SET status = %s, data_received_at = NOW()
            WHERE transaction_id = %s
        """, ('DATA_RECEIVED', transaction_id))

        # Audit log: data received
        cur.execute("""
            INSERT INTO data_access_log (abha_address, transaction_id, action) VALUES (%s, %s, %s)
        """, (abha_address, transaction_id, 'DATA_RECEIVED'))

        conn.commit()
        conn.close()
        log.info("FHIR bundle(s) cached | txn=%s patient=%s", transaction_id, abha_address)
    except Exception as e:
        log.error("Failed to cache FHIR bundle: %s", e)
        # Continue anyway — bundle is already printed to logs

    # Print oncology summary for each bundle
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
    Retention enforcement: query-time filter ensures expired data is never served.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Write audit log: data access
        cur.execute("""
            INSERT INTO data_access_log (abha_address, action) VALUES (%s, %s)
        """, (abha_address, 'DATA_ACCESSED'))
        conn.commit()

        # Query: only return non-deleted, non-expired data
        cur.execute("""
            SELECT transaction_id, stored_at, expires_at, entry_count, fhir_bundle
            FROM health_data_cache
            WHERE abha_address = %s AND is_deleted = FALSE AND expires_at > NOW()
            ORDER BY stored_at DESC
        """, (abha_address,))

        rows = cur.fetchall()
        conn.close()

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

        return {
            "abha_address": abha_address,
            "records": records,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("Error retrieving cached patient data: %s", e)
        raise HTTPException(status_code=500, detail="Error retrieving cached data")


@app.get("/transactions")
async def list_transactions():
    """
    Debug endpoint — see all consent transactions from the database.
    This is now the persistent, restart-safe source of truth.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, transaction_id, consent_id, request_id, abha_address, status,
                   purpose_code, data_erase_at, initiated_at, data_received_at
            FROM consent_transactions
            ORDER BY initiated_at DESC
        """)
        rows = cur.fetchall()
        conn.close()

        transactions = {}
        for row in rows:
            transactions[row['transaction_id']] = {
                "consentId": row['consent_id'],
                "abhaAddress": row['abha_address'],
                "requestId": row['request_id'],
                "status": row['status'],
                "purposeCode": row['purpose_code'],
                "dataEraseAt": row['data_erase_at'].isoformat() if row['data_erase_at'] else None,
                "initiatedAt": row['initiated_at'].isoformat() if row['initiated_at'] else None,
                "receivedAt": row['data_received_at'].isoformat() if row['data_received_at'] else None,
            }
        return transactions
    except Exception as e:
        log.error("Error listing transactions: %s", e)
        # Fallback to in-memory dict (for development/debugging)
        return active_transactions
