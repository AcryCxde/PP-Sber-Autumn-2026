# Integration handoff — Илья

Status date: 2026-10-15. Implemented client adapter; proposed server API, approval absent. Do not enable against gateway without an adapter. No backend changes in this PR.

Early audit: read-only `origin/demo-gateway@24e98dc19f674c2ef4220bf5a0d94818c93490d4`, `backend/main.py`, `backend/schemas.py`, `backend/api/history.py` and dirty-tree `web-demo/src/adapters/gatewayClient.mjs` as reference only. **Implemented in audited backend:** GET /api/conversations, /api/conversations/{id}/messages, /api/runs/{id}/events?after_seq&limit, /api/runs/{id}/events/{seq}/file; WS /ws with run.create/run.subscribe and stable client_request_id, conversation_id, context_mode. Artifact file endpoint returns one bounded text snapshot, not ZIP. **Approved:** none of the following proposed project routes. Mapping conversation/project, auth, clarification and packaging unresolved.

`VITE_API_BASE_URL` includes an optional path prefix, e.g. https://host/api. Proposed suffixes:

| Route | JSON request / response |
|---|---|
| POST projects | {clientProjectId,title} → {id} |
| POST projects/{id}/runs | {context} → Run |
| GET projects/{id}/runs/{runId} | → Run |
| POST projects/{id}/runs/{runId}/answers | {questionId,answer} → same Run |
| POST projects/{id}/artifacts | {runId,resultId} → {saved:true,artifactId,version,savedAt:ISO} |
| GET projects/{id}/artifacts/{artifactId}/zip | binary application/zip |

Run: {id,status:running|clarification|completed|failed,question?:{id,text},result?:{id,title,content}}. Question required for clarification; result required for completed. Status refresh is explicit (no invented timer/progress). Return durable IDs and map domain ownership server-side. Save ack must mean durable server commit, never merely queued. Proposed candidate ZIP: decision brief, sources with verification metadata, manifest/result version and checksums. Client validates MIME and PK signature, not the full archive structure.

POSTs carry Idempotency-Key and X-Correlation-ID. Backend must deduplicate by actor + key and reject conflicting bodies; client persists key + original payload before sending, retains on unknown/error outcome. No automatic retry. Failed validation must not acknowledge storage. GET uses new correlation ID. 20s timeout; auth errors show safe text, no server body echo. Cookie credentials only same-origin; cross-origin cookie auth/CSRF/CORS requires agreement, no token in VITE env. Prefix URLs must use HTTP(S), no embedded credentials/query/hash.

Required real checks before enabling: authenticated ownership/tenant isolation; exact accepted schemas; same run ID on answer/status; real clarification/resume; idempotency after disconnect; bounded artifacts; confirmed durable save/reopen across devices; valid complete ZIP; service downtime/403/timeout; CORS and CSRF. These were NOT validated against a live gateway.

Browser reopening restores local server references/run/save snapshot, not refreshed server history. Running/clarification supports manual status refresh. Project discovery on another device and full server project hydration need an approved read/list API; not implemented or promised here.
