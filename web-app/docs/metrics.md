# Client metrics

Implemented session-only ring buffer (100 entries) plus `window` CustomEvent `coreteams:metric`. No analytics endpoint/sink; no durable monitoring guarantee. Fields: name, ISO at, buildVersion (VITE_BUILD_VERSION or0.1.0), local project UUID, request/correlation UUID, runId and artifactId where known, safe code, optional durationMs. participantId is an optional schema field to bind through an approved consented research flow; it is not fabricated or collected by this UI. No title/context/answer/result/source URLs/credentials.

| Event | Trigger |
|---|---|
| project_started | browser draft successfully created |
| request_sent | actual fetch begins (unavailable does not emit it) |
| response_received | Full HTTP response body consumed, including non-2xx; duration includes body transfer |
| artifact_saved | parsed saved:true server acknowledgement only |
| artifact_downloaded | ZIP bytes validated and passed to browser; not proof written to disk |
| run_failed | unavailable/transport/schema/operation failure surfaced to user; code distinguishes a rejected pre-run request from a real failed run |
| run_created | returned runId linked to originating requestId/projectId |
| workspace_left | user leaves running/clarification workspace; remote run is not cancelled; this is not task_abandoned |

Saving a local draft never emits artifact_saved. There is no unload abandonment metric and no request when app opens by default. Transport timing is client HTTP duration, not AI completion duration. Backend metric durability, run costs, true success and business conversion require separate approval/integration.

The observer records genuine task_abandoned, participant pseudonym, active/wait/pause time and any consented prompt text in the approved study record. Browser navigation alone does not establish abandonment. Arina must define persistent collection/export; Ilya supplies server run/request confirmations. Event generation, server acceptance and durable storage remain distinct.
