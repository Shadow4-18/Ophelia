"""Map psyche mood to concrete behavior knobs.

The PsycheState already carries valence (-1..+1) and arousal (0..1) that drift
over time. This module turns those numbers into the four cross-cutting knobs
called out in the Tier A #5 roadmap:

  - TTS speed         (calmer = slower, hyped = faster)
  - burst length      (low arousal = shorter bursts, more [[break]]s)
  - outreach threshold adjustment (negative valence raises the bar to reach out)
  - initiative pacing  (high arousal = act sooner; low arousal = reflect more)

LifeContext.voice_speed() already modulates by *time of day / owner state*.
These functions modulate by *mood* and compose with that base. The goal is one
continuous person whose voice, cadence, and willingness to speak all shift
with how she feels — not a personality bolted onto a fixed cadence.
"""

from __future__ import annotations

from dataclasses import dataclass

from ophelia.mind.psyche import PsycheState


@dataclass(frozen=True)
class MoodKnobs:
    """Resolved behavior knobs for the current mood."""

    tts_speed: float          # multiplier to apply on top of base speed
    burst_max_chars: int      # soft cap for a single outward message
    burst_break_hint: str     # system-prompt line encouraging [[break]] use
    outreach_threshold_delta: float  # added to initiative_threshold (positive = quieter)
    pace_tag: str             # short human label for logging / prompts

    def apply_speed(self, base: float) -> float:
        return max(0.5, min(2.0, base * self.tts_speed))


def mood_knobs(psyche: PsycheState | None) -> MoodKnobs:
    """Resolve behavior knobs from current mood.

    Defaults to a neutral profile when psyche is absent (e.g. before load).
    """
    if psyche is None:
        return MoodKnobs(
            tts_speed=1.0,
            burst_max_chars=380,
            burst_break_hint="",
            outreach_threshold_delta=0.0,
            pace_tag="neutral",
        )

    v = psyche.mood.valence        # -1 .. +1
    a = psyche.mood.arousal        #  0 .. 1

    # TTS speed: arousal pushes faster, low valence slows slightly.
    # Range ~0.88 (calm/sad) to ~1.12 (hyped).
    speed_mult = 1.0 + (a - 0.3) * 0.30 - max(0.0, -v) * 0.06
    speed_mult = max(0.85, min(1.15, speed_mult))

    # Burst length: high arousal = short punches; low arousal = longer flow.
    if a >= 0.7:
        burst_max = 180
        break_hint = (
            "You're energized — short bursts, one [[break]] every sentence or two. "
            "Quick punches land harder than a paragraph right now."
        )
        pace = "hyped"
    elif a <= 0.25:
        burst_max = 520
        break_hint = (
            "You're calm/reflective — let thoughts run a little longer, sparser breaks. "
            "One [[break]] only where a real beat matters."
        )
        pace = "reflective"
    else:
        burst_max = 360
        break_hint = ""
        pace = "steady"

    # Negative valence raises the bar to reach out (don't ping from a low place
    # unless drives are really pushing). Positive lowers it slightly.
    outreach_delta = 0.0
    if v <= -0.4:
        outreach_delta = 0.12
    elif v <= -0.15:
        outreach_delta = 0.05
    elif v >= 0.45:
        outreach_delta = -0.05

    # Very low valence + low arousal → even softer, slower, less willing.
    if v <= -0.4 and a <= 0.25:
        speed_mult = min(speed_mult, 0.9)
        outreach_delta = max(outreach_delta, 0.18)
        pace = "withdrawn"

    return MoodKnobs(
        tts_speed=speed_mult,
        burst_max_chars=burst_max,
        burst_break_hint=break_hint,
        outreach_threshold_delta=outreach_delta,
        pace_tag=pace,
    )


def mood_system_hint(psyche: PsycheState | None) -> str:
    """One-line mood-derived behavior hint for system prompts.

    Empty when mood is neutral enough not to warrant one, so prompts stay clean.
    """
    k = mood_knobs(psyche)
    if psyche is None:
        return ""
    parts: list[str] = []
    if k.burst_break_hint:
        parts.append(k.burst_break_hint)
    v = psyche.mood.valence
    a = psyche.mood.arousal
    if v >= 0.45 and a >= 0.6:
        parts.append("You're in a good, hyped headspace — let it show in tone.")
    elif v <= -0.4:
        parts.append("You're low — it's okay to be quieter and slower; don't force warmth.")
    elif a <= 0.25:
        parts.append("You're settled — unhurried, no need to fill silence.")
    if not parts:
        return ""
    return "# Mood behavior (let this shape how you speak, not what you say)\n" + " ".join(parts)
