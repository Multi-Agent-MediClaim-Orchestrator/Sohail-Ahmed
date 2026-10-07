# hospital-ui

Next.js 14 (App Router, TypeScript strict, Tailwind, TanStack Query). `make run-ui` serves http://localhost:3100;
the hospital API must be on :8100 (`make run-api`). Demo users: desk1, officer1, officer2, hadmin (password = `DEMO_PW`).

- **Auth:** a sign-in form posts to `/api/auth/login`, which does a password grant against the dev Keycloak client
  `hospital-dev`; tokens stay in server memory and the browser only gets an opaque httpOnly cookie. All API calls go
  through `/api/hosp/*` (adds the bearer token). Events go through `/api/stream` (one-time ticket, piped SSE).
- **Screens:** dashboard, cases list/new, case workspace (documents, checklist, claim editor with sign-off and submit,
  submission, queries, timeline), query inbox, admin config / users / outbox.
- **Rules kept out of the client:** the API decides everything; the UI shows its messages (`lib/errors.ts`) and maps
  server fields to chips in one pure function (`lib/docStatus.ts`). Money is a string end to end (`decimal.js`).
- `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`.
