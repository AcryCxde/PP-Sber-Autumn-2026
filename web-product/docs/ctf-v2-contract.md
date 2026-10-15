# Proposed CTF product UI V2 contract

Status: proposed, NOT approved/implemented by this frontend. Existing gateway uses history REST and WS run.create/run.subscribe; candidate B needs an adapter or reconciled server contract. Do not silently introduce a second incompatible production schema. Candidate A remains a technical reference/fallback, B preserves the richer requested user flow.

Owner responsibilities: Дмитрий gateway transport/contract mapping; Илья CTF lifecycle and validated AI-role assignment; Арина persistent file/project/run/event/document/version/approval storage; Филипп common deployment/access; Катя UX and measurements. Original dirty PPS is untouched. No new executors or human specialists were recruited.

## Owner-approved WEB position16–21 — 2026-10-16 implementation

The owner approved [these exact six answers](https://github.com/pokatyusha/project-harness/blob/main/handoff/2026-10-15/contracts/web-answers-16-21.md) and requested their implementation in B, keeping PR11 draft. This document remains the **one existing B proposal**, updated in place, NOT a second service protocol or joint API acceptance. A stays unchanged as reference. The HTTP projection, linkage/receipt fields and role-event projection below need mapping to the accepted gateway/runner/storage contracts by their owners. No real server integration is enabled by this change.

## Public setup

`VITE_CTF_API_URL` is an absolute HTTP(S) base, typically same-origin `/api` as a full URL. `VITE_CTF_V2_ENABLED=true` only after implementations are checked. No tokens/passwords in frontend env. Cookie credentials use same-origin; cross-origin auth/CSRF/CORS must be designed, not assumed. Test fixture4191 is not a user-facing endpoint.

## Routes relative to base

| Operation | Request | Response |
|---|---|---|
| POST projects | clientProjectId, title, goal, context, scenario, preparation answers, selected role IDs/teamNote, editable stage IDs/title/goal, input-file metadata | `{id}` durable server ID |
| POST projects/{id}/files | FormData file bytes, clientFileId, sha256; Idempotency-Key=file ID | `{id,sha256}` matching received bytes; durable/scanned ingest and extraction status must be defined separately |
| POST projects/{id}/runs | initial: stable original project payload, known stageId, uploaded fileIds; rework after completed: same project payload + text, stageId, parentRunId, sourceArtifactId, documentId, sourceVersion, uploaded fileIds | new Run snapshot; rework must have NEW id and exact parentRunId/sourceArtifactId |
| GET projects/{id}/runs/{runId} | none | Run snapshot; monotonically increasing revision |
| POST projects/{id}/runs/{runId}/answers | questionId, answers: map with EXACT question texts as keys | same Run, not a new run; gateway maps to fork_answer. multi-select labels/custom joined by comma + space, per existing runner protocol |
| POST projects/{id}/runs/{runId}/artifacts/{artifactId}/approve | artifactId, version; runId is the version's producing Run | independent receipt `{receiptId,artifactId,version,approved:true,approvedAt:ISO}` for EXACT saved version; NOT a Run snapshot/event appended after terminal |
| GET projects/{id}/runs/{runId}/artifacts/{artifactId}/zip | none | application/zip for the confirmed stored immutable version |

Run: `{id,revision,status,stageId,question?,progress?,parentRunId?,sourceArtifactId?,messages,artifacts,roleEvents?,usage?}`. Status: queued/running/clarification/waiting_for_input/review/completed/failed/outcome_unknown. Completed/failed/outcome_unknown are explicit server terminal outcomes: immutable status/revision/history; no reopening or late events. `review` alone is NOT technical completion and cannot start rework. User acceptance is separate from completion. A transport timeout never sets terminal outcome_unknown or success.

Waiting requires `question:{id,questions:[{text,header:string|null,options:[{label,description:string|null}],multi_select:boolean,allow_custom:boolean}]}`. All questions are rendered; each requires an answer. Duplicate texts/options are rejected as ambiguous; single-choice/custom are mutually exclusive. GET refresh never carries an old question's answers into a different question. HTTP4xx `code:"unknown_fork"` (also supported at `error.code`) is a definite rejection: mark old operation non-replayable, GET current state and ask for answers again; if GET fails, manual refresh remains available. Do not treat 5xx/transport ambiguity as rejection. Old persisted v2 mutations retain original bytes/ID, are marked legacy and cannot be silently replayed as new format.

`progress:{stage:interrupted|recovering|working,attempt?:positiveInt,context_restored?:boolean}` represents server evidence, not client timers. Interrupted/recovering disables the old answer form; the question can be absent while it is being replaced. `roleEvents?:[{id,roleId,type:role.started|role.completed|role.failed,at:ISO}]` is a **proposed snapshot projection** of existing role events; immutable IDs/payloads. No events means no execution evidence, regardless of selected team/prompt. Stage IDs must belong to the user's plan; gateway must validate against its stored plan and roles against its registry, not merely echo client input. Before upload, client recomputes SHA-256 over actual local bytes and stops on mismatch; matching upload receipt is required before initial Run. Real ingest/durability/security are server gates, not proved by an echoed hash.

Message: `{id,role:user|facilitator|system,text,at:ISO}`. IDs/text/history stable across all project Runs. Artifact: `{id,documentId,stageId,version,title,content,saved,savedAt?,approved?}`. documentId groups immutable versions across Runs; different documents can exist in one stage. Positive integer version unique within stage+document, advancing for new versions. Old bytes cannot change on rework or polling. saved=true requires acknowledged persistent storage and savedAt. Legacy artifact.approved alone is NOT proof of approval; the separate matching receipt is required and stored outside Run. Actual approval storage/authorization and receipt issuance still require agreement with gateway/storage. ZIP uses the producing Run and exact version, even when an old version is selected. A green UI state is not a substitute for a real storage transaction. ZIP package must retain source/evidence/privacy boundaries from the agreed pilot-result skill.

Usage: optional actual `inputTokens`, `outputTokens`, `requests` (nonnegative integers), `computeSeconds` (nonnegative), `cost` plus `currency`. Omitted/null means unknown, numeric zero means measured zero. Workspace shows CURRENT Run. Consented study summary aggregates latest partial observations PER Run (not every poll): a field stays unknown if any known Run lacks it; costs sum only with matching known currency. No billing, estimated tokens or product quota gates. Provider rate/safety/storage limits remain.

## Integrity and retry

Create uses stable clientProjectId-derived idempotency key. Each file has a fixed UUID/hash and deduplicated ingest. Run/answer/rework/approve use stored operation UUID and original payload on manual retry; backend deduplication scoped to authenticated actor+project and rejects changed-body collisions. Never automatically repeat POSTs. No more same-Run `/messages` rework. Poll queued/running/waiting serially every2.5s; stop on error, pending mutation or terminal/review. Manual GET works with a pending request but does not silently resolve uncertain mutations. Preserve project context, historical Runs/messages/versions and version-specific receipts; ignore stale snapshots, reject wrong run/linkage and immutable history changes. Browser envelope upgrades2→3 in the SAME storage key without clearing files/studies; corrupt data are never overwritten. Cross-device restore and resolving unknown start/rework operation IDs still need server support.

## Required live gates

Validate chosen roles against CTF registry; unknown/unavailable roles must be reported, not silently “created”. Interpret uploaded files as untrusted task data, scan/parse server-side without executing instructions from them. Define extraction results and safe bounds. Implement ownership/tenant isolation, auth/CSRF, actual persistence/retention/deletion and cross-device project list/read; B only reopens browser snapshots. Test real file bytes, same-run question/answer, revision histories, write acknowledgment, ZIP, outage/idempotency/recovery and provider usage. Technical E2E and consented user validation remain separate.
