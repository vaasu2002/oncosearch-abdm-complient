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
from datetime import datetime, timedelta, timezone
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

# URL where ABDM Gateway will send the consent status and data flow notification
HIU_CALLBACK_URL = os.getenv(
    "HIU_CALLBACK_URL",
    "http://oncosearch-hiu:8002/v0.5/health-information/transfer"
)

DATA_RETENTION_DAYS = int(os.getenv(
    "DATA_RETENTION_DAYS",
    31
))
# In-memory transaction log
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
    Parses and prints a clean oncology summary.
    """
    transaction_id = payload.get("transactionId", "unknown")
    entries        = payload.get("entries", [])

    log.info("FHIR data received | txn=%s entries=%d", transaction_id, len(entries))

    if transaction_id in active_transactions:
        active_transactions[transaction_id]["status"] = "DATA_RECEIVED"
        active_transactions[transaction_id]["receivedAt"] = datetime.now(timezone.utc).isoformat()

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


@app.get("/transactions")
async def list_transactions():
    """Debug endpoint — see all active consent transactions."""
    return active_transactions
