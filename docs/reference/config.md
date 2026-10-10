# config

`stratarc config` reads the resolved settings and says where every value came from. It is read-only: it never writes a file. This page is the lookup for its commands, flags, the agent relay keys and the error codes.

## commands

| command | prints |
| --- | --- |
| `config get <key>` | the resolved value of one key; a table prefix returns the nested table |
| `config list` | every resolved key |
| `config explain <key>` | the resolution chain for a key, with file and line for every layer |
| `config explain --tree --project P` | the pruned outline of what the project and its agents change, plus the relay edges of its agents |

Every command takes `--project`, `--agent`, `--account`, `--runtime`, `--root PATH` and `--json`. With `--json` the output is one envelope `{ok, data, error}`. Exit codes: 0 ok, 2 invalid input or an unresolvable key.

## layers

From lowest to highest precedence: base, runtime, account, project, agent, env, flags. A higher layer replaces a scalar and merges a table key by key. A list that a higher layer redefines needs a mode, `replace` or `extend`, written in a `_modes` table beside the list. See [layers and inheritance](../../developer-docs/explanation/cli-design.md).

## flags

The flags layer is applied last and wins over every file and variable.

| flag | meaning |
| --- | --- |
| `--set KEY=VALUE` | sets the key to the literal string `VALUE` |
| `--set-json KEY=JSON` | sets the key to a JSON value: a number, boolean, null, list or table |
| `--set-mode KEY=MODE` | `replace` or `extend`, for a list the flag redefines; the key must also be given by `--set-json` |

Each flag is repeatable, works on `get`, `list` and `explain`, and may come before or after the action. A flag step is reported with layer `flags`, file `--set` and line 0:

```text
permissions.timeout   (project: notes)
  base     permissions.json:7                      30
  project  projects-root/notes/permissions.json:2  60
  flags    --set                                   "5"
result: "5"   decided by: flags (set)
```

A flag that redefines a list without `--set-mode` fails with `list-mode-missing`, the same as a file would. A mode on a value that is not a list fails with `mode-invalid`. A value is never echoed in an error message.

```bash
stratarc config get permissions.network.allow --set-json 'permissions.network.allow=["x.example"]' --set-mode permissions.network.allow=extend
```

## the agent relay

A sub-agent started by another agent can take settings from its parent. Nothing is inherited unless the child says so. These keys sit at the top level of an agent file (`agents/<name>.json` or `.toml`, or the same path under `projects-root/<project>/`) and are not settings.

| key | type | meaning |
| --- | --- | --- |
| `parent` | string | the agent that started this one |
| `relay.inherit` | list of strings | key patterns the child takes from the parent; `*` matches any run of characters, dots included; the default is none |
| `account` | string | the account this agent runs under |

```json
{
  "parent": "lead",
  "relay": { "inherit": ["permissions.*"] },
  "permissions": { "timeout": 15 }
}
```

Inherited values join the agent layer below the child's own values, so the child's file wins. The chain is followed to its root and each link applies its own `relay.inherit`, so a grandparent's key reaches the child only if every link lets it through. A chain holds at most 8 agents, and a cycle is an error.

The account is the first that is set of: `--account`, the child's `account`, then the nearest parent's `account`.

`config explain <key> --agent <child>` prints the relay under the agent layer: each edge with its patterns, the parent that supplied the key, a parent value that was left behind with the reason, and where the account came from.

```text
permissions.timeout   (project: notes, agent: worker)
  base     permissions.json:7                      30
  project  projects-root/notes/permissions.json:2  60
  agent    projects-root/notes/agents/lead.json:4  120   (via lead)
  relay    worker <- lead   inherit: permissions.*   (projects-root/notes/agents/worker.json:2)
  relay    permissions.timeout inherited from lead
  relay    account: work (from lead; worker sets none)
result: 120   decided by: agent (set)
```

With `--json`, the top-level `relay` holds `chain` and `account`, each key holds `relay.supplied_by` and `relay.not_inherited`, and each step carries `via`.

## errors

| code | meaning |
| --- | --- |
| `parse-error` | a layer file is not valid JSON or TOML |
| `list-mode-missing` | a layer redefines a list without a mode |
| `mode-invalid` | a mode is not `replace` or `extend`, or sits on a value that is not a list |
| `type-mismatch` | a key changes between scalar, list and table across layers |
| `unknown-key` | no layer sets the key; the hint says when a parent set it but the relay did not pass it |
| `unknown-project`, `unknown-agent`, `unknown-account`, `unknown-runtime` | the named scope has no file or folder; for a parent or an account named in an agent file, the error points at that line |
| `relay-invalid` | `parent`, `relay` or `account` has the wrong shape, or `relay` is set with no `parent` |
| `relay-cycle` | the parent chain loops; the message prints it as `a -> b -> a` |
| `relay-depth` | the chain is longer than 8 agents |
| `flag-invalid` | a flag is not `KEY=VALUE`, a `--set-json` value is not JSON, or `--set-mode` names a key no flag sets |
| `source-root-missing`, `project-required` | the source root does not exist, or `--tree` has no `--project` |
