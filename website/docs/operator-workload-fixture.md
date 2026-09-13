---
title: Isolated operator workload fixture
description: Local synthetic API fixture behind real pairing and encrypted transport.
---

# Isolated operator workload fixture

This opt-in benchmark tool provides generated cluster data to an unchanged
Skulk Operator client. It runs the real signed on-demand relay, gateway,
Ed25519 pairing exchange, token rotation, scoped authorization, and pinned
inner TLS 1.3. It does **not** construct a Skulk node, discover peers, join a
cluster, load models, read a model store, or execute cluster mutations.

This is an experimental qualification tool, not a production service or
capacity evidence. Its generated sizes and stream pacing are **not measured
released-client workload profiles**.

## Start a local session

Build a reviewed `paired-websocket-service` executable in `skulk-relay`, record
its SHA-256 digest, and run from this repository:

```bash
uv run python -m bench.operator_workload_fixture \
  --relay-binary /absolute/path/to/paired-websocket-service \
  --relay-sha256 <exact-sha256> \
  --lifetime-seconds 600
```

The digest is verified before fixture creation. Both provisioning and service
launch execute a protected copy of the exact verified bytes, not a mutable build
pathname. Readiness includes a certificate-verified local TLS handshake and a
live gateway task, not only the relay health endpoint. No remote relay URL, existing
cluster configuration, or supplied credentials are accepted. The tool creates
an owner-only temporary directory and two dynamic IPv4 loopback listeners.
Losing a bind race fails startup rather than reusing an existing listener.

The ready JSON record gives the relay port and paths to `pairing.txt` and
`pairing.png`, both mode `0600`. They contain generated credentials: do not
paste them into logs, PRs, or campaign evidence. The cluster is named
**Fixture**, with explicitly synthetic node/model labels. Its invitation
permits four pairings and expires after the selected session lifetime.

The session lasts 60–7,200 seconds, including setup. Normal exit, cancellation,
or expiry stops the gateway and relay and removes temporary state. An independent
watchdog reaps the relay on parent-pipe EOF, expiry, or termination. A hard kill
of the runner can leave its owner-only temporary directory behind; confirm the
watchdog has stopped before removing that exact generated directory. No cloud
resources are created; this is not a hosted campaign cleanup mechanism.

## Device and privacy boundary

An app schema accepting loopback WebSockets does not prove the installed app's
native networking stack permits cleartext. In particular, Android's release
network-security policy can prohibit a loopback `ws://` connection even when USB
forwarding works. Never weaken that policy or substitute a debug build for
released-client evidence. Verify trusted outer WSS on each physical platform.

The CLI remains loopback-only. A programmatic `private_ingress` context hook on
`isolated_fixture` and `observe_fixture` supports an independently expiring,
device-allowlisted private TLS bridge to the generated relay port. For this
pilot, the advertised origin must be an explicit `wss://` tailnet hostname and
port; public domains, credentials, paths, and cleartext origins are rejected.
The bridge must prohibit public Funnel access, independently stop on parent
death, bound traffic and connections, preserve backpressure, and verify removal.
Origin syntax validation alone is not an access-control check.

Provisioning uses the same private WSS origin for app and gateway roles; all
local listeners remain loopback-bound and signed authority and pinned inner
TLS are unchanged. The context closes the ingress even when provisioning fails.
There is no existing-authority or production-target input. Do not substitute
the operating relay, expose this private pilot publicly, or silently change artifact pins.

### Explicit public rehearsal hook

`isolated_fixture` separately accepts a `PublicFixtureIngress` injected context.
Neither CLI nor `observe_fixture` enables this hook. It is mutually exclusive
with `private_ingress`, limits the fixture lease to one hour, and requires an
exact `wss://rehearsal-<32 lowercase hex run identifier>.<domain>` origin without
a port, path, credentials, query or fragment. The context must return that exact
origin; mismatch or subsequent provisioning failure closes the owned context.
Private-pilot origin validation is unchanged.

The ingress context starts before the generated carrier. It must not block entry
on carrier-dependent public readiness: start its independent guardian, yield the
predetermined owned origin, and verify public readiness after the fixture starts.
Only this explicit public path allows up to 120 seconds for local route readiness,
capped by the remaining whole-session lease. Guardian exit still fails immediately;
timeout never yields pairing to the caller. Local/private readiness retries are
unchanged. The controller must verify public readiness before exposing pairing.

