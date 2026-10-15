# Candidate B verification

2026-10-15. Node24.19.0, pnpm11.25.0, React19.3.0, Vite8.3.4, TypeScript5.9.3. Dependencies locked; installed23 packages from reused store, no version upgrades. Candidate A `web-app` is unchanged from f6eff66.

## Checks

Typecheck/production build pass. Eight domain tests cover Run/schema/actual-versus-unknown usage, saved version acknowledgment, immutable document histories, stale response rejection, known-stage mapping, corrupt/versioned browser drafts, no three-project quota, sample usage unknown and reply draft recovery.

Browser checks use isolated Edge contexts, not the owner's browser data:

- Desktop1440 and mobile390: first-project guide, labelled example, selection of logical artifact and old/new version, unmeasured usage, onboarding, actual text-file bytes persisted in IndexedDB after reload, file preview/keyboard close, preparation answers, role changes, editable plan, workspace, no network request when default CTF unavailable, no horizontal overflow/page errors.
- Test-only network fixture4191: real multipart bytes and matching hash; selected roles/plan; aborted run request retried after reload with same id/body; same-run question/answer and reply draft; document versions, user comment, saved-current-version approval; supplied synthetic usage displayed; text and server ZIP transferred to browser. ZIP fixture checks transport/signature only, not real pilot package semantics.
- Corrupt browser data preserved rather than overwritten. Client stores versioned metadata and full files separately; cross-tab warning prevents unsafe writes.

Fixed mobile sidebar min-content overflow; added modal Escape/focus handling, logical-document grouping, historical version retention and serial polling. Focused UI review used [Vercel Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md): labels, semantic controls, skip link/focus, live feedback, long-content handling, reduced motion, responsive grid. This is not a blanket accessibility certification. Hash-deep-link navigation and binary document rich preview remain future improvements.

Synthetic fixtures are confined to `test/`; static example is visibly marked throughout and cannot send a fake CTF operation. Default `.env.example` disables V2 backend. No live CTF, real extraction, role assignment, server storage, user E2E, common-server deployment or stakeholder acceptance verified. No load test or promise of unlimited server/provider capacity. Acceptance checklist and live contract gates remain authoritative.
