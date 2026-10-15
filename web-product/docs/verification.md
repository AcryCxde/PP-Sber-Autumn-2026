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

## Journey instrumentation — 2026-10-15

17 unit tests pass (8 existing domain + 9 study tests). Study checks cover opt-in/pseudonymous codes, allowlisted exports, manual baseline exclusion of product events, separate active/wait/pause time, unknown reload gaps, version event deduplication, same-person/task comparison, fixture/interface/missing-result gates, clock rollback, corruption/cross-tab conflict, local deletion and quota failure. Actual usage preserves unknown versus zero and partial snapshots. Tests ran with Node `--test-isolation=none` because sandboxed child-process spawning returned EPERM; the normal test command remains supported outside that sandbox.

Production typecheck/build pass (JS307.11kB, CSS19.75kB before gzip; no dependency changes). Vite build and browser required approved execution outside the restrictive child-process sandbox. Existing preview4192 serves the rebuilt files; no second preview process started (strict port detected the existing listener).

Extended visible Edge browser script passes desktop1440/mobile390, existing real-byte/retry/version path and new study lifecycle: no journal before opt-in; disabled start without consent; baseline modes/help/save/rating/finish; recovered prototype session across actual reloads; request failure and same-run question/answer; saved/accepted version IDs deduplicated; selected task result; JSON/CSV downloads; no prompt/file/result text or participant code in product create payload; actual supplied synthetic consumption; corrupted study record preserved with warning. Test environment explicitly marked `fixture`. Isolated fixture exports/screenshots live under ignored `test-results/`; no study data committed. A `web-app` diff against f6eff66 remains empty.

No real consenting participant, comparative user result, CTF integration, shared deployment or durable server analytics verified. Metadata generation, successful local persistence, browser download and server receipt remain distinct. Manual observation, privacy/retention agreement and the deployment gate are still required: [operator guide](journey-metrics.md).

## Baseline journal and inline instructions follow-up

19 unit tests pass (8 domain + 11 study); typecheck/build pass (JS320.98kB, CSS20.14kB before gzip). New study tests verify structured baseline steps, prevention of overlapping steps or early save, per-step active/wait timing, barriers/result kind, immutable-after-result flow, restoration of steps, compatibility with older v1 sessions, unfinished-step closure and allowlist removal of unexpected free text.

Visible Edge desktop/mobile + fixture browser checks pass again. Both baseline screens now perform action/tool → start → barrier/outcome → end → result-format/save → ratings/finish; no early result save, step/barrier/result fields persist, step CSV downloads contain the expected categories, and JSON/content-privacy checks remain intact. Static inline instructions distinguish all three phases and explain private manual handoff to the researcher. One initial browser selector matched instruction prose and the actual summary; narrowed to the summary prefix and reran the full suite successfully. Synthetic screenshots/exports remain ignored under test-results. No real user session or server analytics tested.

Server storage is [PROPOSED only](research-storage-proposal.md): no new endpoint, automatic upload or change to local-only consent. A remains unchanged.

## Russian outcome labels and missing-result diagnosis

20 unit tests, typecheck/build (JS325.01kB, CSS20.14kB before gzip), full desktop/mobile/fixture smoke and separate status-browser tests1440/390 pass. Historical codes remain `completed/incomplete/abandoned` for compatibility; UI/CSV summaries have Russian outcome labels. Elapsed study duration is separate from time-to-saved result. A missing save marker says «Сохранение не отмечено», interface-only says «Не применяется»; no unknown duration becomes0.

Step and session outcomes now require explicit choice, and stopping a session requires confirmation. The diagnostic disclosure uses stored finish events and save markers, without inventing user intent or retroactively editing data. Synthetic status tests prove old journal bytes unchanged by display, stop cancellation keeps a session open, confirmed stop preserves events, and Russian labels/reasons display without horizontal page overflow. Actual participant records/export files are not committed or published. This verifies UI semantics, not research outcomes.
