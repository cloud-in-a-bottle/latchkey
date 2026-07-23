# openhost-latchkey

OpenHost app packaging [latchkey](https://github.com/imbue-ai/latchkey): a credential-injecting
proxy so other apps in the compute space can call third-party APIs (Slack, GitHub, Gmail, ...)
**without ever seeing the secrets**.

## How it works

The container runs three things:

- **The front server** (litestar, single process) — the owner console, the cross-app service
  endpoint, and the permission shim between OpenHost grants and latchkey. Web glue lives in
  `src/server/web/` (routes, request/response models, templates), framework-free logic in
  `src/server/core/`, unit tests in `src/server/tests/`; `tests/` at the repo root holds the
  containerized integration tests.
- **`latchkey gateway`** on localhost — latchkey's own HTTP server, which injects stored
  credentials into proxied requests and enforces per-request permission policies via
  [detent](https://github.com/imbue-ai/detent). Only the front server can reach it (localhost +
  per-boot password).
- **A virtual display** (Xvfb + x11vnc) — for latchkey's interactive `auth browser` logins. The
  owner sees Chromium through noVNC on an owner-gated page, logs in to the third-party service,
  and latchkey extracts the API credentials from the browser session.

Only the front server runs all the time. The gateway and the display stack start on demand — the
gateway on the first request that needs it, the display when a browser login or VNC viewer
connects — and a reaper stops them again after an idle period, so an idle app holds just the one
Python process. Idle windows: `LATCHKEY_GATEWAY_IDLE_SECONDS` (default 300) and
`LATCHKEY_DISPLAY_IDLE_SECONDS` (default 60); `<= 0` disables the idle stop.

There is also at most one copy of the browser stack at a time, so memory stays capped: one login
flow (extra starts get a 409 telling the owner to close the other tab or wait), one connected VNC
viewer (extra websockets are rejected; the connect page explains), and viewer sessions are capped
at `LATCHKEY_VNC_MAX_SESSION_SECONDS` (default 1800) so a stuck client can't hold the display
stack up forever.

Request path for consumers:

```
consumer app ──router service proxy──> front server /api/proxy/<url>
    · translates the consumer's OpenHost grants into a per-consumer detent permissions.json
    · attaches a signed permissions-override JWT (minted via `latchkey gateway create-jwt`)
    ──> latchkey gateway /gateway/<url>  ──creds injected──> third-party API
```

Consumers get exactly the access the owner granted (detent scope + permission schemas), and
credentials live only in this app, encrypted at rest (`LATCHKEY_ENCRYPTION_KEY` generated on first
boot into app data).

See [services/latchkey/](services/latchkey/) for the service spec (openapi.yaml + grant semantics).

## Owner console

- `/` — connect/disconnect services, register custom services.
- `/connect/<service>` — browser login (streamed via noVNC) or manual credential entry.
- `/grant` — consent page consumers send the owner to for app-scoped grants.

## Development

```bash
just setup   # install deps, pre-commit hooks, and the playwright chromium browser
just run     # run locally on http://localhost:8080 (needs OPENHOST_* env; mostly use just test)
just test    # run the test suite (podman must be running)
just check   # lint, format, typecheck
```

Python work uses [uv](https://docs.astral.sh/uv/). Use `uv add <pkg>` to add a
dependency and `uv add --group dev <pkg>` for a dev-only one.

`just test` uses the OpenHost test harness (the `openhost[test-harness]` package),
which builds the Dockerfile and runs the app under **podman** (so podman must be
running on the host) fronted by the real OpenHost router. `stack.url` requires
owner auth (use `stack.owner_session` for requests, or `stack.playwright_login(page)`
for browser tests); `stack.app_url` hits the container directly. See `tests/` for the
`stack` fixture.

## Latchkey packaging notes

- latchkey is consumed as a pinned npm package (`LATCHKEY_VERSION` in the Dockerfile). If we need
  patches, point `npm install` at a fork/branch and PR the change upstream (imbue-ai/latchkey).
- Upstream wishlist: structured error codes on gateway responses (we currently detect permission
  denials by the fixed error string), a flag to disable `auth browser` on the gateway RPC,
  progress reporting for browser-login flows, and a `LATCHKEY_GATEWAY_EXTRA_HEADERS` client option
  so stock latchkey CLIs inside consumer apps can talk through the router.
