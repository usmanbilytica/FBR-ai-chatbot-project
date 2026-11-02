# FBR Greenfield Agentic HR DSS

Enterprise multi-agent decision support over:

1. **HRMS** (`FBR_HRMS`) — scoped Text-to-SQL with rare-table recovery  
2. **Personal Files** (`FBR_OCR_GROUPED`) — full category/event/page access with RBAC redaction  
3. **Policies / JDs** (`RAG Documents/`) — chunked corpus + Schedule-II / Ex-Cadre hops  
4. **Deterministic rule engines** — sanctioned strength, transfer/succession fill-modes, promotion, training, JD gate  

Live HRMS/OCR only — no hardcoded demo officers or seats.

## Quick start

```powershell
cd "c:\Users\User\Desktop\Greenfield Project"
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# Bootstrap live metadata
.\.venv\Scripts\python scripts\bootstrap_status_map.py
.\.venv\Scripts\python scripts\build_catalog.py
.\.venv\Scripts\python scripts\ingest_policies.py
.\.venv\Scripts\python scripts\index_pf_pages.py

# API
.\.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- API docs: `http://127.0.0.1:8000/docs`  
- Chat UI: `http://127.0.0.1:8000/ui/` (sign in with HRMS UserID)  
- Probes: `/api/v1/health` · `/api/v1/ready`

## Auth

Production (`ENVIRONMENT=production`) **requires** a session. Launch via `GET /api/v1/me?userid=` or `POST /api/v1/auth/login`. Dev-only open access: `ENVIRONMENT=development` + `ALLOW_DEV_OPEN_ACCESS=1`.

## Key endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/health` | Liveness + dependency flags |
| GET | `/api/v1/ready` | Readiness (503 if critical deps down) |
| POST | `/api/v1/auth/login` | Session from HRMS UserID |
| GET | `/api/v1/me?userid=` | HRMS launch login → session cookie |
| POST | `/api/v1/chat/ask` | Full answer + evidence + rule_trace |
| POST | `/api/v1/chat/ask/stream` | SSE steps then done |
| GET | `/api/v1/ops/cnic-coverage` | CNIC bridge stats (AccessID 0) |

## Tests

```powershell
.\.venv\Scripts\pytest -q
.\.venv\Scripts\pytest -q -m integration
.\.venv\Scripts\python scripts\eval_golden.py
```

See [RUNBOOK.md](RUNBOOK.md) for production bootstrap, indexing, and Definition of Done.
