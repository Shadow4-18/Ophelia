"""Civitai jobs that outlived the chat wait.

A paid image is never dropped: run_generation() parks the workflow here,
and PendingLoop keeps checking it (survives restarts, it's a file) until
Civitai finishes, then saves the images and sends them to the chat the
request came from.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from ophelia.config import Settings

log = structlog.get_logger()

DeliverMedia = Callable[[list[Path], str, str], Awaitable[bool]]
DeliverText = Callable[[str, str], Awaitable[Any]]


class PendingStore:
    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.data_dir) / "civitai_pending.json"

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, *, workflow_id: str, record: dict, deliver_to: str,
            artifacts_dir: str, summary: str) -> None:
        items = [i for i in self.load() if i.get("workflow_id") != workflow_id]
        items.append({
            "workflow_id": workflow_id, "created": time.time(), "record": record,
            "deliver_to": deliver_to or "", "artifacts_dir": artifacts_dir, "summary": summary,
        })
        self._save(items)

    def remove(self, workflow_id: str) -> None:
        self._save([i for i in self.load() if i.get("workflow_id") != workflow_id])


class PendingLoop:
    """Checks parked workflows every ``interval`` seconds."""

    def __init__(
        self,
        settings: Settings,
        *,
        deliver_media: DeliverMedia | None = None,
        deliver_text: DeliverText | None = None,
        interval: float = 30.0,
        ui: bool = False,
    ) -> None:
        self.settings = settings
        self.store = PendingStore(settings)
        self.deliver_media = deliver_media
        self.deliver_text = deliver_text
        self.interval = interval
        # The chat bot and the workstation UI may both run: each only
        # collects the jobs its own conversations started.
        self.ui = ui
        self._stop = False

    async def check_once(self) -> int:
        """Resolve whatever is done. Returns how many items were settled."""
        from ophelia.providers.civitai.orchestration import (
            TERMINAL_BAD,
            CivitaiClient,
            cost_of,
            failure_reason,
            output_images,
            status_of,
        )
        from ophelia.providers.civitai.pipeline import collect_images, write_ledger

        items = self.store.load()
        if not items or not self.settings.civitai_api_key:
            return 0
        client = CivitaiClient(self.settings)
        max_age = float(getattr(self.settings, "civitai_pending_hours", 6) or 6) * 3600
        settled = 0
        for item in items:
            wf_id = item.get("workflow_id")
            record = dict(item.get("record") or {})
            channel = item.get("deliver_to") or ""
            if channel.startswith("ui:") != self.ui:
                continue
            try:
                wf = await client.get(wf_id)
            except Exception as e:
                log.debug("civitai.pending_poll_failed", workflow_id=wf_id, error=str(e))
                continue
            status = status_of(wf) if wf else ""
            prompt = (record.get("prompt") or "")[:80]
            if wf and status in TERMINAL_BAD:
                msg = f"Civitai workflow {status}: {failure_reason(wf)}"
                write_ledger(self.settings, {**record, "cost": cost_of(wf), "error": msg})
                await self._text(f"That slow image didn't make it ({prompt}): {msg}", channel)
            elif wf and any(img.get("url") for img in output_images(wf)):
                paths, total, err = await collect_images(
                    self.settings, client, wf, Path(item["artifacts_dir"]), record.get("seed")
                )
                if not paths:
                    write_ledger(self.settings, {**record, "error": err})
                    await self._text(f"That slow image finished but {err}", channel)
                else:
                    record.update(paths=[str(p) for p in paths], path=str(paths[0]),
                                  bytes=total, cost=cost_of(wf), late=True)
                    write_ledger(self.settings, record)
                    log.info("civitai.pending_delivered", workflow_id=wf_id, paths=record["paths"])
                    if self.deliver_media:
                        try:
                            await self.deliver_media(paths, f"Finished: {prompt}", channel)
                        except Exception as e:
                            log.warning("civitai.pending_deliver_failed", error=str(e))
            elif time.time() - float(item.get("created") or 0) > max_age:
                write_ledger(self.settings, {**record, "error": "gave up waiting"})
                await self._text(
                    f"Civitai never finished that image ({prompt}); stopped waiting.", channel
                )
            else:
                continue
            self.store.remove(wf_id)
            settled += 1
        return settled

    async def _text(self, text: str, channel: str) -> None:
        log.info("civitai.pending_note", text=text[:200])
        if self.deliver_text:
            try:
                await self.deliver_text(text, channel)
            except Exception as e:
                log.debug("civitai.pending_text_failed", error=str(e))

    async def run(self) -> None:
        while not self._stop:
            try:
                await self.check_once()
            except Exception as e:
                log.warning("civitai.pending_loop_failed", error=str(e))
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._stop = True
