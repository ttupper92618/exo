---
title: Extensions (Plugins)
---

<!-- Copyright 2025 Foxlight Foundation -->

Skulk can load separately installed Python packages as extensions and call
them at well-defined points in the serving path. Extensions are how
deployment-specific behavior (an audit logger, a request policy filter, a
prompt annotator, a memory layer) rides the fabric without forking Skulk.

An extension is a normal Python package installed into the same environment
as Skulk. At node startup Skulk discovers every package that registers an
entry point in the `skulk.extensions` group, version-checks it, and loads it.
No configuration file, no registration API call: install the package and
restart the node.

This is primarily a Python-side contract. Provider discovery and node-to-node
call admission also have control-sized endpoints in the
[HTTP API reference](api-guide.md), but extension authors normally use the
typed surfaces exported from `skulk.extensions` and documented below.

## The contract

An extension provides three things (`src/skulk/extensions/types.py`):

- **`name`**: a short unique name, used in logs.
- **`skulk_requires`**: a [PEP 440](https://peps.python.org/pep-0440/)
  version specifier for the Skulk versions it supports, for example
  `>=1.4,<1.5`. An extension whose specifier does not match the running
  Skulk is refused at load time with a loud error. Mixed plugin/fabric
  versions are the same anti-pattern as mixed-version clusters; upgrade the
  fleet and its extensions together.
- **`chat_middleware()`**: returns the extension's chat middleware, or
  `None` if it has none.

Chat middleware gets two hooks, both `async`:

- **`transform_chat_request(context, task_params)`** runs on the API node
  after the OpenAI adapter has normalized the request and before it is
  dispatched to the cluster. It returns (possibly modified) task params, so
  it can rewrite or augment the prompt, adjust sampling, or annotate the
  request.
- **`observe_chat_response(context, task_params, summary)`** runs as a
  background task after the response has finished streaming. The summary is
  immutable (final text, thinking text, finish reason, error flag); an
  observer can log, index, or learn from it, but can never touch the stream.

Each hook receives an **`ExtensionContext`** carrying the node identity, the
running Skulk version, and `embed_texts`, programmatic in-process access to
the cluster's embedding serving (the equivalent of `POST /v1/embeddings`).
`embed_texts` returns `None` when no embedding instance is available;
extensions must degrade gracefully on `None`, never raise.

### Reading the cluster (`read_cluster`)

`ExtensionContext.read_cluster()` returns an immutable snapshot of the telemetry
plane: one `ClusterNodeView` per node the local node currently sees, each with
`node_id`, `friendly_name`, `backends`, `participation`, `skulk_version`,
`accelerator_vendor`, `ram_total_bytes`, `last_telemetry` (the freshest dedicated
heartbeat or ordinary telemetry fallback receipt), and `capabilities` (the tags
peers have advertised; see below). This is how an
extension discovers the cluster it belongs to instead of being blind to
everything beyond the request in front of it.

The call is cheap and side-effect free (an in-memory snapshot, no network I/O),
so it is safe from an inline hook. It is a **read**: an extension can observe the
cluster but never mutate telemetry. Every field beyond `node_id` may be `None`
(or an empty tuple) when that reading has not yet arrived (telemetry is
last-write-wins and partial), so treat missing values as "not known yet".

```python
for node in context.read_cluster():
    if node.accelerator_vendor == "amd" and node.last_telemetry is not None:
        ...  # e.g. prefer an AMD node for a GGUF-friendly task
```

### Advertising a capability (`advertise_capability`)

`ExtensionContext.advertise_capability(tag)` is the write half of the telemetry
plane: it publishes an opaque capability tag this node offers so peers discover
it the same way native nodes advertise their backends. The tag then appears in
every peer's `read_cluster()` snapshot under `ClusterNodeView.capabilities`.
Tags are free-form strings owned by your extension (for example `"memory"` or
`"embeddings:bge-m3"`); Skulk core neither interprets nor validates them.

Advertising is additive and idempotent, so the natural place to call it is once,
when the extension is constructed or on its first hook:

```python
context.advertise_capability("memory")
# ... later, on any node in the cluster:
peers_with_memory = [
    node for node in context.read_cluster() if "memory" in node.capabilities
]
```

Notes:

- The tag is gossiped on the node's normal telemetry poll, so peers see it
  within a second or two, not instantly. A node that advertised is discoverable
  by nodes that join later (the plane re-gossips it).
- `withdraw_capability(tag)` is the counterpart: it stops advertising the tag
  so callers stop selecting this node for it. When the last tag is withdrawn,
  one final empty reading is published so peers clear their entry; a node's
  tags also disappear when the node leaves the cluster.
- A node must run a worker to gossip its advertisement (the worker owns the
  telemetry emit path). The mainstream node runs both an API and a worker, so
  this is automatic; a rare API-only (`--no-worker`) node records the tag but
  does not gossip it.

## Serving a capability (providers)

Beyond observing chat traffic, an extension can be a **provider**: a plugin
that serves a capability of its own (a memory service, a speech backend,
anything not yet imagined). Skulk cannot enumerate future capabilities, so it
standardizes the *description* instead: a provider publishes one
`CapabilityDescriptor` per capability, a fixed self-describing shape that tells
any caller, human or LLM, how to call it.

A descriptor carries:

- `id` and `version`: the negotiation key (`echo@1.0.0`). The `id` doubles as
  the telemetry discovery tag and is auto-advertised for you.
- `title` and `description`: written for both humans and generative callers
  (an LLM reads the description plus the schemas at runtime to call a
  capability it has never seen, the tool-use model).
- `input_schema` / `output_schema`: JSON Schemas for the call payload and
  result.
- `io_mode`: how the call moves data: `unary`, `server_streaming`,
  `client_streaming`, or `bidirectional`, with chunk schemas for the streaming
  modes.

To become a provider, implement `capabilities()` on your extension; the
`on_start` startup hook is optional and independent (any extension can use it):

```python
class MyExtension:
    # ... name, skulk_requires, chat_middleware() as usual ...

    def capabilities(self) -> list[CapabilityDescriptor]:
        # This method alone makes the extension a provider.
        return [MY_DESCRIPTOR]

    def on_start(self, context: ExtensionContext) -> None:
        # Optional: startup registration with the live context; runs once at
        # node startup. Must be fast; heavy init belongs in background work
        # you own. A pure provider has no chat hook, so this is how it
        # reaches the context without waiting for a chat request.
        ...
```

Providers may implement `CapabilityReadiness.capability_ready(qualified_id)` to
expose cached per-capability health. The synchronous check must be fast and must
not perform I/O. False, a raised exception, or shutdown removes the descriptor
from local and remote discovery and rejects new unary and streaming calls with
`not_found`. A caller retaining an old descriptor cannot bypass the check.
Already admitted work retains its deadline and cancellation contract. Without
this optional facet, existing providers keep their current behavior. Providers
should also use `advertise_capability` / `withdraw_capability` when cached health
changes so telemetry tags follow availability.

`on_start(context)` runs once when the API serving lifetime begins, after node
construction, on the same event loop used for serving. Extensions that own
background tasks or child processes should implement the optional asynchronous
`SupportsExtensionShutdown.on_stop()` hook. At API shutdown, discovery closes
before hooks run concurrently under a shared thirty-second shielded cleanup budget.
Hooks must cooperate with cancellation and move blocking work off the loop;
in-process extensions remain trusted code, not sandboxed processes.

Management-only (`--no-worker`) API nodes publish their capability tags every two
seconds, including empty withdrawals, while advertising no inference backends.
This makes installed services discoverable without requiring a model worker.

Discovery then has two layers, cheap and heavy:

1. **Tag** (telemetry): peers see `"echo"` in `read_cluster()` capabilities.
2. **Descriptor** (describe): `await context.describe_node(node_id)` fetches
   the node's full descriptors; the same list is served over
   `GET /v1/capabilities` on every node.

```python
for node in context.read_cluster():
    if "echo" in node.capabilities:
        descriptors = await context.describe_node(node.node_id)
        # descriptors[0].input_schema tells you exactly what to send
```

A complete reference provider lives at `examples/extensions/echo-provider/` in
the repository.

## Calling a capability (`call_capability` / `handle_call`)

The generic call verb completes the unary loop. On the provider side, an
extension that also implements `handle_call` becomes callable:

```python
class MyExtension:
    # ... capabilities() as above ...

    async def handle_call(
        self, context: ExtensionContext, call: CapabilityCall
    ) -> dict[str, object]:
        # call.payload has already been validated against your input_schema.
        return {"text": call.payload["text"]}
```

On the caller side, any extension invokes a discovered capability through its
context:

```python
descriptors = await context.describe_node(node.node_id)
echo = next(d for d in descriptors if d.qualified_id == "echo@1.0.0")
result = await context.call_capability(
    node.node_id, echo.id, echo.version, descriptor_revision(echo),
    {"text": "hello"},
)
if result.ok:
    print(result.result)
else:
    print(result.error.code, result.error.message)  # typed, never parse prose
```

The call contract:

- **Typed results, never exceptions.** Every failure arrives as a
  machine-readable code on the result: `not_found`, `version_mismatch`,
  `revision_mismatch` (the provider's descriptor drifted since you discovered
  it; re-describe and retry), `invalid_payload`, `invalid_result`,
  `payload_too_large`, `overloaded`, `timeout`, `provider_error`,
  `unreachable`.
- **Pinned contract.** A call carries the exact `id@version` plus the
  descriptor revision digest from discovery, so discovery and invocation can
  never silently disagree.
- **Schema-validated both ways.** The payload is validated against the
  descriptor's `input_schema` before your handler runs, and your result
  against `output_schema` after. Validation never fetches remote schema
  references.
- **Bounded.** Calls have a deadline (default 30s) that spans the whole call,
  including target resolution on the caller and payload validation on the
  provider; payloads and results are capped at 1 MiB, and each node bounds
  concurrent in-flight provider calls; excess calls are rejected as
  `overloaded` rather than queued. Handlers are
  `async`: move CPU-heavy or blocking work off the event loop yourself (a
  worker thread), or you will stall the API node the handler runs on.
- **Direct and off the log.** Calls go node-to-node; the master is never in
  the hot path and calls are never event-sourced. Calling your own node is an
  in-process fast path with identical guards.

## Streaming a capability

All three streaming modes are executable. `server_streaming` providers
implement `handle_stream`; `client_streaming` and `bidirectional` providers
implement `handle_input_stream`. Skulk owns sequence-zero `started` in every
active direction, so provider output begins at sequence one:

```python
from collections.abc import AsyncIterator

from skulk.extensions import (
    CapabilityCall,
    CapabilityStreamFrame,
    ExtensionContext,
    InlineMediaAttachment,
)


async def handle_stream(
    self,
    context: ExtensionContext,
    call: CapabilityCall,
) -> AsyncIterator[CapabilityStreamFrame]:
    yield CapabilityStreamFrame(
        call_id=call.call_id,
        direction="provider_to_caller",
        sequence=1,
        kind="chunk",
        payload={"format": "pcm_s16le"},
        media=InlineMediaAttachment(
            data=audio_frame,
            media_type="audio/pcm",
            codec="pcm_s16le",
            sample_rate=24000,
            channels=1,
        ),
    )
    yield CapabilityStreamFrame(
        call_id=call.call_id,
        direction="provider_to_caller",
        sequence=2,
        kind="completed",
    )
```

The caller opens the stream through its context and checks the typed admission
result before consuming frames:

```python
session = await context.stream_capability(
    node.node_id,
    tts.id,
    tts.version,
    descriptor_revision(tts),
    {"text": "Speak this sentence."},
)
if not session.open_result.ok:
    print(session.open_result.error.code)
else:
    assert session.input is None  # server_streaming has no caller direction
    async for frame in session.frames:
        if isinstance(frame.media, InlineMediaAttachment):
            play(frame.media.data)
```

The opening request is control-sized and direct to the provider node. Output
does not stream over HTTP: it uses the separate `PROVIDER_DATA` type family,
off the master, State, and event log. Same-node output short-circuits locally;
remote frames use bounded independent per-owner/call/direction queues. Structured
payloads are validated against `output_chunk_schema`; inline media remains raw
bytes outside JSON and is capped at 1 MiB per frame. Use a staged
`BlobMediaAttachment` for large immutable objects.

Skulk enforces exact call identity and sequence, one deadline across admission
and streaming, exactly one terminal, bounded reorder/gap handling, and explicit
cancellation when the caller closes the iterator early. A raising or malformed
handler fails only its own stream with a typed terminal. After a handler yields
its terminal it must return. Skulk advances the iterator to exhaustion and
withholds that terminal from callers until the handler's `finally` cleanup has
completed, so dependent work cannot race resources that the provider still
owns. If handler output is malformed or continues after its terminal, Skulk
closes a closable iterator before publishing the synthetic failure terminal.

For `client_streaming` and `bidirectional`, the returned session has a
`CapabilityStreamInput` sink. `send_chunk()` accepts schema-validated metadata
and optional raw media. `complete()` emits the caller terminal and is an input
half-close: provider output remains active for a final transcript or additional
progress. `cancel()` terminates the logical call instead.

```python
session = await context.stream_capability(
    node.node_id,
    stt.id,
    stt.version,
    descriptor_revision(stt),
    {"model": model_id},
)
if session.open_result.ok and session.input is not None:
    await session.input.send_chunk(
        payload={"format": "pcm_s16le"},
        media=InlineMediaAttachment(
            data=pcm_frame,
            media_type="audio/pcm",
            codec="pcm_s16le",
            sample_rate=16000,
            channels=1,
        ),
    )
    await session.input.complete()
    async for frame in session.frames:
        consume_transcript(frame)
```

The provider receives the ordered caller lifecycle through
`handle_input_stream(context, call, input_frames)`. Input chunks are validated
against `input_chunk_schema`; a client-streaming provider can return one
structured payload on its `completed` frame, validated against `output_schema`,
while a bidirectional provider emits chunks validated against
`output_chunk_schema`.

A streaming provider whose availability depends on live state can additionally
implement `admit_stream(context, call)`. This dynamic admission hook runs after
the descriptor schema check and inside the same concurrency/deadline budget,
but before Skulk emits `started`. Return a typed `CapabilityError` to reject the
opening request without creating a stream. Static requirements still belong in
the descriptor schema; use admission only for conditions such as mounted-model
availability or backend health.

### Built-in mounted-model TTS provider

Production Skulk nodes register a first-party `tts@1.0.0` provider facade. It
does not load or run a second speech engine. Core Skulk remains authoritative
for model cards, store staging, mounting, placement, runner lifecycle, and
inference; the facade translates a generic provider call into the existing
`SpeechSynthesisTaskParams` / `AudioChunk` path.

The server-streaming input payload requires `model` and `text`. It optionally
accepts `voice`, `streaming_interval`, `speed`, `instruct`, `lang_code`, and the
speech sampling fields. Version 1 emits MP3 only. Each output `chunk` carries
`model`, `format`, `chunk_index`, `is_partial`, and an optional `sample_rate` in
the schema-validated payload, while the encoded MP3 bytes travel as a raw
`InlineMediaAttachment` rather than base64 JSON.

The descriptor is available through `GET /v1/capabilities`, but its telemetry
tag is advertised only when at least one mounted TTS card declares
`audio.supports_streaming = true` and MP3 output and every routable instance
of an eligible model has a ready runner.

The same requirements are rechecked during dynamic admission for the requested
model. A failure returns a typed opening error before `started`; caller close,
timeout, or transport failure cancels the underlying core synthesis command.
An external extension cannot replace the reserved built-in `tts@1.0.0`
contract; first-party providers take deterministic precedence when extension
registries are combined.

### Built-in voice activity detector

Every production API advertises the stable `vad@1.0.0` bidirectional provider.
It accepts ordered mono PCM16 at 8, 16, 32, or 48 kHz and emits typed
`speech_started` and `speech_stopped` chunks. Callers may configure WebRTC VAD
aggressiveness, 10/20/30 ms classifier frames, minimum speech, silence
hangover, preroll, and maximum utterance duration within bounded schema limits.
The provider processes media per call, retains no completed audio, and has no
mounted-model dependency.

### Built-in mounted-model batch STT provider

Production nodes also reserve `stt@1.0.0`, a bounded batch transform over the
existing `AudioTranscription` command and speech runner. The descriptor uses
`client_streaming` transport even though inference is batch: arbitrary encoded
audio is binary media, not a control-sized unary JSON payload. Callers open with
the mounted `model` plus optional filename, content type, language, prompt, and
model-specific decode controls; send one or more ordered
`InlineMediaAttachment` frames; then call `complete()` to half-close input.
Inference begins only after that half-close and returns one `completed` payload
with `model`, `text`, and optional `language` and `segments`.

The aggregate clip limit is 25 MiB and each provider frame retains the shared
1 MiB media limit. The `stt` telemetry tag is advertised only while a ready,
single-host mounted STT runner exists. This contract does not claim progressive
transcription; that remains exclusive to truthful `stt.realtime@1.0.0` models.
Managed `BlobMediaAttachment` resolution is not advertised yet because Skulk
does not have a general immutable blob service.

## Guarantees

Three invariants shape the design, and Skulk's call sites enforce them:

1. **A raising extension never breaks inference.** Every extension call is
   guarded: an exception is logged loudly and skipped, and the request
   proceeds as if the extension did not exist. Be precise about the scope,
   though: request transforms run inline before dispatch, so a *slow or
   hanging* transform delays the request it is transforming (keep transforms
   fast and bounded). Observers run as background tasks after the stream ends
   and can never affect request latency.
2. **Extensions never own the response stream.** Skulk accumulates the
   response and hands observers a summary, so a buggy extension cannot
   corrupt, reorder, or stall token delivery.
3. **No external extension installed means no external behavior.** External
   hooks are inert when none are loaded. First-party provider facades are
   registered explicitly by the production API and delegate to existing core
   services rather than introducing an independently installed plugin runtime.

## A complete example

A minimal extension that stamps a system-prompt suffix onto every chat
request and logs completions:

```python
# my_skulk_extension/extension.py
from skulk.extensions import (
    BaseChatMiddleware,
    ChatResponseSummary,
    ExtensionContext,
)
from skulk.shared.types.text_generation import TextGenerationTaskParams


class AuditMiddleware(BaseChatMiddleware):
    async def transform_chat_request(
        self,
        context: ExtensionContext,
        task_params: TextGenerationTaskParams,
    ) -> TextGenerationTaskParams:
        # Modify and return the params; return them unchanged to no-op.
        return task_params

    async def observe_chat_response(
        self,
        context: ExtensionContext,
        task_params: TextGenerationTaskParams,
        summary: ChatResponseSummary,
    ) -> None:
        print(f"[audit] finish={summary.finish_reason} chars={len(summary.text)}")


class AuditExtension:
    name = "audit-example"
    skulk_requires = ">=1.4,<1.5"

    def chat_middleware(self) -> AuditMiddleware:
        return AuditMiddleware()
```

Register the zero-argument factory in the package's `pyproject.toml`:

```toml
[project.entry-points."skulk.extensions"]
audit-example = "my_skulk_extension.extension:AuditExtension"
```

Install it next to Skulk on each node and restart:

```bash
uv pip install ./my-skulk-extension
```

The startup log lists every discovered extension and whether it loaded or
was refused (with the reason).

## Operational notes

For a separately installed controller that acquires compute capacity, see
[Controller integration](controller-integration.md) for the existing HTTP API
workflow: identity, two-way membership, exact placement, download and runner
readiness, and independent resource cleanup.

- **Install on every node.** Chat middleware runs on the API node that owns
  the request, and any node can serve API traffic, so install extensions
  fleet-wide (the same discipline as Skulk versions).
- **Kill switch:** `SKULK_EXTENSIONS_DISABLE=1` skips Python entry-point discovery
  and disables managed capability admission. Managed-owner configuration remains available.
- **`BaseChatMiddleware`** is a no-op base class; subclass it and override
  only the hook you need.
- Extension hooks currently cover the chat serving path. The surface will
  grow deliberately; anything an extension can reach is a public contract
  Skulk has to honor across versions.

## Installed node configuration

An extension can implement `NodeConfigurationProvider` from `skulk.extensions`
to expose ordinary settings through the Plugins dashboard and HTTP API. This
optional facet is independent of readiness and does not change existing extension
compatibility. Implement `configuration_nodes()`, `node_configuration(node_id)`,
and `configure_node(node_id, mutation)` using the plugin's existing owner store.

Identify each installed node persistently: multiple advertised capabilities can
share one configuration, while separate installations must never share mutable
settings accidentally. Include disabled nodes in inventory. Changes must check
both `expected_revision` and `expected_schema_digest` before validation and
atomic persistence. The plugin must enforce its own preflight before enabling,
and must retain cleanup obligations when disabling a node. Skulk authorizes
management but cannot approve provider spending through this facet.

Declare ordinary fields in `configuration_schema`; do not return credential
values or local protected-file paths. The dashboard currently renders scalar,
enumerated and nested-object fields with local schema references. Unsupported
forms are identified explicitly; server-side schema validation remains
authoritative. See the [HTTP contract](api-guide.md#plugin-node-configuration).

Credential entry is separate from ordinary settings. The Plugins dashboard uses
masked single-line input by default; choose **Use multiline input** for keys or
other credentials containing line breaks. Multiline drafts are visible while
editing. Switching input mode clears the draft, and submission clears it before
the request is sent. Stored credential values are never returned to either form.

## Separately supervised plugin owners

`ManagedOwner` connects to a locally installed owner process through an owner-only
Unix socket. Skulk imports no plugin SDK and does not launch an executable through
this adapter. Local setup registers a protected JSON connection under
`SKULK_CONFIG_HOME/managed-plugins/` with `plugin_id` (the `managed.` namespace)
and an absolute `state_root`. This record is host-local setup data, not an HTTP
request field. Symlinks, unsafe ownership/permissions and duplicate installation
IDs are refused. Connection failure leaves the configuration provider listed as
unavailable and removes capability readiness. Other valid connections still load.

The preferred local setup writes `SKULK_CONFIG_HOME/managed-service/connection.json`.
Skulk watches this fixed connection and the manager inventory, so setup and later
installation registration appear without restarting the API. The individual
connection records above remain compatible. Manager health independently fences
capability admission; a healthy child cannot override missing manager observations.
An explicit runtime disable also releases cached capability IDs, allowing an
enabled replacement to advertise without an API restart. An unavailable owner
whose enabled state is unknown, including missing or unreadable selection state,
still reserves its IDs to prevent silent takeover.
Neither case removes cached nodes from management.
The [managed lifecycle HTTP routes](api-guide.md#managed-plugin-http-lifecycle) and
Plugins dashboard remain available while children are disabled or broken. The `/plugins`
and `/plugins/` dashboard routes also support direct links and browser refreshes.

The local protocol reads installed node IDs, ordinary settings and cached unary
descriptors; mutations fence node identity, settings revision and schema digest.
The owner must report the same Skulk transport identity. Unary calls retain the
negotiated contract and deadline, and are never replayed after a lost response.
Management and call responses are bounded to 128 KiB; ordinary requests to 16 KiB
and invocation requests to 64 KiB. The socket grants no remote operator scope or
provider spending approval. The existing HTTP authorization boundary still applies.

`DynamicCapabilityProvider.dynamic_capabilities()` is an optional synchronous,
cached unary snapshot. It performs no I/O. The loader consults current snapshots
for discovery and dispatch, so an owner's later arrival or activation needs no
Skulk restart. Static capability IDs retain priority. Duplicate dynamic contracts
are hidden even when one claimant is unavailable. The loader reconciles dynamic
telemetry tags once per second; the managed adapter polls local health with a
one-second timeout and refuses observations older than three seconds.

Owners may advertise `host_callbacks_available: true` in their local description
and expose the fixed `host.sock` beside `control.sock`, with the same owner-only
directory and socket protections. Skulk binds protocol `1` and its transport node
ID; the owner acknowledges `{"ready":true}`. One sequenced callback is outstanding
at a time. The fixed operations are `actions` (live global steward policy),
`revisions` (currently visible owned descriptor revisions), and `invoke` (exact
installed node ID, capability ID/version/revision and JSON arguments). Invocation
uses ordinary Fabric routing and admission. Frames are complete newline-delimited
JSON, bounded to 64 KiB; callbacks have a three-second owner deadline. No callback
accepts an executable, remote address or approval key. Lost calls are never replayed
when the local connection returns.

Such owners also support fixed `steward-tools` and `steward-invoke` control requests.
The former returns at most 16 ordinary `StewardTool` contracts; the latter carries
the exact tool and arguments for fresh owner validation. Discovery and invocation
use the existing steward eligibility, schema and response limits. Reads have a
five-second overall deadline; inert proposal preparation has twenty seconds for
bounded catalog and placement reads. Neither tool mode approves spending. Provider
proposal review, independent approval and approved execution are separate contracts.

Shutting down Skulk closes its host binding, stops observation and withdraws dynamic
tags. The independent owner and its cleanup obligations remain supervised separately.

## Retained proposal references and review

The optional `NodeProposalReviewProvider` facet exposes bounded journal metadata
through `node_proposals(node_id, offset)` and `node_proposal(reference)`. Exported
`ProposalReference` binds `plugin_id`, stable `node_id`, provider-owned opaque
`proposal_id` and immutable `proposal_digest`. Core never equates the ID with its
digest or accepts replacement canonical input. A provider must require both to
match its retained record and repeat current eligibility checks before approval
or dispatch. The reference itself grants no authority.

`ProposalPage` contains at most sixteen `ProposalSummary` entries and an optional
next offset. A summary has plain text bounded to 1,024 characters, expiry in UTC
Unix seconds and observed journal state. `ProposalReview` adds observation time
and up to 32 plain-text fields (label 64 characters, value 2,048 characters).
Review data covers the facts an owner needs for the exact operation. Secrets,
canonical executable input and approval material must remain provider-local.
All responses have an additional 128 KiB aggregate bound. The HTTP representation
uses the standard camel-case aliases; Python field names remain snake_case.

Managed owners advertise `proposals_available` per node and implement fixed
`proposal-list` / `proposal-review` control operations. Listing carries plugin/node
identity and offset; review carries those identities plus the complete reference.
Skulk resolves the actual installation first and validates returned identities.
Listing remains advisory under concurrent changes; a selected record needs a
fresh exact review. This facet provides no signing or execution method. See the
[read-only HTTP contract](api-guide.md#plugin-proposal-review).

## Distinct owner proposal actions

The optional `NodeProposalActionsProvider` facet in `proposal_actions.py` exposes
`approve_node_proposal(mutation, operator_id)`,
`node_proposal_operation(node_id, operation_id)` and
`resume_node_proposal(node_id, operation_id, operator_id)`. Advertise
`proposal_actions_available` independently of review availability. `ProposalApproval`
contains only the durable operation ID, exact reference and `review_revision`;
`ProposalReview.approval_revision` is the provider's current review fence or null.
The API supplies the authenticated actor after explicit approval-scope checks.
This facet must never be exposed as a steward tool or authorized by management
permission alone.

Providers retain original intent before effects, keep signing material private,
revalidate exact reviewed terms, and return bounded `ProposalOperation` observations.
Identical accepted requests observe retained work. Explicit recovery can resume
interrupted approval; submitted and uncertain work is never automatically replayed.
Managed IPC uses fixed `proposal-approve`, `proposal-operation`, and
`proposal-resume` operations. The dashboard renders this generic contract; policy,
approval issuance and provider reconciliation remain plugin responsibilities.
The optional `ProposalOperation.reconciliation` carries a `ProposalReconciliation`
with correlated lifecycle `state`, nullable journal-read `observed_at`, `stale`,
and a nullable safe `code`. Keep original submission `phase` unchanged when cleanup
later confirms absence. Historical absence must survive failed observations;
missing receipts never imply absence. Cleanup reads must not depend on current
spending approval or replay any provider effect. These facts do not prove model
preparation or inference readiness.
See the [owner action HTTP contract](api-guide.md#plugin-owner-proposal-actions).

## Offline runtime verification and staging

Skulk's `extensions/runtime_artifacts.py` verifies the v2 signed runtime envelope
without importing a plugin SDK. Generic claims bind the publisher, exact bundle
and wheel bytes, supported platform/Python version, qualified Skulk build, state
schema and permission summary. Plugin-specific manifest policy stays opaque but
is covered by the signature. Trust comes from owner-provisioned protected local
storage, not the release. Revoked or expired artifacts and incompatible hosts
are refused. Supported targets are Apple Silicon macOS and Ubuntu 24.04 x86_64.

`RuntimeInstaller` in `extensions/runtime_install.py` stages complete supplied
artifacts under a stable service root. It verifies wheel tags, archive paths,
metadata identities and dependencies before running offline pip with exact hashes,
no index, no dependency resolution and no source builds. The archive must contain
the fixed `__owner__.py` entry point; HTTP callers cannot select an executable or
Python module. Staging creates a separate virtual environment and checks its exact
inventory without changing the Skulk environment. The base interpreter remains
an explicitly supported host prerequisite.

The installer journals operation IDs, release-sequence identities and monotonic
trust revisions. `operation(id)` reads progress without replaying work. A disconnected
browser leaves accepted downloads running. Before offline staging starts, those
downloads wait up to 30 seconds for competing local installer ownership and then
revalidate trust and compatibility. Brief supervisor checks therefore delay the
same operation without another download. Timeout or shutdown while waiting leaves
the operation recoverable without starting an installer. A disconnected waiter
does not release the installer lock or prevent successful work from recording
`staged`. A failed or interrupted generation remains `recovery_required`, retaining
its evidence; another call with that ID does not implicitly run installation again.
Completed stages can be reverified from their cached artifacts. Completion requires
a fsynced marker and a fresh measurement of the qualified core build.

Completion also seals the installed runtime's file membership, bytes, permissions
and interpreter target. Cached verification checks that seal before starting
Python, so an added startup file or modified dependency cannot execute during
validation. Runtime commands disable bytecode writes. The seal is protected local
installation evidence, not publisher metadata or a sandbox against the service
user. A missing or mismatched seal requires explicit recovery; the installer does
not bless existing changed files by creating a replacement seal.

Staging does not change active selection, logical plugin identities, configuration,
credentials or cleanup obligations. Installer output is bounded and stored only
as protected host-local evidence. These are local installation primitives; the
configuration HTTP routes do not accept artifact paths or expose raw installer logs.

## Stopped-owner runtime selection

`extensions/runtime_selection.py:RuntimeSelector` provides local activation,
selection with the owner stopped, explicit rollback, retained-state withdrawal and interrupted-selection
recovery. It holds both the installer fence and the existing supervisor lock;
the caller must stop the affected plugin owner first. Skulk inference and an
independent cleanup service are outside this operation's process ownership.

Activation repeats current publisher trust, exact host compatibility, cached
artifact verification and installed-file integrity. One atomic
`runtime-selection.json` publishes the selected generation with a revision and
operation ID. This is desired installation state, not proof of process health.
The terminal and HTTP lifecycle action `select` publishes `enabled: false` after
the same verification and permission checks as `activate`. The dashboard exposes
**Select with owner stopped** for offline setup or migration. No owner runs or
initializes its identity until a later explicit activation using the new revision.
This operation does not migrate state; plugin-owned local tooling handles that.
Interrupted stopped selection revalidates trust and artifacts, while disable
remains available for withdrawing an invalid release. Explicit disable can replace
a stalled activation or stopped selection after the owner fences are acquired,
without executing or trusting that release. Its durable `withdraws_operation_id`
links the old operation; unpublished transitions become terminal `superseded`
history, while already published selections are recorded complete. Both journals
retain enough intent to finish an interrupted withdrawal. Live work and a pending
disable cannot be replaced; the latter uses explicit recovery.

Operation records and pending intent let reconnect read the result and explicit
recovery finish the same local switch after an interruption. Recovery performs
no provider acquisition and does not replay uncertain provider creates.

An installation keeps its bundle identity and stable state directory across
generations. Expanded permissions need explicit acceptance. Lower release
sequences need explicit rollback, and the highest selected sequence is retained
even after rollback. State changes require declared compatibility; configuration
schema changes require migration tooling. Existing development manifests cannot
be mixed into a managed selection. Disable retains the selected release, files,
identities, configuration, credential history and cleanup obligations, and remains
available when release trust is invalid.

Selection is a local primitive: it does not register system services, automatically
stop an owner, provide full node preflight, or approve a paid proposal. The service
launcher must revalidate the selected generation against the live core before
executing the fixed private owner entry point.


### Installing the local manager service

The installed Skulk package includes its declarative model resources. Setup does
not require a Git checkout or a `SKULK_RESOURCES_DIR` override. If resources are
missing, reinstall the complete qualified package before retrying setup.

The candidate `skulk-plugin-service setup` command prepares a verified independent
manager runtime and registers a fixed nonroot system service on Apple Silicon
macOS or Ubuntu 24.04 x86_64. Run it as the existing Skulk owner in the qualified
isolated environment; only its fixed registration helper requests local elevation.
It generates service storage and a local profile connection without configuration
file editing. `skulk-plugin-service status` separates retained setup progress from
current management availability and registered-runtime integrity.
If registration succeeds but readiness is still pending, setup reports
`service_readiness_pending` with the retained operation ID. Once status verifies
both runtime integrity and management availability, repeat setup in the same
qualified environment to complete that operation without elevation or restarting
the healthy service.

See the [local setup contract](api-guide.md#local-system-service-setup) for supported
paths, interruption recovery, privileges and current qualification boundaries.
This installs no private SDK into Skulk, provisions no provider credentials, and
grants no spending approval. Existing independent cleanup supervision is untouched.


## Owner-configured private release source

The [private release HTTP and terminal contract](api-guide.md#private-release-inspection-and-installation)
adds one trusted HTTPS source per managed installation. Direct owner administration
provisions publisher trust and a write-only feed credential; remote plugin grants
can inspect and stage releases but cannot redirect that credential or replace the
trust root. The manager retains exact installation IDs and verifies metadata before
artifact transfer, then passes only signed bytes to the offline installer. Browser
disconnect does not abandon accepted staging. The dashboard separates review,
installation and explicit activation. Interrupted installation and protected partial
evidence remain available for local recovery rather than automatic replay.

`skulk-plugin-service install-plugin` provides the same guided release workflow in
an owner terminal, generating internal IDs and collecting the feed credential through
hidden input. Publisher trust, artifact installation and owner activation each
require distinct consent. The printed `install-plugin MANAGED_ID` command resumes
by reading retained operations; only explicitly confirmed download recovery retries
the original local installation. Continue with the plugin's own configuration and
preflight commands before enabling capability work. This command adds no provider
policy or spending authority to core.


## Public node setup exports

An installed management provider may implement `NodeSetupProvider` from
`skulk.extensions.setup` and set its inventory node's `setup_available` flag.
Its asynchronous `node_setup(node_id)` returns `NodeSetup` with observed
configuration and credential revisions and bounded `SetupArtifact` text files.
Use this for generated public identities or public keys that another local setup
command needs. Keep secrets in the write-only credential workflow.

The facet remains available independently of child readiness. Generate required
internal identities during authorized local installation or owner initialization;
this read must neither generate keys nor change settings, lifecycle state or
spending authority. Return only public data, never executable files or private
host paths. The generic API enforces explicit plugin read authority, exact node
identity, unique safe filenames and response bounds. The Plugins dashboard offers
explicit downloads for nodes declaring support. See the
[HTTP contract](api-guide.md#capability-node-public-setup-files).


### Running an installed plugin's local setup

An optional `__setup__.py` in the signed archive supplies provider-owned local
setup. Owners invoke it with `skulk-plugin-service setup-plugin managed.example --
<plugin setup fields>`. Skulk resolves the installed ID from its protected local
service profile and verifies the selected archive, full dependency runtime, current
publisher trust and Skulk compatibility before executing that fixed entrypoint.
No archive path, Python path, module name or command string is accepted.

The setup process runs as the existing nonroot owner and inherits terminal input
and output. Plugins define their own bounded setup fields and prompt for secrets;
credentials must not be command-line arguments. Only an explicit plugin-local
setup step may request local elevation through its fixed helper. No HTTP route
executes local setup, and a remote management grant cannot invoke it.

A disabled selected plugin can be configured this way. The installer ownership
lock survives process replacement, preventing a concurrent generation switch until
setup exits. Current services are not stopped by the launcher. Missing entrypoints,
changed selections, damaged artifacts, lost trust history and foreign local profile
bindings are refused. The entrypoint and its behavior must be qualified with the
plugin release; the existence of this launcher is not physical-install acceptance.


### Durable nonbillable setup actions

`skulk.extensions.setup_actions.NodeSetupActionsProvider` is an optional management
facet alongside the read-only public setup-files facet. Implement
`node_setup_actions`, `start_node_setup`, `node_setup_operation`, and
`resume_node_setup`; advertise `ConfigurableNode.setup_actions_available` only
when this node supports them. The isolated managed adapter delegates these to the
owner's fixed `setup-actions`, `setup-start`, `setup-operation`, and `setup-resume`
IPC operations. Existing owners omit the flag and remain compatible.

Forms declare only ordinary external inputs. Generate internal paths and identities
inside the owner, and provision credentials through protected owner mechanisms.
A start carries exact configuration/credential/action fences and a durable operation
ID. Reserve intent before effects and return promptly; neither the browser request
nor the unary child invocation slot owns the background task. Preserve progress
and original intent across reconnect, process restart and partial local writes.
Do not automatically retry ambiguous external effects under this setup contract.

Return safe bounded observations while children are disabled or unavailable.
Core validates node/operation identities, bounds forms and rejects schemas declaring
secret fields. It enforces read/manage scopes. Declare `SetupAction.requires_approval`
for setup that prepares owner approval access, and retain it in `SetupOperation`.
Clients send the reviewed `expected_requires_approval`; providers revalidate it
under their write lock. Core additionally requires `plugins:approve` for these
actions and whenever either the original operation or current action requires it
on resume. Defaults preserve existing management-only actions. This interface
cannot grant spending approval. The dashboard reuses ordinary configuration controls, preserves drafts
across changed fences and exposes explicit original-operation resume. See the
[setup API contract](api-guide.md#capability-node-nonbillable-setup-operations).


### Installed terminal management

A selected signed archive may provide a fixed optional `__manage__.py` entrypoint.
`skulk-plugin-service manage-plugin MANAGED_ID -- PLUGIN_ARGUMENTS` discovers the
protected service connection and selected installation, verifies its full runtime,
compatibility, trust and retained release history, then executes only that entrypoint.
The publisher supplies fixed management verbs; the caller cannot select an executable
or module. The plugin derives its internal coordinates from its verified installed
source. Local setup and management wait up to 30 seconds for the generation lock
before executing plugin code, so periodic runtime verification does not cause an
immediate refusal. Selection and trust are checked again after ownership is acquired;
a timeout or changed selection refuses execution. The command is never replayed.
The generation lock survives exec to prevent upgrades during the command.

This local nonroot command retains terminal I/O and does not itself invoke sudo,
change release selection or authorize paid effects. Disabled node management remains
plugin-owned. Missing or invalid runtimes refuse local execution; use the independent
installation manager for recovery when the owner cannot start. Existing
`setup-plugin` behavior and extensions without `__manage__.py` remain compatible.
Plugin-specific commands belong in that plugin's documentation.


Asynchronous proposal execution can report `ProposalOperation.phase = acknowledged`
after the capability controller durably accepts the exact approved request. The
provider request may remain queued. Preserve this observation across restart and
use receipt reconciliation for later active/absent state; neither reconnection nor
status reads may repeat the effect. A lost or uncorrelated response remains uncertain.


### Retained uninstall

The generic manager exposes distinct `disable` and `uninstall` lifecycle actions.
Both use stopped-owner withdrawal and preserve independently supervised cleanup.
Uninstall remains visible in inventory as `uninstalled: true`; registration, runtime
artifacts, configuration, identities, credentials and receipt history are retained
for cleanup and explicit reinstallation. It never means remote resources are absent.
The terminal and dashboard use the same operation, revision checks and recovery.
Only a later committed, verified `select` or `activate` reinstalls the plugin;
merely downloading a release or accepting a pending operation does not.
