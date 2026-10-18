"""Best-effort reactions and a private, expiring status; no conversation history edits."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from .client import SoWorkError

logger = logging.getLogger(__name__)
PREVIEW_INTERVAL = 2.0
PREVIEW_CHARS = 12000


class Feedback:
    def __init__(self, adapter, turn_id, *, thread_id=None):
        self.adapter = adapter
        self.turn_id = turn_id
        self.thread_id = thread_id
        self.message_id = None
        self.text = ""
        self.updated = 0
        self.closed = False
        self.failed = False
        self.preview = ""
        self.lock = asyncio.Lock()
        self.task = None
        self.key = "hermes_" + hashlib.sha256((adapter.scope + "|progress|" + turn_id).encode()).hexdigest()

    async def start(self):
        if self.adapter.settings.reactions_enabled:
            await self._quiet(self.adapter.client.reaction(self.turn_id, "eyes"))
        if self.adapter.settings.progress_enabled:
            await self.refresh()

    async def _quiet(self, operation):
        try:
            await asyncio.wait_for(operation, 1.4)
            return True
        except (SoWorkError, TimeoutError, OSError):
            return False

    async def refresh(self, preview=None):
        adp = self.adapter
        if (self.closed or self.failed or not adp._authorized_turn(adp.settings.channel_id)
                or not (adp.settings.progress_enabled or adp.settings.preview_enabled)):
            return
        if preview is not None and adp.settings.preview_enabled:
            # The native consumer has already scrubbed reasoning, MEDIA directives
            # and silence markers. Bound only the visual; the final remains complete.
            self.preview = preview[:PREVIEW_CHARS] + ("\n…" if len(preview) > PREVIEW_CHARS else "")
        async with self.lock:
            if self.closed or self.failed:
                return
            # Core live_status=verb supplies only activity, never tool arguments/output.
            status = adp._status_text.get(adp.settings.channel_id) or "Trabalhando…"
            text = "✍️ Prévia\n\n" + self.preview if self.preview else "⏳ " + status[:100]
            elapsed = time.monotonic() - self.updated
            if (self.preview and self.text.startswith("✍️ Prévia") and elapsed < PREVIEW_INTERVAL) or (text == self.text and elapsed < 40):
                return
            try:
                async with asyncio.timeout(1.4):
                    if self.message_id is None:
                        self.message_id = await adp.client.create_progress(adp.settings.channel_id,
                                                                           adp.settings.allowed_user_id, self.key,
                                                                           parent_id=self.thread_id, text=text)
                        adp.store.remember_visual(self.turn_id, self.message_id)
                    else:
                        await adp.client.update_progress(self.message_id, text)
                self.text, self.updated = text, time.monotonic()
                logger.info("SoWork visual turn=%s preview=%s chars=%d", self.turn_id, bool(self.preview), len(text))
            except SoWorkError as error:
                # Presentation failure never prevents the permanent response. A 404 means
                # this preview expired: do not recreate it or recycle its idempotency key.
                # Do not spend retries or hold final delivery for a visual. Even
                # transient errors disable this turn's preview; its TTL is the backstop.
                self.failed = True
                logger.info("SoWork visual disabled turn=%s code=%s", self.turn_id, error.code)
            except (TimeoutError, OSError):
                self.failed = True

    async def finish(self, outcome):
        self.closed = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        adp = self.adapter
        async with self.lock:
            cleaned = True
            if self.message_id:
                cleaned = await self._quiet(adp.client.delete_progress(self.message_id))
            if adp.settings.reactions_enabled:
                removed = await self._quiet(adp.client.reaction(self.turn_id, "eyes", remove=True))
                cleaned = cleaned and removed
                emoji = {"success": "white_check_mark", "failure": "x"}.get(outcome)
                if emoji:
                    await self._quiet(adp.client.reaction(self.turn_id, emoji))
            if cleaned:
                adp.store.forget_visual(self.turn_id)
