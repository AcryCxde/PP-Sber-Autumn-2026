# Deployment handoff — Филипп

Deployment remains Филипп's responsibility. This package performs no deploy. Serve `web-app/dist` as static files after `pnpm install --frozen-lockfile && pnpm build`. Node >=22.12 is build-only; output needs no Node server. Vite base './' supports a subdirectory; entry has no browser-history routes.

Public build env: VITE_API_BASE_URL, VITE_PROJECT_API_ENABLED (false by default). Enable only after Илья confirms project API. No credentials in env/browser bundle. Prefer same-origin API reverse proxy for cookie/session auth; clarify CSRF and security headers with backend. ZIP uses authenticated fetch before browser download. Preserve Content-Type and bounded binary responses. API outage should remain an error, never SPA fallback JSON. `.env.example` is safe public config.

Serve long-cache hashed assets and short-cache index.html. Keep origin/port stable to retain local drafts. Moving origin does not migrate browser storage. UI uses system fonts and has no remote font/CDN request. Desktop/mobile Edge smoke is a browser compatibility check, not deployment acceptance. For this pilot use the team server/IP and non-secret, de-identified tasks with access restrictions; domain/TLS are outside the owner's present scope. Verify backend routes, real save/ZIP and ownership permissions before admitting participants.