The separately reviewed controller must create and verify ownership of a new
dedicated hostname and tunnel, expose only the supplied generated carrier port,
enforce aggregate byte and connection limits with backpressure, and arrange
independent expiry even if the controller dies. It must verify tunnel and DNS
cleanup with two independent inventory snapshots. Hostname validation does not
prove ownership, resource bounds, or cleanup; the hook alone is not permission
to create provider resources. Any cleanup uncertainty blocks another run.

No diagnostic listener, existing authority, production configuration or workload
may be exposed. Signed authority and pinned inner TLS remain unchanged. Use only
generated inputs and retain aggregate evidence. A successful controller exit or
zero transport errors alone does not prove client operation, recovery, capacity,
or production readiness; verify those outcomes separately.

Use only generated test prompts and speech input, never personal information.
Generated handlers discard input and do not echo or persist it. Access logs
are disabled. Real authority state lives in the fresh encrypted database.
The optional observer below exports aggregate measurements. Actual released
device capture and source/artifact attestation remain separate steps.

## Observe aggregate workload metadata

`bench.observe_operator_workload` connects the fixture to the compiled aggregate
recorder from `skulk-relay`. Build reviewed recorder modules there first; this
command does not install Node, packages, or cloud infrastructure.

```bash
uv run python -m bench.observe_operator_workload \
  --relay-binary /absolute/path/to/paired-websocket-service \
  --relay-sha256 <exact-relay-sha256> \
  --node-binary /absolute/path/to/node \
  --recorder-module /absolute/path/to/workload-observation.js \
  --recorder-sha256 <exact-recorder-sha256> \
  --cli-module /absolute/path/to/workload-observation-cli.js \
  --cli-sha256 <exact-cli-sha256> \
  --lifetime-seconds 600
```

The two JavaScript modules are copied from verified bytes into a protected
temporary directory. Pair the test app using the generated artifact before
starting a flow, then close the app and let its sockets close. The local stdin
control channel accepts only `begin <flow>`, `end`, and `finish` on separate
lines. Supported flow names are `cold-launch`, `foreground-refresh`,
`settled-foreground`, `background-resume`, `reconnect`, `chat`, and `speech`.
For each flow, begin while no socket is open, perform that flow in the app,
then close the app and wait for sockets to close before `end`. All seven flows
must contain requests before `finish` can produce aggregate JSON. EOF, invalid
commands, incomplete flows, and non-idle transitions fail instead of producing
partial evidence. Control acknowledgements never echo input text.

The observer adds one bounded opaque TCP bridge before the real inner-TLS
listener. Socket lifetimes are measured at this **gateway TCP boundary**, not
at the device's outer WebSocket. ASGI counters measure request bodies consumed
and response bodies offered by the gateway. Completion is recorded when the
final body (or promised trailers) successfully returns from ASGI `send`, not
when application cleanup finishes. A disconnect notification concurrent with
that final send waits for its outcome; this prevents normal streaming teardown
from cancelling its own completion bookkeeping. Early transport closure,
incomplete bodies/trailers, send errors, cancellation and non-2xx statuses remain
failures. A completed gateway response is not proof the device received or
rendered it. No paths, query strings, headers,
peer addresses, credentials, prompts, responses, hashes of bodies, or packet
traces are exported. Unknown paths become the fixed category `other`.

On peer closure, the bridge first closes its backend leg and then allows any
already-running terminal ASGI sends at most one second to settle before closing
its admitted socket and recording connection closure. This prevents an abrupt
streaming-carrier close after complete HTTP framing from being counted as a
failure solely because server cleanup finishes later. Errors, cancellation and
the settlement timeout still fail unfinished requests; partial streams do not
receive a grace period. Observed bridge lifetimes include this bounded settlement
overhead and must not be treated as uninstrumented carrier lifetimes. No extra
background tasks or unbounded queues are introduced.

Per-observer bounds are 512 live connections/requests, 100,000 events, two hours,
and 10 GiB of application-body bytes. The recorder pipe holds at most 512
metadata events of at most 512 bytes each; overflow invalidates capture rather
than dropping samples. Output is at most 256 KiB of aggregate JSON. There are
no raw event files. The application-byte cap counts each body once; it is
**not** a two-relay-leg traffic meter or a provider spending limit.

