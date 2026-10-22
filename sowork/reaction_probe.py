"""Opt-in, one-message observation. An emoji can only dismiss its private indicator."""
import asyncio
import hashlib
import json
import time
from uuid import UUID

from .client import SoWorkError

COMMANDS = {"/reaction-test", "/reaction-status"}
META = "reaction_probe_v1"
MAX_READS, SECONDS, INTERVAL = 12, 120, 10
ACTIVE = {"preparing", "waiting"}
STATES = ACTIVE | {"observed", "expired", "interrupted", "unreported", "preexisting", "denied", "error", "rate_limited"}
LABELS = {"none": "No test started.", "preparing": "Preparing the test.",
    "waiting": "Waiting for your 👀 reaction.", "observed": "Your 👀 reaction was observed; no command was executed.",
    "expired": "The observation period ended.", "interrupted": "Observation stopped; it will not resume after restart.",
    "unreported": "The API did not report verifiable reactions; observation is closed.",
    "preexisting": "Your reaction was already present at baseline; observation was not armed.",
    "denied": "Observation stopped: permission or binding changed.",
    "error": "Observation stopped after a read error; no action executed.",
    "rate_limited": "Observation stopped by the API rate limit; no further queries."}


def command(text):
    # Public ChatMessage.text is Markdown; accept a literal escaped hyphen too.
    value = text.strip().replace("\\-", "-").lower()
    return value if value in COMMANDS else None


def snapshot(store):
    raw = store.get_meta(META)
    try:
        record = json.loads(raw) if raw and len(raw) < 4096 else {}
        if not isinstance(record, dict) or record.get("state") not in STATES:
            return {"state": "none", "reads": 0}
        if type(record.get("reads")) is not int or not 0 <= record["reads"] <= MAX_READS:
            return {"state": "error", "reads": 0}
        return record
    except (ValueError, TypeError):
        return {"state": "error", "reads": 0}


