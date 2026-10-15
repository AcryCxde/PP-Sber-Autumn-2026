# Proposed CTF product UI V2 contract

Status: proposed, NOT approved/implemented by this frontend. Existing gateway uses history REST and WS run.create/run.subscribe; candidate B needs an adapter or reconciled server contract. Do not silently introduce a second incompatible production schema. Candidate A remains a technical reference/fallback, B preserves the richer requested user flow.

Owner responsibilities: Дмитрий gateway transport/contract mapping; Илья CTF lifecycle and validated AI-role assignment; Арина persistent file/project/run/event/document/version/approval storage; Филипп common deployment/access; Катя UX and measurements. Original dirty PPS is untouched. No new executors or human specialists were recruited.

## Public setup

`VITE_CTF_API_URL` is an absolute HTTP(S) base, typically same-origin `/api` as a full URL. `VITE_CTF_V2_ENABLED=true` only after implementations are checked. No tokens/passwords in frontend env. Cookie credentials use same-origin; cross-origin auth/CSRF/CORS must be designed, not assumed. Test fixture4191 is not a user-facing endpoint.

## Routes relative to base

| Operation | Request | Response |
|---|---|---|
| POST projects | clientProjectId, title, goal, context, scenario, preparation answers, selected role IDs/teamNote, editable stage IDs/title/goal, input-file metadata | `{id}` durable server ID |
| POST projects/{id}/files | FormData file bytes, clientFileId, sha256; Idempotency-Key=file ID | `{id,sha256}` matching received bytes; durable/scanned ingest and extraction status must be defined separately |
| POST projects/{id}/runs | stable original project payload plus uploaded fileIds | Run snapshot below |
| GET projects/{id}/runs/{runId} | none | Run snapshot; monotonically increasing revision |
| POST projects/{id}/runs/{runId}/answers | questionId, answer | same Run, not a new run |
| POST projects/{id}/runs/{runId}/messages | text, stageId, optional immutable artifactId | same Run and actual recorded user/facilitator messages; revision request generates a new artifact version, never changes old bytes |
| POST projects/{id}/runs/{runId}/artifacts/{artifactId}/approve | artifactId, version | same Run; backend validates current saved version, rights, and progression |
| GET projects/{id}/runs/{runId}/artifacts/{artifactId}/zip | none | application/zip for the confirmed stored immutable version |

Run: `{id,revision,status,stageId,question?,messages,artifacts,usage?}`. status is running/clarification/review/completed/failed. Clarification requires question `{id,text}`. stageId must match agreed plan. Complete/failed are explicit backend outcomes. No inferred progress percentage, ETA, pretend credits or local work simulation.

Message: `{id,role:user|facilitator|system,text,at:ISO}`. IDs/text/history stable. Artifact: `{id,documentId,stageId,version,title,content,saved,savedAt?,approved?}`. documentId groups one logical artifact across immutable version IDs; different documents can exist in one stage. Positive integer version unique within stage+document. saved=true requires acknowledged persistent storage and savedAt. A green UI state is not a substitute for a real storage transaction. ZIP package must retain source/evidence/privacy boundaries from the agreed pilot-result skill.

Usage: optional actual `inputTokens`, `outputTokens`, `requests` (nonnegative integers), `computeSeconds` (nonnegative), `cost` plus `currency`. Map actual CTF/provider metrics; do not estimate missing values from UI timers or fabricate unit cost. Omitted means unknown, numeric zero means measured zero. Values are per Run; aggregation/billing not implemented. No product token/project/run quotas in UI; provider rate/safety/storage limits remain.

## Integrity and retry

Create uses stable clientProjectId-derived idempotency key. Each file has a fixed UUID/hash and deduplicated ingest. Run/answer/message/approve use stored operation UUID and original payload on retry; backend deduplication scoped to authenticated actor+project and rejects changed-body collisions. No automatic repeat of POSTs. Polling running status every2.5s is serial, stops on error and can resume manually. Frontend preserves known historical messages and versions, ignores stale revisions and rejects mutated version content or different run IDs.

## Required live gates

Validate chosen roles against CTF registry; unknown/unavailable roles must be reported, not silently “created”. Interpret uploaded files as untrusted task data, scan/parse server-side without executing instructions from them. Define extraction results and safe bounds. Implement ownership/tenant isolation, auth/CSRF, actual persistence/retention/deletion and cross-device project list/read; B only reopens browser snapshots. Test real file bytes, same-run question/answer, revision histories, write acknowledgment, ZIP, outage/idempotency/recovery and provider usage. Technical E2E and consented user validation remain separate.
