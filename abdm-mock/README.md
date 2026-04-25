# ABDM Mock Ecosystem — OncoSearch Local Dev Environment

Self-contained Docker environment that simulates the full ABDM data flow
without touching the real NHA sandbox.

## Architecture

```
You (curl)
    │
    ▼
oncosearch-hiu :8002   ←──── FHIR Bundle (webhook) ─────┐
    │                                                     │
    │  POST /v0.5/consent-requests/init                   │
    ▼                                                     │
mock-gateway :8000                                        │
    │                                                     │
    │  POST /v0.5/health-information/request              │
    ▼                                                     │
mock-hip :8001  ──── queries ──►  postgres-db :5432 ─────┘
```

## Quick Start

```bash
cd abdm-mock
docker-compose up --build
```

Wait ~20 seconds for all services to become healthy, then in a **second terminal**:

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

After running either command, switch back to the terminal running
`docker-compose up` and watch the logs. Within 1-2 seconds you will see:

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

## Individual Service APIs

| Service         | Base URL                  | Key Endpoints                                      |
|-----------------|---------------------------|----------------------------------------------------|
| OncoSearch HIU  | http://localhost:8002     | `POST /consent/trigger`, `GET /transactions`       |
| Mock Gateway    | http://localhost:8000     | `POST /v0.5/consent-requests/init`                 |
| Mock HIP        | http://localhost:8001     | `POST /v0.5/health-information/request`            |
| Postgres        | localhost:5432            | DB: `abdm_mock`, User: `abdm_user`, Pass: `abdm_pass` |

### Interactive API docs (FastAPI Swagger UI)

- Gateway:  http://localhost:8000/docs
- HIP:      http://localhost:8001/docs
- HIU:      http://localhost:8002/docs

## Check Active Transactions

```bash
curl -s http://localhost:8002/transactions | python3 -m json.tool
```

## Teardown

```bash
docker-compose down -v   # -v removes the postgres volume (wipes seed data)
```
