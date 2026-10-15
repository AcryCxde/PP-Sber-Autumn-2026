# CoreTeams product workspace candidate B

CoreTeams — Рабочее пространство с AI-командой для сложных задач вне одной экспертизы.

Second separate React/Vite/TypeScript candidate based on the owner-provided v9.2 behaviour. Candidate A in `../web-app` is unchanged; its existing preview4190 remains available. B is not a merged replacement and not a live CTF service.

## Run

From `web-product`, Node22.12+ and pnpm11: `pnpm install --frozen-lockfile`, `pnpm dev` (4191) or `pnpm build` then `pnpm preview` (4192). Do not put credentials in VITE variables. `.env.example` leaves the proposed V2 integration disabled. No backend, domain, cloud publishing or migration of old projects performed.

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
