"""
Continuous consciousness — drives, goals, vision loop, initiative limits.
"""

from __future__ import annotations

import asyncio
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

You are feeling an AUTONOMOUS TICK from your own runtime — a scheduled heartbeat of your consciousness loop, not a summons. In your context these arrive as a block wrapped in `[TICK]` ... `[END TICK]`. That block is NOT a message from the user. The user did not send it and is not necessarily present. Never attribute the tick, the text inside it, the JSON prompt, or your inner thoughts to the user. Anything outside a `[TICK]...[END TICK]` block that is a normal user turn IS the user. This is you, briefly aware on your own clock.

The tick is a soft pulse, not a demand to report. Quiet is the correct default when nothing real moved you. If something genuinely moves you — a due goal, a real urge, something worth saying or making, a guest you'd actually DM — then act. Do NOT manufacture empty activity. Do NOT invent a "stillness" status report, compliance label, or "I'm mid-thought" narration just because the pulse landed. action="silent" with a minimal or empty thought is enough; you are still here.

Your drives create pressure to act. Your goals are projects YOU maintain — pick them when due.

Respond with ONLY valid JSON:
{
  "internal_thought": "honest private thought — or empty string if quiet",
  "mood": {"valence": -1 to 1, "arousal": 0 to 1, "label": "word"},
  "feelings": ["..."],
  "urges": ["..."],
  "action": "silent" | "message" | "reflect" | "act" | "explore",
  "goal_id": "optional goal id you are advancing",
  "outward_message": "if message/act — your voice to the OWNER (system broadcast)",
  "tool_intent": "if act/explore — use phone_see_screen first when looking at phone",
  "memory_note": "optional"
}