Output remains `unattested-aggregate`. The added bridge and recorder overhead
must be measured before profile freeze; gateway timings must not be relabeled
as client timings. Generated integration tests do not establish physical app
provenance, real stream payload distributions, generator headroom, or capacity.

## Generated API behavior

All fixture paths require real scoped bearer authorization except the existing
pairing challenge, exchange, and refresh routes under their normal rules.
The real auth router also supplies device listing and revocation.

| Method and path | Behavior |
| --- | --- |
| `GET /state` | One synthetic healthy node and two ready synthetic instances. |
| `GET /v1/models` | Generated text and streaming speech cards; no weights. |
| `GET /store/registry` | Two synthetic entries with zero artifact bytes. |
| `GET /store/storage` | Empty generated staging inventory. |
| `GET /store/downloads` | Empty generated download list. |
| `GET /v1/audio/voices` | One silence voice; query values ignored. |
| `POST /v1/chat/completions` | Discards bounded input; fixed SSE text, stop event and `[DONE]`, without inference. |
| `POST /v1/audio/speech` | Discards bounded input; 3 seconds of silence, 30 × 4,800-byte chunks paced 100 ms apart; mono PCM16 at 24 kHz with the app's format headers. |

Requests are bounded to 64 KiB before parsing or stream generation; oversized
bodies receive `413`. Unknown mutation paths return `404`. Direct dashboard
invitation management remains unavailable on the relay listener even to scoped
devices. The body bound is not a two-leg traffic meter or hosted budget control.

## Validation and evidence limits

```bash
uv run pytest bench/tests/test_operator_fixture_app.py \
  bench/tests/test_operator_fixture_lease.py \
  bench/tests/test_operator_workload_fixture.py

SKULK_PAIRED_RELAY_BINARY=/absolute/path/to/paired-websocket-service \
uv run pytest bench/tests/test_operator_workload_fixture.py
```

The joined test uses a loopback-only, no-retry WebSocket/TLS client, not the
mobile app. It checks actual pairing, generated reads and complete SSE/PCM
streams with both server-EOF and client-close-on-framing behavior, mutation rejection,
protected files and teardown. Other tests cover real token rotation/revocation,
oversized input, generated streams, watchdog expiry and parent EOF.

Observer tests cover real TCP lifetime, ASGI privacy, terminal send/disconnect
ordering, failed sends, cancellation, trailer completion, fail-closed bounds,
incomplete capture, delayed TLS listener readiness, and verified-copy execution.
The real relay integration runs both with and without observation. To also
exercise the compiled recorder grammar, explicitly select its local directory
and Node executable:

```bash
SKULK_FIXTURE_RECORDER_DIRECTORY=/absolute/path/to/compiled/recorder \
SKULK_FIXTURE_NODE_BINARY=/absolute/path/to/node \
uv run pytest bench/tests/test_operator_fixture_observer.py \
  bench/tests/test_operator_fixture_recorder.py \
  bench/tests/test_observe_operator_workload.py
```

The interoperability test pins the compiled modules by digest. Update those
pins only alongside an inspected recorder change, not to bypass a mismatch.

For source-pinned schema compatibility, supply an existing app checkout with
installed TypeScript and Zod versions matching that commit's lock:

```bash
uv run python -c 'import json; from bench.operator_fixture_app import generated_responses; print(json.dumps(generated_responses()))' |
  node bench/validate_operator_fixture.cjs /absolute/path/to/skulk-app <exact-source-commit>
```

The validator reads a fixed schema-module allowlist with `git show`, without
changing the app checkout. It checks stable identity, ready model projections,
and streaming speech metadata. Generated models use canonical `TextGeneration`
and `TextToSpeech` task values, not model-hub pipeline names. Validation also
requires the text runtime to be chat-selectable; a ready runtime alone is
insufficient. Output explicitly marks physical-device and
capacity evidence false. Source compatibility does not prove the installed
binary, native socket behavior, observed polling/stream profiles, capacity,
endurance, or hosted behavior.

Pass `--streams` as the validator's third argument and include `_chatChunks`
(the decoded chunks from `bench.operator_fixture_app.generated_chat_chunks`) in its
input to exercise the pinned app's actual SSE parser. This requires a complete
text response and exactly one successful completion. Every synthetic SSE chunk,
including the terminal chunk, carries a stable completion `id` and choice
`index: 0`. Speech uses bounded mono PCM16 silence with explicit sample-rate,
channel-count, and sample-format headers; no model inference is performed.
