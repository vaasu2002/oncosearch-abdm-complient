# ABDM Mock Ecosystem — OncoSearch Local Dev Environment

Self-contained Docker environment that simulates the full ABDM (Ayushman Bharat Digital Mission) data flow without touching the real NHA sandbox. Designed for developing and testing the OncoSearch clinical decision support system.

---

## Architecture

```
You (curl / clinician UI)
          │
          │  POST /consent/trigger
          ▼
 ┌─────────────────────┐        FHIR R4 Bundle (webhook)
 │  oncosearch-hiu     │◄────────────────────────────────┐
 │  :8002              │                                  │
 │  (OncoSearch app)   │                                  │
 └────────┬────────────┘                                  │
          │                                               │
          │  POST /v0.5/consent-requests/init             │
          ▼                                               │
 ┌─────────────────────┐   POST /v0.5/health-information  │
 │  mock-gateway       │──────────────/request ──────────►│
 │  :8000              │                                  │
 │  (NHA ABDM Gateway) │                            ┌─────┴──────────────┐
 └─────────────────────┘                            │  mock-hip :8001    │
                                                    │  (Hospital HIP)    │
                                                    └────────┬───────────┘
                                                             │ SQL queries
                                                             ▼
                                              ┌──────────────────────────┐
                                              │  abdm-postgres :5432     │
                                              │  (HIP hospital records)  │
                                              └──────────────────────────┘

 ┌──────────────────────────┐
 │  oncosearch-postgres     │
 │  :5433                   │◄── oncosearch-hiu writes consent
 │  (HIU's own database)    │    transactions + FHIR cache here
 └──────────────────────────┘
```

### Services

| Container              | Port  | Role                                                             |
|------------------------|-------|------------------------------------------------------------------|
| `abdm-oncosearch-hiu`  | 8002  | OncoSearch app (HIU) — triggers consent, receives FHIR bundles  |
| `abdm-mock-gateway`    | 8000  | Simulates NHA ABDM Gateway — auto-approves consent              |
| `abdm-mock-hip`        | 8001  | Simulates hospital HIP — builds and pushes FHIR R4 Bundles      |
| `abdm-postgres`        | 5432  | HIP's hospital database (patients, conditions, labs, meds)      |
| `oncosearch-postgres`  | 5433  | HIU's dedicated database (consent transactions, FHIR cache)     |

> **Database separation is intentional.** The HIU (`oncosearch-postgres`) and the HIP (`abdm-postgres`) are separate organizations with separate data boundaries — mirroring the real ABDM architecture.

---

## Data Flow (step by step)

1. **Clinician triggers consent** — `POST /consent/trigger` on the HIU with a patient's ABHA address.
2. **HIU sends consent request** to the Gateway (`POST /v0.5/consent-requests/init`).  
   The consent is persisted to `consent_transactions` in `oncosearch-postgres`.
3. **Gateway auto-approves** and returns `consentId` + `transactionId`.  
   As a background task, it immediately calls the HIP.
4. **HIP receives the request**, queries its Postgres for the patient's oncology record, assembles a FHIR R4 Bundle (Patient + Conditions + Observations + MedicationStatements), and pushes it to the HIU webhook.
5. **HIU receives the FHIR bundle** at `POST /v0.5/health-information/transfer`.  
   The bundle is stored in `health_data_cache` with an `expires_at` derived from the consent's `dataEraseAt`.  
   An oncology summary is printed to the console logs.

---

## Quick Start

```bash
cd abdm-mock
docker-compose up --build
```

Wait ~20 seconds for all services to become healthy, then open a second terminal.

---

## Trigger the Full Data Flow

### Patient 1 — Arjun Mehta (NSCLC, EGFR mutation, Osimertinib)

```bash
curl -s -X POST http://localhost:8002/consent/trigger \
  -H "Content-Type: application/json" \
  -d '{"abha_address": "arjun.mehta@abdm"}' | python3 -m json.tool
```

### Patient 2 — Priya Sharma (Breast cancer, BRCA1 mutation, Olaparib)

```bash
curl -s -X POST http://localhost:8002/consent/trigger \
  -H "Content-Type: application/json" \
  -d '{"abha_address": "priya.sharma@abdm"}' | python3 -m json.tool
```

After running either command, switch back to the `docker-compose up` terminal. Within 1–2 seconds you'll see:

```
abdm-oncosearch-hiu  | ======================================================================
abdm-oncosearch-hiu  |   ONCOSEARCH — PATIENT DATA RECEIVED
abdm-oncosearch-hiu  | ======================================================================
abdm-oncosearch-hiu  |   Transaction ID : txn-<uuid>
abdm-oncosearch-hiu  |   Patient        : Arjun Mehta
abdm-oncosearch-hiu  |   ABHA Address   : arjun.mehta@abdm
abdm-oncosearch-hiu  |   Bundle Total   : 6 resources
abdm-oncosearch-hiu  | ======================================================================
abdm-oncosearch-hiu  |   DIAGNOSES (1)
abdm-oncosearch-hiu  |     [C34.1] Non-small cell lung cancer, upper lobe | Status: Active | Onset: 2022-11-10
abdm-oncosearch-hiu  |   GENOMIC & LAB RESULTS (3)
abdm-oncosearch-hiu  |     [+] EGFR gene mutation analysis
abdm-oncosearch-hiu  |         Result : EGFR Exon 19 deletion detected (p.E746_A750del)
...
```

---

## API Reference

### OncoSearch HIU — `http://localhost:8002`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/consent/trigger` | Trigger a consent request for a patient by ABHA address |
| `POST` | `/v0.5/health-information/transfer` | Webhook — HIP pushes FHIR R4 Bundle here (not called directly) |
| `GET`  | `/patient/{abha_address}/data` | Retrieve cached FHIR health data for a patient |
| `GET`  | `/transactions` | List all consent transactions (from database) |
| `GET`  | `/health` | Health check |
| `GET`  | `/docs` | Swagger UI |

