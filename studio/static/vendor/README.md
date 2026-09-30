# Daily JavaScript browser client

Pinned dependency: `@daily-co/daily-js` **0.87.0**.

- Source: https://unpkg.com/@daily-co/daily-js@0.87.0/dist/daily.js
- File: `daily-0.87.0.js`
- SHA-256: `1281fea11ca4460456c6b59d5a6f2b16bac2870428350807aa36328032ae11a4`
- License: BSD-2-Clause, retained in `daily-LICENSE.txt`.

Used only by `/vapi-frame` to join a backend-created Daily room. It does not
receive an owner access token or reusable Vapi key. `avoidEval: true` avoids
requiring `unsafe-eval`; the SDK loads its matching runtime bundle from
`https://c.daily.co/call-machine/versioned/0.87.0/static/call-machine-object-bundle.js`.
The dedicated frame CSP permits that origin and Daily media connections. The
main enrollment page retains its self-only script and connection policy.
