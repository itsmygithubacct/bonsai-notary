# Provider-neutral acceptance-node lifecycle

`provider_lifecycle.py` is a reviewed state machine around a provider adapter. It does not contain a
provider credential, hostname, or SDK, and it performs no action merely by being installed.

The adapter is an executable with six JSON operations. It receives one JSON object on stdin and emits one
JSON object on stdout:

| Operation | Side effect | Required response |
|---|---:|---|
| `offers` | no | `{"offers":[...]}` with offer/machine IDs, hardware, `baseHourlyUsd`, and `storageHourlyUsdPerGb` |
| `create` | **starts billing** | echoes `idempotencyToken` with `instanceId` and `machineId` |
| `reconcile` | no new create | echoes the token with terminal `status: "created"` + IDs, terminal `"absent"`, or `"pending"` |
| `ready` | no | `{"ready":true,"ssh":{"host":"…","port":22}}` or `{"ready":false}` |
| `destroy` | destroys one rental | `{"destroyed":true}` only after absence is confirmed |
| `active` | no | `{"instances":[...]}` for the complete provider account |

Offer selection is provider-neutral through [`node-descriptor.example.json`](node-descriptor.example.json).
Both base and storage-inclusive prices must fit their independent ceilings. `plan` is read-only:

```bash
./operations/provider_lifecycle.py plan \
  --descriptor operations/node-descriptor.example.json --adapter /protected/provider-adapter
```

Every operation has a hard subprocess timeout (30 seconds by default, configurable up to 300 seconds with
`adapterTimeoutSeconds` or `--adapter-timeout-seconds`). A timeout kills the adapter process, but it cannot
prove that a remote provider rejected a request, so create has an additional contract:

- the controller generates a 256-bit `idempotencyToken` and fsyncs a pending mode-0600 record before calling
  `create`;
- the adapter must make create idempotent for that token and echo it in any successful response;
- `reconcile` must be lookup-only and authoritative for the token: `created` includes the stable billable ID,
  `absent` promises that the token was not accepted and cannot later materialize, and `pending` remains
  unresolved;
- timeout, malformed JSON, a missing ID, or a token mismatch immediately invokes `reconcile`. An unresolved
  token remains persisted as `reconciliationRequired`, blocks another `up`, and is recoverable with the
  lookup-only `reconcile` command or `down`.

`up` validates a protected SSH private key plus its authorized-keys-form `.pub` sibling, passes only the public
key material to the adapter, and requires both `--authorize-billing` and an exact repetition of
the descriptor's total hourly ceiling. Every resolved rental ID is fsynced before readiness polling. Failed
machines are destroyed, persisted in the exclusion set, and never selected on a retry. Exceptions and
SIGINT/SIGTERM trigger reconciliation plus cleanup.

```bash
./operations/provider_lifecycle.py up --descriptor descriptor.json --adapter /protected/provider-adapter \
  --state /protected/run-state.json --ssh-key /protected/id_ed25519 \
  --authorize-billing --confirm-max-hourly-usd 0.22
```

Resolve an interrupted/ambiguous create without issuing another create:

```bash
./operations/provider_lifecycle.py reconcile \
  --adapter /protected/provider-adapter --state /protected/run-state.json
```

After evidence is pulled, `down --authorize-destroy` destroys every recorded rental and independently calls
`active`; success requires zero active instances across the account. Provider state and SSH endpoints remain
in the private state file and must never be copied into public evidence.
