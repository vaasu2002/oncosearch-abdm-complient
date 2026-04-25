"""
Mock ABDM Gateway Service
Simulates the National Health Authority (NHA) ABDM gateway.
Auto-approves consent requests and forwards health data requests to HIPs.
"""

import uuid
import logging
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GATEWAY] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Mock ABDM Gateway", version="0.5.0")

# ------------------------------------------------------------------ #
# Schemas
# ------------------------------------------------------------------ #

class ConsentPurpose(BaseModel):
    code: str
    text: str

class ConsentRequester(BaseModel):
    name: str
    identifier: dict[str, Any] | None = None

class ConsentPatient(BaseModel):
    id: str  # ABHA address

class ConsentPermission(BaseModel):
    accessMode: str = "VIEW"
    dateRange: dict[str, str] | None = None
    dataEraseAt: str | None = None
    frequency: dict[str, Any] | None = None

class ConsentHIP(BaseModel):
    id: str

class ConsentHIU(BaseModel):
    id: str

class ConsentDetail(BaseModel):
    purpose: ConsentPurpose
    patient: ConsentPatient
    hip: ConsentHIP
    hiu: ConsentHIU
    requester: ConsentRequester
    hiTypes: list[str] = []
    permission: ConsentPermission | None = None

class ConsentInitRequest(BaseModel):
    requestId: str
    timestamp: str
    consent: ConsentDetail
    # HIU callback URL so gateway knows where to send data
    hiuCallbackUrl: str | None = None


class HealthInfoRequest(BaseModel):
    requestId: str
    timestamp: str
    transactionId: str
    consent: dict[str, Any]
    dataPushUrl: str           # HIU endpoint to receive FHIR bundle
    hipId: str | None = None   # which HIP to route to


# ------------------------------------------------------------------ #
# In-memory store (sufficient for a mock)
# ------------------------------------------------------------------ #
consent_store: dict[str, dict] = {}


# ------------------------------------------------------------------ #
# Background task: instruct the HIP to push data
# ------------------------------------------------------------------ #
async def forward_to_hip(transaction_id: str, consent_id: str,
                         abha_address: str, data_push_url: str) -> None:
    hip_url = "http://mock-hip:8001/v0.5/health-information/request"
    payload = {
        "requestId": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transactionId": transaction_id,
        "consentId": consent_id,
        "abhaAddress": abha_address,
        "dataPushUrl": data_push_url,
    }
    log.info("Forwarding data request to HIP | txn=%s patient=%s", transaction_id, abha_address)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(hip_url, json=payload)
            log.info("HIP responded with HTTP %s", resp.status_code)
    except Exception as exc:
        log.error("Failed to reach HIP: %s", exc)


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-abdm-gateway"}


@app.post("/v0.5/consent-requests/init")
async def consent_init(req: ConsentInitRequest, background_tasks: BackgroundTasks):
    """
    HIU calls this to request consent for a patient's health data.
    We auto-approve and return a consentId + transactionId immediately,
    then asynchronously instruct the HIP to push data to the HIU callback.
    """
    consent_id = f"consent-{uuid.uuid4()}"
    transaction_id = f"txn-{uuid.uuid4()}"
    abha_address = req.consent.patient.id

    consent_store[consent_id] = {
        "consentId": consent_id,
        "transactionId": transaction_id,
        "abhaAddress": abha_address,
        "status": "GRANTED", # Auto approved in simulation
        "requestId": req.requestId,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    log.info(
        "Consent AUTO-APPROVED | consentId=%s txn=%s patient=%s",
        consent_id, transaction_id, abha_address
    )

    # Determine where the HIU wants the FHIR bundle delivered
    data_push_url = (
        req.hiuCallbackUrl
        or "http://oncosearch-hiu:8002/v0.5/health-information/transfer"
    )

    # FastAPI will execute forward_to_hip only after the response has been sent to the client (HIU) from gateway.
    # It is an explicit ordering guarantee by the framework.
    # Response sent to HIU  ->  THEN background task starts
    background_tasks.add_task(
        forward_to_hip, transaction_id, consent_id, abha_address, data_push_url
    )

    return {
        "requestId": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resp": {
            "requestId": req.requestId,
            "status": "SUCCESS",
        },
        "consentId": consent_id,
        "transactionId": transaction_id,
        "status": "GRANTED",
    }


@app.get("/v0.5/consents/{consent_id}")
async def get_consent(consent_id: str):
    if consent_id not in consent_store:
        raise HTTPException(status_code=404, detail="Consent not found")
    return consent_store[consent_id]


@app.post("/v0.5/health-information/request")
async def health_info_request(req: HealthInfoRequest, background_tasks: BackgroundTasks):
    """
    Alternate entry: HIU explicitly requests data after consent is granted.
    Routes to the appropriate HIP.
    """
    consent = consent_store.get(req.consent.get("id", ""))
    abha_address = consent["abhaAddress"] if consent else "unknown@abdm"

    background_tasks.add_task(
        forward_to_hip,
        req.transactionId,
        req.consent.get("id", ""),
        abha_address,
        req.dataPushUrl,
    )
    return {"status": "ACCEPTED", "transactionId": req.transactionId}
