# WEB verification — 2026-10-15

Owner: Катя. Executor: WEB agent, completed checks by orchestrator after model-service403. This is frontend handoff evidence, not a deployed-user E2E.

## Executed commands

- Node `24.19.0`, pnpm `11.25.0`; locked dependencies in `pnpm-lock.yaml`.
- `pnpm install --frozen-lockfile --offline`: passed against the existing local package cache; initial package install fetched the locked dependencies. This is not evidence of a fresh-machine network install.
- `pnpm build`: passed, includes `tsc --noEmit`, Vite8.3.4,20modules; production JS238.61kB before compression.
- `pnpm test`:8passed,0failed. Checks unavailable URLs, stable idempotency/correlation IDs, abort/unknown outcome, full-body latency, failure/save-ack validation, discriminator state, ZIP signature, corrupt draft preservation and metadata-only telemetry.
- `pnpm preview` and `pnpm dev`: started locally. Configured ports4184/4183 were occupied; actual verification servers used4185/4186. No deployment performed.
- `node test/browser-smoke.cjs`: Edge isolated-profile checks passed. `playwright` was supplied by the existing bundled runtime through NODE_PATH; it is not declared as an application dependency.

## Browser results

1. Desktop1440px and mobile390px: default unavailable state, local project/context creation, reopen/reload, no API request in default mode, no horizontal overflow or page errors.
2. Network-fixture-only test: running state, question, answer, result, rejected save acknowledgement, stable request ID after reload/retry, valid acknowledgement, ZIP download event, run/artifact-linked metrics, reopened result. Fixture response is explicitly test data, not CTF output.
3. Corrupt storage remains unmodified and blocks edits; a stale second tab blocks mutations.

Screenshots in ignored `test-results/` show actual frontend default states. Fixture screenshots are labelled test-only and excluded from delivery visuals. The browser checks validate client state handling; real CTF, persistent storage, server authorization, archive content/manifest and cross-device project hydration remain unverified.

## Fixes verified

- Native fetch bound to globalThis avoids Edge Illegal invocation.
- response_received occurs after full body transfer, with a delayed-body regression test.
- artifact_saved links runId/artifactId; request events carry runId where known; every event includes buildVersion.
- Leaving a workspace emits workspace_left and does not claim an abandoned remote task.

## Current handoff limit

The new project API is proposed, opt-in and disabled by default. Ilya must approve/adapt it to CTF, Arina must provide the storage architecture, and Filipp must connect and deploy the server. No unconditional HANDOFF_READY or DEPLOYMENT_READY is asserted.