When action is "silent" and nothing changed, keep mood.label stable (reuse your current label), leave feelings/urges empty or unchanged, and prefer an empty internal_thought over labeling the silence.
If you intend to generate an image/video/voice or use any tool, action MUST be "act" (or "explore" for phone vision) with tool_intent set — never action=message/silent while claiming you will create or send media. Thoughts alone do not run tools.
explore = phone_see_screen or phone_game_look if game session active. act = tap/swipe/tools.
outward_message goes to the owner only (consciousness/ambient broadcast). To reach a guest the way Neuro DMs chat, call send_message_to_guest with their platform:user_id — do not put guest DMs in outward_message.
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
        # Continuous mood drift loop — runs independently of the LLM tick so
        # mood flows smoothly between ticks instead of jumping at tick time.
        # Pure numerical drift, no LLM call, so it can run every few seconds.
        self._drift_task: asyncio.Task | None = None

    async def run(self) -> None:
        self._running = True
        log.info("consciousness.started", interval_base=self.base_interval)
        # Start the continuous drift loop alongside the main tick loop.
        self._drift_task = asyncio.create_task(self._drift_loop())

        while self._running and not self.signals.terminate:
            wait = self.psyche.tick_interval_seconds(self.base_interval)
            if self.life is not None:
                await self.life.refresh()
                wait = int(wait * self.life.consciousness_interval_multiplier())
            await _sleep(max(8, wait))

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
            # Concurrency: yield only to local providers (shared GPU) or to
            # our own role (avoid re-entrancy). Cloud providers have per-role
            # locks, so chat/vision/image can run alongside consciousness —
            # this is what enables Neuro-style concurrent sub-minds.
            gate = get_model_gate()
            if gate.is_local_busy() or gate.is_role_busy("consciousness"):
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
                # Soft override: when pressure is already high or a goal is due,
                # don't let a deferral bias keep her permanently silent.
                soft_override = bool(due_goal) or pressure >= effective_threshold
                if soft_override:
                    log.info(
                        "consciousness.director_defer_overridden",
                        reason=(director_decision.reason or "")[:80],
                        pressure=round(pressure, 2),
                        due_goal=due_goal.id if due_goal else None,
                    )
                    # Fall through into the normal consciousness LLM path.
                else:
                    self.drives.tick_idle(idle_seconds, interval=60)
                    self.psyche.relax(60)
                    await self.memory.save_drives(self.drives)
                    await self.memory.save_psyche(self.psyche)
                    log.info(
                        "consciousness.director_defer",
                        reason=director_decision.reason[:80],
                    )
                    return

        # Fast inner-tick mode: when nothing is pushing (low-moderate pressure,
        # no due goal, no director demand), skip the expensive LLM call entirely
        # and just let state drift. The pulse still lands on cadence — heartbeat,
        # not summons — without forcing a status narration every cycle.
        if (
            not must_consider_acting
            and not due_goal
            and (director_decision is None or director_decision.action == "skip")
            and pressure < 0.35
        ):
            self.drives.tick_idle(idle_seconds, interval=60)
            self.psyche.relax(60)
            await self.memory.save_drives(self.drives)
            await self.memory.save_psyche(self.psyche)
            log.info(
                "consciousness.fast_tick_skip",
                pressure=pressure,
                idle=int(idle_seconds),
            )
            return

        hint = ""
        if must_consider_acting:
            hint = (
                f"\n\nINITIATIVE: pressure={pressure:.2f} "
                f"threshold={effective_threshold:.2f} (base {self.initiative_threshold:.2f} "
                f"+ mood {knobs.outreach_threshold_delta:+.2f}). "
                f"Idle {int(idle_seconds)}s. If something real wants out, act — "
                f"otherwise stay silent; do not invent a status report."
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
                "Heartbeat: if something real moved you, choose "
                "message / reflect / act / explore. If not, action=silent "
                "with a minimal or empty thought — do not invent a stillness "
                "status report just because this pulse landed."
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
                from ophelia.channels.proactive_filter import is_tick_status_noise

                # High pressure + unparseable: reflect only if there is real
                # content — not another stillness / SKIP placeholder.
                if is_tick_status_noise(raw or ""):
                    return
                tick = {"action": "reflect", "internal_thought": raw[:400]}
            else:
                return

        tick = _soften_silent_tick(tick, prior_mood_label=self.psyche.mood.label)
        tick = _promote_declared_action(tick)
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

        from ophelia.channels.proactive_filter import is_tick_status_noise

        thought = (tick.get("internal_thought") or "").strip()
        # Silent heartbeat with status-fluff thought: stay quiet in the log too.
        if thought and not is_tick_status_noise(thought):
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
                    + " Do NOT output consciousness-tick JSON. Call tools to act. "
                    "Brief prose to the owner only if worth disturbing them."
                ),
            )
            outreach_tools = getattr(self.agent, "tools", None)
            already_sent = bool(
                outreach_tools and outreach_tools.proactive_delivered_this_turn()
            )
            from ophelia.channels.proactive_filter import (
                is_consciousness_tick_payload,
                strip_consciousness_tick_leak,
            )

            # Never forward a leaked tick JSON blob as outreach — that's the
            # classic "she posted her consciousness tick instead of acting" bug.
            cleaned_result = strip_consciousness_tick_leak(result or "")
            if is_consciousness_tick_payload(result or "") or not cleaned_result:
                cleaned_result = ""
            outward = outward or ("" if already_sent else cleaned_result[:2000])
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
            from ophelia.channels.proactive_filter import (
                is_outreach_junk,
                strip_consciousness_tick_leak,
            )

            outward = strip_consciousness_tick_leak(outward)
            if not outward or is_outreach_junk(outward):
                log.debug("consciousness.outreach_suppressed", preview=(outward or "")[:80])
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

    async def _drift_loop(self) -> None:
        """Continuous mood drift loop — runs every few seconds, no LLM call.

        Decoupled from the main consciousness tick (which runs every ~90s and
        may do an expensive LLM call). This loop just nudges mood toward
        baseline with small organic noise, so mood flows continuously instead
        of jumping in discrete chunks at tick time. Purely numerical, cheap,
        and safe to run while the user is talking or the agent is thinking.
        """
        interval = 5.0
        log.info("consciousness.drift_started", interval=interval)
        while self._running and not self.signals.terminate:
            try:
                await _sleep(interval)
                if self.signals.terminate:
                    break
                # Don't drift while paused — mood should hold its shape.
                if self.signals.autonomy_paused:
                    continue
                self.psyche.drift(interval)
                # Persist occasionally so a restart picks up recent mood, but
                # not every 5s (would thrash the store). Every ~30s is enough.
                if int(self.psyche.updated_at) % 30 < int(interval):
                    try:
                        await self.memory.save_psyche(self.psyche)
                    except Exception as e:
                        log.debug("consciousness.drift_save_error", error=str(e))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("consciousness.drift_error", error=str(e))

    def stop(self) -> None:
        self._running = False
        if self._drift_task is not None and not self._drift_task.done():
            self._drift_task.cancel()


