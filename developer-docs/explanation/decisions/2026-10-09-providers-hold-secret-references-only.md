# 2026-10-09-providers-hold-secret-references-only

## status

accepted, 2026-10-09

## context

A provider file names an endpoint, the models it serves and, usually, a credential. Provider files live in the home, are copied into backups, can be pasted into issues and may be read by the API. A literal credential in any of those places is a leak. See [providers.py](../../../stratarc/providers.py).

## decision

Provider files accept only `secret://` references for credentials. Validation rejects any string the token-shaped detector matches and any secret field that is not a reference, and rejects endpoints that carry credentials or query strings. Errors never echo the offending value. `add` runs a probe, injectable so tests never touch the network, that sees the endpoint, the reference and the model ids but never a secret value.

## alternatives

- Store the value, protected by file mode. A mode does not protect backups, copies or pasted output.
- Allow query-string credentials in endpoints. Some services use them, but the URL is logged and echoed too easily.
- Echo the rejected value to help the user fix it. It would copy the leak into the terminal and logs.

## consequences

- Providers are safe to back up, show and include in a bug report.
- Users need a secret resolver to turn a reference into a value at run time.
- A service that insists on credentials in the URL cannot be registered without a proxy or a header-based setup.
- Messages for a rejected value name the field, not its content, so the user must find the offending text themselves.
