# RUNBOOK — FBR Greenfield DSS (Production)

## 1. Environment

- Python 3.12+, ODBC Driver 18 for SQL Server  
- `.env` with `HRMS_DB_*`, `OCR_DB_*`, `LLM_*`, strong `SESSION_SECRET` (≥24 chars)  
- OCR port **1433**, HRMS port **1434**  
- `ENVIRONMENT=production` (default) — **denies unauthenticated chat**  
- Optional local-only: `ENVIRONMENT=development` + `ALLOW_DEV_OPEN_ACCESS=1`  
- `RATE_LIMIT_PER_MINUTE=60` · `COOKIE_SECURE=1` behind HTTPS  

## 2. Bootstrap (required before recommend tools)

```powershell
python scripts/bootstrap_status_map.py   # data/status_map.json (holding/block statuses)
python scripts/build_catalog.py          # data/catalog/schema_catalog.json
# Always use the project venv (system Python often has old qdrant-client 1.12)
.\.venv\Scripts\python.exe scripts\ingest_policies.py        # PDFs → policy_facts.db + OCR scans → data/qdrant_policy
.\.venv\Scripts\python.exe scripts\index_pf_pages.py         # streamed full PF vectors → data/qdrant_pf
.\.venv\Scripts\python.exe scripts\cnic_coverage.py          # HRMS↔OCR bridge coverage
```

Staged: `.\.venv\Scripts\python.exe scripts\index_pf_pages.py --limit 5000`  
Resume PF: `--resume` · wipe PF store: `--reset-store`  

**Notes**
- `CreateCollection` errors with system `python` = old qdrant-client 1.12 — use `.venv\Scripts\python.exe`
- PF vectors: `data/qdrant_pf` · Policy vectors: `data/qdrant_policy` (safe to run both indexers in parallel)
- Scanned/image PDFs are OCR’d via RapidOCR when native text is empty/sparse

## 3. Run

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Docker:

```powershell
docker compose up --build
```

Probes: `GET /api/v1/health` · `GET /api/v1/ready` (503 if HRMS/OCR/catalog/status_map missing)

## 4. Auth (end-user)

- **No cookie ⇒ 401** on `/chat/ask` (production)  
- HRMS launch: `GET /api/v1/me?userid=` or UI `POST /api/v1/auth/login` with UserID  
- Validates `adm_user_token` (unexpired) + loads `adm_user_company` AccessID  
- Cookie: `fbr_session` (HttpOnly, 12h) · `POST /api/v1/auth/logout`  
- AccessID: 0 full · 1 location · 2 org · 3 self  
  - Self: no org succession recommend; own PF only  
  - Finalize / ops CNIC coverage: full only  
- All Text-to-SQL + candidate pools honor AccessScope predicates + post-filter  

## 5. Agentic multi-source explorer (default)

- `AGENTIC_EXPLORER=1` (default): **no hard source router**. Full tool surface:
  - **HRMS:** `search_catalog`, `describe_table`, `search_designations|locations|org_levels|grades|positions`, `list_sanction_allocations`, `sanction_strength` (grade|designation|**position**), `run_hrms_sql`, `resolve_officer`
  - **PF:** `pf_resolve`, `pf_categories`, `pf_pages`, `pf_events`, `pf_segments`, `pf_files`, `pf_profile`, `pf_vector_search`, `pf_search`
  - **Policy:** `search_policies` (filters), `list_policy_docs`, `list_policy_doc_types`, `policy_corpus_stats`, `jd_authority`, `schedule_ii`, `list_schedule_ii`
  - **Rules-as-tools:** `rule_promotion`, `rule_succession`, `rule_training`, `rule_jd`
- Caps: `AGENT_MAX_STEPS` (default 10) · `AGENT_ALLOW_SQL=1`
- Escape hatch: `AGENTIC_EXPLORER=0` → legacy `route_intent` pipeline
- Example: IRS Additional Director Hyderabad → sanctioned **3** via allocation explore

## 6. Live rule engines (no demo fixtures)

| Ask | Engine | Live inputs |
|-----|--------|-------------|
| Sanctioned / vacancy | `sanction_strength` | grades / designation / `pr_position_mf` + status_map holding IDs |
| Permanent / OPS / TPS / look-after | `succession_search` + fill_modes | live vacant seats + scoped candidates from HRMS |
| Cadre promotion N | `promotion_cadre` | live officer, Schedule-II years, trainings, PF discipline flags, grade vacancy |
| Ex-Cadre promotion | `promotion_excadre` | gazette hops (seed ∪ policy-extracted) + designation capacity |
| Training nomination | `eligibility_training_nomination` | live course + officer DOB/org + prior courses |
| JD gate | `jd_gate` | authority books + retrieved JD text |

## 7. Personal Files

Hybrid retrieval: OCR SQL (category/CNIC) **+** Qdrant `data/qdrant_pf` vectors when the store is unlocked. While `index_pf_pages.py` holds the Windows lock, vector search returns `status=unavailable` and SQL continues. Scope redaction strips medical/family/discipline as required by AccessID. No-PF ⇒ explicit note.

## 8. Production hardening flags

| Flag | Prod intent |
|------|-------------|
| `REQUIRE_TOKEN_DECRYPT=1` | Launch login fails if token decrypt fails |
| `REQUIRE_REAL_EMBEDDINGS=1` | No silent hash-embed fallback |
| `ALLOW_OPEN_SQL` | When on, production still restricts open SQL to AccessID 0 |
| `COOKIE_SECURE=1` | Secure cookies; use `ALLOW_INSECURE_COOKIES=1` only for local HTTP UAT |
| Policy vectors | `data/qdrant_policy` only (legacy `data/qdrant` unused) |

UAT goldens: `data/uat_goldens.json` (all three sources + rules).

## 9. Definition of Done checklist

- [x] App runs from this repo with `.env` only  
- [x] Auth deny-by-default in production; login via userid  
- [x] No hardcoded demo officers/seats in orchestrator  
- [x] RBAC predicates on Text-to-SQL + succession candidates + PF  
- [x] Sanction uses status_map holding_occupy_ids  
- [x] Index scripts support full corpus (`INDEX_*_LIMIT=0`)  
- [x] Rate limit + security headers + `/ready` (policy vector counts; PF via checkpoint)  
- [x] Chat UI requires sign-in  
- [x] Unit tests include auth + no-demo guardrail  
- [x] PF hybrid + policy `qdrant_policy` + synth merges all sources  
- [ ] Full PF reindex completed in target environment  
- [ ] UAT golden cases signed off by HR domain owners (`data/uat_goldens.json`)  
- [ ] `COOKIE_SECURE=1` + HTTPS termination verified (disable `ALLOW_INSECURE_COOKIES`)  

## 10. Non-goals

Read/recommend only (no HRMS write-back). No invented bars beyond encoded rules + JD hard codes + Schedule-II / gazette facts.
