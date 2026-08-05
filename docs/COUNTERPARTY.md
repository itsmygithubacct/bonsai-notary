# Standing up a counterparty

A receipt carries two signatures. The model signature says an engine produced this
output; the counterparty signature says someone other than that engine agrees. The
second is the reason a receipt is worth more than a log line.

It is worth nothing if both keys live on the producer. That is one party signing twice,
and **no receipt can express the difference** — two keys on one disk have two distinct
key ids and two valid signatures, and the bytes are indistinguishable from genuine
two-party attestation. Independence is arranged at production time or not at all, which
is why the engine now refuses the co-resident default instead of warning about it.

## What a counterparty needs

Less than people expect, and deliberately so:

| | |
|---|---|
| CPU / RAM | anything. It hashes a small object and makes one signature |
| Python | 3.11+ with **`ecdsa`** |
| Network | inbound SSH from the producer. **No listening service** |
| Disk | one 32-byte secret |

No numpy, no model artifact, no inference stack. Verification re-executes the model and
needs all of that; a counterparty never verifies, it signs. The asymmetry is the point:
the machine whose isolation is carrying the guarantee should be the one that is easiest
to keep clean. A Raspberry Pi is a perfectly good counterparty and a hopeless verifier.

## Provision it

Run this **on the counterparty**, not on the producer:

```sh
git clone https://github.com/itsmygithubacct/bonsai-notary.git
./bonsai-notary/scripts/provision-counterparty.sh
```

It resolves Python (system if `ecdsa` is already there, a venv if not), fetches the
receipt signer sources, mints a key, installs the signer wrapper, and then **signs a
throwaway message end to end and verifies the answer** before reporting success.
Provisioning that never exercises what it provisioned is a guess.

It prints exactly what the producer needs — the public half, never the secret:

```
On the PRODUCER, pin this host — the public key, never the secret:

  export TRINOTE_COUNTERPARTY_COMMAND="ssh -o BatchMode=yes <host> /home/<user>/.local/bin/trinote-counterparty-sign"
  export TRINOTE_COUNTERPARTY_PUBKEY="03…"
```

Then, on the producer:

```sh
rm ~/.local/trinote/keys/counterparty.key.json     # archive it first if you may want it
run_bonsai_cli … --context-commit <hex>            # remote counterparty is now the default
```

## Why the producer pins a key rather than a host

`TRINOTE_COUNTERPARTY_PUBKEY` is not optional. A signature carries the key that made it,
which proves only that *someone* signed — so without a pin, whatever answered the SSH
command would become the counterparty, and an attacker who can redirect the command has
silently become the second party. The producer refuses to start without it, and refuses
a signature from any other key with `unpinned-counterparty`.

Learning an identity from the party being identified is trust-on-first-use wearing a
protocol. The key id in the receipt is derived locally from the pinned public key, so
the producer never has to ask the counterparty who it is.

## Why the producer connects outward

The counterparty runs no daemon and opens no port. The producer runs the signer over
SSH, so the connection is outbound from the producer and the counterparty has nothing
inbound to defend but sshd. One invocation per signature; no session, no state, nothing
to poll.

## What the counterparty will refuse

- **Anything that is not a counterparty message.** The producer sends *named fields* and
  the service rebuilds the canonical message itself, so it can never be used as an oracle
  to sign arbitrary bytes.
- **Unbound (v2) receipts**, by default. A v2 counterparty entry vouches for a
  computation without recording which request asked for it, and a vouch that cannot be
  tied to a request can be replayed against a different one. `--allow-unbound` opts out.
- **Signing when no key is present.** Every other key path in this stack is
  load-or-generate; here a key invented on first contact would make the producer's pin
  meaningless, so a missing key is a hard error. Only provisioning may mint.

Refusals come back as bounded codes. A signing service that explained itself in detail
would be describing its policy to whoever probes it.

## Reading a failure

| code | what it means |
|---|---|
| `unbound-request` | policy: this counterparty co-signs only request-bound receipts |
| `unpinned-counterparty` | something signed correctly, but not with the pinned key |
| `not-a-counterparty-message` | the producer offered something that is not a vouch |
| `non-canonical-payload` | the two sides disagree about canonical encoding — stop and fix it |
| `transport-failed` | genuinely could not reach the host, or it produced no answer |

`transport-failed` means the network. Everything else means the policy — the client
reads the service's answer before it judges the exit status, precisely so a refusal is
not reported as an outage.

## Availability

Signing is on the critical path of every receipt: if the counterparty is unreachable, no
receipts are produced. That is the correct failure. The alternative — falling back to a
local key — would silently produce single-party receipts that look like two-party ones,
which is the failure this whole arrangement exists to prevent. If you want a receipt
without an independent counterparty, ask for one explicitly with `--counterparty-local`.
