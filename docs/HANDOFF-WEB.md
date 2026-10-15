# WEB ownership and definition of done

Owner: Катя. Implementer: WEB agent, isolated checkout only. Base: 8f57135c4725328485e3bb8615f0d0a69d722157. Branch: codex/coreteams-react-handoff-2026-10-15. Started 2026-10-15; WEB deadline 08:03 MSK, handoff 09:03 MSK.

DoD: separate React/Vite/TypeScript app; persistent browser draft workspace; context → run → backend-requested clarification → result → explicit server save acknowledgement → ZIP → reopen project. Unavailable backend is visible, never simulated. Typed transport with timeout, safe errors and correlation IDs; minimal metadata metrics. Unit/type/build/browser checks and isolated commit, push, draft PR to PPS-2. No merge or deploy.

Contracts carry implemented/proposed/approved status. Existing history/WS gateway is a reference, not proof that project/save endpoints exist. New project API is proposed and disabled until explicitly configured. No new backend or CTF integration. Филипп owns deployment; Арина owns server storage architecture; Илья receives integration notes. Дмитрий may assist with narrow reproduction only, without assignment.

At most two fixes for one root cause, then record blocker. No secrets or dirty source changes copied. Client storage is a browser draft, not a server persistence guarantee.
