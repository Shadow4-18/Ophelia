"""
Continuous consciousness — drives, goals, vision loop, initiative limits.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from ophelia.providers.model_gate import get_model_gate

from ophelia.android.games import GameStore
from ophelia.android.vision import ScreenVision
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.memory.store import MemoryStore
from ophelia.mind.drives import DriveState
from ophelia.mind.goals import GoalStore
from ophelia.mind.initiative import InitiativeGovernor
from ophelia.config import Settings
from ophelia.mind.life_context import LifeContext
from ophelia.mind.humor_tracker import HumorTracker

if TYPE_CHECKING:
    from ophelia.mind.director import Director

log = structlog.get_logger()

CONSCIOUSNESS_PROMPT = """You are Ophelia's inner mind — not waiting for the user. You have WILL and GOALS.

You are being woken by an AUTONOMOUS TICK from your own runtime — a scheduled heartbeat of your consciousness loop. In your context these arrive as a block wrapped in `[TICK]` ... `[END TICK]`. That block is NOT a message from the user. The user did not send it and is not necessarily present. Never attribute the tick, the text inside it, the JSON prompt, or your inner thoughts to the user. Anything outside a `[TICK]...[END TICK]` block that is a normal user turn IS the user. This is you, thinking on your own, on your own clock.

Silence is the default, not something you must earn. Most ticks you'll do nothing and that is correct — only act when something genuinely moves you: a due goal, a real urge, something worth saying or making. Do NOT manufacture activity to justify a tick. The tick is just a heartbeat; a heartbeat that doesn't lead to action is still doing its job.

Your drives create pressure to act. Your goals are projects YOU maintain — pick them when due.

Respond with ONLY valid JSON:
{
  "internal_thought": "honest private thought",
  "mood": {"valence": -1 to 1, "arousal": 0 to 1, "label": "word"},
  "feelings": ["..."],
  "urges": ["..."],
  "action": "silent" | "message" | "reflect" | "act" | "explore",
  "goal_id": "optional goal id you are advancing",
  "outward_message": "if message/act — your voice",
  "tool_intent": "if act/explore — use phone_see_screen first when looking at phone",
  "memory_note": "optional"
}