class ReactionProbe:
    def __init__(self, adapter):
        self.adapter = adapter
        self.task = None
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self.record = None
        self.deadline = None
        self.starting = False

    def save(self, **changes):
        self.record.update(changes)
        with self.adapter.store.db:
            self.adapter.store._set_meta(META, json.dumps(self.record, separators=(",", ":")))

    def recover(self):
        self.record = snapshot(self.adapter.store)
        if self.record["state"] in ACTIVE:
            self.save(state="interrupted")  # Never rearm, infer an edge, or resume a callback.

    def allowed(self):
        a = self.adapter
        identity = a.client.identity if a.client else None
        try:
            return bool(a._running and a.store and a.settings.reaction_probe_enabled
                and a.config.enabled is True and a.config.extra.get("allow_from") == [a.settings.allowed_user_id]
                and a._is_sender_authorized(a.settings.allowed_user_id, "dm", a.settings.channel_id) is True
                and identity["installation"]["id"] == a.credentials.installation_id
                and identity["installation"]["state"] == "active"
                and "chat.history" in identity["installation"]["consentedScopes"]
                and identity["group"]["id"] == a.settings.workspace_id
                and identity["appUser"]["id"] == a.settings.app_user_id)
        except (KeyError, TypeError):
            return False

    async def read_reaction(self):
        a = self.adapter
        if not self.allowed():
            raise SoWorkError(403, "probe_binding_changed")
        if self.record["reads"] >= MAX_READS or self.clock() >= self.deadline:
            raise SoWorkError(409, "probe_budget_exhausted")
        # Counts the actual message GET, including a timeout/HTTP error. The
        # request disables retries, auth refresh retries and grant re-probes.
        self.save(reads=self.record["reads"] + 1)
        async with asyncio.timeout(min(4, self.deadline - self.clock())):
            page = await a.client.request("GET", f"/v1/chat/messages/{self.record['message_id']}/replies",
                                          params={"limit": 1}, retry=False)
        if not self.allowed():
            raise SoWorkError(403, "probe_binding_changed")
        if self.clock() >= self.deadline:
            raise SoWorkError(409, "probe_budget_exhausted")
        message = page.get("parent") if isinstance(page, dict) else None
        if (not isinstance(message, dict) or not isinstance(message.get("channel"), dict)
                or not isinstance(message.get("sender"), dict) or message.get("id") != self.record["message_id"]
                or message.get("channel", {}).get("id") != a.settings.channel_id
                or message.get("channel", {}).get("type") != "dm"
                or message.get("sender", {}).get("id") != a.settings.app_user_id or message.get("parentId")):
            raise SoWorkError(403, "probe_message_scope_mismatch")
        groups = message.get("reactions")
        if not isinstance(groups, list):
            return None  # Omitted is unknown, never an authoritative empty list.
        found, seen = False, set()
        for group in groups:
            if (not isinstance(group, dict) or not isinstance(group.get("type"), str)
                    or group["type"] in seen or type(group.get("count")) is not int or group["count"] < 1
                    or not isinstance(group.get("users"), list) or not group["users"]):
                return None
            seen.add(group["type"])
            for user in group["users"]:
                if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
                    return None
                if group["type"] == "eyes" and user["id"] == a.settings.allowed_user_id:
                    found = True
        return found

    async def cleanup(self):
        a = self.adapter
        if self.record and self.record.get("progress_id"):
            try:
                await asyncio.wait_for(a.client.delete_progress(self.record["progress_id"]), 2)
            except (SoWorkError, TimeoutError, OSError):
                return  # Its own expiry/native visual recovery handles an unavailable DELETE.
        if self.record and self.record.get("command_id"):
            a.store.forget_visual(self.record["command_id"])

    def failure(self, error):
        self.save(state=("rate_limited" if isinstance(error, SoWorkError) and error.status == 429
            else "denied" if isinstance(error, SoWorkError) and error.status in (401, 403)
            else "expired" if isinstance(error, SoWorkError) and error.code == "probe_budget_exhausted" else "error"))

    async def start(self, event):
        a = self.adapter
        if not a.settings.reaction_probe_enabled:
            return await a.send(event.source.chat_id, "The reaction prototype is disabled.")
        if event.source.thread_id:
            return await a.send(event.source.chat_id, "This prototype only works in the main DM. Use /reaction-test there.")
        if self.starting or (self.task and not self.task.done()):
            return await a.send(event.source.chat_id, "A message is already being observed. Use /reaction-status.")
        if not self.allowed():
            return await a.send(event.source.chat_id, "Test not started: binding or read permission is not confirmed.")
        self.starting = True
        self.record = {"state": "preparing", "reads": 0, "command_id": event.message_id}
        self.save()
        try:
            result = await a.send(event.source.chat_id,
                "Limited reaction test: wait for the private indicator, then react with 👀 to THIS message. "
                "Only this message is observed, for at most two minutes and 12 queries. The emoji only dismisses the indicator; "
                "it never approves tools or executes commands. Use /reaction-status to see the result.")
            if not result.success or not result.message_id or str(UUID(result.message_id)) != result.message_id:
                raise SoWorkError(502, "probe_send_unconfirmed")
            # Bind exclusively to the already-verified receipt in our durable outbox.
            row = a.store.db.execute("SELECT receipt FROM outbox WHERE turn_id=? AND status='sent'",
                                     (event.message_id,)).fetchall()
            if len(row) != 1 or json.loads(row[0][0]).get("id") != result.message_id:
                raise SoWorkError(403, "probe_outbox_mismatch")
            self.deadline = self.clock() + SECONDS
            self.save(message_id=result.message_id, started_at=int(time.time()), expires_at=int(time.time()) + SECONDS)
            baseline = await self.read_reaction()
            if baseline is not False:
                self.save(state="preexisting" if baseline is True else "unreported")
                return
            a.store.begin_visual(event.message_id)
            key = "probe_" + hashlib.sha256((a.scope + event.message_id).encode()).hexdigest()
            progress = await asyncio.wait_for(a.client.create_progress(a.settings.channel_id, a.settings.allowed_user_id, key,
                text="👀 Observing only the test message. React to it with 👀; /reaction-status shows the result."), 4)
            a.store.remember_visual(event.message_id, progress)
            self.save(state="waiting", progress_id=progress)
            self.task = asyncio.create_task(self.watch(), name="sowork-reaction-probe")
        except asyncio.CancelledError:
            self.save(state="interrupted")
            await self.cleanup()
            raise
        except (SoWorkError, TimeoutError, OSError, ValueError, TypeError, KeyError) as error:
            self.failure(error)
            await self.cleanup()
        finally:
            self.starting = False

    async def watch(self):
        try:
            while self.clock() < self.deadline and self.record["reads"] < MAX_READS:
                await self.sleep(min(INTERVAL, max(0, self.deadline - self.clock())))
                if self.clock() >= self.deadline:
                    break
                matched = await self.read_reaction()
                if matched is None:
                    self.save(state="unreported")
                    return
                if matched:
                    # Terminal state BEFORE the sole fixed effect: remove our own UI.
                    # No events, model calls, tools, approvals or user-defined callbacks.
                    self.save(state="observed")
                    return
            self.save(state="expired")
        except asyncio.CancelledError:
            self.save(state="interrupted")
            raise
        except (SoWorkError, TimeoutError, OSError, ValueError, TypeError, KeyError) as error:
            self.failure(error)
        finally:
            await self.cleanup()

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # A task cancelled before its first instruction cannot run its finally.
        if self.record and self.record["state"] in ACTIVE:
            self.save(state="interrupted")
            await self.cleanup()
        self.task = None

    async def handle(self, event):
        if self.adapter.settings.channels:
            return await self.adapter.send(event.source.chat_id,
                "The limited reaction probe is available only with the original single-DM configuration.")
        if command(event.text) == "/reaction-test":
            await self.start(event)
        else:
            record = snapshot(self.adapter.store)
            await self.adapter.send(event.source.chat_id,
                LABELS[record["state"]] + f" Message queries: {record['reads']}/{MAX_READS}.")
