# CoreTeams product workspace

CoreTeams — Рабочее пространство с AI-командой для сложных задач вне одной экспертизы.

React/Vite/TypeScript workspace based on the owner-provided v9.2 behaviour, wired to the CoreTeams backend through contract V2 (`/api/v2`, implemented in `backend/api/product.py` on top of the existing Claude runs).

## Run

- Docker stack: `docker compose up --build`, open http://localhost:5173/product/ and sign in or register. The main interface is the CoreTeams UI from `frontend/` at `/` (free-form chat at `/#chat`); both share the session. The admin panel is at `/product/#admin`.
- Dev: Node 22.12+, pnpm 10.11.0 (pinned in `packageManager`; install with `npm install --global pnpm@10.11.0`). From `web-product`: `pnpm install --frozen-lockfile`, `pnpm dev` (4191). `/api` is proxied to `BACKEND_URL` (default http://127.0.0.1:8000); the app shows its own sign-in screen.
- `VITE_CTF_V2_ENABLED=false` returns to local-only drafts; see `.env.example`.

## How V2 maps onto the backend

- A V2 Run is a chain of conversation runs: the initial or rework request plus its answers to facilitator questions. Rework after completion is a new Run linked by `parentRunId`/`sourceArtifactId`.
- The initial run covers the whole plan; the agent saves each stage document with Write to `coreteams/<stageId>.md`. Every save is a new immutable version of that document; other written files go to the run's stage.
- Questions come from the runner (`question.asked`); answers are sent as one text to every question of the fork. `unknown_fork`/`answers_mismatch`/`version_conflict`/`idempotency_conflict` are returned as top-level `code`.
- Usage reports requests, compute seconds and cost in USD from finished runs; tokens are not measured yet. Role events are not emitted (no execution evidence per role).
- Approval receipts are stored per version (`artifact_approvals`); ZIP contains the version and a manifest.
- Projects from the previous v9 interface stay in the database unchanged and are listed read-only on the home page (view, download Markdown).
- Drafts are stored in browser storage per account; the server keeps them, but there is no project list/restore across devices yet.

## User journey

1. Explain capability and show a five-step, explicitly labelled static example before a first own project. The example workspace supports stage/document/version exploration; sample sending is disabled.
2. Create goal/context and add real input file bytes, persisted in IndexedDB across reload. Metadata/answers/team/plan are versioned browser drafts. Text preview is bounded; PDF/Office extraction needs backend processing. No arbitrary file-count or credit quota introduced; browser storage/technical limits remain.
3. Answer preparation questions; choose AI-roles and request another expertise in a note. These are proposed roles, not proof of CTF availability or real human participation.
4. Edit/approve the local plan, including adding/removing stages. This does not execute a run.
5. Simplified three-zone workspace: stage/material navigation, facilitator conversation, per-stage documents and immutable versions. Freeform comments and facilitator answer drafts are retained. Real questions, new versions, approvals and usage are returned by the backend only.
6. Export displayed text; server ZIP only for saved artifacts. Unknown outcomes retain the original request/body; uploaded file checksum is acknowledged; server IDs persist. Runtime has no simulated completion/timer/credit values.

Usage shows actual backend input/output tokens, request count, compute seconds and cost+currency when supplied. Unknown values remain “Не измерено”; no product quotas enforced. This does not remove provider rate limits, server safety controls or browser quotas. Different-device recovery/auth/storage require implementation on the backend.

## Opt-in journey measurement

Use **«Замер пути»** for consented baseline, interface-only or CoreTeams sessions. Metadata/intervals persist locally, with JSON/CSV export and explicitly matched comparison; no automatic analytics upload. Read [the operator guide and event/privacy contract](docs/journey-metrics.md) before collecting any user data. Saved task-result selection is separate from intermediate artifacts. Fixture and interrupted sessions cannot establish a time-saving result.

## Integration and evidence

Read [proposed V2 contract](docs/ctf-v2-contract.md), [acceptance](docs/acceptance.md) and [verification](docs/verification.md). This proposed contract adds file ingestion, chosen team/plan, messages, document/version snapshots, approvals and consumption. It is not the implemented gateway WebSocket contract and must not be enabled without review by Дмитрий/Илья/Арина/Филипп.

Tests: `pnpm test`, `pnpm build`. Browser script uses the bundled Playwright via NODE_PATH and two local URLs; synthetic network responses exist only in the test. Its fixture operations/usage/ZIP do not prove live CTF, source truth, deployment or user outcomes.

## Agreed WEB16–21 update

B supports explicit unknown/recovery states, full structured facilitator questions and stale-fork recovery, actual file-byte/hash checks, linked NEW Runs for post-completion rework within one project, project-wide immutable version/history browsing and separate version-specific server approval receipts. Requested roles are distinct from execution evidence. Old browser drafts migrate without clearing files or study journals; legacy in-flight requests are not silently rewritten. A and backend remain unchanged; PR11 remains draft, no merge/deploy.

Additional regression: `node test/contracts-browser.cjs` on the explicitly marked fixture dev server (4191, `VITE_RESEARCH_ENV=fixture`). The updated [existing contract](docs/ctf-v2-contract.md) identifies client-side fields still requiring joint mapping/acceptance; this is not a claim that the current gateway implements them.
