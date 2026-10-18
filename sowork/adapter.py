"""Private SoWork DMs through the native Hermes gateway lifecycle."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import sys
import time

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome, SendResult
from gateway.session import SessionSource
from hermes_constants import get_hermes_home

from .client import Client, DM_EVENT, THREAD_EVENT, SoWorkError
from .config import Credentials, Settings
from .lock import SubscriptionLock
from .feedback import Feedback
from .reaction_probe import ReactionProbe, command as reaction_command
from .media import read_outbound, receive_media, valid_attachments
from .state import ReconciliationRequired, Store
from . import proactive, threads

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    message_id: str
    sequence: int = 0
    active: bool = True
    thread_id: str | None = None


_turn: ContextVar[Turn | None] = ContextVar("sowork_turn", default=None)


class SoWorkAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 19000
    trusted_inbound = False
    supports_status_text = True
    splits_long_messages = True
    # Native consumer's cumulative-draft contract keeps one preview across tool
    # boundaries. This draft never counts as durable delivery: only send() does.
    draft_stream_is_message = True

    def __init__(self, config, *, host, client=None, store=None):
        super().__init__(config, Platform("sowork"))
        self.host = host
        self.settings = Settings.from_extra(config.extra)
        self._home = get_hermes_home().resolve()
        self.credentials = client.credentials if client else Credentials.from_scope(self.settings.credential_env)
        self.client = client
        self.store = store
        self._receiver = None
        self._outbox_lock = asyncio.Lock()
        self._start_at = 0
        self._lock_acquired = False
        self._subscription_lock = SubscriptionLock(self.scope)
        self._feedback = {}
        self._admission_lock = asyncio.Lock()
        self._admissions = {}
        self._admission_keys = {}
        self._thread_dm_verified = False
        self._thread_context_seen = set()
        self._reaction_probe = ReactionProbe(self)

    @property
    def scope(self):
        return self.credentials.installation_id + ":" + self.settings.subscription_id

    @property
    def supports_async_delivery(self):
        # Native cron origin capture uses this surface capability, not a process env flag.
        return proactive.enabled(self.config, self.settings)

    async def get_chat_info(self, chat_id):
        if chat_id != self.settings.channel_id:
            return {"id": chat_id, "type": "unknown"}
        return {"id": chat_id, "name": "SoWork private DM", "type": "dm"}

    async def connect(self, *, is_reconnect=False):
        if self._receiver is not None:
            await self.disconnect()
        # No --replace/takeover: this experiment must never terminate a peer.
        self._platform_lock_takeover_allowed = False
        if not self._subscription_lock.acquire():
            self._set_fatal_error("sowork_lock", "Subscription already has a local consumer", retryable=False)
            return False
        if not self._acquire_platform_lock("sowork", self.scope, "SoWork subscription"):
            self._subscription_lock.release()
            return False
        self._lock_acquired = True
        try:
            if self.client is None:
                self.client = Client(self.credentials)
            await self.client.verify_identity(workspace_id=self.settings.workspace_id, app_user_id=self.settings.app_user_id)
            if self.settings.reaction_probe_enabled and "chat.history" not in self.client.identity["installation"]["consentedScopes"]:
                raise SoWorkError(403, "reaction_probe_scope_missing")
            if self.settings.reaction_probe_enabled:
                await threads.verify_dm(self)
            if self.settings.threads_enabled:
                required = {"chat.history", "events.subscribe:" + THREAD_EVENT}
                if not required.issubset(set(self.client.identity["installation"]["consentedScopes"])):
                    raise SoWorkError(403, "thread_scopes_missing")
                if not threads.enabled(self):
                    raise SoWorkError(403, "thread_allowlist_mismatch")
                await threads.verify_dm(self)
            subscriptions = await self.client.subscriptions()
            sub = next((s for s in subscriptions if s["id"] == self.settings.subscription_id), None)
            if not sub or sub.get("url") or sub.get("disabledAt") or sub.get("officeId") != self.settings.workspace_id or sorted(sub.get("events", [])) != sorted(threads.events(self.settings)):
                raise SoWorkError(403, "pilot_subscription_mismatch")
            if sub.get("creator", {}).get("id") != self.settings.app_user_id:
                raise SoWorkError(403, "subscription_owner_mismatch")
            if self.store is None:
                name = hashlib.sha256(self.scope.encode()).hexdigest()[:24] + ".sqlite3"
                self.store = Store(get_hermes_home() / "sowork" / name, self.scope)
            self.store.check_retention()
            self.store.recover_claims()
            self._reaction_probe.recover()
            self._start_at = float(self.store.get_meta("accept_after") or int(time.time()))
            with self.store.db:
                self.store._set_meta("accept_after", self._start_at)
            self._mark_connected()
            self._receiver = asyncio.create_task(self._receive(), name="sowork-inbox")
            return True
        except (SoWorkError, ValueError, ReconciliationRequired) as error:
            self._set_fatal_error("sowork_connect", str(error), retryable=False)
            await self.disconnect()
            return False
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self):
        self._mark_disconnected()
        task, self._receiver = self._receiver, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._reaction_probe.close()
        await self._cancel_admissions()
        await self.cancel_background_tasks()
        for feedback in list(self._feedback.values()):
            await feedback.finish("cancelled")
        self._feedback.clear()
        if self.client:
            await self.client.close()
            self.client = None
        if self.store:
            self.store.close()
            self.store = None
        if self._lock_acquired:
            self._release_platform_lock()
            self._lock_acquired = False
        self._subscription_lock.release()

    def verdict(self, event):
        if event.get("installationId") != self.credentials.installation_id:
            return "wrong_installation"
        if event.get("officeId") != self.settings.workspace_id:
            return "wrong_workspace"
        if event.get("type") not in threads.events(self.settings):
            return "unsupported_event"
        data = event.get("data")
        if not isinstance(data, dict):
            return "malformed"
        message = data.get("message", {})
        if not isinstance(message, dict):
            return "malformed"
        channel = message.get("channel", {})
        sender = message.get("sender", {})
        if not isinstance(channel, dict) or not isinstance(sender, dict):
            return "malformed"
        if channel.get("type") != "dm":
            return "not_individual_dm"
        if event["type"] == DM_EVENT and (data.get("conversationType") != "direct" or data.get("memberCount") != 2):
            return "not_individual_dm"
        if event["type"] == THREAD_EVENT and not isinstance(data.get("channel"), dict):
            return "malformed"
        if event["type"] == THREAD_EVENT and data["channel"].get("type") != "dm":
            return "not_individual_dm"
        envelope_channel = data.get("channelId") if event["type"] == DM_EVENT else data["channel"].get("id")
        if channel.get("id") != self.settings.channel_id or envelope_channel != channel.get("id"):
            return "wrong_channel"
        if sender.get("id") == self.settings.app_user_id:
            return "self_message"
        if sender.get("id") != self.settings.allowed_user_id:
            return "unbound_sender"
        try:
            root = threads.root_id(event)
        except (ValueError, TypeError, AttributeError):
            return "malformed"
        if root and not threads.enabled(self):
            return "unsupported_content"
        attachments = message.get("attachments", [])
        if not valid_attachments(attachments):
            return "malformed"
        if attachments and not self.settings.media_enabled:
            return "unsupported_content"
        if not isinstance(message.get("text", ""), str) or not (message.get("text", "").strip() or attachments) or not message.get("id"):
            return "malformed"
        stamp = event.get("createdAt")
        if not isinstance(stamp, (int, float)) or stamp > time.time() + 60:
            return "malformed"
        if stamp < self._start_at or time.time() - stamp > self.settings.max_event_age_seconds:
            return "stale_requires_review"
        return "accepted"

    def normalize(self, event):
        message = event["data"]["message"]
        source = SessionSource(
            platform=self.platform, chat_id=message["channel"]["id"], chat_type="dm",
            user_id=message["sender"]["id"], message_id=message["id"],
            scope_id=self.settings.workspace_id,
            thread_id=threads.root_id(event),
        )
        normalized = MessageEvent(
            text=message.get("text", ""), source=source, user_id=source.user_id,
            raw_message=message,
            message_type=(MessageType.DOCUMENT if message.get("attachments") else MessageType.TEXT),
            message_id=message["id"], timestamp=datetime.fromtimestamp(event["createdAt"], timezone.utc),
            internal=False, allow_gateway_control=True,
        )
        if self.settings.media_enabled and not normalized.is_command():
            # Runtime capabilities ride the new turn's native context. Do not
            # rewrite a conversation's cached prompt or old text-only replies.
            normalized.channel_context = (
                "[SoWork gateway transport: current capabilities]\n"
                "Native file attachments are enabled in this DM. To send a local file, "
                "include MEDIA:/absolute/path/to/file in your final response, replacing "
                "the example path with the real file path. The gateway uploads the file "
                "with the app's identity into this same DM; no MCP upload tool or public link is needed. "
                f"File size must be between 10 and {self.settings.max_attachment_bytes} bytes. "
                "Earlier text-only descriptions of this bridge are outdated."
            )
        if proactive.enabled(self.config, self.settings) and not normalized.is_command():
            normalized.channel_context = (normalized.channel_context or "") + (
                "\nNative Hermes cron delivery is enabled for this exact private DM. "
                "Use the normal cronjob tool for reminders and scheduled results, with deliver='origin' "
                "(the default when created here), or deliver='sowork' for the pinned home DM. "
                "Put the result and any MEDIA paths in the job's final response; the scheduler delivers them. "
                "Other SoWork recipients and groups remain disabled."
            )
        if self.settings.threads_enabled and not normalized.is_command():
            normalized.channel_context = (normalized.channel_context or "") + (
                "\nReplies and files stay in the current SoWork thread when this message belongs to one. "
                "Each thread has its own Hermes conversation, separate from the main DM and other threads."
            )
        return normalized

    async def ingest(self, log_id, event):
        verdict = self.verdict(event)
        self.store.ingest(log_id, event, verdict)
        logger.info("SoWork inbound event=%s cursor=%s verdict=%s", event.get("id"), log_id, verdict)
        await self._dispatch_queued()

    async def _dispatch_queued(self):
        if not self._message_handler:
            return
        for raw in self.store.queued():
            if self.verdict(raw) != "accepted":
                self.store.finish(raw["data"]["message"]["id"], "rejected")
                continue
            event = self.normalize(raw)
            if self._is_sender_authorized(event.source.user_id, "dm", event.source.chat_id) is not True:
                self.store.finish(event.message_id, "rejected")
                continue
            if not self.store.claim(event.message_id):
                continue
            # handle_message preserves native session queues, textual controls,
            # interruptions and the real GatewayRunner's second ACL check.
            if event.is_command():
                # Controls bypass file downloads as well as both native busy guards.
                if event.get_command() == "stop":
                    await self._cancel_admissions(self._event_session_key(event))
                token = _turn.set(Turn(event.message_id, thread_id=event.source.thread_id))
                try:
                    await threads.hydrate(self, event)
                    if reaction_command(event.text):
                        try:
                            await self._reaction_probe.handle(event)
                        finally:
                            self.store.finish(event.message_id, "complete")
                            _turn.get().active = False
                    else:
                        await self.handle_message(event)
                except SoWorkError:
                    self.store.finish(event.message_id, "rejected")
                finally:
                    _turn.reset(token)
            else:
                self._admission_keys[event.message_id] = self._event_session_key(event)
                self._admissions[event.message_id] = asyncio.create_task(self._admit_event(event))

    async def _cancel_admissions(self, session_key=None):
        pending = [(mid, task) for mid, task in self._admissions.items()
                   if session_key is None or self._admission_keys.get(mid) == session_key]
        for _, task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        # A task cancelled before its first instruction never executes its finally block.
        for message_id, _ in pending:
            self._admissions.pop(message_id, None)
            self._admission_keys.pop(message_id, None)
            self.store.finish(message_id, "cancelled")

    async def _admit_event(self, event):
        token = _turn.set(Turn(event.message_id, thread_id=event.source.thread_id))
        try:
            # Hydrate media BEFORE native pending-event merging, while the SSE receiver
            # stays free to receive /stop and approvals during a slow download.
            async with self._admission_lock:
                if not self._authorized_turn(event.source.chat_id):
                    self.store.finish(event.message_id, "rejected")
                    return
                await threads.hydrate(self, event)
                # Transport bookkeeping is process-local. Native Hermes admission
                # accepts a closed metadata schema; never smuggle flags into it.
                event._sowork_thread_ready = True
                if self.settings.media_enabled:
                    await receive_media(self, event)
                event._sowork_media_ready = True
                event._sowork_input_ids = [event.message_id]
                key = self._event_session_key(event)
                pending = self._pending_messages.get(key)
                await self.handle_message(event)
                if pending is not None and self._pending_messages.get(key) is pending:
                    pending._sowork_input_ids = [*getattr(pending, "_sowork_input_ids", [pending.message_id]), event.message_id]
        except asyncio.CancelledError:
            self.store.finish(event.message_id, "cancelled")
            raise
        except Exception as error:
            self.store.finish(event.message_id, "failed")
            logger.warning("SoWork admission failed message=%s error=%s", event.message_id, type(error).__name__)
        finally:
            self._admissions.pop(event.message_id, None)
            self._admission_keys.pop(event.message_id, None)
            _turn.reset(token)

    async def on_processing_start(self, event):
        # Queued follow-ups may be scheduled from the previous task's context.
        _turn.set(Turn(event.message_id, thread_id=event.source.thread_id))
        if not self._authorized_turn(event.source.chat_id):
            return
        if not getattr(event, "_sowork_thread_ready", False):
            await threads.hydrate(self, event)
        if self.settings.reactions_enabled or self.settings.progress_enabled or self.settings.preview_enabled:
            self.store.begin_visual(event.message_id)
            feedback = Feedback(self, event.message_id, thread_id=event.source.thread_id)
            self._feedback[event.message_id] = feedback
            feedback.task = asyncio.create_task(feedback.start())
        if self.settings.media_enabled and not getattr(event, "_sowork_media_ready", False):
            await receive_media(self, event)

    async def _dispatch_active_session_command(self, event, session_key, cmd):
        # Native busy-session controls bypass the background processing hooks.
        try:
            await super()._dispatch_active_session_command(event, session_key, cmd)
        except BaseException:
            self.store.finish(event.message_id, "failed")
            raise
        else:
            self.store.finish(event.message_id, "complete")
        finally:
            turn = _turn.get()
            if turn and turn.message_id == event.message_id:
                turn.active = False

    async def on_processing_complete(self, event, outcome):
        status = {ProcessingOutcome.SUCCESS: "complete", ProcessingOutcome.FAILURE: "failed", ProcessingOutcome.CANCELLED: "cancelled"}[outcome]
        for message_id in getattr(event, "_sowork_input_ids", [event.message_id]):
            self.store.finish(message_id, status)
        turn = _turn.get()
        if turn and turn.message_id == event.message_id:
            turn.active = False
        feedback = self._feedback.pop(event.message_id, None)
        if feedback:
            await feedback.finish(outcome.value)
        logger.info("SoWork turn message=%s outcome=%s", event.message_id, status)

    async def _receive(self):
        failures = 0
        initialized = False
        while self._running:
            try:
                if not initialized:
                    # Native startup publishes adapters only AFTER connect() returns.
                    # Its common ACL resolves extra.allow_from through that registry.
                    # Wait for our own publication before any inbox or outbox work;
                    # a false startup ACL verdict must not discard queued messages.
                    runner = getattr(self, "gateway_runner", None)
                    if runner is None:
                        raise SoWorkError(403, "gateway_runner_missing")
                    while self._running and runner._authorization_adapter(
                        self.platform, getattr(self, "_owner_profile", None)
                    ) is not self:
                        await asyncio.sleep(.05)
                    if not self._running:
                        return
                    if self._is_sender_authorized(self.settings.allowed_user_id, "dm", self.settings.channel_id) is not True:
                        raise SoWorkError(403, "gateway_acl_denied")
                    await self._recover_visuals()
                    await self._recover_outbox()
                    await self._dispatch_queued()
                    initialized = True
                self.store.check_retention()
                while True:
                    page = await self.client.poll(self.settings.subscription_id, self.store.cursor)
                    for entry in page["items"]:
                        await self.ingest(entry["logId"], entry["event"])
                    self.store.observed()
                    if not page["hasMore"]:
                        break
                    if not page["items"]:
                        raise SoWorkError(502, "invalid_empty_page")
                async for frame in self.client.stream_once(self.settings.subscription_id, self.store.cursor):
                    if frame.event is not None:
                        await self.ingest(frame.log_id, frame.event)
                    else:
                        self.store.observed()
                    failures = 0
                await asyncio.sleep(self.settings.reconnect_seconds)
            except asyncio.CancelledError:
                raise
            except SoWorkError as error:
                failures += 1
                if (not error.retryable and error.status != 401) or (error.status == 401 and failures >= 3):
                    self._set_fatal_error("sowork_receive", str(error), retryable=False)
                    self._mark_disconnected()
                    await self._notify_fatal_error()
                    return
                await asyncio.sleep(error.retry_after or min(2 ** min(failures, 5), 30))
            except (ReconciliationRequired, ValueError, KeyError, TypeError, AttributeError):
                self._set_fatal_error("sowork_receive", "Inbox requires operator reconciliation", retryable=False)
                self._mark_disconnected()
                await self._notify_fatal_error()
                return

    async def _deliver_row(self, row):
        if row["status"] == "sent":
            return json.loads(row["receipt"])
        if row["channel_id"] != self.settings.channel_id:
            raise SoWorkError(403, "outbox_destination_mismatch")
        if row["turn_id"].startswith("proactive_") and not proactive.enabled(self.config, self.settings):
            raise SoWorkError(403, "proactive_delivery_disabled")
        if time.time() - row["created"] >= 3600:
            raise SoWorkError(409, "outbox_reconciliation_required")
        if self._is_sender_authorized(self.settings.allowed_user_id, "dm", row["channel_id"]) is not True:
            raise SoWorkError(403, "gateway_acl_denied")
        body = json.loads(row["body"])
        if body.get("parentId") and not threads.known(self, body["parentId"]):
            raise SoWorkError(403, "thread_destination_unverified")
        receipt = await self.client.send(row["channel_id"], body, row["idempotency_key"])
        if (not receipt.get("id") or receipt.get("channel", {}).get("id") != row["channel_id"]
                or receipt.get("sender", {}).get("id") != self.settings.app_user_id):
            raise SoWorkError(502, "send_receipt_mismatch")
        if body.get("parentId") and receipt.get("parentId") != body["parentId"]:
            raise SoWorkError(502, "send_thread_receipt_mismatch")
        safe_receipt = {"id": receipt["id"], "channel_id": row["channel_id"], "sender_id": self.settings.app_user_id, "created_at": receipt.get("createdAt")}
        self.store.sent(row["logical_id"], safe_receipt)
        logger.info("SoWork sent logical=%s message=%s", row["logical_id"], receipt["id"])
        return safe_receipt

    async def _recover_outbox(self):
        for row in self.store.pending_sends():
            if time.time() - row["created"] >= 3600:
                raise ReconciliationRequired("Old pending send requires readback and operator review")
            await self._deliver_row(row)

    async def _recover_visuals(self):
        for row in self.store.unfinished_visuals():
            try:
                await asyncio.wait_for(self.client.reaction(row["turn_id"], "eyes", remove=True), 1.4)
                if row["message_id"]:
                    await asyncio.wait_for(self.client.delete_progress(row["message_id"]), 1.4)
            except (SoWorkError, TimeoutError, OSError):
                continue  # Expiring UI is never a reason to block conversation recovery.
            self.store.forget_visual(row["turn_id"])

    def _authorized_turn(self, chat_id):
        turn = _turn.get()
        return bool(self.store and self.client and turn and turn.active
                    and chat_id == self.settings.channel_id
                    and self._is_sender_authorized(self.settings.allowed_user_id, "dm", chat_id) is True)

    def supports_draft_streaming(self, chat_type=None, metadata=None, chat_id=None):
        turn = _turn.get()
        return bool(self.settings.preview_enabled and chat_type in (None, "", "dm")
                    and self._authorized_turn(chat_id)
                    and (metadata or {}).get("thread_id", turn.thread_id) == turn.thread_id)

    def streaming_overflow_limit(self):
        # Disable consumer-side overflow sends: the adapter splits the complete
        # final itself. The ephemeral visual is bounded independently in Feedback.
        return sys.maxsize if self.settings.preview_enabled else None

    async def send_draft(self, chat_id, draft_id, content, metadata=None):
        if not self.supports_draft_streaming("dm", metadata, chat_id):
            return SendResult(False, error="Preview destination is not authorized", error_kind="forbidden")
        turn = _turn.get()
        feedback = self._feedback.get(turn.message_id)
        if feedback and isinstance(content, str) and content.strip():
            try:
                await asyncio.wait_for(feedback.refresh(content), 1.4)
            except (TimeoutError, OSError):
                feedback.failed = True
        # Success here ACKs only a disposable draft, with NO message_id. Returning
        # failure would make the core fall back to permanent partial sends. Visual
        # failures instead stay silent; the normal final/outbox path still runs.
        return SendResult(True)

    async def abandon_open_draft(self, chat_id, content="", metadata=None):
        turn = _turn.get()
        feedback = self._feedback.get(turn.message_id) if turn else None
        if self._authorized_turn(chat_id) and feedback:
            await feedback.finish("cancelled")

    async def send_typing(self, chat_id, metadata=None):
        # The native typing-refresh lifecycle renders an expiring private status.
        # No removed SoWork typing endpoint is called.
        turn = _turn.get()
        feedback = self._feedback.get(turn.message_id) if turn else None
        if self._authorized_turn(chat_id) and feedback:
            await feedback.refresh()

    async def stop_typing(self, chat_id, metadata=None):
        # on_processing_complete owns cleanup; a late stop from a preceding turn
        # must never remove the current turn's progress.
        pass

    async def send_or_update_status(self, chat_id, status_key, content, *, metadata=None):
        # This lane contains diagnostics, not tool output. Render only generic activity;
        # approvals and clarify prompts keep using the permanent text-send contract.
        if not self._authorized_turn(chat_id):
            return SendResult(False, error="Status delivery is not authorized", error_kind="forbidden")
        await self.send_typing(chat_id, metadata)
        return SendResult(True)

    async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        # Client retries the immutable body/key. Base's alternate-body formatting
        # fallback would turn an uncertain delivery into a second logical send.
        return await self.send(chat_id, content, reply_to, metadata)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if (metadata or {}).get("expect_edits"):
            # A misconfigured/failed draft transport must not create an uneditable
            # permanent preview. The consumer can still send its completed final.
            return SendResult(False, error="Permanent messages are not editable", error_kind="unsupported")
        turn = _turn.get()
        from .hermes_compat import tool_call_id
        if tool_call_id() or self.host.current_cron_execution() or not self._authorized_turn(chat_id):
            try:
                identity = proactive.authorized(self, chat_id, metadata, reply_to)
                if not content:
                    raise SoWorkError(400, "empty_proactive_delivery")
                async with proactive.lease(self.store, identity):
                    chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)
                    return await self._deliver_bodies(identity, "text", chat_id, [{"text": text} for text in chunks], parent_id=threads.destination(self, metadata))
            except (SoWorkError, ValueError) as error:
                return SendResult(False, error=error.code if isinstance(error, SoWorkError) else "proactive_payload_changed",
                                  error_kind="forbidden", retryable=False)
        if not self._authorized_turn(chat_id):
            return SendResult(False, error="No authorized inbound turn for this DM", error_kind="forbidden")
        if self._is_sender_authorized(self.settings.allowed_user_id, "dm", chat_id) is not True:
            return SendResult(False, error="Gateway ACL denied delivery", error_kind="forbidden")
        if not content:
            return SendResult(False, error="Empty text response", error_kind="bad_format")
        if (metadata or {}).get("thread_id", turn.thread_id) != turn.thread_id:
            return SendResult(False, error="Thread routing mismatch", error_kind="forbidden")
        if self.settings.preview_enabled and (metadata or {}).get("notify") is True and not (metadata or {}).get("_interim_send"):
            # Consumer final + native fallback refer to the SAME durable operation,
            # even if the first final's receipt was lost after the server accepted it.
            sequence = "preview_final"
        else:
            sequence = turn.sequence
            turn.sequence += 1
        # Persist ALL chunks before any network side effect, including identical
        # adjacent chunks. Logical ordinal distinguishes them; retries don't.
        chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)
        return await self._deliver_bodies(turn.message_id, sequence, chat_id, [{"text": text} for text in chunks], parent_id=turn.thread_id)

    async def _deliver_bodies(self, turn_id, sequence, chat_id, bodies, *, parent_id=None):
        if parent_id:
            bodies = [{**body, "parentId": parent_id} for body in bodies]
        async with self._outbox_lock:
            rows = self.store.prepare_messages(turn_id, sequence, chat_id, bodies)
            receipts = []
            try:
                for row in rows:
                    receipts.append(await self._deliver_row(row))
            except SoWorkError as error:
                return SendResult(False, error=str(error), retryable=False, error_kind="transient" if error.retryable else "forbidden")
        return SendResult(True, message_id=receipts[-1]["id"], continuation_message_ids=tuple(r["id"] for r in receipts[:-1]))

    async def _send_file(self, chat_id, path, caption=None, *, file_name=None, metadata=None, reply_to=None):
        from .hermes_compat import tool_call_id
        is_proactive = bool(tool_call_id() or self.host.current_cron_execution() or not self._authorized_turn(chat_id))
        if not self.settings.media_enabled:
            return SendResult(False, error="Media delivery is not authorized", error_kind="forbidden")
        try:
            identity = proactive.authorized(self, chat_id, metadata, reply_to) if is_proactive else None
            if not is_proactive and (metadata or {}).get("thread_id", _turn.get().thread_id) != _turn.get().thread_id:
                raise SoWorkError(403, "thread_routing_mismatch")
        except SoWorkError as error:
            return SendResult(False, error=error.code, error_kind="forbidden")
        safe = self.validate_media_delivery_path(str(path))
        if not safe:
            return SendResult(False, error="Attachment path refused", error_kind="forbidden")
        try:
            name, mime, data = await asyncio.to_thread(read_outbound, safe, self.settings.max_attachment_bytes, file_name)
            if identity:
                sequence = "file_" + hashlib.sha256(safe.encode()).hexdigest()
                async with proactive.lease(self.store, identity):
                    return await self._deliver_file_data(identity, sequence, chat_id, name, mime, data, caption, parent_id=threads.destination(self, metadata))
            return await self._send_file_data(chat_id, name, mime, data, caption)
        except (SoWorkError, OSError, ValueError) as error:
            code = error.code if isinstance(error, SoWorkError) else "attachment_read_failed"
            return SendResult(False, error=code, error_kind="transient" if isinstance(error, SoWorkError) and error.retryable else "bad_format")

    async def _send_file_data(self, chat_id, name, mime, data, caption=None):
        if not self._authorized_turn(chat_id):
            return SendResult(False, error="Media delivery is not authorized", error_kind="forbidden")
        turn = _turn.get()
        sequence = turn.sequence
        turn.sequence += 1
        return await self._deliver_file_data(turn.message_id, sequence, chat_id, name, mime, data, caption, parent_id=turn.thread_id)

    async def _deliver_file_data(self, turn_id, sequence, chat_id, name, mime, data, caption=None, *, parent_id=None):
        logical = f"{turn_id}:{sequence}:upload"
        fingerprint = hashlib.sha256(json.dumps([chat_id, name, mime, hashlib.sha256(data).hexdigest(), *([parent_id] if parent_id else [])]).encode()).hexdigest()
        attachment = self.store.upload(logical, fingerprint)
        if attachment is None:
            if time.time() - self.store.upload_created(logical) >= 3600:
                raise SoWorkError(409, "upload_reconciliation_required")
            attachment = await self.client.upload_attachment(chat_id, name, mime, data)
            self.store.uploaded(logical, attachment)
        texts = self.truncate_message(caption, self.MAX_MESSAGE_LENGTH) if caption else []
        bodies = [{"attachments": [attachment], **({"text": texts[0]} if texts else {})}]
        bodies.extend({"text": text} for text in texts[1:])
        return await self._deliver_bodies(turn_id, sequence, chat_id, bodies, parent_id=parent_id)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_file(chat_id, image_path, caption, metadata=metadata, reply_to=reply_to)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None):
        if not self.settings.media_enabled or not self._authorized_turn(chat_id):
            return SendResult(False, error="Media delivery is not authorized", error_kind="forbidden")
        from gateway.platforms.base import cache_image_from_url
        try:
            path = await cache_image_from_url(image_url)
            return await self._send_file(chat_id, path, caption, metadata=metadata)
        except Exception:
            return SendResult(False, error="image_download_failed", error_kind="bad_format")

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_file(chat_id, file_path, caption, file_name=file_name, metadata=metadata, reply_to=reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_file(chat_id, audio_path, caption, metadata=metadata, reply_to=reply_to)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_file(chat_id, video_path, caption, metadata=metadata, reply_to=reply_to)
