# PC Workstation UI

Neuro-style **command center** in your browser — avatar stage, chat, live mood/drives, inner monologue. No Telegram required.

## Launch

```powershell
pip install -e .
ophelia ui
```

Opens **http://127.0.0.1:8765/** (configurable).

```powershell
ophelia ui --no-browser
```

## Layout

| Panel | Shows |
|-------|--------|
| **Stage** | Avatar — context-driven expression, animations, lip sync (procedural / Live2D / VRoid / VRChat) |
| **Channel** | Chat with Ophelia (same memory as `ui:local` session) |
| **State** (toggle) | Mood, valence/arousal, drives, urges, last inner thought |
| **Inner monologue** (toggle) | Live stream from `~/.ophelia/data/inner_monologue.md` |
| **Status bar** | Model, initiative pressure, consciousness, avatar backend |

Consciousness runs in the background — spontaneous messages appear in chat (highlighted) and animate the avatar. **Pause mind** stops outreach without stopping the server. Use **state** in the top bar to open psyche + inner panels.

### Performance (expressions · animations · lip sync)

The avatar bus is driven by psyche **and** the active conversation:

| Field | Meaning |
|-------|---------|
| `activity` | `idle` \| `listening` \| `thinking` \| `speaking` \| `reacting` |
| `animation` | Gesture id (`idle_breathe`, `think`, `talk`, `nod`, `react`, …) |
| `expression` | From mood, feelings, urges, inner thought, and the current chat turn |
| `viseme` / `visemes` | Approximate lip shapes (honors `[pause:Xs]` in spoken text) |
| `gesture` | Breath rate, sway, lean-in, nod, blink rate for the stage |

Flow: user message → listening/thinking → reply with lip sync → brief reacting → idle. Initiative lines set `speak_source=initiative`.

## Avatar / Live2D / VRoid / VRChat

The workstation ships a **procedural** VTuber-style stage that reads a shared parameter bus. Mood, drives, and speaking state stream over WebSocket (`avatar` events) and `GET /api/avatar`.

| Backend | File | Notes |
|---------|------|--------|
| `procedural` | (none) | Built-in canvas presence |
| `live2d` | `*.model3.json` | Cubism Core not bundled — bus ready for your runtime |
| `vroid` | `*.vrm` | VRoid / UniVRM export; three.js + `@pixiv/three-vrm` (CDN) |
| `vrchat` | `*.fbx` (primary), also `*.glb` / `*.gltf` | Unity humanoid FBX; morph targets + bones |

`OPHELIA_AVATAR_BACKEND=auto` prefers `.vrm`, then `.fbx`, then `.glb`/`.gltf`, then Live2D, else procedural.

### Drop in a VRoid model

1. Export **VRM** from [VRoid Studio](https://vroid.com/) (VRM 0.x or 1.0).
2. Place it under the avatar directory:

```text
~/.ophelia/avatar/
  model.vrm
```

### Drop in a VRChat model

VRChat avatars are authored as **FBX** (Unity humanoid). Drop the FBX on the stage:

```text
~/.ophelia/avatar/
  model.fbx          # also: ophelia.fbx, avatar.fbx, vrchat.fbx, any *.fbx
```

```env
OPHELIA_AVATAR_ENABLED=true
OPHELIA_AVATAR_DIR=~/.ophelia/avatar
OPHELIA_AVATAR_MODEL=model.fbx
OPHELIA_AVATAR_BACKEND=auto   # auto | procedural | live2d | vroid | vrchat
```

The stage loads FBX via three.js `FBXLoader`, matches morph names such as `vrc.v_aa`, `vrc.blink`, `Joy` / `Angry` / `Sorrow`, and poses `Head` / `Neck` / `Spine` bones from the shared param bus.

Alternates: `.glb`/`.gltf` still work; UniVRM `.vrm` uses the `vroid` backend. Native **`.vrca` AssetBundles cannot load in the browser**.

### Drop in a Cubism model

```text
~/.ophelia/avatar/
  model.model3.json
  …textures / motions…
```

```env
OPHELIA_AVATAR_MODEL=model.model3.json
OPHELIA_AVATAR_BACKEND=live2d
```

Model files are served at `/avatar/…`. Cubism Core is **not** bundled (Live2D license). VTube Studio / custom bridges can subscribe to the same `/api/avatar` + `/ws` events.

## Env

```env
OPHELIA_UI_HOST=127.0.0.1
OPHELIA_UI_PORT=8765
OPHELIA_UI_OPEN_BROWSER=true
OPHELIA_AVATAR_ENABLED=true
```

Configure a provider first — see [pc-setup.md](pc-setup.md).

## vs Telegram

| | Workstation UI | Telegram |
|--|----------------|----------|
| Platform | PC browser | Phone/desktop app |
| Phone tools | off by default | N/A on PC |
| Consciousness | yes | yes |
| Avatar stage | yes (procedural / Live2D / VRoid / VRChat) | no |
| Voice STT/TTS | no (yet) | yes (xAI) |

Use **both**: run `ophelia ui` on PC and `ophelia run` on phone with Telegram — separate channels unless you unify later.

See also: [neuro-ensemble.md](neuro-ensemble.md) (avatar mind roadmap).