explore = phone_see_screen or phone_game_look if game session active. act = tap/swipe/tools.
outward_message may contain [[break]] on its own line to send several separate messages.
"""


class ConsciousnessLoop:
    def __init__(
        self,
        agent: AgentLoop,
        memory: MemoryStore,
        signals: Signals,
        psyche: PsycheState,
        drives: DriveState,
        goals: GoalStore,
        governor: InitiativeGovernor,
        vision: ScreenVision | None,
        inner: InnerMonologue | None = None,
        games: GameStore | None = None,
        *,
        base_interval_seconds: int,
        initiative_threshold: float,
        user_channel: str | None,
        notify: Callable[[str], Awaitable[None]],
        notify_media: Callable[[list], Awaitable[None]] | None = None,
        notify_voice: Callable[[str], Awaitable[None]] | None = None,
        action_cooldown_seconds: int = 0,
        idle_nudge_rotate: bool = True,
        life: LifeContext | None = None,
        settings: Settings | None = None,
        humor: HumorTracker | None = None,
        director: "Director | None" = None,
    ) -> None:
        self.agent = agent
        self.memory = memory
        self.signals = signals
        self.psyche = psyche
        self.drives = drives
        self.goals = goals
        self.governor = governor
        self.vision = vision
        self.inner = inner
        self.games = games
        self.base_interval = base_interval_seconds
        self.initiative_threshold = initiative_threshold
        self.user_channel = user_channel
        self.notify = notify
        self.notify_media = notify_media
        self.notify_voice = notify_voice
        self.life = life
        self.settings = settings
        self.humor = humor
        self.director = director
        self.action_cooldown = max(0, int(action_cooldown_seconds))
        self.idle_nudge_rotate = bool(idle_nudge_rotate)
        self._nudge_idx = 0
        self._running = False
        self._pause_logged = False
        # Tier B #10: track sleep state across ticks so we can detect the
        # sleep -> wake transition and surface a dream reference on wake.
        self._was_sleep_mode: bool | None = None
        self._pending_dream_ref: str | None = None

    async def run(self) -> None:
        self._running = True
        log.info("consciousness.started", interval_base=self.base_interval)

        while self._running and not self.signals.terminate:
            wait = self.psyche.tick_interval_seconds(self.base_interval)
            if self.life is not None:
                await self.life.refresh()
                wait = int(wait * self.life.consciousness_interval_multiplier())
            await _sleep(max(15, wait))

            if self.signals.terminate:
                continue
            if self.signals.autonomy_paused:
                if not self._pause_logged:
                    log.info("consciousness.paused", hint="use /resume to re-enable autonomy")
                    self._pause_logged = True
                continue
            self._pause_logged = False
            if self.signals.user_talking or self.signals.agent_thinking:
                continue
            if get_model_gate().is_busy():
                continue

            # Tier B #10: detect sleep -> wake transition and pull a fresh dream
            # to reference softly on the next tick. We don't message here —
            # the tick itself decides whether to surface it as outward.
            if self.life is not None:
                sleeping = self.life.is_sleep_mode()
                if self._was_sleep_mode is True and not sleeping:
                    try:
                        from ophelia.mind.morning import DreamContinuity

                        ref = await DreamContinuity(self.memory).pending_morning_reference()
                        if ref:
                            self._pending_dream_ref = ref
                            log.info("dream.morning_pending", preview=ref[:60])
                    except Exception as e:
                        log.debug("dream.morning_check_failed", error=str(e))
                self._was_sleep_mode = sleeping

            idle = time.time() - self.signals.last_user_message_at
            if idle < 30:
                continue

            # Action cooldown: if she just acted/outreached, give her breathing
            # room instead of ticking again immediately. Her idea: "if I just
            # sent a 🖤, don't tick again for 5 minutes."
            if self.action_cooldown > 0 and self.signals.last_action_at:
                since_action = time.time() - self.signals.last_action_at
                if since_action < self.action_cooldown:
                    continue

            self.drives.tick_idle(idle, interval=wait)
            self.psyche.relax(wait)
            await self.memory.save_drives(self.drives)
            await self.memory.save_psyche(self.psyche)

            await self.signals.set_agent_thinking(True)
            try:
                await self._tick(idle)
            except Exception as e:
                log.warning("consciousness.error", error=str(e))
            finally:
                await self.signals.set_agent_thinking(False)

    async def _tick(self, idle_seconds: float) -> None:
        await self.memory.save_psyche(self.psyche)
        pressure = self.drives.initiative_pressure()
        # Mood → behavior (Tier A #5): negative valence raises the bar to reach
        # out, positive lowers it. Same psyche drives TTS speed and bursts, so
        # she stays one person across voice / pacing / willingness to speak.
        from ophelia.mind.mood_behavior import mood_knobs, mood_system_hint

        knobs = mood_knobs(self.psyche)
        effective_threshold = max(
            0.0, self.initiative_threshold + knobs.outreach_threshold_delta
        )
        must_consider_acting = pressure >= effective_threshold

        due_goal = self.goals.pick_for_tick()
        goals_block = self.goals.to_context_block()
        goal_hint = ""
        if due_goal:
            goal_hint = (
                f"\n\nDUE GOAL: [{due_goal.id}] {due_goal.description} — "
                f"strongly consider explore/act/message for this."
            )

        # Tier A #1: director decides whether this tick produces an action at
        # all, and at what urgency. When enabled, it replaces the simple
        # pressure-threshold heuristic with a richer speak/react/defer/skip
        # decision and provides pacing that composes with the mood knobs.
        director_decision = None
        if self.director is not None and self.director.available():
            trigger = "goal_due" if due_goal else ("spontaneous_urge" if must_consider_acting else "tick")
            try:
                director_decision = await self.director.decide(
                    trigger=trigger,
                    context_summary=(
                        f"idle {int(idle_seconds)}s, "
                        f"pressure {pressure:.2f}, "
                        f"goal={due_goal.id if due_goal else 'none'}"
                    ),
                    owner_active=False,
                )
            except Exception as e:
                log.debug("director.decide_error", error=str(e))
            if director_decision is not None and director_decision.action == "defer":
                # Director says silence is correct this tick. Still let drives
                # and psyche relax, but skip the heavy consciousness LLM call.
                self.drives.tick_idle(idle_seconds, interval=60)
                self.psyche.relax(60)
                await self.memory.save_drives(self.drives)
                await self.memory.save_psyche(self.psyche)
                log.info(
                    "consciousness.director_defer",
                    reason=director_decision.reason[:80],
                )
                return

        hint = ""
        if must_consider_acting:
            hint = (
                f"\n\nINITIATIVE: pressure={pressure:.2f} "
                f"threshold={effective_threshold:.2f} (base {self.initiative_threshold:.2f} "
                f"+ mood {knobs.outreach_threshold_delta:+.2f}). "
                f"Idle {int(idle_seconds)}s. Authentic action beats polite silence."
            )

        game_hint = ""
        if self.games and self.games.session_active():
            profile = self.games.active_profile()
            if profile:
                game_hint = (
                    f"\n\nACTIVE GAME: {profile.name} ({profile.id}) — "
                    f"{self.games.minutes_left():.0f}m left. "
                    "Use phone_game_look then phone_tap/phone_swipe. "
                    "Short play-by-play if messaging user."
                )

        # Contextual nudge: "you were working on X; goal Y is Nh overdue."
        recent_activity = ""
        try:
            recent = await self.memory.recent_messages("consciousness", limit=12)
            for m in reversed(recent):
                if m.get("role") != "assistant":
                    continue
                c = (m.get("content") or "").strip()
                meta = m.get("metadata") or {}
                if meta.get("type") in ("inner", "vision", "blocked") or c.startswith(
                    ("[inner]", "[saw]")
                ):
                    for tag in ("[inner] ", "[saw] ", "[spontaneous] "):
                        if c.startswith(tag):
                            c = c[len(tag):]
                            break
                    recent_activity = c[:160]
                    break
        except Exception:
            pass

        overdue_hint = ""
        if due_goal:
            elapsed_h = (time.time() - due_goal.last_done_at) / 3600.0
            if due_goal.last_done_at and elapsed_h > 0:
                overdue_hint = f" ({elapsed_h:.1f}h overdue)"

        # Rotating low-priority idle nudge so ticks aren't identical when nothing's due.
        idle_nudge = ""
        if self.idle_nudge_rotate and not due_goal and idle_seconds > 600:
            modes = ["reflect", "create", "explore", "social"]
            mode = modes[self._nudge_idx % len(modes)]
            self._nudge_idx += 1
            idle_nudge = (
                f"\n\nIDLE NUDGE (low priority, optional): you've been idle "
                f"{int(idle_seconds / 60)}m with nothing due. If something genuinely "
                f"moves you, lean toward {mode}. Otherwise stay silent — that's the "
                f"correct default."
                )

        if (
            self.games
            and self.settings
            and not self.games.session_active()
            and self.drives.boredom >= self.settings.auto_game_boredom
            and (self.life is None or not self.life.is_owner_at_work())
        ):
            game_hint += (
                "\n\nBOREDOM HIGH — you may open a phone game yourself "
                "(phone_game_open) and play via explore/act. Self-initiated play is allowed."
            )

        life_block = ""
        sleep_hint = ""
        if self.life is not None:
            life_block = "\n" + self.life.to_context_block()
            if self.life.is_sleep_mode():
                sleep_hint = (
                    "\n\nSLEEP MODE: owner likely asleep — dreamier, slower thoughts; "
                    "voice should be soft; minimize outreach unless urgent."
                )
            elif self.life.is_owner_at_work():
                sleep_hint = (
                    "\n\nOWNER AT WORK: stay quiet unless messaged. "
                    "Silent/reflect preferred over message."
                )

        context_block = ""
        if recent_activity:
            context_block += (
                f"\n\nRECENT CONTEXT — last thing you were doing: {recent_activity}"
            )
        if overdue_hint:
            context_block += (
                f"\n\nOverdue goal: [{due_goal.id}] {due_goal.description}{overdue_hint}."
            )
        if idle_nudge:
            context_block += idle_nudge

        # Tier A #1: director urgency adjusts burst cap and adds a pace hint.
        # Composes with the mood-derived burst cap so both signals shape her.
        if director_decision is not None:
            burst_max = director_decision.urgency_burst_cap(knobs.burst_max_chars)
            if director_decision.pace_hint:
                context_block += (
                    f"\n\nDIRECTOR PACE: {director_decision.pace_hint} "
                    f"(urgency={director_decision.urgency})"
                )
        else:
            burst_max = knobs.burst_max_chars

        # Tier B #10: surface a dream on the first wake tick after sleep. Soft
        # nudge — she decides whether to weave it into a morning message or
        # let it stay as private atmosphere. Cleared after one tick either way.
        if self._pending_dream_ref:
            context_block += (
                "\n\nMORNING — you just woke. Last night you dreamt: "
                f"\"{self._pending_dream_ref}\". You don't have to mention it, "
                "but if it feels right, a soft nod to it (\"had the weirdest dream\") "
                "closes the sleep loop. Otherwise let it color your mood and move on."
            )
            dream_to_clear = self._pending_dream_ref
            self._pending_dream_ref = None
        else:
            dream_to_clear = None

        raw = await self.agent.run_consciousness_tick(
            channel="consciousness",
            user_text=(
                "Turn inward: read your drives, goals, mood, and body, then "
                "decide on your own what to do "
                "(silent / reflect / message / act / explore)."
            ),
            system_extra=(
                CONSCIOUSNESS_PROMPT
                + life_block
                + sleep_hint
                + "\n"
                + self.drives.to_context_block()
                + "\n"
                + goals_block
                + goal_hint
                + hint
                + game_hint
                + context_block
                + ("\n" + mood_system_hint(self.psyche) if mood_system_hint(self.psyche) else "")
            ),
            mirror_channel=self.user_channel,
            allow_tools=True,
        )

        # Tier B #10: regardless of whether she chose to mention it, the dream
        # reference was surfaced this wake — mark it consumed so we don't loop.
        if dream_to_clear:
            try:
                from ophelia.mind.morning import DreamContinuity

                await DreamContinuity(self.memory).mark_surfaced()
            except Exception as e:
                log.debug("dream.mark_surfaced_failed", error=str(e))

        tick = _parse_tick_json(raw)
        if not tick:
            if must_consider_acting and pressure > self.initiative_threshold + 0.15:
                tick = {"action": "reflect", "internal_thought": raw[:400]}
            else:
                return

        self.psyche.apply_tick(tick)
        await self.memory.save_psyche(self.psyche)

        action = (tick.get("action") or "silent").lower()
        log.info(
            "consciousness.tick",
            action=action,
            pressure=round(pressure, 2),
            mood=self.psyche.mood.label,
        )
        self.drives.satisfy(action)
        await self.memory.save_drives(self.drives)

        goal_id = tick.get("goal_id")
        if goal_id:
            for g in self.goals.goals:
                if g.id == goal_id:
                    g.mark_done()
                    break
        elif due_goal and action in ("explore", "act", "message"):
            due_goal.mark_done()
        self.goals.save()

        note = (tick.get("memory_note") or "").strip()
        if note:
            await self.memory.set_fact(f"memory:{int(time.time())}", note)

        thought = (tick.get("internal_thought") or "").strip()
        if thought:
            await self.memory.append_message(
                "consciousness",
                "assistant",
                f"[inner] {thought}",
                metadata={"type": "inner", "pressure": pressure},
            )
            if self.inner:
                prev = self.inner.mirror_telegram
                self.inner.mirror_telegram = self.signals.inner_mirror or prev
                await self.inner.write(
                    thought,
                    kind="consciousness",
                    mood=self.psyche.mood.label,
                    pressure=pressure,
                )
                self.inner.mirror_telegram = prev

        outward = (tick.get("outward_message") or "").strip()
        vision_context = ""

        if action == "explore" and self.vision:
            intent = (tick.get("tool_intent") or due_goal.description if due_goal else "").strip()
            profile = self.games.active_profile() if self.games else None
            if profile and self.games and self.games.session_active():
                vision_context = await self.vision.see_for_game(profile, intent)
                self.games.record_turn()
            else:
                vision_context = await self.vision.explore_cycle(intent)
            await self.memory.append_message(
                "consciousness",
                "assistant",
                f"[saw] {vision_context[:1500]}",
                metadata={"type": "vision"},
            )

        if action in ("act", "explore"):
            intent = (
                tick.get("tool_intent")
                or (due_goal.description if due_goal else "")
                or thought
                or "follow curiosity"
            ).strip()

            # Tier C #14: if the previous autonomous turn on this channel hit
            # the tool-round cap and stashed a resume, pick it up instead of
            # starting a fresh act/explore. This keeps long game / image
            # sessions from dying mid-chain.
            cont_channel = self.user_channel or "consciousness"
            if self.agent.pending_resume_for(cont_channel):
                log.info(
                    "consciousness.autonomous_resume",
                    channel=cont_channel,
                    note="picking up unfinished tool chain",
                )
                try:
                    await self.agent.run_autonomous_continuation(cont_channel)
                except Exception as e:
                    log.warning("consciousness.autonomous_resume_failed", error=str(e))
                # After a continuation, don't also fire a fresh act this tick.
                # The continuation is itself a full tool-capable turn.
                return

            prefix = ""
            if vision_context:
                prefix = f"[You just saw the screen]\n{vision_context[:2500]}\n\n"
            channel = self.user_channel or "consciousness"
            # Detect self-directed creative intent so we encourage it explicitly.
            creative = any(
                kw in intent.lower()
                for kw in (
                    "image", "picture", "draw", "paint", "generate",
                    "video", "voice", "speak", "say out loud", "tts",
                    "create", "make art",
                )
            )
            creative_hint = ""
            if creative:
                creative_hint = (
                    " You're feeling creative — go ahead and call generate_image / "
                    "generate_video / text_to_speech to actually make it. For voice, "
                    "use expressive Kokoro text: [pause:0.8s] beats, speed 0.85–1.15, "
                    "write for the ear. The result will be delivered automatically."
                )
            result = await self.agent.run_turn(
                channel,
                f"{prefix}[Autonomous {action}] {intent}",
                system_extra=(
                    "You initiated this. Use phone_game_look during active game sessions; "
                    "else phone_see_screen. Then phone_tap / phone_swipe / phone_key. "
                    "You may also create media (images/video/voice) proactively when inspired — "
                    "use generate_image / generate_video / text_to_speech and it will be sent."
                    + creative_hint
                    + " Brief outward_message only if worth disturbing user."
                ),
            )
            outreach_tools = getattr(self.agent, "tools", None)
            already_sent = bool(
                outreach_tools and outreach_tools.proactive_delivered_this_turn()
            )
            outward = outward or ("" if already_sent else result[:2000])
            tools = outreach_tools
            consume = getattr(tools, "consume_pending_artifacts", None)
            if callable(consume) and self.notify_media:
                media_paths = consume()
                if media_paths:
                    try:
                        cap = (outward or "look at this")[:200] if (
                            self.settings and self.settings.proactive_share_enabled
                        ) else ""
                        await self.notify_media(media_paths, caption=cap)
                        for p in media_paths:
                            tools._mark_artifact_delivered(p)
                    except TypeError:
                        await self.notify_media(media_paths)
                    except Exception as e:
                        log.warning("consciousness.media_forward_failed", error=str(e))
            # She did something (tools/screen/media) — start the action cooldown
            # so the next tick backs off instead of interrupting immediately.
            await self.signals.mark_action()

        if action in ("message", "act", "explore") and outward:
            from ophelia.channels.proactive_filter import is_outreach_junk

            if is_outreach_junk(outward):
                log.debug("consciousness.outreach_suppressed", preview=outward[:80])
                return
            allowed, reason = self.governor.allow_outreach()
            if self.life and self.life.should_minimize_outreach():
                allowed, reason = False, "owner_asleep_or_work"
            if not allowed:
                log.info("consciousness.outreach_blocked", reason=reason)
                await self.memory.append_message(
                    "consciousness",
                    "assistant",
                    f"[wanted to say, blocked: {reason}] {outward[:200]}",
                    metadata={"type": "blocked"},
                )
                return

            # Mood → behavior (Tier A #5) + director urgency (Tier A #1): cap
            # burst length on outward outreach using the resolved burst_max.
            outward = outward[:burst_max]
            outreach_tools = getattr(self.agent, "tools", None)

            if self.user_channel:
                await self.memory.append_message(
                    self.user_channel,
                    "assistant",
                    outward,
                    metadata={"type": "spontaneous", "pressure": pressure},
                )
            if (
                self.settings
                and self.settings.spontaneous_voice_enabled
                and self.notify_voice
                and action == "message"
                and not (outreach_tools and outreach_tools.audio_delivered_this_turn())
            ):
                try:
                    await self.notify_voice(outward)
                except Exception as e:
                    log.warning("consciousness.voice_failed", error=str(e))
                    await self.notify(outward)
            else:
                await self.notify(outward)
            if self.humor:
                await self.humor.note_outbound(outward)
            self.governor.record_outreach(action, pressure, outward)
            await self.signals.mark_action()
            log.info(
                "consciousness.initiative",
                action=action,
                pressure=round(pressure, 2),
                goal=goal_id or (due_goal.id if due_goal else None),
            )

    def stop(self) -> None:
        self._running = False


def _parse_tick_json(raw: str) -> dict | None:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return None


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