**`POST /consent/trigger` request body:**

```json
{
  "abha_address": "arjun.mehta@abdm",
  "hip_id": "mock-hip-001",
  "hiu_id": "oncosearch-hiu-001",
  "purpose_code": "CAREMGT",
  "purpose_text": "Care Management — Oncology Clinical Decision Support"
}
```

**`GET /patient/{abha_address}/data` — retrieve cached data:**

```bash
curl -s http://localhost:8002/patient/arjun.mehta@abdm/data | python3 -m json.tool
```

Returns the cached FHIR R4 Bundle(s) without re-requesting consent. Only returns records that are not expired and not soft-deleted.

**`GET /transactions` — inspect consent lifecycle:**

```bash
curl -s http://localhost:8002/transactions | python3 -m json.tool
```

### Mock Gateway — `http://localhost:8000`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v0.5/consent-requests/init` | Receive consent request from HIU; auto-approve and forward to HIP |
| `POST` | `/v0.5/health-information/request` | Alternate explicit data request after consent |
| `GET`  | `/v0.5/consents/{consent_id}` | Look up a stored consent by ID |
| `GET`  | `/health` | Health check |
| `GET`  | `/docs` | Swagger UI |

### Mock HIP — `http://localhost:8001`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v0.5/health-information/request` | Receive data request from Gateway; build FHIR bundle and push to HIU |
| `GET`  | `/health` | Health check |
| `GET`  | `/docs` | Swagger UI |

---

## Database Reference

### HIP database — `abdm-postgres` (port 5432)

```
DB:   abdm_mock
User: abdm_user   Pass: abdm_pass
```

Contains the hospital's oncology records. Seeded by `postgres-db/init.sql`.

Tables: `patients`, `conditions`, `observations`, `medication_statements`

### HIU database — `oncosearch-postgres` (port 5433)

```
DB:   oncosearch_db
User: oncosearch_user   Pass: oncosearch_pass
```

The HIU's own persistent store. Initialized by `oncosearch-hiu/db/init.sql`.

| Table | Purpose |
|-------|---------|
| `consent_transactions` | Lifecycle of every consent request (survives HIU restarts) |
| `health_data_cache` | Cached FHIR R4 Bundles with `expires_at` and soft-delete flag |
| `data_access_log` | Append-only audit trail — records consent, data receipt, access, and erasure events |

**Connect directly (for inspection):**

```bash
# HIU database
psql -h localhost -p 5433 -U oncosearch_user -d oncosearch_db

# HIP database
psql -h localhost -p 5432 -U abdm_user -d abdm_mock
```

---

## Data Retention

The HIU enforces ABDM-mandated data retention automatically.

**Default retention period:** 31 days (configurable via `DATA_RETENTION_DAYS` env var)

### Two-layer enforcement

1. **Query-time filter** — Every `SELECT` on `health_data_cache` includes `expires_at > NOW() AND is_deleted = FALSE`. Expired data is never served even if cleanup hasn't run yet.

2. **Background scheduler** (APScheduler):
   - **Hourly** — Soft-deletes expired rows and writes `DATA_EXPIRED` entries to the audit log.
   - **Daily at 2:00 AM UTC** — Hard-deletes rows that have been soft-deleted for >24 hours; marks consent transactions as `EXPIRED`.

### Consent transaction statuses

| Status | Meaning |
|--------|---------|
| `AWAITING_DATA` | Consent granted; waiting for HIP to push FHIR bundle |
| `DATA_RECEIVED` | FHIR bundle received and cached |
| `EXPIRED` | Data retention window elapsed; data erased |
| `ERROR` | Something went wrong during the flow |

---

## Useful Commands

```bash
# Start all services
docker-compose up --build

# Follow logs for a specific service
docker-compose logs -f oncosearch-hiu
docker-compose logs -f mock-gateway
docker-compose logs -f mock-hip

# Check active transactions
curl -s http://localhost:8002/transactions | python3 -m json.tool

# Retrieve cached patient data
curl -s "http://localhost:8002/patient/arjun.mehta@abdm/data" | python3 -m json.tool

# Inspect the audit log (HIU database)
psql -h localhost -p 5433 -U oncosearch_user -d oncosearch_db \
  -c "SELECT * FROM data_access_log ORDER BY occurred_at DESC LIMIT 20;"

# Check consent transactions
psql -h localhost -p 5433 -U oncosearch_user -d oncosearch_db \
  -c "SELECT transaction_id, abha_address, status, data_erase_at FROM consent_transactions;"

# Teardown (removes all volumes and seed data)
docker-compose down -v
```

---

## Environment Variables

| Variable | Service | Default | Description |
|----------|---------|---------|-------------|
| `GATEWAY_URL` | oncosearch-hiu | `http://mock-gateway:8000` | URL of the ABDM Gateway |
| `HIU_CALLBACK_URL` | oncosearch-hiu | `http://oncosearch-hiu:8002/v0.5/health-information/transfer` | Webhook URL the HIP pushes FHIR data to |
| `DATABASE_URL` | oncosearch-hiu | `postgresql://oncosearch_user:...@oncosearch-postgres:5432/oncosearch_db` | HIU's PostgreSQL connection string |
| `DATA_RETENTION_DAYS` | oncosearch-hiu | `31` | How long FHIR data is retained before erasure |
| `DATABASE_URL` | mock-hip | `postgresql://abdm_user:...@postgres-db:5432/abdm_mock` | HIP's PostgreSQL connection string |
| `HIP_URL` | mock-gateway | `http://mock-hip:8001` | URL of the HIP the gateway routes to |
