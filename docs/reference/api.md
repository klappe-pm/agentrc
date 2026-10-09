# api

This page is the reference for `stratarc api`, the read-only local interface to the data behind the `config`, `projects`, `runtime`, `adapter`, `provider`, `log` and `verify` commands. It describes the transport, the routes, the response envelope and the limits. The design is in [cli-design](../../developer-docs/explanation/cli-design.md). The machine-readable contract is the JSON Schema `stratarc/data/schema/api-v1.schema.json`, served at `GET /v1/schema` and printed by `stratarc api schema`.

## commands

| command | effect |
| --- | --- |
| `stratarc api serve` | serve `/v1` on the Unix socket `~/.stratarc/state/api.sock` until interrupted |
| `stratarc api serve --socket PATH` | serve on a Unix socket at `PATH` |
| `stratarc api serve --port N` | serve on `127.0.0.1:N` instead of a socket; `0` picks a free port |
| `stratarc api serve --root R` | the source root to read; defaults to the same root the other commands use |
| `stratarc api schema` | print the published JSON schema and exit |

`--socket` and `--port` exclude each other. The server prints `listening on <address>` once it is ready. Exit statuses: 0 ok, 2 invalid input (a missing source root, a bad port), 5 unavailable (the socket path is too long, is not a socket, or another server holds it), 130 interrupted.

## transport

The socket file has mode 0600 and is removed when the server stops. A socket left by a stopped server is replaced; one a live server holds is refused. The path must be 100 bytes or shorter. A port binds `127.0.0.1` and nothing else, and a request whose `Host` header is not `127.0.0.1`, `localhost` or `[::1]` answers 403 `forbidden-host`. There is no authentication beyond the socket mode, no TLS and no cross-origin header.

The API is read-only. Only `GET` is served; any other method, including `HEAD`, answers 405 `method-not-allowed` with `Allow: GET`. No route writes to the source root, the home or the change log. Writes through the API are out of scope for version 1.

## routes

Every route is under `/v1`. A trailing slash is accepted. A query parameter a route does not list answers 400 `unknown-param`; a repeated one answers 400 `duplicate-param`.

| route | parameters | data |
| --- | --- | --- |
| `/v1/health` | none | the API and engine version, `read_only`, the source root |
| `/v1/config/keys` | `project`, `agent`, `account`, `runtime`, paging | a list of `{key, value, decided_by}`, with `scope` and `errors` for keys that fail to resolve |
| `/v1/config/explain` | `key` (required), `project`, `agent`, `account`, `runtime`, paging | a list of resolutions, each with the `steps` of the chain: layer, file, line, op, mode, value and what it overrode; a table prefix returns one per leaf key |
| `/v1/projects` | paging | a list of `{name, has_permissions, has_settings, agents}` |
| `/v1/runtimes` | paging | a list of `{name, adapter_module, default_target, hook_registry, enabled, target, target_exists}` |
| `/v1/adapters` | paging | a list of adapter manifests with a `deprecation` notice or `null` |
| `/v1/providers` | paging | a list of `{name, endpoint, models, has_secret, description}` |
| `/v1/log` | `id`, `actor_kind`, `actor`, `layer`, `key`, `status`, `cause_id`, `source_ref`, `file`, `command`, `project`, `since`, `until`, paging | a list of changes, newest first |
| `/v1/log/{id}` | none | `{change, caused, story}` for one change |
| `/v1/verify/last` | none | the latest verify report; 404 `no-verify-report` when none exists |
| `/v1/schema` | none | the published JSON schema |

The filters of `/v1/log` are those of `stratarc log show`. `since` and `until` take `YYYY-MM-DD` or a UTC timestamp.

Providers report whether a secret reference is set (`has_secret`) and never the reference or the value. `/v1/schema` carries the schema inside the envelope like every other route; `stratarc api schema` prints the bare schema.

## envelope

Every response is one JSON object with `ok`, `data` and `error`. On success `ok` is true and `error` is null. On failure `ok` is false, `data` is null and `error` carries `code`, `message`, `param` (the offending parameter or key, or null) and `hint` (the recovery, or null).

```json
{"ok": false, "data": null, "error": {"code": "unknown-key", "message": "No layer sets \"nope\".", "param": "nope", "hint": "Run `stratarc config list` to see the keys."}}
```

| status | codes |
| --- | --- |
| 400 | `unknown-param`, `duplicate-param`, `missing-param`, `invalid-param`, `config-invalid`, `source-root-missing`, and the layer errors `parse-error`, `list-mode-missing`, `mode-invalid`, `type-mismatch`, `project-required` |
| 403 | `forbidden-host` |
| 404 | `not-found`, `unknown-key`, `unknown-project`, `unknown-agent`, `unknown-account`, `unknown-runtime`, `unknown-change`, `no-verify-report` |
| 405 | `method-not-allowed` |
| 500 | `internal-error`, `response-too-large` |
| 503 | `log-unavailable`, `redaction-unavailable` |

## paging

A list route takes `limit` (1 to 1000, default 100) and `offset` (default 0). Its data carries `total` (rows that match), `shown` (rows in this response), `truncated` (true when rows remain after this page), `rows`, `next_offset` (the offset of the next page, or null) and `output_truncated`.

A response is at most 200000 bytes. When the page would be larger, rows are dropped from its end, `output_truncated` and `truncated` become true, `shown` falls, and `next_offset` points at the first row that was dropped, so following `next_offset` always continues without a gap. `/v1/verify/last` is cut the same way through its `files` list and reports `files_total` and `output_truncated`.

## redaction

Every string in a response passes through the secret scanner the change log uses, and each token-shaped substring becomes `[REDACTED:<kind>]`. The home directory is shown as `~`. When the scanner cannot run, the route answers 503 `redaction-unavailable` and sends no data.
