# Bundled reference voices

These project-authored recordings provide stable, named reference conditioning
for speech models that truthfully support voice cloning. Each profile pairs one
audio file with its exact transcript and a checksum in `catalog.toml`.

The runtime resolves profiles by stable ID, verifies containment and SHA-256,
and loads the asset only on the worker serving the request. Clients select a
profile through the standard `voice` field; uploaded reference audio remains an
explicit request-scoped override.

## Authoring

Every profile is a fully synthetic voice: designed with a voice-design-capable
TTS model, with the best output frozen as ~14 seconds of mono 44.1 kHz MP3.
No profile is a recording of a real person, so no consent or likeness
questions attach to any of them. The original ten (angus through sylvie) were
designed with the Qwen3-TTS 0.6B CustomVoice model before its card was
retired as unstable (#752); `skulk`, the product's signature voice and the
steward's voice, was constructed the same way as a separate authoring pass.

All profiles deliberately share one conditioning transcript (each profile's
`reference.txt` is identical). Holding the text constant makes the voice the
only variable when validating cloning fidelity across models, so any audible
difference between two profiles is voice identity, never script content.
