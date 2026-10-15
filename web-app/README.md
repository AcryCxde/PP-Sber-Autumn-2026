# CoreTeams workspace

Separate React + Vite + TypeScript application. No team builder or simulated agent runtime. Drafts are stored only in this browser/origin; clearing storage removes them. No server persistence promise. Use non-secret tasks.

Node >=22.12, pnpm 11. From `web-app/`: `pnpm install --frozen-lockfile`, `pnpm dev` (http://127.0.0.1:4183), `pnpm typecheck`, `pnpm test`, `pnpm build`, `pnpm preview` (4184).

Default: service unavailable, create/context/reopen works locally. To test an implemented adapter, set public `VITE_API_BASE_URL` and `VITE_PROJECT_API_ENABLED=true` before build. Read `docs/integration.md`: project routes are **proposed**, not the existing gateway contract. Runtime has no fixtures. Browser test uses network fixtures only in its script. Keep `.env` and credentials out of Git.

Pilot input is an arbitrary non-secret business decision, including constraints and verifiable sources. Decision brief + sources + metadata ZIP is a candidate bundle, not a validated segment or real buyer test.

Source map: App.tsx UI/workspace; api.ts transport/schema guards; storage.ts versioned draft reader; metrics.ts session-only event hook; style.css responsive UI. Tests cover unavailable, timeout, retries, invalid ack, ZIP and corrupt storage. The client requires backend idempotency; it stores pending IDs and original payload before mutation and retries unchanged after unknown outcomes. It does not automatically retry or cancel a remote run when leaving.