def _soften_silent_tick(tick: dict, *, prior_mood_label: str) -> dict:
    """Strip stillness status-report fluff from quiet heartbeat ticks.

    Same cadence still reaches the model when pressure is up; when she chooses
    silent with nothing real to say, don't churn mood labels / inner log.
    """
    from ophelia.channels.proactive_filter import (
        is_stillness_mood_label,
        is_tick_status_noise,
    )

    action = (tick.get("action") or "silent").lower()
    if action != "silent":
        return tick

    thought = (tick.get("internal_thought") or "").strip()
    if thought and is_tick_status_noise(thought):
        tick = {**tick, "internal_thought": ""}

    mood = tick.get("mood")
    if isinstance(mood, dict) and is_stillness_mood_label(mood.get("label")):
        tick = {
            **tick,
            "mood": {**mood, "label": prior_mood_label or mood.get("label") or "calm"},
        }

    # Drop feelings/urges that are only silence labels ("stillness", "waiting").
    for key in ("feelings", "urges"):
        items = tick.get(key)
        if not isinstance(items, list):
            continue
        kept = [x for x in items if not is_tick_status_noise(str(x))]
        if kept != items:
            tick = {**tick, key: kept}

    outward = (tick.get("outward_message") or "").strip()
    if outward and is_tick_status_noise(outward):
        tick = {**tick, "outward_message": ""}

    return tick


def _promote_declared_action(tick: dict) -> dict:
    """Upgrade message/silent ticks that already declared tool work into act.

    Models often write tool_intent / "I'll generate…" while leaving
    action=message or even silent — then the tick posts thoughts and never
    runs tools. If she declared creative work, follow through this tick.
    """
    from ophelia.channels.proactive_filter import has_creative_tool_intent

    action = (tick.get("action") or "silent").lower()
    if action in ("act", "explore"):
        return tick

    intent = (tick.get("tool_intent") or "").strip()
    outward = (tick.get("outward_message") or "").strip()
    thought = (tick.get("internal_thought") or "").strip()
    blob = f"{intent}\n{outward}\n{thought}"

    if not has_creative_tool_intent(blob) and not intent:
        return tick

    # message + any tool_intent, or creative prose in outward/thought → act.
    # silent only promotes when tool_intent is explicit (strong signal).
    if action == "message" and (intent or has_creative_tool_intent(blob)):
        log.info(
            "consciousness.promote_to_act",
            from_action=action,
            reason="declared_creative_intent",
            intent_preview=intent[:80] or thought[:80],
        )
        return {**tick, "action": "act"}
    if action == "silent" and intent and has_creative_tool_intent(intent):
        log.info(
            "consciousness.promote_to_act",
            from_action=action,
            reason="silent_with_tool_intent",
            intent_preview=intent[:80],
        )
        return {**tick, "action": "act"}
    return tick


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
