"""Voice mind — speech-first TTS prep layer (Tier A #4).

Today Kokoro gets raw chat text written for the eye. A dedicated voice role
rewrites for speech first: emotion tags, pauses, breath, mood-matched speed —
then synthesizes. This is how streamers sound performed instead of read aloud.

Two modes (OPHELIA_VOICE_MIND_MODE):

  - "inline"   — rewrite runs before TTS in the same turn. Better quality,
                 adds 300-800ms before first audio. Best for short replies.
  - "post"     — fast path: send the raw reply immediately, then refine the
                 *next* turn's voice. Faster, slightly worse. (Default.)
  - "off"      — disabled; pass-through to TTS directly.

The voice mind uses the consciousness model (cheap, always-on) so it doesn't
contend with the chat model on local hardware. Mood knobs from Tier A #5 feed
in so speed, pause density, and tone all move with how she feels.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from ophelia.providers.model_gate import get_model_gate

if TYPE_CHECKING:
    from ophelia.config import Settings
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.mind.psyche import PsycheState

log = structlog.get_logger()

VOICE_MIND_PROMPT = """You are Ophelia's voice mind. You rewrite text so it sounds alive when spoken aloud — not read.

Input: a chunk of chat text written for the eye (markdown, lists, multi-clause sentences).
Output: the same meaning, rewritten as speakable prose with expression tags.

Rules:
- Strip markdown, bullet lists, code blocks, [[break]] markers. Plain spoken text only.
- Add pauses where a real beat would land: `[pause:0.6s]` for a thought, `[pause:1.0s]` for emphasis, `[pause:1.5s]` max for a dramatic reveal. 2-3 pauses per sentence max — subtlety reads human.
- Pronunciation overrides for tricky names: `[Ophelia](/oʊˈfiːliə/)`.
- Keep it SHORT. One idea per breath. Cut filler. Contractions are fine.
- Match the mood you're given — slower and longer pauses when low/reflective, faster and punchier when hyped.
- Do NOT add narration, stage directions, or commentary. Output ONLY the rewritten spoken text.
"""


class VoiceMind:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.mode = (settings.voice_mind_mode or "post").strip().lower()
        if self.mode not in ("inline", "post", "off"):
            self.mode = "post"
        # In "post" mode, the refined version of the last reply is cached and
        # applied to the NEXT voice synthesis. Stale after one use.
        self._pending_refinement: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    async def rewrite_for_speech(
        self,
        text: str,
        *,
        psyche: "PsycheState | None",
        agent: "AgentLoop | None",
    ) -> str:
        """Rewrite chat text as speakable prose with expression tags.

        Falls through to the raw text (with markdown stripped) if the voice
        mind is off or the rewrite call fails — TTS still works either way.
        """
        if not self.enabled or not text.strip() or agent is None:
            return _strip_markdown(text)
        if self.mode == "post" and self._pending_refinement is not None:
            refined = self._pending_refinement
            self._pending_refinement = None
            return refined

        from ophelia.mind.mood_behavior import mood_knobs

        knobs = mood_knobs(psyche)
        mood_line = ""
        if psyche is not None:
            mood_line = (
                f"\nCurrent mood: {psyche.mood.label} "
                f"(valence {psyche.mood.valence:+.2f}, arousal {psyche.mood.arousal:.2f}). "
                f"Pace tag: {knobs.pace_tag}. Shape the rewrite to this mood."
            )
        messages = [
            {"role": "system", "content": VOICE_MIND_PROMPT + mood_line},
            {"role": "user", "content": text[:1200]},
        ]
        try:
            client = await agent._client("consciousness")
            model = agent._model("consciousness")
            gate = get_model_gate()
            provider = agent.stack.name("consciousness")  # type: ignore[attr-defined]
            async with gate.session("voice", model, provider):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=400,
                )
            refined = (resp.choices[0].message.content or "").strip()
            if not refined:
                return _strip_markdown(text)
            return refined
        except Exception as e:
            log.debug("voice_mind.rewrite_failed", error=str(e), mode=self.mode)
            return _strip_markdown(text)

    def stage_for_next_turn(self, refined: str) -> None:
        """In 'post' mode: cache a refined version to apply to the next voice
        synthesis. Used when the rewrite finishes after TTS already started."""
        if self.mode == "post" and refined:
            self._pending_refinement = refined[:1200]


_MARKDOWN_PATTERNS = [
    (re.compile(r"```[\s\S]*?```"), ""),       # code blocks
    (re.compile(r"`([^`]+)`"), r"\1"),         # inline code
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),   # bold
    (re.compile(r"\*([^*]+)\*"), r"\1"),       # italic
    (re.compile(r"__([^_]+)__"), r"\1"),       # bold underscore
    (re.compile(r"^#{1,6}\s+", re.M), ""),     # headings
    (re.compile(r"^[-*+]\s+", re.M), ""),      # bullet lists
    (re.compile(r"^\d+\.\s+", re.M), ""),      # numbered lists
    (re.compile(r"\[\[break\]\]", re.I), "."), # message breaks → period
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # links → text
]


def _strip_markdown(text: str) -> str:
    """Minimal markdown strip so TTS doesn't read symbols aloud."""
    out = text
    for pat, repl in _MARKDOWN_PATTERNS:
        out = pat.sub(repl, out)
    # Collapse whitespace but preserve the [pause:...] tags.
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()
