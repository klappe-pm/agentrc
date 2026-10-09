# 2026-10-09-local-api-safeguards

## status

accepted, 2026-10-09

## context

The [read-only local API](2026-10-09-read-only-local-api.md) is a local server, and a local server is still reachable by other programs on the machine and, through a browser, by web pages that guess its port. The first record fixed the transport and the verbs. It did not list the narrower safeguards the build added.

## decision

The server accepts only GET and answers any other method with 405 and an `Allow: GET` header. A Unix socket is created mode 0600 and removed on close; a stale socket is reclaimed, and a live one or a non-socket file is refused. A port binds 127.0.0.1 only and rejects a request whose Host header is not loopback, which blocks a rebinding attack from a web page. Socket paths longer than the platform limit are refused with a clear message. Unknown or repeated query parameters get a 400. Every string passes through the change log's redaction, and if the scanner is unavailable the route answers 503 and sends no data. A provider route reports only whether a secret is set, never its `secret://` reference.

## alternatives

A shared token in a header was rejected because a file mode on a socket already gives the same guarantee without a secret to store. Binding all interfaces for remote dashboards was rejected as out of scope for a local tool. Ignoring unknown query parameters was rejected because a misspelled filter would silently return everything.

## consequences

The API is safe to leave running next to a browser. The cost is strictness: clients must send known parameters and a loopback Host. A future write surface needs its own record and its own safeguards.
