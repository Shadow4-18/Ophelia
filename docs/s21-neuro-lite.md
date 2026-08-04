# S21 Ultra Neuro-lite stack (local / free)

Goal: as close as practical to a Neuro-sama multi-mind feel while running
**only on a Galaxy S21 Ultra** — no paid APIs required.

The model gate still loads **one** Ollama model at a time. Keep chat ≤3B and
inner/reaction minds at **1B** so swaps stay tolerable on phone RAM/heat.

## Pull once

```bash
ollama pull llama3.2:3b
ollama pull llama3.2:1b
# optional faster reflex: ollama pull gemma3:1b
```

## `~/.ophelia/.env` (phone)

```env
OPHELIA_PROVIDER=ollama

# Chat = the “main” mind (tools, longer replies)
OLLAMA_MODEL=llama3.2:3b

# Inner life + director + voice rewrite (cheap, always-on roles)
OLLAMA_CONSCIOUSNESS_MODEL=llama3.2:1b

# Reaction = ultra-short quips when director picks react
OPHELIA_PROVIDER_REACTION=ollama
OLLAMA_REACTION_MODEL=llama3.2:1b
OPHELIA_REACTION=true

# Ensemble decision layer
OPHELIA_DIRECTOR=true

# Alive Free Will aggressive defaults (already code defaults)
OPHELIA_CONSCIOUSNESS_INTERVAL=45
OPHELIA_INITIATIVE_THRESHOLD=0.32
OPHELIA_MAX_SPONTANEOUS_PER_HOUR=20

# Phone body optional
OPHELIA_ANDROID_ENABLED=true

# Skip cloud media unless you opt in later
# OPHELIA_PROVIDER_IMAGE=pollinations   # free, no key
```

## What each mind does on this stack

| Mind | Model | Job |
|------|--------|-----|
| Chat | 3B | Full replies + tools |
| Consciousness | 1B | Inner ticks, JSON, drives |
| Director | 1B (via consciousness) | speak / react / defer / skip |
| Reaction | 1B | One-liner when director says `react` |
| Voice | 1B (via consciousness) | TTS rewrite |

Vision/image/video stay optional — on-device vision (llava) is heavy; prefer
skipping or rare use on S21.

## Verify

```bash
ophelia check --chat-only
ophelia providers   # should list reaction → ollama / llama3.2:1b
```

Then `ophelia update` + restart after pulling this build.

## Reality check

You will **not** get Neuro’s PC multi-GPU latency. You *will* get:

- separate roles for chat vs reflex vs inner life
- director routing into a fast reaction path
- event wakes + soft satiation (Alive Free Will)

Still later for true streamer parity: Filter mind, game-event vision reactions,
dedicated director provider role, avatar/VTS bridge.

See [neuro-ensemble.md](neuro-ensemble.md) and [local-first.md](local-first.md).
