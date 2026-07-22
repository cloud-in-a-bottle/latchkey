# Latchkey service spec

Service URL: `github.com/imbue-openhost/openhost-latchkey/services/latchkey` — version `0.1.0`

The formal API spec lives in [openapi.yaml](openapi.yaml); this file covers the grant semantics
and typical consumer flow.

Lets consumer apps call third-party HTTP APIs (Slack, GitHub, Gmail, ...) with the owner's
credentials injected by [latchkey](https://github.com/imbue-ai/latchkey), without the consumer ever
seeing the credentials. Enforcement uses latchkey's [detent](https://github.com/imbue-ai/detent)
permission engine, so grants are expressed in detent's vocabulary of *scope* and *permission*
schemas (e.g. scope `slack-api`, permission `slack-read-all`).

## Grant payload

```json
{"scope": "<detent scope schema>", "permissions": ["<detent permission schema>", ...]}
```

- `scope` — a detent scope schema name, e.g. `slack-api`, `google-gmail-api`. Scope/permission
  names for built-in services are documented in the latchkey/detent repos.
- `permissions` — detent permission schema names allowed within that scope,
  e.g. `slack-read-all`.
- `schemas` (optional) — object of custom detent request schemas (JSON-schema-like matchers over
  `domain` / `method` / `path` / body), for services detent has no built-in schemas for
  (self-hosted or runtime-registered services). They are merged into the enforcement config.

Note detent's rule resolution: rules are evaluated top-to-bottom and the **first rule whose scope
matches the request decides the outcome**. Grants with the same scope name are merged (permissions
unioned); grants with *different* scope names whose schemas overlap can shadow each other. When
defining custom schemas, make scope schemas disjoint (e.g. match domain *and* path prefix), or
reuse one scope name per service.

The pseudo-scope `latchkey-meta` is handled by this app itself rather than detent:

```json
{"scope": "latchkey-meta", "permissions": ["services-read"]}
```

grants access to the service-listing endpoints below.

Grants can be declared in the consumer's manifest (global scope, approvable at install time) or
acquired at runtime via the app-scoped consent flow (`POST grants/request` below, or the
`grant_url` in any `permission_required` error).

## Endpoints

All paths are relative to the provider endpoint (`/api/`), i.e. called as
`{OPENHOST_ROUTER_URL}/api/services/v2/call/<shortname>/<path>` with
`Authorization: Bearer $OPENHOST_APP_TOKEN`.

### `ANY proxy/<absolute-url>`

Forwards the request to `<absolute-url>` with credentials injected, e.g.

```
GET  proxy/https://slack.com/api/conversations.list
POST proxy/https://api.github.com/repos/owner/repo/issues
```

Method, headers, query string, and body are forwarded (hop-by-hop headers, `Authorization`, and
`Cookie` are stripped). The upstream response is returned as-is.

Error responses (all JSON):

- `403 {"error": "permission_required", "message": ..., "grant_url": ..., "required_grant"?: ...}` —
  the consumer has no grant covering this request. Redirect the owner to `grant_url` (append or
  pass `return_to` when requesting the grant to get the owner sent back).
- `400 {"error": ...}` from the gateway — e.g. no credentials stored for the target service, or the
  URL doesn't belong to any known service. The message says which.
- Upstream errors (including upstream 403s) pass through with the upstream's own body.

### `GET services` / `GET services/<name>`

Requires the `latchkey-meta` / `services-read` grant.

- `services` → `{"services": ["slack", "github", ...]}`
- `services/<name>` → latchkey's service info, including `credentialStatus`
  (`missing` / `valid` / `invalid` / `unknown`), `baseApiUrls`, and `authOptions`. Use this to
  decide whether to prompt the owner to connect a service before calling it.

### `POST grants/request`

Body: `{"grant": <grant payload>, "return_to"?: "<url on the consumer's subdomain>"}`

Returns `{"grant_url": ..., "required_grant": ...}`. Redirect the owner's browser to `grant_url`;
this app renders a consent page describing exactly what is being granted, creates the app-scoped
grant in the router on approval, and redirects back to `return_to`.

## Typical consumer flow

1. Declare the service in the manifest:

   ```toml
   [[services.v2.consumes]]
   service = "github.com/imbue-openhost/openhost-latchkey/services/latchkey"
   shortname = "latchkey"
   version = ">=0.1.0"
   grants = [
       {scope = "latchkey-meta", permissions = ["services-read"]},
       {scope = "slack-api", permissions = ["slack-read-all"]},
   ]
   ```

2. Check `GET services/slack` → if `credentialStatus` is `missing`, tell the owner to connect
   Slack in the latchkey console.
3. Call `proxy/https://slack.com/api/...`.
4. On `permission_required`, send the owner to `grant_url` and retry after they approve.
