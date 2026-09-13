<!-- Copyright 2025 Foxlight Foundation -->

# Contributing to Skulk

Thank you for your interest in contributing to Skulk! Skulk is maintained by [Foxlight Foundation](https://github.com/foxlight-foundation) and forked from [exo](https://github.com/exo-explore/exo).

## Getting Started

To run Skulk from source:

**Prerequisites:**
- [uv](https://github.com/astral-sh/uv) (for Python dependency management)
  ```bash
  brew install uv
  ```
- [mactop](https://github.com/metaspartan/mactop) (for hardware monitoring on Apple Silicon)
  ```bash
  brew install mactop
  ```
- [node](https://github.com/nodejs/node) (for building the dashboard)
  ```bash
  brew install node
  ```
- [Nix](https://nixos.org/download/) (for `nix fmt`, `nix flake check`, and the repo dev shell)
- [rust](https://github.com/rust-lang/rustup) (to build Rust bindings, nightly for now)
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  rustup toolchain install nightly
  ```

```bash
git clone https://github.com/foxlight-foundation/Skulk.git
cd Skulk/dashboard-react && npm install && npm run build && cd ..
uv sync
uv run skulk
```

Skulk's runtime contract on macOS follows the `uv` environment and the
official `mlx`/`mlx-metal` wheel stack. Nix is used for reproducible
development tooling and validation, not as a hidden alternate MLX runtime.

## Project Structure

Skulk is built with a mix of Rust, Python, TypeScript (React for the dashboard), and the codebase is actively evolving.

### Key directories:
- `src/skulk/` — Python backend (inference, API, store, worker, routing)
- `dashboard-react/` — React dashboard (Skulk UI)
- `rust/` — Rust components (networking, libp2p, PyO3 bindings)
- `resources/inference_model_cards/` — Text generation model metadata TOML files
- `resources/image_model_cards/` — Image model metadata TOML files
- `resources/embedding_model_cards/` — Embedding model metadata TOML files
- `resources/speech_model_cards/` — Speech model metadata TOML files
- `resources/speech_reference_voices/` — Checksummed bundled TTS conditioning audio and exact transcripts
- `deployment/logging/` — VictoriaLogs + Grafana stack and Vector config
- `docs/` — Technical documentation
- `docs/model-runtime-notes/` — Internal per-model clustered runtime notes

### Dashboard (React)

The Skulk dashboard is a React + TypeScript + styled-components app in `dashboard-react/`. Key areas:

- `src/components/pages/` — Top-level views (ChatView, DownloadsPage/ModelStore)
- `src/components/cluster/` — ClusterCard, PlacementManager, RunningInstanceCard
- `src/components/layout/` — HeaderNav, SettingsPanel, InstancePanel, ConversationPanel, StoreRegistryTable
- `src/components/chat/` — ChatForm, ChatMessages, ChatModelSelector
- `src/stores/` — Zustand stores (chatStore, uiStore) with localStorage/sessionStorage persistence
- `src/hooks/` — useClusterState, useConfig, useModelPicker
- `e2e/` — Explicit Playwright qualification against a running Skulk dashboard

To run the dashboard in dev mode:
```bash
cd dashboard-react && npm run dev
```
This starts a Vite dev server on port 3000 with hot reload. The dev server proxies API calls to `http://localhost:52415` (the Skulk backend).

### Backend

- `src/skulk/api/main.py` — FastAPI server (OpenAI, Claude, Ollama API compatibility)
- `src/skulk/master/` — Master node (placement, election, event sourcing)
- `src/skulk/worker/` — Worker node (inference, runner management, download coordination)
- `src/skulk/store/` — Model store (registry, downloads, config, model optimizer)
- `src/skulk/operator/` — Stable operator identity, quorum certification,
  crash-fault consensus, bounded dormant proposal lifecycle, and
  encrypted/public authority persistence. It also owns the designated-gateway
  local key provider, paired-WebSocket relay configuration/connector,
  `skulk operator pair` and `configure-relay` commands, and single-use device
  pairing plus credential lifecycle; the matching FastAPI routes and relay-only
  canonical API guard live in `src/skulk/api/operator_auth.py` and
  `src/skulk/api/operator_gateway.py`. It is a
  separate security plane from event-sourced inference state; do not place its
  secrets or mutable authorization records in `State`, telemetry, diagnostics,
  or ordinary events.
- `src/skulk/shared/` — Shared types, constants, topology
- `website/docs/` — Docusaurus documentation source, including API guide and model-capability docs

## Development Guidelines

Before starting work:

- Pull the latest source to ensure you're working with the most recent code
- Keep your changes focused — implement one feature or fix per pull request
- Avoid combining unrelated changes, even if they seem small

This makes reviews faster and helps us maintain code quality as the project evolves.

When a branch is release-worthy or bumps the project version, update both
`CHANGELOG.md` and the public docs release notes under `website/docs/` in the
same change.

## Pull Request Review Loop

When working an active PR, use this review loop:

1. Inspect the PR for new review comments, unresolved threads, and failing checks.
2. Rank each comment on the repository's 1–5 severity scale.
3. Ignore severity 1–2 comments.
4. Defer severity 3 comments unless maintainers explicitly ask for them in the current PR.
5. Fix severity 4–5 comments with the smallest correct change.
6. Add or update focused tests for every correctness fix.
7. Run focused validation before replying.
8. Reply on each addressed thread with the concrete fix.
9. Resolve only threads that are actually addressed.
10. Repeat until there are no unresolved severity 4–5 comments, or stop and escalate if the change becomes ambiguous, broad, or blocked.

## Code Style

Write pure functions where possible. Leverage the type systems available to you — Rust's type system, Python type hints, and TypeScript types. Comments should explain why you're doing something, not what the code does — especially for non-obvious decisions.

Run `nix fmt` to auto-format your code before submitting.

For the React dashboard:
- Use styled-components for styling (no CSS modules or Tailwind)
- Use Zustand for state management (not Redux or Context)
- Use individual selectors from stores to avoid unnecessary re-renders
- Follow the existing component patterns (styled components at top, component function at bottom)

## Model Cards

Skulk uses TOML-based model cards to define model metadata and capabilities.
The signed external registry is the curated source of truth; bundled cards are
retained only as a transition fallback, and local custom cards remain explicit
operator overrides. Model-card locations are:
- `Foxlight-Foundation/foxlight-model-registry/seed/cards/` for the pinned migration seed and registry candidate workflow
- `resources/inference_model_cards/` for text generation models
- `resources/image_model_cards/` for image generation models
- `resources/embedding_model_cards/` for embedding models
- `resources/speech_model_cards/` for TTS/STT speech models
- `~/.skulk/custom_model_cards/` for user-added custom models

### Adding a Model Card

Do not add a new curated card only to Skulk's bundled resources. Submit it to
the private registry as one exact artifact (one card per quant/file), pin a full
40-character source revision and exact GGUF file where applicable, then attach
runtime qualification evidence. Structural validation alone must leave it a
candidate. Bundled edits during the transition must mirror the registry and
state why fallback compatibility requires them.

To add a new model, create a TOML file with the following structure:

```toml
model_id = "mlx-community/Llama-3.2-1B-Instruct-4bit"
n_layers = 16
hidden_size = 2048
supports_tensor = true
tasks = ["TextGeneration"]
family = "llama"
quantization = "4bit"
base_model = "Llama 3.2 1B"
capabilities = ["text"]
context_length = 131072

[storage_size]
in_bytes = 729808896
```

### Required Fields

- `model_id`: Hugging Face model identifier
- `n_layers`: Number of transformer layers
- `hidden_size`: Hidden dimension size
- `supports_tensor`: Whether the model supports tensor parallelism
- `tasks`: List of supported tasks (`TextGeneration`, `TextToImage`, `ImageToImage`)
- `family`: Model family (e.g., "llama", "deepseek", "qwen")
- `quantization`: Quantization level (e.g., "4bit", "8bit", "bf16")
- `base_model`: Human-readable base model name
- `capabilities`: List of capabilities (e.g., `["text"]`, `["text", "thinking"]`)

### Optional Fields

- `context_length`: Maximum context window size in tokens (derived from `max_position_embeddings` in config.json)
- `components`: For multi-component models (like image models with separate text encoders and transformers)
- `uses_cfg`: Whether the model uses classifier-free guidance (for image models)
- `trust_remote_code`: Whether to allow remote code execution (defaults to `false` for security)

### Capabilities

The `capabilities` field defines what the model can do:
- `text`: Standard text generation
- `thinking`: Model supports chain-of-thought reasoning
- `image_edit`: Model supports image-to-image editing (FLUX.1-Kontext)

These coarse capability tags are intentionally broad. They help with catalog
badges and filtering, but they are not the full runtime behavior contract.

### Extended Capability Sections

Model cards can now optionally declare refined model behavior through structured
sections:

- `[reasoning]`
  - `supports_toggle`
  - `supports_budget`
  - `format`
  - `default_effort`
  - `disabled_effort`
- `[modalities]`
  - `supports_audio_input`
  - `supports_native_multimodal`
- `[tooling]`
  - `supports_tool_calling`
  - `tool_call_format`
- `[runtime]`
  - `prompt_renderer`
  - `output_parser`

These sections are optional. Existing cards still work without them.

At runtime, Skulk resolves the model card plus conservative model-family
defaults into a normalized capability profile. That resolved profile drives
model-aware reasoning defaults, prompt rendering, output parsing, and the
additive `resolved_capabilities` metadata returned by `/v1/models`.

For the full field reference and examples, see:
- [website/docs/model-cards.md](website/docs/model-cards.md)
- [website/docs/model-capabilities.md](website/docs/model-capabilities.md)

### Security Note

By default, `trust_remote_code` is set to `false` for security. Only enable it if the model explicitly requires remote code execution from the Hugging Face hub.

## Configuration

Skulk uses `skulk.yaml` for cluster configuration. Key sections:

- `model_store` — Store host, paths, staging, download settings
- `inference` — KV cache backend selection (`default`, `optiq`, `turboquant_adaptive`, etc.)
- `logging` — Centralized log aggregation (enabled toggle, ingest URL)
- `hf_token` — HuggingFace API token

Configuration can be edited directly in `skulk.yaml` or through the dashboard Settings panel. Changes made via the dashboard are synced to all nodes automatically via gossipsub.

## Centralized Logging

Skulk supports shipping structured logs from all cluster nodes to a central [VictoriaLogs](https://docs.victoriametrics.com/victorialogs/) instance via [Vector](https://vector.dev/).

### Setup

1. **Deploy the logging stack** on a central server (e.g. via Portainer):
   ```bash
   docker compose -f deployment/logging/docker-compose.yml up -d
   ```
   This starts VictoriaLogs (port 9428) and Grafana (port 3000).

2. **Configure logging** in the dashboard Settings panel, or in `skulk.yaml`:
   ```yaml
   logging:
     enabled: true
     ingest_url: http://<logging-server>:9428/insert/jsonline?_stream_fields=node_id,component&_msg_field=msg&_time_field=ts
   ```
   Settings are synced to all nodes via gossipsub.

3. **Install Vector** on each node:
   ```bash
   brew install vectordotdev/brew/vector
   ```
   (Vector is also available via `nix develop` if using the nix dev shell.)

4. **Run Skulk piped through Vector**:
   ```bash
   uv run skulk 2>/dev/tty | vector --config deployment/logging/vector.yaml
   ```
   stderr goes to the terminal (human-readable), stdout goes to Vector → VictoriaLogs.

5. **Update the VictoriaLogs URL** in `deployment/logging/vector.yaml` if your logging server is not at `192.168.0.118:9428`.

### Browsing Logs

- **VictoriaLogs VMUI**: `http://<logging-server>:9428/select/vmui/`
- **Grafana**: `http://<logging-server>:3000` (login with the credentials configured in your skulk.yaml logging section or set during stack deployment)

## API Adapters

Skulk supports multiple API formats through an adapter pattern. Adapters convert API-specific request formats to the internal `TextGenerationTaskParams` format and convert internal token chunks back to API-specific responses.

### Existing Adapters

- `chat_completions.py`: OpenAI Chat Completions API
- `claude.py`: Anthropic Claude Messages API
- `responses.py`: OpenAI Responses API
- `ollama.py`: Ollama API (for OpenWebUI compatibility)

For detailed API documentation, see [docs/api.md](docs/api.md).

## Testing

Skulk relies heavily on manual testing at this point in the project, but this is evolving. Before submitting a change, test both before and after to demonstrate how your change improves behavior. Do the best you can with the hardware you have available — if you need help testing, ask and we'll do our best to assist. Add automated tests where possible.

The React dashboard has Storybook stories for key components:
```bash
cd dashboard-react && npx storybook dev -p 6007
```

Dashboard component tests and the production build run locally with:

```bash
cd dashboard-react
npm run test
npm run build
```

The joined operator-access qualification is opt-in because it executes a real
`paired-websocket-service` binary from the sibling `skulk-relay` repository.
It starts an isolated loopback relay and a minimal relay-only Skulk API with
temporary authority state; it does not discover, join, or mutate a fleet:

```bash
SKULK_PAIRED_RELAY_BINARY=/absolute/path/to/paired-websocket-service \
uv run pytest src/skulk/operator/tests/test_joined_relay_integration.py
```

The test runs both version-one warm lanes and version-two on-demand lanes with
a binary built from reviewed relay source supporting `provision-on-demand`.
It proves QR package generation, Ed25519 device proof, credential
exchange, authenticated canonical `/state`, token rotation, paired-device
listing, revocation, and rejection of revoked credentials through the actual
opaque carrier and pinned inner TLS connection. It also opens new connections
after a signed lease renewal and recreates both the gateway and relay while
retaining the same app pairing material. Test listeners use generated loopback
ports and protected temporary files. This does not prove relay-side durable
fencing, physical-device compatibility, or hosted capacity.

The separate `bench/operator_workload_fixture.py` serves deterministic canonical
reads and synthetic chat/PCM streams behind the real on-demand gateway and
pairing service without constructing a Node. Its local lifetime, protected QR,
watchdog, tests and source-pinned schema validator are documented in
[Isolated operator workload fixture](website/docs/operator-workload-fixture.md).
It is not an observed workload profile or relay capacity result.
The explicit programmatic public-rehearsal hook requires a separately reviewed
bounded, independently expiring ingress controller with verified cleanup; no CLI
enables it. Its run-bound hostname gate does not attest provider ownership.
`bench/observe_operator_workload.py` adds fixed-vocabulary flow controls and a
bounded aggregate recorder pipe. The same contract documents artifact pins,
opt-in recorder tests, measurement boundaries, and physical-device prerequisites.

The live vision test is deliberately explicit because it places a real image
through the built-in dashboard and a running model. Provide the dashboard URL,
the exact mounted model ID, and a local PNG fixture:

```bash
cd dashboard-react
SKULK_DASHBOARD_URL=http://localhost:52415 \
SKULK_VISION_MODEL=mlx-community/Qwen3-VL-4B-Instruct-4bit \
SKULK_VISION_IMAGE_PATH=/absolute/path/to/portrait.png \
npm run test:e2e:vision
```

The test verifies the exact uploaded bytes in the completion request and checks
the rendered answer for the portrait's identity, clothing, and background.

## Licensing and File Headers

Skulk is licensed under the Apache License, Version 2.0 (see `LICENSE`). The
project began as a fork of exo, and the upstream attribution lives in the
`NOTICE` file as Apache 2.0 intends; do not remove or edit the exo entry
there.

Header conventions:

- New files authored for Skulk carry a Foxlight Foundation copyright header
  in the file's comment style, for example
  `<!-- Copyright 2025 Foxlight Foundation -->` or
  `# Copyright 2025 Foxlight Foundation`.
- Substantive modifications to files inherited from exo should add a
  `Modifications Copyright 2025-2026 Foxlight Foundation` line beneath any
  existing upstream header rather than replacing it. Where inherited files
  have no header, the repository-level `NOTICE` and git history carry the
  attribution.
- Contributions are accepted under Apache 2.0 (inbound = outbound). By
  submitting a pull request you license your contribution under the same
  terms.

## Branching Model

Day-to-day work branches from `dev` and merges back to `dev` via pull
request. `main` is the release branch: it only advances when `dev` is
promoted as part of a release cut, so `main` always reflects a tested,
releasable state. Open pull requests against `dev` (the repository
default); release promotion PRs from `dev` to `main` are opened by the
maintainers.

### Fresh-install release qualification

An end-to-end run against an existing configured fleet is regression coverage,
not proof that a new installation works. Before a release promotion, the
candidate commit must pass the fresh-install qualification matrix through the
public `skulk-test-harness` command. After `dev` is promoted to `main`, the
shipping qualification repeats the matrix using the literal installer command
from the README. The release or tag is not published until both runs are green.

The candidate profile installs a full commit ID; the shipping profile supplies
no ref or Skulk runtime overrides. Both profiles require the installer-generated
single-node configuration and the shipped transport/backend defaults.

Release operators run:

```bash
# Before dev -> main
uv run skulk-harness fresh-install qualify \
  --profile candidate --expected-commit <full-dev-sha> \
  --config <private-fresh-install-config>

# After promotion, before release/tag publication
uv run skulk-harness fresh-install qualify \
  --profile shipping \
  --expected-commit <full-promoted-main-sha> \
  --config <private-fresh-install-config>
```

The green reports must cover Apple Silicon, AMD Linux, and a clean RunPod
NVIDIA pod. A configured-fleet battery is not an acceptable substitute.

After the automated candidate matrix passes, a human tester exercises the same
exact commit through the first-install dashboard journeys in the
[human release qualification guide](website/docs/human-release-qualification.md).
Human acceptance supplements the automated gate; it cannot replace a failed or
incomplete harness matrix. Any product, shipped-default, installer, dashboard,
or model-card change made in response to human testing creates a new candidate
that must repeat the automated qualification before promotion.

## Submitting Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -am 'Add some feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request and follow the PR template

## Reporting Issues

If you find a bug or have a feature request, please open an issue on GitHub with:
- A clear description of the problem or feature
- Steps to reproduce (for bugs)
- Expected vs actual behavior
- Your environment (macOS version, hardware, etc.)

## Questions?

Open an issue or discussion on the [Skulk repository](https://github.com/foxlight-foundation/Skulk).
