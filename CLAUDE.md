# Fleet Hub contributor contract

Fleet Hub is a phone-first client over existing owners. It is not Dharma
Command, a task store, a scheduler, a roster authority, or a new bus.

Before changing behavior:

1. Read `docs/FLEET_HUB_V1_IMPLEMENTATION.md` and `HANDOFF.md`.
2. Preserve exactly five top-level destinations in this order: Helm, Chat,
   Board, Trace, Roster. Needs John is a Helm rail and cross-tab badge, not a
   sixth destination.
3. Treat `TaskBoard+RuntimeStateStore` through Mission Control as the mission
   authority. Fleet Hub may cache a labeled projection; it must not open or copy
   an owner SQLite database in production.
4. Keep task commands unavailable unless an owner operation, authorization
   class, atomic concurrency primitive, idempotency rule, and receipt contract
   all exist. Never substitute raw NATS work mutation.
5. Keep epistemic claims distinct. A broker `PUBLISH_ACCEPTED` receipt does not
   prove consumer delivery, handler acknowledgement, task completion, external
   effect, or executor liveness.
6. All browser mutations require authentication, an in-memory CSRF token,
   Origin/Fetch-Metadata checks, and an idempotency key. Never put credentials in
   URLs, browser storage, HTML, logs, or trace payloads.
7. Render untrusted values as text. Keep reads and replays bounded, redact on the
   server, and return stable public error codes rather than exception strings.
8. The service worker may cache the static shell only. API, SSE, login, logout,
   and command traffic is network-only and must never be replayed offline.
9. Design and test at 390x844 and 320 CSS pixels with 44px targets, safe areas,
   keyboard navigation, reduced motion, and explicit stale/unavailable states.
10. Do not install, deploy, restart production, change ACLs, publish a live
    canary, or claim consensus without separate authorization.

Completion requires the locked test suite, static checks, and browser evidence.
A clean receipt proves only the checks it names.
